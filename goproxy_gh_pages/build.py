"""Builds the site from all sources: collects versions, applies retention, checks hashes against
the live site and the checksum database, and writes the site."""

from __future__ import annotations

import glob
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field

from goproxy_gh_pages import github
from goproxy_gh_pages.github import Asset, default_base_url, list_releases, matching_assets
from goproxy_gh_pages.module import ModuleVersion, Version, escape, latest_version, sort_versions
from goproxy_gh_pages.modzip import format_time, parse_time
from goproxy_gh_pages.net import Client, HTTPError
from goproxy_gh_pages.seed import PROXY, SUMDB, verify_sumdb
from goproxy_gh_pages.site import Rule, ServedModule, SiteResult, parse_rules, write_site
from goproxy_gh_pages.sources import (
    Candidate,
    ConflictError,
    Fetched,
    Fetcher,
    Known,
    identify_asset,
    parallel,
    proxy_file_url,
    read_list,
    read_local,
    read_manifest,
)

PAGES_LIMIT = 1 << 30


class BuildError(Exception):
    pass


@dataclass
class BuildOptions:
    output: str = '_site'
    base_url: str | None = None
    path: str = 'go'
    modules: str = ''
    zips: list[str] = field(default_factory=list)
    repository: str | None = None
    release_assets: list[str] = field(default_factory=lambda: ['*.zip'])
    releases: int = 30
    include_drafts: bool = True
    mirror: bool = True
    fallback_proxy: str | None = f'{PROXY}/cached-only'
    keep: int = 30
    verify_sumdb: bool = True
    proxy: str = PROXY
    sumdb: str = SUMDB
    merge: bool = False
    home_url: str | None = None
    redirect_root: bool = True
    not_found_page: bool = True
    max_size: int = PAGES_LIMIT
    parallelism: int = 8
    work_dir: str | None = None


@dataclass
class BuildResult:
    output: str
    base_url: str
    proxy_url: str
    modules: dict[str, list[str]]
    new_versions: list[str]
    site: SiteResult
    sources: dict[str, int]


def expand_zips(patterns: list[str]) -> list[str]:
    paths: list[str] = []
    for pattern in patterns:
        if glob.has_magic(pattern):
            matches = sorted(path for path in glob.glob(pattern, recursive=True) if os.path.isfile(path))
            if not matches:
                github.warning(f'zips: {pattern} matches no files')
            paths += matches
        elif os.path.isfile(pattern):
            paths.append(pattern)
        else:
            raise BuildError(f'zips: {pattern} is not a file')
    return list(dict.fromkeys(os.path.abspath(path) for path in paths))


def select_versions(versions: list[str], keep: int, required: set[str]) -> list[str]:
    """The versions to serve: the newest `keep`, the newest of each major version, and the
    required ones. All of them if `keep` is 0."""
    ordered = sort_versions(versions)
    if keep <= 0:
        return ordered
    kept = set(ordered[-keep:]) | required
    newest_of_major: dict[int, str] = {}
    for version in ordered:
        newest_of_major[Version(version).major] = version
    kept |= set(newest_of_major.values())
    return [version for version in ordered if version in kept]


class _Timer:
    def __init__(self) -> None:
        self.start = time.monotonic()
        self.last = self.start

    def lap(self, message: str) -> None:
        now = time.monotonic()
        github.log(f'{message} ({now - self.last:.1f}s)')
        self.last = now


def build(options: BuildOptions, client: Client) -> BuildResult:
    timer = _Timer()
    workers = max(1, options.parallelism)
    repo = options.repository
    base_url = (options.base_url or default_base_url(client, repo) or '').rstrip('/')
    if not base_url:
        raise BuildError('base-url is required when the repository is not known')
    path = options.path.strip('/')
    proxy_url = base_url + (f'/{path}' if path else '')
    rules = parse_rules(options.modules)
    proxy_rules = [rule for rule in rules if rule.mode == 'proxy']
    other_rules = [rule for rule in rules if rule.mode != 'proxy']
    output = options.output
    if os.path.isdir(output) and os.listdir(output) and not options.merge:
        raise BuildError(f'{output} is not empty; set merge to add the site to the files in it')
    os.makedirs(output, exist_ok=True)
    github.log(f'Building the Go module proxy {proxy_url} in {output}')

    ignored: set[str] = set()

    def served_here(module: str) -> bool:
        for rule in other_rules:
            if rule.covers(module):
                if module not in ignored:
                    github.warning(f'{module} is served by {rule.go_import}, so its zips are ignored')
                    ignored.add(module)
                return False
        if proxy_rules and not any(rule.covers(module) for rule in proxy_rules):
            if module not in ignored:
                github.log(f'Ignoring {module}, which is not in modules')
                ignored.add(module)
            return False
        return True

    candidates: dict[ModuleVersion, Candidate] = {}
    fetched: dict[ModuleVersion, Fetched] = {}

    def candidate(module: str, version: str) -> Candidate:
        key = ModuleVersion(module, version)
        if key not in candidates:
            candidates[key] = Candidate(module, version)
        return candidates[key]

    # local zips, which are always served
    for local_candidate, local in parallel(read_local, expand_zips(options.zips), workers):
        key = local_candidate.key
        if not served_here(key.path):
            continue
        if key in fetched and fetched[key].h1 != local.h1:
            raise ConflictError(f'{key}: {fetched[key].path} and {local.path} have different content')
        candidate(*key).local = local.path
        fetched[key] = local
    timer.lap(f'Read {len(fetched)} local zips')

    # the live site: its manifest, or else the version lists of the modules it serves
    manifest = read_manifest(client, proxy_url)
    live: dict[str, set[str]] = {}
    asset_versions: dict[int, ModuleVersion] = {}
    for module, versions in (manifest or {}).items():
        live[module] = set(versions)
        for version, known in versions.items():
            if known.asset is not None:
                asset_versions[known.asset] = ModuleVersion(module, version)

    # release assets, identified by the module version in their zip directory. Assets the
    # manifest knows are not read again.
    assets_found = 0
    if repo and options.release_assets:
        releases = list_releases(client, repo, options.releases, options.include_drafts)
        assets = [asset for release in releases for asset in matching_assets(release, options.release_assets)]
        unknown = [asset for asset in assets if asset.id not in asset_versions]

        def identify(asset: Asset) -> ModuleVersion | None:
            return identify_asset(client, asset)

        identified = dict(zip((asset.id for asset in unknown), parallel(identify, unknown, workers), strict=True))
        for asset in assets:  # newest release first
            module_version = asset_versions.get(asset.id) or identified.get(asset.id)
            if module_version is None or not served_here(module_version.path):
                continue
            entry = candidate(*module_version)
            if entry.asset is None:
                entry.asset = asset
                assets_found += 1
        timer.lap(
            f'Found {assets_found} module zips in {len(assets)} assets of {len(releases)} releases '
            f'of {repo}, read {len(unknown)}'
        )

    modules = sorted(
        {key.path for key in candidates}
        | {module for module in live if served_here(module)}
        | {rule.prefix for rule in proxy_rules}
    )
    if manifest is None:
        lists = parallel(lambda module: (module, read_list(client, proxy_url, module)), modules, workers)
        for module, listed in lists:
            if listed:
                live[module] = set(listed)
        timer.lap(f'Read the version lists of {len(modules)} modules from the live site')
    for module in modules:
        for version in live.get(module, ()):
            candidate(module, version).live = options.mirror

    if options.fallback_proxy:
        fallback_url = options.fallback_proxy
        lists = parallel(lambda module: (module, read_list(client, fallback_url, module)), modules, workers)
        for module, listed in lists:
            for version in listed or []:
                candidate(module, version).fallback = True
        timer.lap(f'Read the version lists of {len(modules)} modules from {fallback_url}')

    for key, entry in candidates.items():
        if manifest and key.version in manifest.get(key.path, {}):
            entry.known = manifest[key.path][key.version]

    # retention
    by_module: dict[str, list[Candidate]] = {}
    for entry in candidates.values():
        if entry.local or entry.live or entry.asset or entry.fallback:
            by_module.setdefault(entry.module, []).append(entry)
    selected: list[Candidate] = []
    for module in sorted(by_module):
        entries = {entry.version: entry for entry in by_module[module]}
        required = {version for version, entry in entries.items() if entry.local}
        kept = select_versions(list(entries), options.keep, required)
        selected += [entries[version] for version in kept]
        dropped = sorted(set(live.get(module, ())) - set(kept))
        if dropped:
            gone = [version for version in dropped if version not in entries]
            if gone:
                github.warning(f'{module}: the live site serves {", ".join(gone)}, which no source has anymore')
            retired = [version for version in dropped if version in entries]
            if retired:
                github.log(f'{module}: not serving {", ".join(retired)} anymore, keeping the newest {options.keep}')
    for rule in proxy_rules:
        if not any(rule.covers(module) for module in by_module):
            github.warning(f'No versions found for {rule.prefix}')

    # local zips must match the live site, and release assets of the same version
    for key, local in fetched.items():
        known = candidates[key].known
        if known.h1 and known.h1 != local.h1:
            raise ConflictError(
                f'{key} in {local.path} has the hash {local.h1}, but the live site serves {known.h1}. '
                'A published version must never change.'
            )
    if options.work_dir:
        os.makedirs(options.work_dir, exist_ok=True)
    work_dir = tempfile.mkdtemp(prefix='goproxy-gh-pages-', dir=options.work_dir)
    try:
        fetcher = Fetcher(client, work_dir, proxy_url if options.mirror else None, options.fallback_proxy)
        compare = [
            Candidate(entry.module, entry.version, asset=entry.asset, known=Known(h1=fetched[entry.key].h1))
            for entry in selected
            if entry.local
            and entry.asset
            and entry.asset.digest
            and entry.asset.digest != f'sha256:{fetched[entry.key].sha256}'
        ]
        parallel(fetcher.fetch, compare, workers)

        def fallback_time(entry: Candidate, fallback_url: str) -> str | None:
            url = proxy_file_url(fallback_url, entry.module, escape(entry.version) + '.info')
            try:
                return format_time(parse_time(client.get_json(url)['Time']))
            except (HTTPError, ValueError, KeyError, TypeError, AttributeError) as error:
                github.warning(f'{entry.key}: could not read the time from {url}: {error}')
                return None

        def fetch(entry: Candidate) -> Fetched | Exception:
            try:
                result = fetcher.fetch(entry)
                # zips of the proxy have no time; its .info has the time of the version
                if options.fallback_proxy and result.source == options.fallback_proxy and not entry.known.time:
                    result.time = fallback_time(entry, options.fallback_proxy) or result.time
                return result
            except ConflictError:
                raise
            except Exception as error:  # noqa: BLE001 - reported as a warning below
                return error

        missing = [entry for entry in selected if entry.key not in fetched]
        for entry, result in zip(missing, parallel(fetch, missing, workers), strict=True):
            if isinstance(result, Exception):
                github.warning(str(result))
                continue
            fetched[entry.key] = result
        counts: dict[str, int] = {}
        for entry in selected:
            if entry.key in fetched:
                source = fetched[entry.key].source.split(' ', 1)[0]
                counts[source] = counts.get(source, 0) + 1
        timer.lap(
            f'Fetched {len(missing)} versions: '
            + ', '.join(f'{count} from {source}' for source, count in sorted(counts.items()))
        )

        served = [entry for entry in selected if entry.key in fetched]
        verified: dict[tuple[str, str], bool] = {}
        if options.verify_sumdb:
            to_verify = []
            for entry in served:
                result = fetched[entry.key]
                if entry.known.sumdb and entry.known.sha256 == result.sha256:
                    verified[entry.key] = True
                else:
                    to_verify.append(entry)

            def verify(entry: Candidate) -> bool | None:
                result = fetched[entry.key]
                try:
                    return verify_sumdb(client, options.proxy, options.sumdb, entry.key, result.h1, result.mod_h1)
                except HTTPError as error:
                    github.warning(f'Could not check {entry.key} against the checksum database: {error}')
                    return None

            for entry, status in zip(to_verify, parallel(verify, to_verify, workers), strict=True):
                if status:
                    verified[entry.key] = True
            timer.lap(f'Checked {len(to_verify)} versions against the checksum database')

        served_modules: dict[str, ServedModule] = {}
        asset_ids: dict[tuple[str, str], int] = {}
        for entry in served:
            result = fetched[entry.key]
            served_module = served_modules.setdefault(entry.module, ServedModule(entry.module, {}))
            served_module.versions[entry.version] = result
            served_module.packages.update(result.packages)
            asset_id = entry.asset.id if entry.asset else entry.known.asset
            if asset_id is not None:
                asset_ids[entry.key] = asset_id
        site = write_site(
            output=output,
            proxy_path=path,
            base_url=base_url,
            proxy_url=proxy_url,
            served=list(served_modules.values()),
            rules=rules,
            home_url=options.home_url,
            redirect_root=options.redirect_root,
            not_found_page=options.not_found_page,
            sumdb_verified=verified,
            asset_ids=asset_ids,
            merge=options.merge,
        )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
    for warning in site.warnings:
        github.warning(warning)
    timer.lap(f'Wrote {site.files} files, {site.size / (1 << 20):.1f} MiB, {site.pages} go-import pages')

    if site.size > options.max_size:
        raise BuildError(
            f'The site has {site.size} bytes, more than the limit of {options.max_size}; serve fewer versions with keep'
        )
    if site.size > options.max_size * 0.8:
        github.warning(
            f'The site has {site.size} bytes, {site.size * 100 // options.max_size}% of the limit of {options.max_size}'
        )

    result_modules = {module: sort_versions(list(entry.versions)) for module, entry in sorted(served_modules.items())}
    new_versions = [
        f'{module}@{version}'
        for module, versions in result_modules.items()
        for version in versions
        if version not in live.get(module, set())
    ]
    for rule in other_rules:
        result_modules.setdefault(rule.prefix, [])
    github.log(f'Done in {time.monotonic() - timer.start:.1f}s')
    return BuildResult(
        output=output,
        base_url=base_url,
        proxy_url=proxy_url,
        modules=result_modules,
        new_versions=new_versions,
        site=site,
        sources=counts,
    )


def summary(result: BuildResult, rules: list[Rule] | None = None) -> str:
    new = set(result.new_versions)
    lines = [
        f'### Go module proxy `{result.proxy_url}`',
        '',
        '| Module | Versions | Latest | New |',
        '| --- | --- | --- | --- |',
    ]
    for module, versions in result.modules.items():
        added = [version for version in versions if f'{module}@{version}' in new]
        lines.append(f'| `{module}` | {len(versions)} | {latest_version(versions) or ""} | {", ".join(added)} |')
    lines.append('')
    lines.append(f'{result.site.files} files, {result.site.size / (1 << 20):.1f} MiB')
    return '\n'.join(lines) + '\n'
