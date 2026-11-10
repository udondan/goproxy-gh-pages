"""Packs a module directory into a module zip, optionally uploaded to a GitHub release."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone

from goproxy_gh_pages import github
from goproxy_gh_pages.github import find_release, upload_asset
from goproxy_gh_pages.module import escape, module_path_of_go_mod, split_path_version
from goproxy_gh_pages.modzip import ZipError, ZipResult, create_zip, file_sha256, parse_time
from goproxy_gh_pages.net import Client

DEFAULT_OUTPUT = '{name}-go-module-{version}.zip'


@dataclass
class PackResult:
    zip: ZipResult
    upload: str | None


def normalize_version(version: str) -> str:
    version = version.strip()
    if version.startswith('refs/tags/'):
        version = version[len('refs/tags/') :]
    return version if version.startswith('v') else f'v{version}'


def version_time(text: str | None, module_dir: str) -> datetime:
    """The time of the version: given, or SOURCE_DATE_EPOCH, or the time of the HEAD commit, or now.
    A time that doesn't change when the job runs again makes the zip the same zip."""
    if text:
        return parse_time(text)
    epoch = os.environ.get('SOURCE_DATE_EPOCH', '').strip()
    if epoch:
        return parse_time(epoch)
    try:
        commit_time = subprocess.run(
            ['git', 'log', '-1', '--format=%ct'],
            cwd=module_dir,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout.strip()
        if commit_time:
            return parse_time(commit_time)
    except (OSError, subprocess.SubprocessError):
        pass
    return datetime.now(timezone.utc)


def output_name(template: str, module: str, version: str, repository: str | None) -> str:
    prefix, _ = split_path_version(module) or (module, '')
    name = repository.split('/', 1)[1] if repository and '/' in repository else prefix.rsplit('/', 1)[-1]
    return template.format(
        name=name,
        module=escape(module).replace('/', '_'),
        version=version,
        major=version.split('.', 1)[0],
    )


def pack(
    module_dir: str,
    version: str,
    output: str = DEFAULT_OUTPUT,
    time: str | None = None,
    compression_level: int = 9,
    repository: str | None = None,
    release_tag: str | None = None,
    asset_name: str | None = None,
    client: Client | None = None,
) -> PackResult:
    go_mod = os.path.join(module_dir, 'go.mod')
    if not os.path.isfile(go_mod):
        raise ZipError(f'{module_dir} has no go.mod file')
    with open(go_mod, 'rb') as file:
        module = module_path_of_go_mod(file.read())
    version = normalize_version(version)
    path = output_name(output, module, version, repository)
    result = create_zip(module_dir, version, path, version_time(time, module_dir), compression_level)
    github.log(
        f'Wrote {result.path}: {result.module}@{result.version}, {result.files} files, {result.size} bytes, {result.h1}'
    )
    for omitted, reason in result.omitted:
        github.log(f'  left out {omitted}: {reason}')
    upload = None
    if release_tag:
        if not repository or client is None:
            raise ValueError('uploading to a release needs the repository and a token')
        release = find_release(client, repository, release_tag)
        name = output_name(asset_name, module, version, repository) if asset_name else os.path.basename(result.path)
        upload = upload_asset(client, release, result.path, name, file_sha256(result.path))
        github.log(f'{name} on release {release_tag}: {upload}')
    return PackResult(result, upload)
