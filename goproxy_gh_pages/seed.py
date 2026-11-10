"""The public Go module mirror and checksum database: lookups, seeding new versions and waiting
until they are available to consumers."""

from __future__ import annotations

import contextlib
import json
import random
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field

from goproxy_gh_pages import github
from goproxy_gh_pages.module import ModuleVersion, escape
from goproxy_gh_pages.net import Client, HTTPError
from goproxy_gh_pages.sources import proxy_file_url

PROXY = 'https://proxy.golang.org'
SUMDB = 'https://sum.golang.org'
INDEX = 'https://index.golang.org'
PKGSITE = 'https://pkg.go.dev'
TARGETS = ('proxy', 'latest', 'sumdb', 'index', 'pkgsite')


def sumdb_lookup(client: Client, sumdb: str, module_version: ModuleVersion) -> tuple[str, str] | None:
    """The h1 hashes of a module version and of its go.mod in the checksum database, or None if it
    doesn't have them. A lookup of an unknown version makes the database fetch it from the proxy."""
    url = f'{sumdb.rstrip("/")}/lookup/{escape(module_version.path)}@{escape(module_version.version)}'
    response = client.exists(url)
    if response is None:
        return None
    h1 = mod_h1 = None
    for line in response.text().splitlines():
        fields = line.split()
        if len(fields) != 3 or fields[0] != module_version.path:
            continue
        if fields[1] == module_version.version:
            h1 = fields[2]
        elif fields[1] == f'{module_version.version}/go.mod':
            mod_h1 = fields[2]
    if not h1 or not mod_h1:
        return None
    return h1, mod_h1


def cached(client: Client, proxy: str, module_version: ModuleVersion) -> bool:
    """Whether the proxy has a version cached already. Asks the cached-only endpoint, which never
    fetches from the origin, so asking doesn't make the proxy cache a version that isn't there yet."""
    url = proxy_file_url(
        f'{proxy.rstrip("/")}/cached-only', module_version.path, escape(module_version.version) + '.info'
    )
    return client.exists(url) is not None


def verify_sumdb(
    client: Client, proxy: str, sumdb: str, module_version: ModuleVersion, h1: str, mod_h1: str
) -> bool | None:
    """Checks a version against the checksum database: True if it matches, None if the database
    doesn't have the version. Raises if the database has different hashes."""
    if not cached(client, proxy, module_version):
        return None
    hashes = sumdb_lookup(client, sumdb, module_version)
    if hashes is None:
        return None
    if hashes != (h1, mod_h1):
        raise ValueError(
            f'{module_version} does not match the checksum database: it has {hashes[0]} and go.mod {hashes[1]}, '
            f'the site would serve {h1} and go.mod {mod_h1}. The go command would refuse it. '
            'Published versions must never change; publish a new version instead.'
        )
    return True


@dataclass
class WaitResult:
    available: dict[str, dict[str, float]] = field(default_factory=dict)
    missing: dict[str, list[str]] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return not any(self.missing.values())


@dataclass
class Seeder:
    client: Client
    proxy: str = PROXY
    sumdb: str = SUMDB
    index: str = INDEX
    pkgsite: str = PKGSITE
    interval: float = 10.0
    timeout: float = 900.0
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    wall_clock: Callable[[], float] = time.time
    expected: dict[ModuleVersion, tuple[str, str]] = field(default_factory=dict)

    def _poll(self, check: Callable[[], bool], deadline: float) -> bool:
        delay = self.interval
        while True:
            if check():
                return True
            remaining = deadline - self.clock()
            if remaining <= 0:
                return False
            self.sleep(min(delay * (0.8 + 0.4 * random.random()), remaining))
            delay = min(delay * 1.5, self.interval * 6)

    def _file(self, base: str, module_version: ModuleVersion, name: str) -> str:
        return proxy_file_url(base, module_version.path, name)

    def wait_for_site(self, site: str, versions: list[ModuleVersion], deadline: float) -> list[ModuleVersion]:
        """Waits until the site serves the versions; GitHub Pages takes a moment after the deploy.
        Returns the versions it doesn't serve."""
        pending = list(versions)

        def check() -> bool:
            pending[:] = [
                version
                for version in pending
                if self.client.exists(self._file(site, version, escape(version.version) + '.info')) is None
            ]
            return not pending

        self._poll(check, deadline)
        return pending

    def seed(self, versions: list[ModuleVersion]) -> list[str]:
        """Asks the proxy for the versions, which makes it fetch and cache them, and the checksum
        database, which records their hashes. Returns errors."""
        errors = []
        for version in versions:
            escaped = escape(version.version)
            try:
                for name in (f'{escaped}.info', f'{escaped}.mod', f'{escaped}.zip'):
                    self.client.drain(self._file(self.proxy, version, name))
                sumdb_lookup(self.client, self.sumdb, version)
                github.log(f'Seeded {version}')
            except HTTPError as error:
                errors.append(f'{version}: {error}')
        return errors

    def _available(self, target: str, version: ModuleVersion) -> bool:
        escaped = escape(version.version)
        if target == 'proxy':
            return self.client.exists(self._file(self.proxy, version, f'{escaped}.info')) is not None
        if target == 'latest':
            response = self.client.exists(self._file(self.proxy, version, 'list'))
            return response is not None and version.version in response.text().split()
        if target == 'sumdb':
            hashes = sumdb_lookup(self.client, self.sumdb, version)
            if hashes is None:
                return False
            expected = self.expected.get(version)
            if expected and hashes != expected:
                raise ValueError(f'{version} has the hashes {hashes} in the checksum database, expected {expected}')
            return True
        if target == 'pkgsite':
            url = f'{self.pkgsite.rstrip("/")}/{urllib.parse.quote(str(version), safe="/@")}'
            return self.client.exists(url) is not None
        raise ValueError(f'unknown target {target}')

    def _index(self, cursor: list[str], pending: set[ModuleVersion]) -> set[ModuleVersion]:
        """The pending versions that are in the module index after the cursor, which moves on, so
        each poll only reads what is new"""
        found = set()
        for _ in range(100):
            response = self.client.get(
                f'{self.index.rstrip("/")}/index?since={urllib.parse.quote(cursor[0])}&limit=2000'
            )
            lines = [line for line in response.text().splitlines() if line.strip()]
            for line in lines:
                entry = json.loads(line)
                module_version = ModuleVersion(entry['Path'], entry['Version'])
                if module_version in pending:
                    found.add(module_version)
                cursor[0] = entry['Timestamp']
            if len(lines) < 2000:
                break
        return found

    def request_pkgsite(self, versions: list[ModuleVersion]) -> None:
        """Asks pkg.go.dev to fetch the versions now, as its button does. Best effort."""
        for version in versions:
            url = f'{self.pkgsite.rstrip("/")}/fetch/{urllib.parse.quote(str(version), safe="/@")}'
            with contextlib.suppress(HTTPError):
                self.client.open(url, method='POST', ok=(400, 404, 405, 429, 500))

    def wait(self, versions: list[ModuleVersion], targets: list[str], started: float | None = None) -> WaitResult:
        """Polls each target until it serves all versions, or until the timeout. The index is read
        from 15 minutes before the start, which covers versions the proxy fetched before."""
        start = self.clock() if started is None else started
        deadline = start + self.timeout
        cursor = [time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(self.wall_clock() - 900))]
        result = WaitResult()
        pending = {target: set(versions) for target in targets}
        for target in targets:
            result.available[target] = {}
        if 'pkgsite' in targets:
            self.request_pkgsite(versions)

        def check() -> bool:
            for target in targets:
                if not pending[target]:
                    continue
                if target == 'index':
                    try:
                        done = self._index(cursor, pending[target])
                    except (HTTPError, ValueError, KeyError) as error:
                        github.log(f'index: {error}')
                        done = set()
                else:
                    done = set()
                    for version in sorted(pending[target]):
                        try:
                            if self._available(target, version):
                                done.add(version)
                        except HTTPError as error:
                            github.log(f'{target}: {version}: {error}')
                for version in done:
                    result.available[target][str(version)] = round(self.clock() - start, 1)
                    github.log(f'{version} is available on {target} after {result.available[target][str(version)]}s')
                pending[target] -= done
            return not any(pending.values())

        self._poll(check, deadline)
        result.missing = {target: sorted(str(version) for version in pending[target]) for target in targets}
        return result


def parse_targets(text: str) -> list[str]:
    targets = [target.strip() for target in text.replace('\n', ',').split(',') if target.strip()]
    if targets == ['none'] or not targets:
        return []
    unknown = [target for target in targets if target not in TARGETS]
    if unknown:
        raise ValueError(f'unknown wait-for target {", ".join(unknown)}; expected {", ".join(TARGETS)} or none')
    return list(dict.fromkeys(targets))


def parse_versions(text: str) -> list[ModuleVersion]:
    """module@version lines, or a JSON list of them as the build action writes it"""
    text = text.strip()
    if not text:
        return []
    items = json.loads(text) if text.startswith('[') else text.replace(',', '\n').split()
    return list(dict.fromkeys(ModuleVersion.parse(str(item)) for item in items))


def summary(result: WaitResult) -> str:
    lines = ['| Target | Version | Available after |', '| --- | --- | --- |']
    for target, versions in result.available.items():
        for version, seconds in sorted(versions.items()):
            lines.append(f'| {target} | `{version}` | {seconds:.0f}s |')
        for version in result.missing.get(target, []):
            lines.append(f'| {target} | `{version}` | not yet |')
    return '\n'.join(lines) + '\n'
