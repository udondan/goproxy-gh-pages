from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import goproxy_gh_pages
from goproxy_gh_pages import __version__
from goproxy_gh_pages.cli import InputError, client_for, lines, main, parse_bool, parse_int, parse_size
from goproxy_gh_pages.net import Client
from goproxy_gh_pages.seed import Seeder
from tests.unit.conftest import FakeServer, Reply, write_module
from tests.unit.proxyfakes import (
    make_zip,
    manifest_entry,
    serve_cached,
    serve_manifest,
    serve_releases,
    serve_sumdb,
    without_time,
)
from tests.unit.test_seed import Clock

M = '127.0.0.1/site/lib'
REPO = 'owner/repo'


class Actions:
    """The files and variables of a GitHub Actions step"""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.output = tmp_path / 'github_output'
        self.summary = tmp_path / 'step_summary'
        self.monkeypatch = monkeypatch
        monkeypatch.setenv('GITHUB_ACTIONS', 'true')
        monkeypatch.setenv('GITHUB_OUTPUT', str(self.output))
        monkeypatch.setenv('GITHUB_STEP_SUMMARY', str(self.summary))

    def inputs(self, **values: str) -> None:
        for name, value in values.items():
            self.monkeypatch.setenv(f'INPUT_{name.upper()}', value)

    def outputs(self) -> dict[str, str]:
        result: dict[str, str] = {}
        if not self.output.exists():
            return result
        text = self.output.read_text().splitlines()
        index = 0
        while index < len(text):
            name, _, delimiter = text[index].partition('<<')
            end = text.index(delimiter, index + 1)
            result[name] = '\n'.join(text[index + 1 : end])
            index = end + 1
        return result


@pytest.fixture
def actions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Actions:
    return Actions(tmp_path, monkeypatch)


def output(capsys: pytest.CaptureFixture[str]) -> str:
    """Everything printed, whichever stream logs go to"""
    captured = capsys.readouterr()
    return captured.out + captured.err


@pytest.fixture
def fast(monkeypatch: pytest.MonkeyPatch, server: FakeServer) -> Clock:
    """Clients without retries and seeders on a fake clock, so nothing sleeps"""
    clock = Clock()

    def client(token: str | None = None) -> Client:
        return Client(token=token, retries=0, timeout=10, sleep=clock.sleep, api_url=server.url)

    def seeder(client: Client, **values: Any) -> Seeder:
        return Seeder(client, clock=clock, sleep=clock.sleep, wall_clock=lambda: 1_700_000_000.0, **values)

    monkeypatch.setattr('goproxy_gh_pages.cli.Client', client)
    monkeypatch.setattr('goproxy_gh_pages.cli.Seeder', seeder)
    monkeypatch.setattr('goproxy_gh_pages.seed.random.random', lambda: 0.5)
    return clock


def test_parse_size() -> None:
    assert parse_size('1GiB') == 1 << 30
    assert parse_size('1 GB') == 10**9
    assert parse_size('1.5k') == 1536
    assert parse_size('500') == 500
    assert parse_size('10b') == 10
    with pytest.raises(InputError, match='not a size'):
        parse_size('lots')


def test_parse_bool_int_and_lines() -> None:
    assert parse_bool('x', ' Yes ') is True
    assert parse_bool('x', '') is False
    with pytest.raises(InputError, match='x: expected true or false'):
        parse_bool('x', 'maybe')
    assert parse_int('n', ' 3 ') == 3
    with pytest.raises(InputError, match='n: expected a number'):
        parse_int('n', 'three')
    assert lines(' a, b\n\nc ') == ['a', 'b', 'c']


def test_client_for(monkeypatch: pytest.MonkeyPatch) -> None:
    assert client_for('given').token == 'given'
    monkeypatch.setenv('GH_TOKEN', 'gh')
    assert client_for(None).token == 'gh'
    monkeypatch.setenv('GITHUB_TOKEN', 'github')
    assert client_for('').token == 'github'


def test_pack(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    module_dir = write_module(tmp_path / 'mod', M)
    output = tmp_path / '{name}-{version}.zip'
    assert main(['pack', str(module_dir), '1.0.0', '-o', str(output), '--time', '2024-01-01T00:00:00Z']) == 0
    out = capsys.readouterr().out
    printed = json.loads(out[out.index('{') :])
    assert printed['zip'] == str(tmp_path / 'lib-v1.0.0.zip')
    assert printed['module'] == M
    assert printed['time'] == '2024-01-01T00:00:00Z'
    assert printed['h1'].startswith('h1:')


def test_action_pack_uploads_to_a_release(
    server: FakeServer, fast: Clock, actions: Actions, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_dir = write_module(tmp_path / 'mod', M)
    serve_releases(server, REPO, [{'tag': 'v1.0.0', 'draft': True}])
    server.route('/uploads/1?name=repo-go-module-v1.0.0.zip', Reply(201, b'{}'), method='POST')
    monkeypatch.chdir(tmp_path)
    actions.inputs(
        module_dir=str(module_dir),
        version='v1.0.0',
        time='1704067200',
        compression_level='6',
        release_tag='v1.0.0',
        repository=REPO,
        token='secret',
    )
    assert main(['action', 'pack']) == 0
    outputs = actions.outputs()
    assert outputs['zip'] == 'repo-go-module-v1.0.0.zip'
    assert outputs['version'] == 'v1.0.0'
    assert outputs['mod-h1'].startswith('h1:')
    upload = next(request for request in server.requests if request.method == 'POST')
    assert upload.headers['Authorization'] == 'Bearer secret'
    assert upload.body == (tmp_path / 'repo-go-module-v1.0.0.zip').read_bytes()


@pytest.mark.parametrize(
    ('inputs', 'message'),
    [
        ({'compression_level': '10', 'module_dir': 'x', 'version': 'v1.0.0'}, 'compression-level: expected 0 to 9'),
        ({'module_dir': '', 'version': 'v1.0.0'}, 'module-dir and version are required'),
        ({'release_tag': 'v1.0.0', 'repository': '', 'time': '0'}, 'needs the repository'),
    ],
)
def test_action_pack_errors(
    inputs: dict[str, str],
    message: str,
    actions: Actions,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    actions.inputs(**{'module_dir': str(write_module(tmp_path / 'mod', M)), 'version': 'v1.0.0', **inputs})
    assert main(['action', 'pack']) == 1
    error = capsys.readouterr().out.splitlines()[-1]
    assert error.startswith('::error::')
    assert message in error


def test_errors_outside_of_actions(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(['pack', str(tmp_path), 'v1.0.0']) == 1
    assert capsys.readouterr().err == f'error: {tmp_path} has no go.mod file\n'
    assert main(['build', '--base-url', 'https://example.com', '--modules', 'not a module path!']) == 1
    assert capsys.readouterr().err.startswith('error: modules, line 1')


def test_error_messages_are_escaped_in_actions(
    actions: Actions, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / 'modules').write_text('not a module path!\n')
    assert main(['build', '--base-url', 'https://example.com', '--modules', f'@{tmp_path / "modules"}']) == 1
    out = capsys.readouterr().out
    assert out.startswith('::error::modules, line 1')
    assert '\n' not in out.rstrip('\n')


def test_build(server: FakeServer, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    zip_file = make_zip(tmp_path, M, 'v1.0.0')
    (tmp_path / 'modules').write_text(f'{M}\n')
    argv = [
        'build',
        '-o',
        str(tmp_path / 'out'),
        '--base-url',
        f'{server.url}/site',
        '--modules',
        f'@{tmp_path / "modules"}',
        '--zip',
        str(tmp_path / 'zips' / '**' / '*.zip'),
        '--no-release-assets',
        '--fallback-proxy',
        '',
        '--no-verify-sumdb',
        '--no-404',
        '--no-redirect-root',
        '--max-size',
        '10MiB',
    ]
    assert main(argv) == 0
    out = capsys.readouterr().out
    printed = json.loads(out[out.index('{\n') :])
    assert printed == {
        'proxy-url': f'{server.url}/site/go',
        'modules': {M: ['v1.0.0']},
        'new-versions': [f'{M}@v1.0.0'],
    }
    assert not (tmp_path / 'out' / '404.html').exists()
    assert (tmp_path / 'out' / 'go' / M / '@v' / 'v1.0.0.zip').read_bytes() == Path(zip_file.path).read_bytes()


def test_action_build(
    server: FakeServer, fast: Clock, actions: Actions, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    zip_file = make_zip(tmp_path, M, 'v1.0.0')
    serve_releases(server, REPO, [])
    monkeypatch.setenv('GITHUB_REPOSITORY', REPO)
    monkeypatch.setenv('RUNNER_TEMP', str(tmp_path))
    actions.inputs(
        output=str(tmp_path / 'out'),
        base_url=f'{server.url}/site',
        zips=zip_file.path,
        fallback_proxy='',
        verify_sumdb='false',
        keep='5',
    )
    assert main(['action', 'build']) == 0
    outputs = actions.outputs()
    assert outputs['path'] == str(tmp_path / 'out')
    assert outputs['base-url'] == f'{server.url}/site'
    assert outputs['proxy-url'] == f'{server.url}/site/go'
    assert json.loads(outputs['modules']) == {M: ['v1.0.0']}
    assert json.loads(outputs['new-versions']) == [f'{M}@v1.0.0']
    assert '### Go module proxy' in actions.summary.read_text()
    assert 'https://github.com/owner/repo' in (tmp_path / 'out' / 'index.html').read_text()
    assert f'/repos/{REPO}/releases?per_page=100' in server.paths()


def seed_routes(server: FakeServer, version: str = 'v1.0.0') -> None:
    for suffix in ('info', 'mod', 'zip'):
        server.route(f'/proxy/{M}/@v/{version}.{suffix}', b'{}')


def test_action_seed(
    server: FakeServer, fast: Clock, actions: Actions, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    zip_file = make_zip(tmp_path, M, 'v1.0.0')
    serve_manifest(server, '/site/go', {M: {'v1.0.0': manifest_entry(zip_file)}})
    server.route(f'/site/go/{M}/@v/v1.0.0.info', b'{}')
    seed_routes(server)
    serve_sumdb(server, '/sumdb', M, 'v1.0.0', zip_file.h1, zip_file.mod_h1)
    actions.inputs(
        versions=json.dumps([f'{M}@v1.0.0']),
        site_url=f'{server.url}/site/go',
        wait_for='proxy,sumdb',
        wait_interval='1',
        wait_timeout='60',
        proxy=f'{server.url}/proxy',
        sumdb=f'{server.url}/sumdb',
    )
    assert main(['action', 'seed']) == 0
    assert json.loads(actions.outputs()['available']) == {'proxy': {f'{M}@v1.0.0': 0.0}, 'sumdb': {f'{M}@v1.0.0': 0.0}}
    assert f'| sumdb | `{M}@v1.0.0` | 0s |' in actions.summary.read_text()
    assert f'Seeded {M}@v1.0.0' in output(capsys)


def test_action_seed_without_versions(fast: Clock, actions: Actions, capsys: pytest.CaptureFixture[str]) -> None:
    actions.inputs(versions='[]')
    assert main(['action', 'seed']) == 0
    assert actions.outputs() == {'available': '{}'}
    assert not actions.summary.exists()
    assert 'No versions to seed' in output(capsys)


def test_action_seed_fails_on_timeout(
    server: FakeServer, fast: Clock, actions: Actions, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_routes(server)
    server.route(f'/proxy/{M}/@v/v1.0.0.info', Reply(404))
    actions.inputs(
        versions=f'{M}@v1.0.0',
        wait_timeout='30',
        proxy=f'{server.url}/proxy',
        sumdb=f'{server.url}/sumdb',
    )
    assert main(['action', 'seed']) == 1
    out = output(capsys)
    error = next(line for line in out.splitlines() if line.startswith('::error::'))
    # the seed error and the timeout are reported together
    assert f'/proxy/{M}/@v/v1.0.0.info; proxy: {M}@v1.0.0 not available after 30s' in error
    assert f'| proxy | `{M}@v1.0.0` | not yet |' in out


def test_seed_warns_when_the_site_does_not_serve_a_version(
    server: FakeServer, fast: Clock, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_routes(server, 'v1.1.0')
    server.route(f'/site/go/{M}/@v/v1.1.0.info', b'{}')
    server.route(f'/proxy/{M}/@v/v1.1.0.zip', Reply(500))
    argv = [
        'seed',
        f'{M}@v1.0.0',
        f'{M}@v1.1.0',
        '--site-url',
        f'{server.url}/site/go',
        '--wait-for',
        'none',
        '--wait-timeout',
        '20',
        '--no-fail-on-timeout',
        '--proxy',
        f'{server.url}/proxy',
        '--sumdb',
        f'{server.url}/sumdb',
    ]
    assert main(argv) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out[captured.out.rindex('{') :]) == {}
    assert f'the site does not serve {M}@v1.0.0' in captured.err
    assert f'{M}@v1.1.0: ' in captured.err
    # only the version the site serves was seeded
    assert f'/proxy/{M}/@v/v1.0.0.info' not in server.paths()


def test_wait_does_not_seed(server: FakeServer, fast: Clock, capsys: pytest.CaptureFixture[str]) -> None:
    server.route(f'/proxy/{M}/@v/v1.0.0.info', b'{}')
    argv = ['wait', f'{M}@v1.0.0', '--site-url', f'{server.url}/site/go', '--proxy', f'{server.url}/proxy']
    assert main(argv) == 0
    out = capsys.readouterr().out
    assert json.loads(out[out.index('{') :]) == {'proxy': {f'{M}@v1.0.0': 0.0}}
    assert server.paths() == ['/site/go/index.json', f'/proxy/{M}/@v/v1.0.0.info']


def test_verify(server: FakeServer, fast: Clock, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    one = make_zip(tmp_path, M, 'v1.0.0')
    two = make_zip(tmp_path, M, 'v1.1.0')
    three = make_zip(tmp_path, M, 'v1.2.0')
    broken = tmp_path / 'broken.zip'
    broken.write_bytes(b'not a zip')
    assert main(['verify', one.path]) == 0
    assert capsys.readouterr().out == f'{one.path}: {M}@v1.0.0 {one.time} {one.h1} go.mod {one.mod_h1}\n'
    timeless = tmp_path / 'timeless.zip'
    timeless.write_bytes(without_time(one.path))
    assert main(['verify', str(timeless)]) == 0
    assert capsys.readouterr().out == f'{timeless}: {M}@v1.0.0 unknown time {one.h1} go.mod {one.mod_h1}\n'
    serve_sumdb(server, '/sumdb', M, 'v1.0.0', one.h1, one.mod_h1)
    serve_sumdb(server, '/sumdb', M, 'v1.1.0', 'h1:other=', two.mod_h1)
    serve_cached(server, '/proxy', M, 'v1.0.0')
    argv = ['verify', one.path, two.path, three.path, str(broken), '--sumdb', f'{server.url}/sumdb']
    assert main(argv) == 1
    captured = capsys.readouterr()
    assert '  matches the checksum database' in captured.out
    assert '  not in the checksum database' in captured.out
    assert '  does not match the checksum database: h1:other=' in captured.err
    assert f'{broken}: ' in captured.err


def test_main_module_runs_isolated(tmp_path: Path) -> None:
    package = os.path.dirname(goproxy_gh_pages.__file__)
    # a module in the working directory must not shadow the package or the standard library
    (tmp_path / 'json.py').write_text('raise SystemExit("shadowed")\n')
    result = subprocess.run(
        [sys.executable, '-I', package, '--version'],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode, result.stdout.strip()) == (0, __version__)
