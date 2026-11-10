"""Helpers for build, seed and CLI tests: module zips, and fake proxies, live sites, checksum
databases and GitHub releases on a FakeServer."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from goproxy_gh_pages.module import escape
from goproxy_gh_pages.modzip import ZipResult, create_zip, file_sha256
from tests.unit.conftest import FakeServer, Reply, write_module

TIME = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def make_zip(
    tmp_path: Path,
    module: str,
    version: str,
    files: dict[str, str | bytes] | None = None,
    name: str | None = None,
) -> ZipResult:
    """A module zip of a module with the given files"""
    tag = f'{escape(module).replace("/", "_")}@{version}'
    source = write_module(tmp_path / 'src' / tag / (name or 'default'), module, files)
    output = tmp_path / 'zips' / (name or 'default') / f'{tag}.zip'
    output.parent.mkdir(parents=True, exist_ok=True)
    return create_zip(str(source), version, str(output), TIME)


def without_time(path: str) -> bytes:
    """The zip as proxy.golang.org serves it: no comment and zero DOS dates"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(buffer, 'w') as target:
        for info in source.infolist():
            target.writestr(zipfile.ZipInfo(info.filename, (1980, 0, 0, 0, 0, 0)), source.read(info))
    return buffer.getvalue()


def serve_proxy(server: FakeServer, prefix: str, module: str, zips: dict[str, ZipResult], listed: bool = True) -> None:
    """Serves module versions like a proxy below `prefix`"""
    base = f'{prefix}/{escape(module)}/@v'
    if listed:
        server.route(f'{base}/list', ''.join(f'{version}\n' for version in zips))
    for version, result in zips.items():
        name = escape(version)
        server.route(f'{base}/{name}.info', json.dumps({'Version': version, 'Time': result.time}))
        server.route(f'{base}/{name}.zip', Path(result.path).read_bytes())
        server.route(f'{base}/{name}.mod', f'module {module}\n\ngo 1.21\n')


def manifest_entry(result: ZipResult, **extra: Any) -> dict[str, Any]:
    return {
        'time': result.time,
        'h1': result.h1,
        'mod_h1': result.mod_h1,
        'sha256': file_sha256(result.path),
        'size': Path(result.path).stat().st_size,
        **extra,
    }


def serve_manifest(server: FakeServer, prefix: str, modules: dict[str, dict[str, dict[str, Any]]]) -> None:
    server.route(
        f'{prefix}/index.json',
        json.dumps(
            {'modules': {module: {'mode': 'proxy', 'versions': versions} for module, versions in modules.items()}}
        ),
    )


def asset(server: FakeServer, asset_id: int, name: str, data: bytes, digest: str | None = None) -> dict[str, Any]:
    """A release asset served by the fake server"""
    server.route(f'/assets/{asset_id}', Reply(body=data))
    entry: dict[str, Any] = {'id': asset_id, 'name': name, 'size': len(data), 'url': f'{server.url}/assets/{asset_id}'}
    if digest:
        entry['digest'] = digest
    return entry


def serve_releases(server: FakeServer, repo: str, releases: list[dict[str, Any]]) -> None:
    """Releases, newest first: {'tag': ..., 'draft': ..., 'assets': [asset(...)]}"""
    data = [
        {
            'id': number,
            'tag_name': release['tag'],
            'draft': release.get('draft', False),
            'published_at': '2024-01-01T00:00:00Z',
            'upload_url': f'{server.url}/uploads/{number}{{?name,label}}',
            'assets': release.get('assets', []),
        }
        for number, release in enumerate(releases, 1)
    ]
    server.route(f'/repos/{repo}/releases?per_page=100', json.dumps(data))


def serve_sumdb(server: FakeServer, prefix: str, module: str, version: str, h1: str, mod_h1: str) -> None:
    server.route(
        f'{prefix}/lookup/{escape(module)}@{escape(version)}',
        f'123\n{module} {version} {h1}\n{module} {version}/go.mod {mod_h1}\n\ngo.sum database tree\n',
    )


def serve_cached(server: FakeServer, proxy: str, module: str, version: str) -> None:
    server.route(f'{proxy}/cached-only/{escape(module)}/@v/{escape(version)}.info', json.dumps({'Version': version}))
