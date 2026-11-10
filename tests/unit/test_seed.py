from __future__ import annotations

import json
import time
import urllib.parse
from collections.abc import Callable

import pytest

from goproxy_gh_pages.module import ModuleError, ModuleVersion
from goproxy_gh_pages.net import Client
from goproxy_gh_pages.seed import (
    Seeder,
    WaitResult,
    cached,
    parse_targets,
    parse_versions,
    sumdb_lookup,
    summary,
    verify_sumdb,
)
from tests.unit.conftest import FakeServer, Reply, Request
from tests.unit.proxyfakes import serve_cached, serve_sumdb

M = 'example.com/lib'
V1 = ModuleVersion(M, 'v1.0.0')
V2 = ModuleVersion(M, 'v1.1.0')
WALL = 1_700_000_900.0
SINCE = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(WALL - 900))


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture(autouse=True)
def _no_jitter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('goproxy_gh_pages.seed.random.random', lambda: 0.5)


@pytest.fixture
def clock() -> Clock:
    return Clock()


def seeder(server: FakeServer, client: Client, clock: Clock, **overrides: object) -> Seeder:
    values: dict[str, object] = {
        'proxy': f'{server.url}/proxy',
        'sumdb': f'{server.url}/sumdb',
        'index': f'{server.url}/index',
        'pkgsite': f'{server.url}/pkgsite',
        'interval': 10.0,
        'timeout': 100.0,
        'clock': clock,
        'sleep': clock.sleep,
        'wall_clock': lambda: WALL,
    }
    values.update(overrides)
    return Seeder(client, **values)  # type: ignore[arg-type]


def after(polls: int, reply: Reply) -> Callable[[Request], Reply]:
    """A route that is not found for the first `polls` requests"""
    count = [0]

    def handler(_: Request) -> Reply:
        count[0] += 1
        return reply if count[0] > polls else Reply(404)

    return handler


def test_sumdb_lookup(server: FakeServer, client: Client) -> None:
    sumdb = f'{server.url}/sumdb'
    assert sumdb_lookup(client, sumdb, V1) is None
    serve_sumdb(server, '/sumdb', M, 'v1.0.0', 'h1:zip=', 'h1:mod=')
    assert sumdb_lookup(client, sumdb, V1) == ('h1:zip=', 'h1:mod=')
    server.route(f'/sumdb/lookup/{M}@v1.1.0', f'1\n{M} v1.1.0 h1:zip=\n{M} v1.0.0 h1:x=\nother v1.1.0/go.mod h1:x=\n')
    assert sumdb_lookup(client, sumdb, V2) is None


def test_cached_and_verify_sumdb(server: FakeServer, client: Client) -> None:
    proxy, sumdb = f'{server.url}/proxy', f'{server.url}/sumdb'
    assert cached(client, proxy, V1) is False
    assert verify_sumdb(client, proxy, sumdb, V1, 'h1:zip=', 'h1:mod=') is None
    assert not [path for path in server.paths() if path.startswith('/sumdb')]
    serve_cached(server, '/proxy', M, 'v1.0.0')
    assert cached(client, proxy, V1) is True
    assert verify_sumdb(client, proxy, sumdb, V1, 'h1:zip=', 'h1:mod=') is None
    serve_sumdb(server, '/sumdb', M, 'v1.0.0', 'h1:zip=', 'h1:mod=')
    assert verify_sumdb(client, proxy, sumdb, V1, 'h1:zip=', 'h1:mod=') is True
    with pytest.raises(ValueError, match='does not match the checksum database'):
        verify_sumdb(client, proxy, sumdb, V1, 'h1:zip=', 'h1:other=')


def test_poll_backs_off_up_to_six_intervals(server: FakeServer, client: Client, clock: Clock) -> None:
    assert seeder(server, client, clock)._poll(lambda: False, 300.0) is False
    assert clock.sleeps[:7] == [10.0, 15.0, 22.5, 33.75, 50.625, 60.0, 60.0]
    assert clock.now == 300.0  # the last sleep ends at the deadline


def test_wait_for_site(server: FakeServer, client: Client, clock: Clock) -> None:
    site = f'{server.url}/site'
    server.route(f'/site/{M}/@v/v1.0.0.info', after(2, Reply(body=b'{}')))
    instance = seeder(server, client, clock)
    assert instance.wait_for_site(site, [V1, V2], deadline=50.0) == [V2]
    assert clock.sleeps[:2] == [10.0, 15.0]


def test_seed(server: FakeServer, client: Client, clock: Clock, capsys: pytest.CaptureFixture[str]) -> None:
    for version in ('v1.0.0', 'v1.1.0'):
        for suffix in ('info', 'mod', 'zip'):
            server.route(f'/proxy/{M}/@v/{version}.{suffix}', b'x')
    server.route(f'/proxy/{M}/@v/v1.1.0.zip', Reply(503))
    errors = seeder(server, client, clock).seed([V1, V2])
    assert len(errors) == 1
    assert errors[0].startswith(f'{V2}: ')
    assert f'/sumdb/lookup/{M}@v1.0.0' in server.paths()
    assert f'Seeded {V1}' in capsys.readouterr().err


def test_wait_for_proxy_and_latest(server: FakeServer, client: Client, clock: Clock) -> None:
    server.route(f'/proxy/{M}/@v/v1.0.0.info', after(1, Reply(body=b'{}')))
    lists = iter(['', 'v0.9.0\n', 'v0.9.0\nv1.0.0\n'])
    server.route(f'/proxy/{M}/@v/list', lambda _: Reply(body=next(lists).encode()))
    result = seeder(server, client, clock).wait([V1], ['proxy', 'latest'])
    assert result.complete
    assert result.available == {'proxy': {str(V1): 10.0}, 'latest': {str(V1): 25.0}}
    assert result.missing == {'proxy': [], 'latest': []}


def test_wait_for_latest_without_a_list(server: FakeServer, client: Client, clock: Clock) -> None:
    result = seeder(server, client, clock, timeout=5.0).wait([V1], ['latest'])
    assert result.missing == {'latest': [str(V1)]}
    assert not result.complete


def test_wait_for_sumdb(server: FakeServer, client: Client, clock: Clock) -> None:
    instance = seeder(server, client, clock)
    instance.expected[V1] = ('h1:zip=', 'h1:mod=')
    serve_sumdb(server, '/sumdb', M, 'v1.0.0', 'h1:zip=', 'h1:mod=')
    serve_sumdb(server, '/sumdb', M, 'v1.1.0', 'h1:any=', 'h1:mod=')
    assert instance.wait([V1, V2], ['sumdb']).complete
    instance.expected[V2] = ('h1:zip=', 'h1:mod=')
    with pytest.raises(ValueError, match='expected'):
        instance.wait([V2], ['sumdb'])


def test_wait_for_sumdb_timeout(server: FakeServer, client: Client, clock: Clock) -> None:
    result = seeder(server, client, clock, timeout=30.0).wait([V1], ['sumdb'], started=-5.0)
    assert result.missing == {'sumdb': [str(V1)]}
    assert clock.now == 25.0


def test_wait_logs_errors_and_retries(
    server: FakeServer, client: Client, clock: Clock, capsys: pytest.CaptureFixture[str]
) -> None:
    replies = iter([Reply(503), Reply(503), Reply(503), Reply(body=b'{}')])
    server.route(f'/proxy/{M}/@v/v1.0.0.info', lambda _: next(replies))
    assert seeder(server, client, clock).wait([V1], ['proxy']).complete
    assert f'proxy: {V1}:' in capsys.readouterr().err


def index_url(since: str) -> str:
    return f'/index/index?since={urllib.parse.quote(since)}&limit=2000'


def index_lines(entries: list[tuple[str, str, str]]) -> bytes:
    return ''.join(
        json.dumps({'Path': path, 'Version': version, 'Timestamp': stamp}) + '\n' for path, version, stamp in entries
    ).encode()


def test_wait_for_index(server: FakeServer, client: Client, clock: Clock, capsys: pytest.CaptureFixture[str]) -> None:
    # first poll: a full page of other modules, then a page with v1.0.0
    full = [('example.com/other', 'v1.0.0', f'2023-11-14T22:14:{n % 60:02d}.{n:06d}Z') for n in range(2000)]
    last = full[-1][2]
    server.route(index_url(SINCE), index_lines(full))
    server.route(index_url(last), index_lines([(M, 'v1.0.0', '2023-11-14T22:20:00Z')]))
    # second poll: garbage once, then v1.1.0
    replies = iter([Reply(body=b'not json\n'), Reply(body=index_lines([(M, 'v1.1.0', '2023-11-14T22:21:00Z')]))])
    server.route(index_url('2023-11-14T22:20:00Z'), lambda _: next(replies))
    result = seeder(server, client, clock).wait([V1, V2], ['index'])
    assert result.available == {'index': {str(V1): 0.0, str(V2): 25.0}}
    assert 'index: ' in capsys.readouterr().err


def test_index_reads_at_most_100_pages_per_poll(server: FakeServer, client: Client, clock: Clock) -> None:
    # a page that doesn't move the cursor must not make a poll read forever
    server.route(index_url(SINCE), index_lines([('example.com/other', 'v1.0.0', SINCE)] * 2000))
    result = seeder(server, client, clock, timeout=0.0).wait([V1], ['index'])
    assert result.missing == {'index': [str(V1)]}
    assert server.paths().count(index_url(SINCE)) == 100


def test_wait_for_pkgsite(server: FakeServer, client: Client, clock: Clock) -> None:
    server.route(f'/pkgsite/fetch/{V1}', Reply(400), method='POST')
    server.route(f'/pkgsite/fetch/{V2}', Reply(503), method='POST')
    server.route(f'/pkgsite/{V1}', after(1, Reply(body=b'docs')))
    server.route(f'/pkgsite/{V2}', b'docs')
    result = seeder(server, client, clock).wait([V1, V2], ['pkgsite'])
    assert result.complete
    posts = server.paths('POST')
    assert posts.count(f'/pkgsite/fetch/{V1}') == 1
    assert f'/pkgsite/fetch/{V2}' in posts


def test_wait_for_an_unknown_target(server: FakeServer, client: Client, clock: Clock) -> None:
    with pytest.raises(ValueError, match='unknown target'):
        seeder(server, client, clock).wait([V1], ['bogus'])


def test_parse_targets() -> None:
    assert parse_targets('proxy, latest\nproxy') == ['proxy', 'latest']
    assert parse_targets('none') == []
    assert parse_targets(' ') == []
    with pytest.raises(ValueError, match='unknown wait-for target bogus'):
        parse_targets('proxy,bogus')


def test_parse_versions() -> None:
    assert parse_versions('') == []
    assert parse_versions(f'{V1}\n{V2}\n{V1}') == [V1, V2]
    assert parse_versions(f'{V1}, {V2}') == [V1, V2]
    assert parse_versions(json.dumps([str(V2), str(V1)])) == [V2, V1]
    with pytest.raises(ModuleError):
        parse_versions('example.com/lib')


def test_summary() -> None:
    result = WaitResult(available={'proxy': {str(V2): 12.4, str(V1): 3}}, missing={'proxy': ['example.com/x@v1.0.0']})
    assert summary(result).splitlines() == [
        '| Target | Version | Available after |',
        '| --- | --- | --- |',
        f'| proxy | `{V1}` | 3s |',
        f'| proxy | `{V2}` | 12s |',
        '| proxy | `example.com/x@v1.0.0` | not yet |',
    ]
