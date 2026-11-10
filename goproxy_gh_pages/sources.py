"""Where module versions come from: local zips, the live site, release assets and a fallback proxy.

Discovery is cheap: local zips are read, the live site has a manifest and `@v/list` files, release
assets are identified by the first file name in their zip directory, read with a range request.
Only the versions that end up on the site are downloaded, each once, from the first source that
has it.
"""

from __future__ import annotations

import os
import struct
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TypeVar

from goproxy_gh_pages import github
from goproxy_gh_pages.github import Asset
from goproxy_gh_pages.module import ModuleError, ModuleVersion, check_version, escape
from goproxy_gh_pages.modzip import ZipError, file_sha256, read_zip
from goproxy_gh_pages.net import Client, HTTPError

MANIFEST = 'index.json'
# the source of a zip from the live site
LIVE_SOURCE = 'live site'
_TAIL = 64 << 10

T = TypeVar('T')
R = TypeVar('R')


def parallel(function: Callable[[T], R], items: Iterable[T], workers: int) -> list[R]:
    items = list(items)
    if workers <= 1 or len(items) <= 1:
        return [function(item) for item in items]
    with ThreadPoolExecutor(max_workers=workers) as executor:
        return list(executor.map(function, items))


class ConflictError(ZipError):
    """Two sources have different content for the same module version"""


@dataclass
class Known:
    """What is known about a version before it is downloaded"""

    time: str | None = None
    h1: str | None = None
    mod_h1: str | None = None
    sha256: str | None = None
    asset: int | None = None
    sumdb: bool = False


@dataclass
class Candidate:
    """A module version and where to get it, in the order of preference"""

    module: str
    version: str
    local: str | None = None
    live: bool = False
    asset: Asset | None = None
    fallback: bool = False
    known: Known = field(default_factory=Known)
    time: str | None = None

    @property
    def key(self) -> ModuleVersion:
        return ModuleVersion(self.module, self.version)


@dataclass
class Fetched:
    module: str
    version: str
    path: str
    source: str
    time: str
    go_mod: bytes
    h1: str
    mod_h1: str
    sha256: str
    size: int
    packages: list[str]


def proxy_file_url(proxy: str, module: str, name: str) -> str:
    return f'{proxy.rstrip("/")}/{escape(module)}/@v/{name}'


def read_manifest(client: Client, proxy_url: str) -> dict[str, dict[str, Known]] | None:
    """The manifest of the live site: module → version → what is known about it"""
    try:
        response = client.exists(f'{proxy_url.rstrip("/")}/{MANIFEST}')
    except HTTPError as error:
        github.warning(f'Could not read the manifest of the live site: {error}')
        return None
    if response is None:
        return None
    try:
        data = response.json()
        return {
            module: {
                version: Known(
                    time=info.get('time'),
                    h1=info.get('h1'),
                    mod_h1=info.get('mod_h1'),
                    sha256=info.get('sha256'),
                    asset=info.get('asset'),
                    sumdb=info.get('sumdb') is True,
                )
                for version, info in entry.get('versions', {}).items()
            }
            for module, entry in data.get('modules', {}).items()
            if entry.get('mode', 'proxy') == 'proxy'
        }
    except (ValueError, AttributeError) as error:
        github.warning(f'The manifest of the live site is not valid: {error}')
        return None


def read_list(client: Client, proxy_url: str, module: str) -> list[str] | None:
    """The versions in the `@v/list` of a module on a proxy, or None if the proxy doesn't know it"""
    try:
        response = client.exists(proxy_file_url(proxy_url, module, 'list'))
    except HTTPError as error:
        github.warning(f'Could not read the versions of {module} from {proxy_url}: {error}')
        return None
    if response is None:
        return None
    versions = []
    for line in response.text().splitlines():
        version = line.split(' ', 1)[0].strip()
        if not version:
            continue
        try:
            check_version(module, version)
        except ModuleError:
            continue
        versions.append(version)
    return versions


def first_zip_entry(tail: bytes, size: int, fetch: Callable[[int, int], bytes]) -> str | None:
    """The name of the first file in the central directory of a zip, from the end of the zip.
    `fetch(start, end)` reads more of the zip if the central directory is not in the tail."""
    position = tail.rfind(b'PK\x05\x06')
    if position < 0 or len(tail) - position < 22:
        return None
    entries, cd_size, cd_offset = struct.unpack('<10xHII', tail[position : position + 20])
    tail_start = size - len(tail)
    # a zip with more than 65535 files, as pack writes for big modules, has a zip64 end record,
    # which a locator right before the end record points to
    locator = tail[position - 20 : position] if position >= 20 else b''
    if locator[:4] == b'PK\x06\x07':
        (record_offset,) = struct.unpack('<Q', locator[8:16])
        if record_offset >= tail_start:
            record = tail[record_offset - tail_start : record_offset - tail_start + 56]
        else:
            record = fetch(record_offset, record_offset + 55)
        if len(record) < 56 or record[:4] != b'PK\x06\x06':
            return None
        entries, cd_size, cd_offset = struct.unpack('<32xQQQ', record)
    elif cd_offset == 0xFFFFFFFF or cd_size == 0xFFFFFFFF:
        return None  # zip64 values without a zip64 end record
    if entries == 0 or cd_offset >= size:
        return None
    if cd_offset >= tail_start:
        header = tail[cd_offset - tail_start : cd_offset - tail_start + min(cd_size, 46 + 65535)]
    else:
        header = fetch(cd_offset, cd_offset + min(cd_size, 46 + 65535) - 1)
    if len(header) < 46 or header[:4] != b'PK\x01\x02':
        return None
    name_length = struct.unpack('<H', header[28:30])[0]
    name = header[46 : 46 + name_length]
    if len(name) != name_length:
        return None
    return name.decode('utf-8', 'replace')


def _range(client: Client, asset: Asset, start: int, end: int) -> bytes:
    status, _, body = client.open(
        asset.url,
        headers={'Accept': 'application/octet-stream', 'Range': f'bytes={start}-{end}'},
    )
    data = bytes(body)
    if status == 200 and len(data) == asset.size:
        # the server ignored the range and sent all of it
        return data[start : end + 1]
    if status != 206:
        raise HTTPError(asset.url, status, 'range requests are not supported')
    return data


def identify_asset(client: Client, asset: Asset) -> ModuleVersion | None:
    """The module version of a release asset, read from the first file name in its zip directory
    without downloading it. None if the asset is not a module zip."""
    if asset.size < 22:
        return None
    start = max(asset.size - _TAIL, 0)
    try:
        tail = _range(client, asset, start, asset.size - 1)
        name = first_zip_entry(tail, asset.size, lambda first, last: _range(client, asset, first, last))
    except HTTPError as error:
        github.warning(f'Could not read the zip directory of {asset.release}/{asset.name}: {error}')
        return None
    return module_version_of_entry(name) if name else None


def module_version_of_entry(name: str) -> ModuleVersion | None:
    """The module version of a file in a module zip: `<module>@<version>/<file>`"""
    module, _, rest = name.partition('@')
    try:
        return ModuleVersion.parse(f'{module}@{rest.split("/", 1)[0]}')
    except ModuleError:
        return None


def packages_of(files: list[str]) -> list[str]:
    """The package directories of a module, relative to the module root ('' is the root)"""
    return sorted({os.path.dirname(name).replace(os.sep, '/') for name in files if name.endswith('.go')})


class Fetcher:
    def __init__(self, client: Client, work_dir: str, proxy_url: str | None, fallback_url: str | None) -> None:
        self.client = client
        self.work_dir = work_dir
        self.proxy_url = proxy_url
        self.fallback_url = fallback_url

    def _download(self, url: str, candidate: Candidate, kind: str, headers: dict[str, str] | None = None) -> str:
        # the proxy layout: module paths can't contain @, so no two module versions share a path
        path = os.path.join(
            self.work_dir, kind, *escape(candidate.module).split('/'), '@v', escape(candidate.version) + '.zip'
        )
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.client.download(url, path, headers=headers)
        return path

    def fetch(self, candidate: Candidate) -> Fetched:
        attempts: list[tuple[str, Callable[[], str]]] = []
        if candidate.local:
            local = candidate.local
            attempts.append(('local', lambda: local))
        if candidate.live and self.proxy_url:
            live_url = proxy_file_url(self.proxy_url, candidate.module, escape(candidate.version) + '.zip')
            attempts.append((LIVE_SOURCE, lambda: self._download(live_url, candidate, 'live')))
        if candidate.asset:
            asset = candidate.asset
            attempts.append(
                (
                    f'release asset {asset.release}/{asset.name}',
                    lambda: self._download(asset.url, candidate, 'assets', {'Accept': 'application/octet-stream'}),
                )
            )
        if candidate.fallback and self.fallback_url:
            fallback_url = proxy_file_url(self.fallback_url, candidate.module, escape(candidate.version) + '.zip')
            attempts.append((self.fallback_url, lambda: self._download(fallback_url, candidate, 'fallback')))
        errors = []
        for source, get in attempts:
            try:
                path = get()
                return self._check(candidate, path, source)
            except ConflictError:
                raise
            except (HTTPError, OSError, ZipError) as error:
                errors.append(f'{source}: {error}')
        raise ZipError(f'{candidate.key}: no source has a valid zip:\n  ' + '\n  '.join(errors or ['no source']))

    def _check(self, candidate: Candidate, path: str, source: str) -> Fetched:
        sha256 = file_sha256(path)
        known = candidate.known
        # the hashes of the manifest are good for the exact same zip, which is then not hashed again
        trusted = bool(known.sha256 and known.sha256 == sha256 and known.h1 and known.mod_h1)
        module_zip = read_zip(path, expect=(candidate.module, candidate.version), hash_files=not trusted)
        h1 = known.h1 if trusted and known.h1 else module_zip.h1
        if known.h1 and known.h1 != h1:
            raise ConflictError(
                f'{candidate.key} from {source} has the hash {h1}, but the live site serves {known.h1}. '
                'A published version must never change.'
            )
        return Fetched(
            module=candidate.module,
            version=candidate.version,
            path=path,
            source=source,
            time=known.time or candidate.time or module_zip.time or '1980-01-01T00:00:00Z',
            go_mod=module_zip.go_mod,
            h1=h1,
            mod_h1=module_zip.mod_h1,
            sha256=sha256,
            size=os.path.getsize(path),
            packages=packages_of(module_zip.files),
        )


def read_local(path: str) -> tuple[Candidate, Fetched]:
    """A local module zip as a fetched candidate"""
    module_zip = read_zip(path)
    candidate = Candidate(module_zip.module, module_zip.version, local=path, time=module_zip.time)
    fetched = Fetched(
        module=module_zip.module,
        version=module_zip.version,
        path=path,
        source='local',
        time=module_zip.time or '1980-01-01T00:00:00Z',
        go_mod=module_zip.go_mod,
        h1=module_zip.h1,
        mod_h1=module_zip.mod_h1,
        sha256=file_sha256(path),
        size=os.path.getsize(path),
        packages=packages_of(module_zip.files),
    )
    return candidate, fetched


__all__ = [
    'LIVE_SOURCE',
    'MANIFEST',
    'Candidate',
    'ConflictError',
    'Fetched',
    'Fetcher',
    'Known',
    'first_zip_entry',
    'identify_asset',
    'packages_of',
    'parallel',
    'proxy_file_url',
    'read_list',
    'read_local',
    'read_manifest',
]
