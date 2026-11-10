"""Command line: `pack`, `build`, `seed`, `wait` and `verify`, and `action <command>`, which reads
the inputs of the GitHub Actions from INPUT_* environment variables and writes their outputs."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Sequence

from goproxy_gh_pages import __version__, github
from goproxy_gh_pages.build import PAGES_LIMIT, BuildError, BuildOptions, build
from goproxy_gh_pages.build import summary as build_summary
from goproxy_gh_pages.module import ModuleError, ModuleVersion
from goproxy_gh_pages.modzip import ZipError, ZipResult, read_zip
from goproxy_gh_pages.net import Client, HTTPError
from goproxy_gh_pages.pack import DEFAULT_OUTPUT, pack
from goproxy_gh_pages.seed import PROXY, SUMDB, Seeder, parse_targets, parse_versions, sumdb_lookup
from goproxy_gh_pages.seed import summary as seed_summary
from goproxy_gh_pages.sources import read_manifest


class InputError(ValueError):
    pass


def parse_size(text: str) -> int:
    match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*([kmgt]?)(i?b?)\s*', text.lower())
    if not match:
        raise InputError(f'not a size: {text!r}')
    number, unit, suffix = match.groups()
    base = 1024 if suffix.startswith('i') or not suffix else 1000
    return int(float(number) * base ** ' kmgt'.index(unit or ' '))


def parse_bool(name: str, text: str) -> bool:
    value = text.strip().lower()
    if value in ('true', 'yes', '1', 'on'):
        return True
    if value in ('false', 'no', '0', 'off', ''):
        return False
    raise InputError(f'{name}: expected true or false, got {text!r}')


def parse_int(name: str, text: str) -> int:
    try:
        return int(text.strip())
    except ValueError:
        raise InputError(f'{name}: expected a number, got {text!r}') from None


def lines(text: str) -> list[str]:
    return [line.strip() for line in text.replace(',', '\n').splitlines() if line.strip()]


def client_for(token: str | None) -> Client:
    return Client(token=token or os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN'))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='goproxy-gh-pages',
        description='Serve Go modules from GitHub Pages as a static Go module proxy',
    )
    parser.add_argument('--version', action='version', version=__version__)
    commands = parser.add_subparsers(dest='command', required=True)

    command = commands.add_parser('pack', help='write the module zip of a module directory')
    command.add_argument('module_dir')
    command.add_argument('version')
    command.add_argument('-o', '--output', default=DEFAULT_OUTPUT, help=f'zip path (default: {DEFAULT_OUTPUT})')
    command.add_argument('--time', help='version time, RFC 3339 or seconds since the epoch')
    command.add_argument('--compression-level', type=int, default=9)
    command.add_argument('--repository', default=github.repository())
    command.add_argument('--release-tag', help='upload the zip to this release')
    command.add_argument('--asset-name')

    command = commands.add_parser('build', help='write the static site')
    command.add_argument('-o', '--output', default='_site')
    command.add_argument('--base-url')
    command.add_argument('--path', default='go')
    command.add_argument('--modules', default='', help='module lines, or @file')
    command.add_argument('--zip', dest='zips', action='append', default=[], help='local zip or glob (repeatable)')
    command.add_argument('--repository', default=github.repository())
    command.add_argument('--release-assets', action='append', help='asset name glob (default: *.zip)')
    command.add_argument('--no-release-assets', action='store_true')
    command.add_argument('--releases', type=int, default=30)
    command.add_argument('--no-drafts', action='store_true')
    command.add_argument('--no-mirror', action='store_true')
    command.add_argument('--fallback-proxy', default=f'{PROXY}/cached-only', help="'' to disable")
    command.add_argument('--keep', type=int, default=30)
    command.add_argument('--no-verify-sumdb', action='store_true')
    command.add_argument('--merge', action='store_true')
    command.add_argument('--home-url')
    command.add_argument('--no-redirect-root', action='store_true')
    command.add_argument('--no-404', action='store_true')
    command.add_argument('--max-size', type=parse_size, default=PAGES_LIMIT)
    command.add_argument('--parallelism', type=int, default=8)

    for name in ('seed', 'wait'):
        command = commands.add_parser(
            name,
            help='have the public mirror fetch new versions and wait for them'
            if name == 'seed'
            else 'wait until the public mirror serves versions',
        )
        command.add_argument('versions', nargs='+', help='module@version, or a JSON list of them')
        command.add_argument('--site-url', help='the proxy URL of the site, to wait for the deploy')
        command.add_argument('--wait-for', default='proxy', help='proxy, latest, sumdb, index, pkgsite or none')
        command.add_argument('--wait-interval', type=float, default=10)
        command.add_argument('--wait-timeout', type=float, default=900)
        command.add_argument('--no-fail-on-timeout', action='store_true')
        command.add_argument('--proxy', default=PROXY)
        command.add_argument('--sumdb', default=SUMDB)

    command = commands.add_parser('verify', help='check module zips and print their hashes')
    command.add_argument('zips', nargs='+')
    command.add_argument('--sumdb', help='also compare with this checksum database, e.g. https://sum.golang.org')

    action = commands.add_parser('action', help='run as GitHub Action, with inputs from INPUT_* variables')
    action.add_argument('action', choices=('pack', 'build', 'seed'))
    return parser


def _input(name: str, default: str = '') -> str:
    return os.environ.get(f'INPUT_{name.upper().replace("-", "_")}', default)


def run_pack(args: argparse.Namespace) -> int:
    result = pack(
        args.module_dir,
        args.version,
        output=args.output,
        time=args.time,
        compression_level=args.compression_level,
        repository=args.repository,
        release_tag=args.release_tag,
        asset_name=args.asset_name,
        client=client_for(None) if args.release_tag else None,
    )
    print(json.dumps(_pack_outputs(result.zip), indent=2))
    return 0


def _pack_outputs(result: ZipResult) -> dict[str, str]:
    return {
        'zip': result.path,
        'module': result.module,
        'version': result.version,
        'time': result.time,
        'h1': result.h1,
        'mod-h1': result.mod_h1,
    }


def action_pack() -> int:
    repository = _input('repository') or github.repository()
    release_tag = _input('release-tag').strip() or None
    level = parse_int('compression-level', _input('compression-level', '9'))
    if not 0 <= level <= 9:
        raise InputError('compression-level: expected 0 to 9')
    module_dir = _input('module-dir').strip()
    version = _input('version').strip()
    if not module_dir or not version:
        raise InputError('module-dir and version are required')
    result = pack(
        module_dir,
        version,
        output=_input('output').strip() or DEFAULT_OUTPUT,
        time=_input('time').strip() or None,
        compression_level=level,
        repository=repository,
        release_tag=release_tag,
        asset_name=_input('asset-name').strip() or None,
        client=client_for(_input('token')) if release_tag else None,
    )
    for name, value in _pack_outputs(result.zip).items():
        github.set_output(name, value)
    return 0


def _build_options(args: argparse.Namespace) -> BuildOptions:
    modules = args.modules
    if modules.startswith('@'):
        with open(modules[1:], encoding='utf-8') as file:
            modules = file.read()
    return BuildOptions(
        output=args.output,
        base_url=args.base_url,
        path=args.path,
        modules=modules,
        zips=args.zips,
        repository=args.repository,
        release_assets=[] if args.no_release_assets else (args.release_assets or ['*.zip']),
        releases=args.releases,
        include_drafts=not args.no_drafts,
        mirror=not args.no_mirror,
        fallback_proxy=args.fallback_proxy or None,
        keep=args.keep,
        verify_sumdb=not args.no_verify_sumdb,
        merge=args.merge,
        home_url=args.home_url,
        redirect_root=not args.no_redirect_root,
        not_found_page=not args.no_404,
        max_size=args.max_size,
        parallelism=args.parallelism,
    )


def run_build(args: argparse.Namespace) -> int:
    result = build(_build_options(args), client_for(None))
    print(
        json.dumps(
            {'proxy-url': result.proxy_url, 'modules': result.modules, 'new-versions': result.new_versions}, indent=2
        )
    )
    return 0


def action_build() -> int:
    repository = _input('repository').strip() or github.repository()
    server = os.environ.get('GITHUB_SERVER_URL', 'https://github.com')
    home_url = _input('home-url').strip() or (f'{server}/{repository}' if repository else None)
    options = BuildOptions(
        output=_input('output').strip() or '_site',
        base_url=_input('base-url').strip() or None,
        path=_input('path', 'go').strip(),
        modules=_input('modules'),
        zips=lines(_input('zips')),
        repository=repository,
        release_assets=lines(_input('release-assets', '*.zip')),
        releases=parse_int('releases', _input('releases', '30')),
        include_drafts=parse_bool('include-drafts', _input('include-drafts', 'true')),
        mirror=parse_bool('mirror', _input('mirror', 'true')),
        fallback_proxy=_input('fallback-proxy', f'{PROXY}/cached-only').strip() or None,
        keep=parse_int('keep', _input('keep', '30')),
        verify_sumdb=parse_bool('verify-sumdb', _input('verify-sumdb', 'true')),
        merge=parse_bool('merge', _input('merge', 'false')),
        home_url=home_url,
        redirect_root=parse_bool('redirect-root', _input('redirect-root', 'true')),
        not_found_page=parse_bool('not-found-page', _input('not-found-page', 'true')),
        max_size=parse_size(_input('max-size', '1GiB')),
        parallelism=parse_int('parallelism', _input('parallelism', '8')),
        work_dir=os.environ.get('RUNNER_TEMP') or None,
    )
    result = build(options, client_for(_input('token')))
    github.set_output('path', result.output)
    github.set_output('base-url', result.base_url)
    github.set_output('proxy-url', result.proxy_url)
    github.set_output('modules', json.dumps(result.modules))
    github.set_output('new-versions', json.dumps(result.new_versions))
    github.add_summary(build_summary(result))
    return 0


def _seed(
    versions: list[ModuleVersion],
    site_url: str | None,
    seed: bool,
    targets: list[str],
    interval: float,
    timeout: float,
    fail_on_timeout: bool,
    proxy: str,
    sumdb: str,
    client: Client,
) -> tuple[int, dict[str, dict[str, float]], str]:
    if not versions:
        github.log('No versions to seed')
        return 0, {}, ''
    seeder = Seeder(client, proxy=proxy, sumdb=sumdb, interval=interval, timeout=timeout)
    start = seeder.clock()
    deadline = start + timeout
    problems: list[str] = []
    to_seed = versions
    if seed and site_url:
        github.log(f'Waiting until {site_url} serves {len(versions)} versions')
        not_live = seeder.wait_for_site(site_url, versions, deadline)
        if not_live:
            problems.append('the site does not serve ' + ', '.join(str(version) for version in not_live))
            to_seed = [version for version in versions if version not in not_live]
    if site_url:
        # read after the wait, so the manifest is the one of the new deployment
        manifest = read_manifest(client, site_url) or {}
        for version in versions:
            known = manifest.get(version.path, {}).get(version.version)
            if known and known.h1 and known.mod_h1:
                seeder.expected[version] = (known.h1, known.mod_h1)
    if seed:
        problems += seeder.seed(to_seed)
    result = seeder.wait(to_seed, targets, started=start) if targets else None
    available = result.available if result else {}
    report = seed_summary(result) if result else ''
    if result and not result.complete:
        problems += [
            f'{target}: {", ".join(missing)} not available after {timeout:.0f}s'
            for target, missing in result.missing.items()
            if missing
        ]
    if problems:
        message = '; '.join(problems)
        if fail_on_timeout:
            github.log(report)
            raise TimeoutError(message)
        github.warning(message)
    return 0, available, report


def run_seed(args: argparse.Namespace) -> int:
    code, available, _ = _seed(
        parse_versions('\n'.join(args.versions)),
        args.site_url,
        args.command == 'seed',
        parse_targets(args.wait_for),
        args.wait_interval,
        args.wait_timeout,
        not args.no_fail_on_timeout,
        args.proxy,
        args.sumdb,
        client_for(None),
    )
    print(json.dumps(available, indent=2))
    return code


def action_seed() -> int:
    site_url = _input('site-url').strip() or None
    code, available, report = _seed(
        parse_versions(_input('versions')),
        site_url,
        parse_bool('seed', _input('seed', 'true')),
        parse_targets(_input('wait-for', 'proxy')),
        float(_input('wait-interval', '10') or 10),
        float(_input('wait-timeout', '900') or 900),
        parse_bool('fail-on-timeout', _input('fail-on-timeout', 'true')),
        _input('proxy', PROXY).strip() or PROXY,
        _input('sumdb', SUMDB).strip() or SUMDB,
        Client(),
    )
    github.set_output('available', json.dumps(available))
    if report:
        github.add_summary(report)
    return code


def run_verify(args: argparse.Namespace) -> int:
    client = Client() if args.sumdb else None
    failed = 0
    for path in args.zips:
        try:
            module_zip = read_zip(path)
        except (ZipError, ModuleError, OSError) as error:
            print(f'{path}: {error}', file=sys.stderr)
            failed += 1
            continue
        print(
            f'{path}: {module_zip.module}@{module_zip.version} {module_zip.time or "unknown time"} '
            f'{module_zip.h1} go.mod {module_zip.mod_h1}'
        )
        if client:
            hashes = sumdb_lookup(client, args.sumdb, ModuleVersion(module_zip.module, module_zip.version))
            if hashes is None:
                print('  not in the checksum database')
            elif hashes != (module_zip.h1, module_zip.mod_h1):
                print(f'  does not match the checksum database: {hashes[0]} go.mod {hashes[1]}', file=sys.stderr)
                failed += 1
            else:
                print('  matches the checksum database')
    return 1 if failed else 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == 'action':
            return {'pack': action_pack, 'build': action_build, 'seed': action_seed}[args.action]()
        return {
            'pack': run_pack,
            'build': run_build,
            'seed': run_seed,
            'wait': run_seed,
            'verify': run_verify,
        }[args.command](args)
    except (
        BuildError,
        InputError,
        ModuleError,
        ZipError,
        HTTPError,
        LookupError,
        RuntimeError,
        TimeoutError,
        ValueError,
        OSError,
    ) as error:
        if os.environ.get('GITHUB_ACTIONS') == 'true':
            print(
                f'::error::{str(error).replace("%", "%25").replace(chr(13), "%0D").replace(chr(10), "%0A")}', flush=True
            )
        else:
            print(f'error: {error}', file=sys.stderr)
        return 1
