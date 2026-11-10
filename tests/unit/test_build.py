from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from goproxy_gh_pages.build import BuildError, BuildOptions, build, expand_zips, select_versions, summary
from goproxy_gh_pages.net import Client
from goproxy_gh_pages.sources import ConflictError
from tests.unit.conftest import FakeServer, Reply, Request
from tests.unit.proxyfakes import (
    asset,
    make_zip,
    manifest_entry,
    serve_cached,
    serve_manifest,
    serve_proxy,
    serve_releases,
    serve_sumdb,
    without_time,
)

M = '127.0.0.1/site/lib'
LIVE = '/site/go'
REPO = 'owner/repo'


def options(server: FakeServer, tmp_path: Path, **overrides: Any) -> BuildOptions:
    values: dict[str, Any] = {
        'output': str(tmp_path / 'out'),
        'base_url': f'{server.url}/site',
        'fallback_proxy': None,
        'verify_sumdb': False,
        'release_assets': [],
        'work_dir': str(tmp_path),
        'parallelism': 4,
    }
    values.update(overrides)
    return BuildOptions(**values)


def manifest(tmp_path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((tmp_path / 'out' / 'go' / 'index.json').read_text())
    return data


def test_local_zips(server: FakeServer, client: Client, tmp_path: Path) -> None:
    one = make_zip(tmp_path, M, 'v1.0.0')
    two = make_zip(tmp_path, M, 'v1.1.0', {'lib.go': 'package lib\n', 'sub/sub.go': 'package sub\n'})
    result = build(options(server, tmp_path, zips=[one.path, two.path, one.path]), client)
    out = tmp_path / 'out'
    assert result.modules == {M: ['v1.0.0', 'v1.1.0']}
    assert result.new_versions == [f'{M}@v1.0.0', f'{M}@v1.1.0']
    assert result.sources == {'local': 2}
    assert result.proxy_url == f'{server.url}/site/go'
    assert (out / 'go' / M / '@v' / 'list').read_text() == 'v1.0.0\nv1.1.0\n'
    assert (out / 'go' / M / '@v' / 'v1.1.0.zip').read_bytes() == Path(two.path).read_bytes()
    assert json.loads((out / 'go' / M / '@latest').read_text())['Version'] == 'v1.1.0'
    assert (out / 'lib' / 'index.html').exists()
    assert (out / 'lib' / 'sub' / 'index.html').exists()
    assert (out / '404.html').exists()
    versions = manifest(tmp_path)['modules'][M]['versions']
    assert versions['v1.0.0']['h1'] == one.h1
    assert 'sumdb' not in versions['v1.0.0']
    # the live site and its lists were asked, nothing else
    assert f'{LIVE}/index.json' in server.paths()
    assert f'{LIVE}/{M}/@v/list' in server.paths()


def test_base_url_from_the_repository(server: FakeServer, client: Client, tmp_path: Path) -> None:
    server.route(f'/repos/{REPO}/pages', json.dumps({'html_url': f'{server.url}/site/'}))
    zip_file = make_zip(tmp_path, M, 'v1.0.0')
    result = build(options(server, tmp_path, base_url=None, repository=REPO, zips=[zip_file.path], path='/'), client)
    assert result.base_url == f'{server.url}/site'
    assert result.proxy_url == f'{server.url}/site'
    assert (tmp_path / 'out' / M.removeprefix('127.0.0.1/site/') / '@v' / 'list').exists() is False
    assert (tmp_path / 'out' / M / '@v' / 'list').exists()


def test_base_url_is_required(server: FakeServer, client: Client, tmp_path: Path) -> None:
    with pytest.raises(BuildError, match='base-url is required'):
        build(options(server, tmp_path, base_url=None), client)


def test_output_must_be_empty_unless_merged(server: FakeServer, client: Client, tmp_path: Path) -> None:
    out = tmp_path / 'out'
    out.mkdir()
    (out / 'index.html').write_text('docs')
    zip_file = make_zip(tmp_path, M, 'v1.0.0')
    with pytest.raises(BuildError, match='not empty'):
        build(options(server, tmp_path, zips=[zip_file.path]), client)
    build(options(server, tmp_path, zips=[zip_file.path], merge=True), client)
    assert (out / 'index.html').read_text() == 'docs'


def test_local_zips_with_different_content_conflict(server: FakeServer, client: Client, tmp_path: Path) -> None:
    one = make_zip(tmp_path, M, 'v1.0.0', name='one')
    two = make_zip(tmp_path, M, 'v1.0.0', {'lib.go': 'package lib // changed\n'}, name='two')
    with pytest.raises(ConflictError, match='different content'):
        build(options(server, tmp_path, zips=[one.path, two.path]), client)


def test_release_assets(server: FakeServer, client: Client, tmp_path: Path) -> None:
    old = make_zip(tmp_path, M, 'v1.0.0')
    new = make_zip(tmp_path, M, 'v1.1.0')
    other = make_zip(tmp_path, '127.0.0.1/site/other', 'v1.0.0')
    serve_releases(
        server,
        REPO,
        [
            {
                'tag': 'v1.1.0',
                'draft': True,
                'assets': [
                    asset(server, 1, 'lib-go-module-v1.1.0.zip', Path(new.path).read_bytes()),
                    asset(server, 2, 'notes.txt', b'text'),
                    asset(server, 3, 'other.zip', Path(other.path).read_bytes()),
                    asset(server, 4, 'tiny.zip', b'PK'),
                ],
            },
            {
                'tag': 'v1.0.0',
                'assets': [
                    asset(server, 5, 'lib-go-module-v1.0.0.zip', Path(old.path).read_bytes()),
                    # the same version again: the newest release wins
                    asset(server, 6, 'again.zip', Path(new.path).read_bytes()),
                ],
            },
        ],
    )
    result = build(
        options(server, tmp_path, repository=REPO, release_assets=['*.zip'], modules=f'{M}\n'),
        client,
    )
    assert result.modules == {M: ['v1.0.0', 'v1.1.0']}
    assert result.sources == {'release': 2}
    assert '/assets/2' not in server.paths()
    assert '/assets/6' in server.paths()  # identified, not downloaded
    versions = manifest(tmp_path)['modules'][M]['versions']
    assert versions['v1.1.0']['asset'] == 1
    assert versions['v1.0.0']['asset'] == 5


def test_manifest_asset_ids_skip_identification(server: FakeServer, client: Client, tmp_path: Path) -> None:
    released = make_zip(tmp_path, M, 'v1.0.0')
    data = Path(released.path).read_bytes()
    serve_releases(server, REPO, [{'tag': 'v1.0.0', 'assets': [asset(server, 7, 'lib.zip', data)]}])
    serve_manifest(server, LIVE, {M: {'v1.0.0': manifest_entry(released, asset=7)}})
    serve_proxy(server, LIVE, M, {'v1.0.0': released})
    result = build(options(server, tmp_path, repository=REPO, release_assets=['*.zip']), client)
    assert result.modules == {M: ['v1.0.0']}
    assert result.new_versions == []
    assert result.sources == {'live': 1}
    assert '/assets/7' not in server.paths()
    assert f'{LIVE}/{M}/@v/list' not in server.paths()  # the manifest has the versions
    assert manifest(tmp_path)['modules'][M]['versions']['v1.0.0']['asset'] == 7


def test_mirror_without_manifest(server: FakeServer, client: Client, tmp_path: Path) -> None:
    live = make_zip(tmp_path, M, 'v1.0.0')
    serve_proxy(server, LIVE, M, {'v1.0.0': live})
    new = make_zip(tmp_path, M, 'v1.1.0')
    result = build(options(server, tmp_path, modules=M, zips=[new.path]), client)
    assert result.modules == {M: ['v1.0.0', 'v1.1.0']}
    assert result.new_versions == [f'{M}@v1.1.0']
    assert result.sources == {'live': 1, 'local': 1}


def test_mirror_off_warns_about_versions_no_source_has(
    server: FakeServer, client: Client, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    live = make_zip(tmp_path, M, 'v1.0.0')
    serve_proxy(server, LIVE, M, {'v1.0.0': live})
    new = make_zip(tmp_path, M, 'v1.1.0')
    result = build(options(server, tmp_path, zips=[new.path], mirror=False), client)
    assert result.modules == {M: ['v1.1.0']}
    assert 'the live site serves v1.0.0, which no source has anymore' in capsys.readouterr().err


def test_fallback_proxy(server: FakeServer, client: Client, tmp_path: Path) -> None:
    old = make_zip(tmp_path, M, 'v1.0.0')
    serve_proxy(server, '/fallback', M, {'v1.0.0': old})
    new = make_zip(tmp_path, M, 'v1.1.0')
    result = build(options(server, tmp_path, zips=[new.path], fallback_proxy=f'{server.url}/fallback'), client)
    assert result.modules == {M: ['v1.0.0', 'v1.1.0']}
    assert result.sources == {server.url + '/fallback': 1, 'local': 1}


def test_fallback_proxy_time_comes_from_the_info(
    server: FakeServer, client: Client, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    zips = {version: make_zip(tmp_path, M, version) for version in ('v1.0.0', 'v1.1.0')}
    serve_proxy(server, '/fallback', M, zips)
    for version, result in zips.items():
        server.route(f'/fallback/{M}/@v/{version}.zip', without_time(result.path))
    server.route(f'/fallback/{M}/@v/v1.0.0.info', '{"Version": "v1.0.0", "Time": "2023-05-06T07:08:09.123Z"}')
    server.route(f'/fallback/{M}/@v/v1.1.0.info', Reply(404))
    work_dir = tmp_path / 'missing' / 'work'
    build(options(server, tmp_path, modules=M, work_dir=str(work_dir), fallback_proxy=f'{server.url}/fallback'), client)
    assert work_dir.is_dir()
    versions = manifest(tmp_path)['modules'][M]['versions']
    assert versions['v1.0.0']['time'] == '2023-05-06T07:08:09Z'
    assert versions['v1.1.0']['time'] == '1980-01-01T00:00:00Z'
    captured = capsys.readouterr()
    assert f'{M}@v1.1.0: could not read the time from {server.url}/fallback/{M}/@v/v1.1.0.info' in (
        captured.out + captured.err
    )


def test_conflict_with_the_live_site(server: FakeServer, client: Client, tmp_path: Path) -> None:
    published = make_zip(tmp_path, M, 'v1.0.0', name='published')
    changed = make_zip(tmp_path, M, 'v1.0.0', {'lib.go': 'package lib // changed\n'}, name='changed')
    serve_manifest(server, LIVE, {M: {'v1.0.0': manifest_entry(published)}})
    with pytest.raises(ConflictError, match='must never change'):
        build(options(server, tmp_path, zips=[changed.path]), client)


def test_local_zip_differing_from_release_asset(server: FakeServer, client: Client, tmp_path: Path) -> None:
    local = make_zip(tmp_path, M, 'v1.0.0', name='local')
    released = make_zip(tmp_path, M, 'v1.0.0', {'lib.go': 'package lib // other\n'}, name='released')
    data = Path(released.path).read_bytes()
    serve_releases(
        server, REPO, [{'tag': 'v1.0.0', 'assets': [asset(server, 1, 'lib.zip', data, digest='sha256:other')]}]
    )
    with pytest.raises(ConflictError, match='live site serves'):
        build(options(server, tmp_path, repository=REPO, release_assets=['*.zip'], zips=[local.path]), client)


def test_local_zip_matching_release_asset(server: FakeServer, client: Client, tmp_path: Path) -> None:
    local = make_zip(tmp_path, M, 'v1.0.0')
    data = Path(local.path).read_bytes()
    serve_releases(
        server,
        REPO,
        [
            {'tag': 'v1.0.0', 'assets': [asset(server, 1, 'a.zip', data, digest='sha256:stale')]},
            {'tag': 'v0.9.0', 'assets': [asset(server, 2, 'b.zip', data, digest=None)]},
        ],
    )
    result = build(options(server, tmp_path, repository=REPO, release_assets=['*.zip'], zips=[local.path]), client)
    assert result.sources == {'local': 1}
    # the assets with a different digest or none were downloaded and compared
    assert '/assets/1' in server.paths()
    assert '/assets/2' in server.paths()


def test_local_zip_differing_from_release_asset_without_digest(
    server: FakeServer, client: Client, tmp_path: Path
) -> None:
    local = make_zip(tmp_path, M, 'v1.0.0', name='local')
    released = make_zip(tmp_path, M, 'v1.0.0', {'lib.go': 'package lib // other\n'}, name='released')
    data = Path(released.path).read_bytes()
    serve_releases(server, REPO, [{'tag': 'v1.0.0', 'assets': [asset(server, 1, 'lib.zip', data, digest=None)]}])
    with pytest.raises(ConflictError):
        build(options(server, tmp_path, repository=REPO, release_assets=['*.zip'], zips=[local.path]), client)


def test_longest_rule_wins(server: FakeServer, client: Client, tmp_path: Path) -> None:
    generated = make_zip(tmp_path, f'{M}/generated', 'v1.0.0')
    other = make_zip(tmp_path, f'{M}/other', 'v1.0.0')
    modules = f'{M} git https://github.com/owner/lib\n{M}/generated proxy'
    result = build(options(server, tmp_path, modules=modules, zips=[generated.path, other.path]), client)
    assert result.modules == {f'{M}/generated': ['v1.0.0'], M: []}
    out = tmp_path / 'out'
    assert (out / 'go' / M / 'generated' / '@v' / 'v1.0.0.zip').exists()
    assert f'{M}/generated mod {server.url}/site/go' in (out / 'lib' / 'generated' / 'index.html').read_text()
    assert 'git https://github.com/owner/lib' in (out / 'lib' / 'index.html').read_text()


def test_retention(server: FakeServer, client: Client, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    versions = ['v0.1.0', 'v0.2.0', 'v1.0.0', 'v1.1.0', 'v1.2.0']
    zips = {version: make_zip(tmp_path, M, version) for version in versions}
    serve_proxy(server, LIVE, M, {version: zips[version] for version in versions[:-1]})
    result = build(options(server, tmp_path, keep=2, zips=[zips['v1.2.0'].path]), client)
    assert result.modules == {M: ['v0.2.0', 'v1.1.0', 'v1.2.0']}
    assert result.new_versions == [f'{M}@v1.2.0']
    assert 'not serving v0.1.0, v1.0.0 anymore, keeping the newest 2' in capsys.readouterr().err


def test_release_asset_conflicting_with_the_live_site(server: FakeServer, client: Client, tmp_path: Path) -> None:
    published = make_zip(tmp_path, M, 'v1.0.0', name='published')
    changed = make_zip(tmp_path, M, 'v1.0.0', {'lib.go': 'package lib // changed\n'}, name='changed')
    data = Path(changed.path).read_bytes()
    serve_releases(server, REPO, [{'tag': 'v1.0.0', 'assets': [asset(server, 7, 'lib.zip', data)]}])
    serve_manifest(server, LIVE, {M: {'v1.0.0': manifest_entry(published, asset=7)}})
    with pytest.raises(ConflictError, match='must never change'):
        build(options(server, tmp_path, repository=REPO, release_assets=['*.zip'], mirror=False), client)


def test_select_versions() -> None:
    versions = ['v1.0.0', 'v0.1.0', 'v1.1.0-rc.1', 'v1.1.0', 'v0.2.0']
    assert select_versions(versions, 0, set()) == ['v0.1.0', 'v0.2.0', 'v1.0.0', 'v1.1.0-rc.1', 'v1.1.0']
    assert select_versions(versions, 1, set()) == ['v0.2.0', 'v1.1.0']
    assert select_versions(versions, 1, {'v0.1.0'}) == ['v0.1.0', 'v0.2.0', 'v1.1.0']


def test_fetch_failures_become_warnings(
    server: FakeServer, client: Client, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    released = make_zip(tmp_path, M, 'v1.0.0')
    data = Path(released.path).read_bytes()
    serve_releases(server, REPO, [{'tag': 'v1.0.0', 'assets': [asset(server, 1, 'lib.zip', data)]}])

    def probe_only(request: Request) -> Reply:
        return Reply(body=data) if 'Range' in request.headers else Reply(500, b'broken')

    server.route('/assets/1', probe_only)
    kept = make_zip(tmp_path, M, 'v0.9.0')
    result = build(
        options(server, tmp_path, repository=REPO, release_assets=['*.zip'], zips=[kept.path]),
        client,
    )
    assert result.modules == {M: ['v0.9.0']}
    assert 'no source has a valid zip' in capsys.readouterr().err


def test_modules_without_versions_and_ignored_modules(
    server: FakeServer, client: Client, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lib = make_zip(tmp_path, M, 'v1.0.0')
    vanity = make_zip(tmp_path, '127.0.0.1/site/vanity', 'v1.0.0')
    vanity_two = make_zip(tmp_path, '127.0.0.1/site/vanity', 'v1.1.0')
    stray = make_zip(tmp_path, '127.0.0.1/site/stray', 'v1.0.0')
    stray_two = make_zip(tmp_path, '127.0.0.1/site/stray', 'v1.1.0')
    modules = '\n'.join(
        [
            M,
            '127.0.0.1/site/empty',
            '127.0.0.1/site/vanity git https://github.com/owner/vanity',
            '127.0.0.1/site/ext mod https://proxy.example.com',
        ]
    )
    result = build(
        options(
            server, tmp_path, modules=modules, zips=[lib.path, vanity.path, vanity_two.path, stray.path, stray_two.path]
        ),
        client,
    )
    assert result.modules == {
        M: ['v1.0.0'],
        '127.0.0.1/site/vanity': [],
        '127.0.0.1/site/ext': [],
    }
    captured = capsys.readouterr()
    assert 'No versions found for 127.0.0.1/site/empty' in captured.err
    assert captured.err.count('127.0.0.1/site/vanity is served by') == 1
    assert captured.err.count('Ignoring 127.0.0.1/site/stray') == 1
    page = (tmp_path / 'out' / 'vanity' / 'index.html').read_text()
    assert 'git https://github.com/owner/vanity' in page
    assert 'mod https://proxy.example.com' in (tmp_path / 'out' / 'ext' / 'index.html').read_text()
    text = summary(result)
    assert '| `127.0.0.1/site/vanity` | 0 |  |  |' in text


def test_live_modules_outside_the_rules_are_ignored(server: FakeServer, client: Client, tmp_path: Path) -> None:
    lib = make_zip(tmp_path, M, 'v1.0.0')
    other = make_zip(tmp_path, '127.0.0.1/site/other', 'v1.0.0')
    serve_manifest(server, LIVE, {'127.0.0.1/site/other': {'v1.0.0': manifest_entry(other)}})
    result = build(options(server, tmp_path, modules=M, zips=[lib.path]), client)
    assert result.modules == {M: ['v1.0.0']}


def sumdb_options(server: FakeServer, tmp_path: Path, **overrides: Any) -> BuildOptions:
    return options(
        server, tmp_path, verify_sumdb=True, proxy=f'{server.url}/proxy', sumdb=f'{server.url}/sumdb', **overrides
    )


def test_verify_sumdb_match(server: FakeServer, client: Client, tmp_path: Path) -> None:
    zip_file = make_zip(tmp_path, M, 'v1.0.0')
    unknown = make_zip(tmp_path, M, 'v1.1.0')
    serve_cached(server, '/proxy', M, 'v1.0.0')
    serve_sumdb(server, '/sumdb', M, 'v1.0.0', zip_file.h1, zip_file.mod_h1)
    build(sumdb_options(server, tmp_path, zips=[zip_file.path, unknown.path]), client)
    versions = manifest(tmp_path)['modules'][M]['versions']
    assert versions['v1.0.0']['sumdb'] is True
    assert 'sumdb' not in versions['v1.1.0']
    # a version the proxy hasn't cached is not looked up, which would make the database fetch it
    assert f'/sumdb/lookup/{M}@v1.1.0' not in server.paths()


def test_verify_sumdb_mismatch(server: FakeServer, client: Client, tmp_path: Path) -> None:
    zip_file = make_zip(tmp_path, M, 'v1.0.0')
    serve_cached(server, '/proxy', M, 'v1.0.0')
    serve_sumdb(server, '/sumdb', M, 'v1.0.0', 'h1:other=', zip_file.mod_h1)
    with pytest.raises(ValueError, match='does not match the checksum database'):
        build(sumdb_options(server, tmp_path, zips=[zip_file.path]), client)


def test_verify_sumdb_errors_are_warnings(
    server: FakeServer, client: Client, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    zip_file = make_zip(tmp_path, M, 'v1.0.0')
    server.route(f'/proxy/cached-only/{M}/@v/v1.0.0.info', Reply(503))
    build(sumdb_options(server, tmp_path, zips=[zip_file.path]), client)
    assert 'Could not check' in capsys.readouterr().err


def test_verify_sumdb_trusts_the_manifest(server: FakeServer, client: Client, tmp_path: Path) -> None:
    zip_file = make_zip(tmp_path, M, 'v1.0.0')
    serve_manifest(server, LIVE, {M: {'v1.0.0': manifest_entry(zip_file, sumdb=True)}})
    serve_proxy(server, LIVE, M, {'v1.0.0': zip_file})
    build(sumdb_options(server, tmp_path), client)
    assert not [path for path in server.paths() if path.startswith(('/proxy', '/sumdb'))]
    assert manifest(tmp_path)['modules'][M]['versions']['v1.0.0']['sumdb'] is True


def test_size_budget(server: FakeServer, client: Client, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    zip_file = make_zip(tmp_path, M, 'v1.0.0')
    size = build(options(server, tmp_path, output=str(tmp_path / 'a'), zips=[zip_file.path]), client).site.size
    build(options(server, tmp_path, output=str(tmp_path / 'b'), zips=[zip_file.path], max_size=size), client)
    assert '100% of the limit' in capsys.readouterr().err
    with pytest.raises(BuildError, match='more than the limit'):
        build(options(server, tmp_path, output=str(tmp_path / 'c'), zips=[zip_file.path], max_size=size - 1), client)


def test_site_warnings_are_reported(
    server: FakeServer, client: Client, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    elsewhere = make_zip(tmp_path, 'example.com/elsewhere', 'v1.0.0')
    build(options(server, tmp_path, zips=[elsewhere.path]), client)
    assert 'has no go-import page' in capsys.readouterr().err


def test_summary_shows_the_latest_release() -> None:
    from goproxy_gh_pages.build import BuildResult
    from goproxy_gh_pages.site import SiteResult

    result = BuildResult(
        output='_site',
        base_url='https://example.com',
        proxy_url='https://example.com/go',
        modules={'example.com/lib': ['v1.0.0', 'v1.1.0-rc.1']},
        new_versions=['example.com/lib@v1.1.0-rc.1'],
        site=SiteResult('_site/go', 1, 10, 3 << 20, []),
        sources={},
    )
    text = summary(result)
    assert '| `example.com/lib` | 2 | v1.0.0 | v1.1.0-rc.1 |' in text
    assert '10 files, 3.0 MiB' in text


def test_expand_zips(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / 'a').mkdir()
    (tmp_path / 'a' / 'one.zip').write_bytes(b'')
    (tmp_path / 'a' / 'two.zip').write_bytes(b'')
    (tmp_path / 'a' / 'dir.zip').mkdir()
    one = str(tmp_path / 'a' / 'one.zip')
    assert expand_zips([str(tmp_path / '**' / '*.zip'), one]) == [one, str(tmp_path / 'a' / 'two.zip')]
    assert expand_zips([str(tmp_path / '*.none')]) == []
    assert 'matches no files' in capsys.readouterr().err
    with pytest.raises(BuildError, match='is not a file'):
        expand_zips([str(tmp_path / 'missing.zip')])
