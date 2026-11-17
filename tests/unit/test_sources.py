from __future__ import annotations

import io
import json
import os
import struct
import threading
import zipfile
from pathlib import Path
from typing import Any

import pytest

from goproxy_gh_pages import modzip
from goproxy_gh_pages.github import Asset
from goproxy_gh_pages.module import ModuleVersion
from goproxy_gh_pages.modzip import ZipError, file_sha256
from goproxy_gh_pages.net import Client
from goproxy_gh_pages.sources import (
    Candidate,
    ConflictError,
    Fetcher,
    Known,
    first_zip_entry,
    identify_asset,
    module_version_of_entry,
    packages_of,
    parallel,
    proxy_file_url,
    read_list,
    read_local,
    read_manifest,
)
from tests.unit.conftest import FakeServer, Reply
from tests.unit.sitehelpers import make_zip

MODULE = 'example.com/mod'


def test_parallel_keeps_order() -> None:
    threads: set[int] = set()

    def square(value: int) -> int:
        threads.add(threading.get_ident())
        return value * value

    assert parallel(square, range(10), workers=4) == [value * value for value in range(10)]
    assert parallel(square, [3], workers=4) == [9]
    assert parallel(square, iter([1, 2]), workers=1) == [1, 4]
    assert parallel(square, [], workers=4) == []


def test_proxy_file_url_escapes_module() -> None:
    assert (
        proxy_file_url('https://x.dev/go/', 'github.com/Foo/bar', 'list')
        == 'https://x.dev/go/github.com/!foo/bar/@v/list'
    )


# manifest and lists


def test_read_manifest(server: FakeServer, client: Client) -> None:
    manifest = {
        'modules': {
            'example.com/a': {
                'mode': 'proxy',
                'versions': {
                    'v1.0.0': {
                        'time': '2024-01-01T00:00:00Z',
                        'h1': 'h1:zip',
                        'mod_h1': 'h1:mod',
                        'sha256': 'abc',
                        'asset': 7,
                        'sumdb': True,
                    },
                    'v1.1.0': {'sumdb': 'yes'},
                },
            },
            'example.com/legacy': {'versions': {'v0.1.0': {}}},
            'example.com/empty': {},
            'example.com/vanity': {'mode': 'git', 'url': 'https://github.com/x/y'},
            'example.com/other': {'mode': 'mod', 'url': 'https://proxy.example.com'},
        }
    }
    server.route('/go/index.json', json.dumps(manifest))
    assert read_manifest(client, f'{server.url}/go/') == {
        'example.com/a': {
            'v1.0.0': Known('2024-01-01T00:00:00Z', 'h1:zip', 'h1:mod', 'abc', 7, True),
            'v1.1.0': Known(),
        },
        'example.com/legacy': {'v0.1.0': Known()},
        'example.com/empty': {},
    }


def test_read_manifest_missing(server: FakeServer, client: Client) -> None:
    assert read_manifest(client, f'{server.url}/go') is None


@pytest.mark.parametrize('body', [b'not json', b'[]', b'{"modules": {"a": {"versions": []}}}'])
def test_read_manifest_invalid(
    server: FakeServer, client: Client, body: bytes, capsys: pytest.CaptureFixture[str]
) -> None:
    server.route('/go/index.json', body)
    assert read_manifest(client, f'{server.url}/go') is None
    assert 'manifest of the live site is not valid' in capsys.readouterr().err


def test_read_manifest_http_error(server: FakeServer, client: Client, capsys: pytest.CaptureFixture[str]) -> None:
    server.route('/go/index.json', Reply(500))
    assert read_manifest(client, f'{server.url}/go') is None
    assert 'Could not read the manifest' in capsys.readouterr().err


def test_read_list(server: FakeServer, client: Client) -> None:
    body = 'v1.0.0\n\nv1.1.0 2024-01-01T00:00:00Z\nv2.0.0\nnot-a-version\nv1.2.0-rc.1\n'
    server.route('/go/example.com/!upper/@v/list', body)
    assert read_list(client, f'{server.url}/go', 'example.com/Upper') == ['v1.0.0', 'v1.1.0', 'v1.2.0-rc.1']


def test_read_list_missing_and_error(server: FakeServer, client: Client, capsys: pytest.CaptureFixture[str]) -> None:
    assert read_list(client, server.url, MODULE) is None
    server.route(f'/{MODULE}/@v/list', Reply(503))
    assert read_list(client, server.url, MODULE) is None
    assert 'Could not read the versions of example.com/mod' in capsys.readouterr().err


def test_read_list_empty(server: FakeServer, client: Client) -> None:
    server.route(f'/{MODULE}/@v/list', b'')
    assert read_list(client, server.url, MODULE) == []


# zip directory


def zip_bytes(names: list[str], comment: bytes = b'') -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name in names:
            archive.writestr(name, b'x')
        archive.comment = comment
    return buffer.getvalue()


def fetcher_of(data: bytes) -> Any:
    calls: list[tuple[int, int]] = []

    def fetch(start: int, end: int) -> bytes:
        calls.append((start, end))
        return data[start : end + 1]

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


def test_first_zip_entry_in_tail() -> None:
    data = zip_bytes([f'{MODULE}@v1.0.0/go.mod', f'{MODULE}@v1.0.0/a.go'], comment=b'{"Version":"v1.0.0"}')
    fetch = fetcher_of(data)
    assert first_zip_entry(data, len(data), fetch) == f'{MODULE}@v1.0.0/go.mod'
    assert fetch.calls == []


def test_first_zip_entry_fetches_directory_before_tail() -> None:
    data = zip_bytes([f'{MODULE}@v1.0.0/go.mod', f'{MODULE}@v1.0.0/a.go'])
    fetch = fetcher_of(data)
    assert first_zip_entry(data[-22:], len(data), fetch) == f'{MODULE}@v1.0.0/go.mod'
    assert len(fetch.calls) == 1


def test_first_zip_entry_of_non_zips() -> None:
    def fetch(start: int, end: int) -> bytes:
        raise AssertionError('not called')

    assert first_zip_entry(b'just some bytes', 15, fetch) is None
    assert first_zip_entry(b'PK\x05\x06' + b'\x00' * 10, 14, fetch) is None
    empty = zip_bytes([])
    assert first_zip_entry(empty, len(empty), fetch) is None


def _patch_end(
    data: bytes, entries: int | None = None, cd_size: int | None = None, cd_offset: int | None = None
) -> bytes:
    position = data.rfind(b'PK\x05\x06')
    disk_entries, total, size, offset = struct.unpack('<HHII', data[position + 8 : position + 20])
    values = (
        disk_entries if entries is None else entries,
        total if entries is None else entries,
        size if cd_size is None else cd_size,
        offset if cd_offset is None else cd_offset,
    )
    return data[: position + 8] + struct.pack('<HHII', *values) + data[position + 20 :]


@pytest.fixture(scope='module')
def zip64() -> bytes:
    # more than 65535 files make zipfile, and the go command, write a zip64 end record
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr(f'{MODULE}@v1.0.0/go.mod', b'module example.com/mod\n')
        for number in range(0x10000):
            archive.writestr(f'{MODULE}@v1.0.0/{number}', b'')
    data = buffer.getvalue()
    assert b'PK\x06\x06' in data[-100:]
    return data


@pytest.mark.parametrize('tail', [1 << 16, 22 + 20], ids=['record in tail', 'record fetched'])
def test_first_zip_entry_of_zip64(zip64: bytes, tail: int) -> None:
    fetch = fetcher_of(zip64)
    assert first_zip_entry(zip64[-tail:], len(zip64), fetch) == f'{MODULE}@v1.0.0/go.mod'


def test_first_zip_entry_of_broken_zip64(zip64: bytes) -> None:
    locator = len(zip64) - 22 - 20
    record = struct.unpack('<Q', zip64[locator + 8 : locator + 16])[0]
    broken = zip64[:record] + b'XXXX' + zip64[record + 4 :]
    assert first_zip_entry(broken, len(broken), fetcher_of(broken)) is None


def test_first_zip_entry_of_zip64_values_and_broken_directories() -> None:
    data = zip_bytes([f'{MODULE}@v1.0.0/go.mod'])
    fetch = fetcher_of(data)
    # 65535 files fit into a zip without zip64
    assert first_zip_entry(_patch_end(data, entries=0xFFFF), len(data), fetch) == f'{MODULE}@v1.0.0/go.mod'
    # zip64 values without a zip64 end record
    assert first_zip_entry(_patch_end(data, cd_offset=0xFFFFFFFF), len(data), fetch) is None
    assert first_zip_entry(_patch_end(data, cd_size=0xFFFFFFFF), len(data), fetch) is None
    # the directory doesn't start with a file header
    assert first_zip_entry(_patch_end(data, cd_offset=1), len(data), fetch) is None
    # the directory is too short for the header or the file name
    assert first_zip_entry(_patch_end(data, cd_size=20), len(data), fetch) is None
    assert first_zip_entry(_patch_end(data, cd_size=50), len(data), fetch) is None


def test_module_version_of_entry() -> None:
    assert module_version_of_entry('github.com/a/b/v2@v2.1.0/x/y.go') == ModuleVersion('github.com/a/b/v2', 'v2.1.0')
    assert module_version_of_entry('example.com/m@v1.0.0/go.mod') == ModuleVersion('example.com/m', 'v1.0.0')
    assert module_version_of_entry('example.com/m@v1.0.0') == ModuleVersion('example.com/m', 'v1.0.0')
    assert module_version_of_entry('README.md') is None
    assert module_version_of_entry('example.com/m@latest/go.mod') is None
    assert module_version_of_entry('example.com/m@v2.0.0/go.mod') is None


def test_packages_of() -> None:
    files = ['go.mod', 'a.go', 'sub/b.go', 'sub/c.go', 'sub/x.txt', 'deep/er/c.go', 'docs/readme.md']
    assert packages_of(files) == ['', 'deep/er', 'sub']
    assert packages_of(['go.mod']) == []


# release assets


def asset_of(server: FakeServer, data: bytes, path: str = '/assets/1', name: str = 'm.zip') -> Asset:
    return Asset(1, name, len(data), f'{server.url}{path}', None, 'v1.0.0', None)


def test_identify_asset_with_range_requests(server: FakeServer, client: Client) -> None:
    data = zip_bytes([f'{MODULE}/v3@v3.1.0/go.mod'])
    server.route('/assets/1', data)
    assert identify_asset(client, asset_of(server, data)) == ModuleVersion(f'{MODULE}/v3', 'v3.1.0')
    (request,) = server.requests
    assert request.headers['Range'] == f'bytes=0-{len(data) - 1}'
    assert request.headers['Accept'] == 'application/octet-stream'


def large_zip() -> bytes:
    """A zip whose central directory is larger than the tail that is read first"""
    names = [f'{MODULE}@v1.2.3/{"d" * 60}/file{index:05}.go' for index in range(1200)]
    return zip_bytes([f'{MODULE}@v1.2.3/go.mod', *names])


@pytest.mark.parametrize('ranges', [True, False])
def test_identify_asset_with_large_directory(server: FakeServer, client: Client, ranges: bool) -> None:
    data = large_zip()
    assert len(data) > 2 * (64 << 10)
    server.route('/assets/1', Reply(body=data, ranges=ranges))
    assert identify_asset(client, asset_of(server, data)) == ModuleVersion(MODULE, 'v1.2.3')
    assert len(server.requests) == 2
    assert server.requests[0].headers['Range'] == f'bytes={len(data) - (64 << 10)}-{len(data) - 1}'


def test_identify_asset_rejects_unexpected_responses(
    server: FakeServer, client: Client, capsys: pytest.CaptureFixture[str]
) -> None:
    data = zip_bytes([f'{MODULE}@v1.0.0/go.mod'])
    server.route('/assets/1', Reply(body=b'something else', ranges=False))
    assert identify_asset(client, asset_of(server, data)) is None
    assert 'range requests are not supported' in capsys.readouterr().err
    assert identify_asset(client, asset_of(server, data, '/missing')) is None
    assert 'Could not read the zip directory of v1.0.0/m.zip' in capsys.readouterr().err


def test_identify_asset_of_other_files(server: FakeServer, client: Client) -> None:
    server.route('/assets/1', b'not a zip at all, but long enough')
    assert identify_asset(client, asset_of(server, b'not a zip at all, but long enough')) is None
    small = Asset(1, 'tiny.zip', 10, f'{server.url}/assets/1', None, 'v1', None)
    assert identify_asset(client, small) is None
    other = zip_bytes(['README.md'])
    server.route('/assets/2', other)
    assert identify_asset(client, asset_of(server, other, '/assets/2')) is None


# fetching


@pytest.fixture
def module_zip(tmp_path: Path) -> Any:
    return make_zip(tmp_path, MODULE, 'v1.0.0', {'a.go': 'package a\n', 'sub/b.go': 'package sub\n'})


def serve_everywhere(server: FakeServer, path: str) -> None:
    data = Path(path).read_bytes()
    server.route(f'/live/{MODULE}/@v/v1.0.0.zip', data)
    server.route('/assets/1', data)
    server.route(f'/fallback/{MODULE}/@v/v1.0.0.zip', data)


def full_candidate(server: FakeServer, local: str | None = None, size: int = 0) -> Candidate:
    asset = Asset(1, 'mod.zip', size, f'{server.url}/assets/1', None, 'v1.0.0', None)
    return Candidate(MODULE, 'v1.0.0', local=local, live=True, asset=asset, fallback=True)


def fetcher(server: FakeServer, client: Client, tmp_path: Path, live: bool = True, fallback: bool = True) -> Fetcher:
    return Fetcher(
        client,
        str(tmp_path / 'work'),
        f'{server.url}/live' if live else None,
        f'{server.url}/fallback' if fallback else None,
    )


def test_fetch_prefers_local(server: FakeServer, client: Client, tmp_path: Path, module_zip: Any) -> None:
    serve_everywhere(server, module_zip.path)
    result = fetcher(server, client, tmp_path).fetch(full_candidate(server, local=module_zip.path))
    assert result.source == 'local'
    assert result.path == module_zip.path
    assert result.h1 == module_zip.h1
    assert result.mod_h1 == module_zip.mod_h1
    assert result.sha256 == file_sha256(module_zip.path)
    assert result.size == os.path.getsize(module_zip.path)
    assert result.packages == ['', 'sub']
    assert result.time == '2024-05-06T07:08:09Z'
    assert result.go_mod.startswith(b'module example.com/mod')
    assert server.requests == []


def test_fetch_prefers_live_site(server: FakeServer, client: Client, tmp_path: Path, module_zip: Any) -> None:
    serve_everywhere(server, module_zip.path)
    result = fetcher(server, client, tmp_path).fetch(full_candidate(server))
    assert result.source == 'live site'
    assert result.path == str(tmp_path / 'work' / 'live' / 'example.com' / 'mod' / '@v' / 'v1.0.0.zip')
    assert Path(result.path).read_bytes() == Path(module_zip.path).read_bytes()
    assert server.paths() == [f'/live/{MODULE}/@v/v1.0.0.zip']


def test_fetch_falls_through_sources(server: FakeServer, client: Client, tmp_path: Path, module_zip: Any) -> None:
    data = Path(module_zip.path).read_bytes()
    other = make_zip(tmp_path, MODULE, 'v1.0.1')
    server.route(f'/live/{MODULE}/@v/v1.0.0.zip', Path(other.path).read_bytes())  # a different version
    server.route('/assets/1', Reply(404))
    server.route(f'/fallback/{MODULE}/@v/v1.0.0.zip', data)
    result = fetcher(server, client, tmp_path).fetch(full_candidate(server))
    assert result.source == f'{server.url}/fallback'
    assert result.path == str(tmp_path / 'work' / 'fallback' / 'example.com' / 'mod' / '@v' / 'v1.0.0.zip')
    assert server.paths() == [f'/live/{MODULE}/@v/v1.0.0.zip', '/assets/1', f'/fallback/{MODULE}/@v/v1.0.0.zip']
    assert server.requests[1].headers['Accept'] == 'application/octet-stream'


def test_fetch_from_release_asset(server: FakeServer, client: Client, tmp_path: Path, module_zip: Any) -> None:
    serve_everywhere(server, module_zip.path)
    result = fetcher(server, client, tmp_path, live=False).fetch(full_candidate(server))
    assert result.source == 'release asset v1.0.0/mod.zip'
    assert result.path == str(tmp_path / 'work' / 'assets' / 'example.com' / 'mod' / '@v' / 'v1.0.0.zip')


def test_fetch_fails_without_valid_source(server: FakeServer, client: Client, tmp_path: Path) -> None:
    server.route('/assets/1', b'garbage')
    with pytest.raises(ZipError) as error:
        fetcher(server, client, tmp_path).fetch(full_candidate(server))
    message = str(error.value)
    assert message.startswith('example.com/mod@v1.0.0: no source has a valid zip:')
    assert 'live site: 404' in message
    assert 'release asset v1.0.0/mod.zip: not a zip file' in message
    assert f'{server.url}/fallback: 404' in message


def test_fetch_without_sources(server: FakeServer, client: Client, tmp_path: Path) -> None:
    candidate = Candidate(MODULE, 'v1.0.0', live=True, fallback=True)
    with pytest.raises(ZipError, match=r'no source$'):
        fetcher(server, client, tmp_path, live=False, fallback=False).fetch(candidate)
    assert server.requests == []


def test_fetch_missing_local_file(server: FakeServer, client: Client, tmp_path: Path) -> None:
    candidate = Candidate(MODULE, 'v1.0.0', local=str(tmp_path / 'missing.zip'))
    with pytest.raises(ZipError, match='local: '):
        fetcher(server, client, tmp_path).fetch(candidate)


def test_fetch_conflict_stops(server: FakeServer, client: Client, tmp_path: Path, module_zip: Any) -> None:
    serve_everywhere(server, module_zip.path)
    candidate = full_candidate(server)
    candidate.known = Known(h1='h1:something-else=')
    with pytest.raises(ConflictError, match='the live site serves h1:something-else='):
        fetcher(server, client, tmp_path).fetch(candidate)
    assert len(server.requests) == 1


def test_fetch_trusts_manifest_for_the_same_zip(
    server: FakeServer, client: Client, tmp_path: Path, module_zip: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve_everywhere(server, module_zip.path)
    calls: list[bool] = []
    real_read_zip = modzip.read_zip

    def read_zip(path: str, expect: Any = None, hash_files: bool = True) -> Any:
        calls.append(hash_files)
        return real_read_zip(path, expect=expect, hash_files=hash_files)

    monkeypatch.setattr('goproxy_gh_pages.sources.read_zip', read_zip)
    candidate = full_candidate(server)
    candidate.time = '2020-01-01T00:00:00Z'
    candidate.known = Known(
        time='2021-01-01T00:00:00Z', h1=module_zip.h1, mod_h1=module_zip.mod_h1, sha256=file_sha256(module_zip.path)
    )
    result = fetcher(server, client, tmp_path).fetch(candidate)
    assert calls == [False]
    assert result.h1 == module_zip.h1
    assert result.time == '2021-01-01T00:00:00Z'

    candidate.known.sha256 = 'different'
    result = fetcher(server, client, tmp_path).fetch(candidate)
    assert calls == [False, True]
    assert result.h1 == module_zip.h1


def test_fetch_time_from_candidate(server: FakeServer, client: Client, tmp_path: Path, module_zip: Any) -> None:
    candidate = Candidate(MODULE, 'v1.0.0', local=module_zip.path, time='2020-01-01T00:00:00Z')
    assert fetcher(server, client, tmp_path).fetch(candidate).time == '2020-01-01T00:00:00Z'


def test_fetch_escapes_module_paths(server: FakeServer, client: Client, tmp_path: Path) -> None:
    upper = make_zip(tmp_path, 'example.com/Upper/Case', 'v1.0.0')
    server.route('/live/example.com/!upper/!case/@v/v1.0.0.zip', Path(upper.path).read_bytes())
    candidate = Candidate('example.com/Upper/Case', 'v1.0.0', live=True)
    result = fetcher(server, client, tmp_path).fetch(candidate)
    assert result.path == str(tmp_path / 'work' / 'live' / 'example.com' / '!upper' / '!case' / '@v' / 'v1.0.0.zip')


def test_candidate_key() -> None:
    assert Candidate(MODULE, 'v1.0.0').key == ModuleVersion(MODULE, 'v1.0.0')


def test_read_local(tmp_path: Path, module_zip: Any) -> None:
    candidate, fetched = read_local(module_zip.path)
    assert candidate == Candidate(MODULE, 'v1.0.0', local=module_zip.path, time='2024-05-06T07:08:09Z')
    assert fetched.source == 'local'
    assert fetched.h1 == module_zip.h1
    assert fetched.mod_h1 == module_zip.mod_h1
    assert fetched.sha256 == file_sha256(module_zip.path)
    assert fetched.size == os.path.getsize(module_zip.path)
    assert fetched.packages == ['', 'sub']
    assert fetched.time == '2024-05-06T07:08:09Z'
