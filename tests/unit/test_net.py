from __future__ import annotations

import hashlib
import itertools
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from goproxy_gh_pages.net import USER_AGENT, Client, HTTPError, Response
from tests.unit.conftest import FakeServer, Reply, Request


def recording_client(server: FakeServer, **kwargs: Any) -> tuple[Client, list[float]]:
    delays: list[float] = []
    options: dict[str, Any] = {'retries': 3, 'backoff': 0.0, 'timeout': 10, 'api_url': server.url}
    options.update(kwargs)
    return Client(sleep=delays.append, **options), delays


def flaky(*replies: Reply) -> Any:
    """A handler that answers with the replies in order, then repeats the last one"""
    queue = list(replies)

    def handle(_: Request) -> Reply:
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return handle


def test_response_json_and_text() -> None:
    response = Response(200, {}, '{"a": "ä"}'.encode())
    assert response.json() == {'a': 'ä'}
    assert response.text() == '{"a": "ä"}'
    assert Response(200, {}, b'\xff').text() == '�'


def test_http_error_message() -> None:
    error = HTTPError('https://example.com/x', 404, 'Not Found')
    assert str(error) == '404 Not Found: https://example.com/x'
    assert (error.url, error.status) == ('https://example.com/x', 404)


def test_api_url_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('GITHUB_API_URL', 'https://ghe.example.com/api/v3/')
    client = Client(token='t')
    assert client.api_url == 'https://ghe.example.com/api/v3'
    assert 'ghe.example.com' in client._token_hosts
    assert Client().api_url == 'https://ghe.example.com/api/v3'
    monkeypatch.delenv('GITHUB_API_URL')
    assert Client().api_url == 'https://api.github.com'
    assert Client(token='').token is None


def test_retries_transient_status(server: FakeServer) -> None:
    server.route('/x', flaky(Reply(503), Reply(429), Reply(200, b'ok')))
    client, delays = recording_client(server)
    assert client.get(f'{server.url}/x').body == b'ok'
    assert len(delays) == 2
    assert len(server.requests) == 3


def test_backoff_grows_with_jitter(server: FakeServer) -> None:
    server.route('/x', Reply(500))
    client, delays = recording_client(server, backoff=1.0, retries=3)
    with pytest.raises(HTTPError) as error:
        client.get(f'{server.url}/x')
    assert error.value.status == 500
    assert len(delays) == 3
    for attempt, delay in enumerate(delays):
        assert 0.5 * 2**attempt <= delay <= 1.5 * 2**attempt


def test_retry_after_header(server: FakeServer) -> None:
    server.route('/x', flaky(Reply(503, headers={'Retry-After': '7'}), Reply(200, b'ok')))
    client, delays = recording_client(server)
    client.get(f'{server.url}/x')
    assert delays == [7.0]


def test_retry_after_date_is_ignored(server: FakeServer) -> None:
    server.route('/x', flaky(Reply(503, headers={'Retry-After': 'Wed, 21 Oct 2015 07:28:00 GMT'}), Reply(200)))
    client, delays = recording_client(server)
    client.get(f'{server.url}/x')
    assert delays == [0.0]


@pytest.mark.parametrize('status', [403, 429])
def test_rate_limit_waits_for_reset(server: FakeServer, status: int) -> None:
    reset = str(int(time.time()) + 20)
    limited = Reply(status, headers={'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': reset})
    server.route('/x', flaky(limited, Reply(200, b'ok')))
    client, delays = recording_client(server)
    assert client.get(f'{server.url}/x').body == b'ok'
    assert len(delays) == 1
    assert 15 <= delays[0] <= 22


def test_rate_limit_wait_is_capped(server: FakeServer) -> None:
    reset = str(int(time.time()) + 10_000)
    server.route(
        '/x', flaky(Reply(403, headers={'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': reset}), Reply(200))
    )
    client, delays = recording_client(server)
    client.get(f'{server.url}/x')
    assert delays == [300]


def test_rate_limit_reset_ignored_with_remaining_requests(server: FakeServer) -> None:
    reset = str(int(time.time()) + 100)
    server.route(
        '/x', flaky(Reply(429, headers={'X-RateLimit-Remaining': '5', 'X-RateLimit-Reset': reset}), Reply(200))
    )
    client, delays = recording_client(server)
    client.get(f'{server.url}/x')
    assert delays == [0.0]


def test_forbidden_without_rate_limit_is_not_retried(server: FakeServer) -> None:
    server.route('/x', Reply(403, headers={'X-RateLimit-Remaining': '10'}))
    client, delays = recording_client(server)
    with pytest.raises(HTTPError) as error:
        client.get(f'{server.url}/x')
    assert error.value.status == 403
    assert delays == []
    assert len(server.requests) == 1


def test_not_found_is_not_retried(server: FakeServer) -> None:
    client, delays = recording_client(server)
    with pytest.raises(HTTPError) as error:
        client.get(f'{server.url}/missing')
    assert error.value.status == 404
    assert delays == []


def test_ok_statuses_are_returned(server: FakeServer) -> None:
    server.route('/x', Reply(500, b'broken', {'X-Test': '1'}))
    client, delays = recording_client(server)
    status, headers, body = client.open(f'{server.url}/x', ok=(500,))
    assert (status, body, headers['X-Test']) == (500, b'broken', '1')
    assert delays == []


def _raise(error: BaseException) -> Any:
    def urlopen(*_: Any, **__: Any) -> Any:
        raise error

    return urlopen


def test_http_error_without_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    delays: list[float] = []
    client = Client(retries=1, backoff=0, sleep=delays.append)
    error = urllib.error.HTTPError('https://example.com', 503, 'unavailable', None, None)  # type: ignore[arg-type]
    monkeypatch.setattr(urllib.request, 'urlopen', _raise(error))
    with pytest.raises(HTTPError) as raised:
        client.get('https://example.com')
    assert raised.value.status == 503
    assert delays == [0.0]
    status, headers, _ = client.open('https://example.com', ok=(503,))
    assert (status, headers) == (503, {})


def test_forbidden_without_headers_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    client = Client(retries=1, backoff=0, sleep=lambda _: None)
    error = urllib.error.HTTPError('https://example.com', 403, 'forbidden', None, None)  # type: ignore[arg-type]
    monkeypatch.setattr(urllib.request, 'urlopen', _raise(error))
    with pytest.raises(HTTPError) as raised:
        client.get('https://example.com')
    assert raised.value.status == 403


def test_network_errors_are_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    delays: list[float] = []
    client = Client(retries=2, backoff=0, sleep=delays.append)
    monkeypatch.setattr(urllib.request, 'urlopen', _raise(urllib.error.URLError('name resolution failed')))
    with pytest.raises(HTTPError) as raised:
        client.get('https://example.com')
    assert raised.value.status == 0
    assert 'name resolution failed' in str(raised.value)
    assert len(delays) == 2


@pytest.mark.parametrize(
    'error',
    [TimeoutError('timed out'), ConnectionResetError('reset'), urllib.error.URLError(TimeoutError('timed out'))],
)
def test_timeouts_and_resets_are_retried(error: BaseException, monkeypatch: pytest.MonkeyPatch) -> None:
    delays: list[float] = []
    client = Client(retries=2, backoff=0, sleep=delays.append)
    monkeypatch.setattr(urllib.request, 'urlopen', _raise(error))
    with pytest.raises(HTTPError) as raised:
        client.get('https://example.com')
    assert raised.value.status == 0
    assert len(delays) == 2


@pytest.mark.parametrize(
    'error',
    [ConnectionRefusedError('refused'), urllib.error.URLError(ConnectionRefusedError(61, 'Connection refused'))],
)
def test_connection_refused_is_not_retried(error: BaseException, monkeypatch: pytest.MonkeyPatch) -> None:
    delays: list[float] = []
    client = Client(retries=3, backoff=0, sleep=delays.append)
    monkeypatch.setattr(urllib.request, 'urlopen', _raise(error))
    with pytest.raises(HTTPError) as raised:
        client.get('https://example.com')
    assert raised.value.status == 0
    assert 'efused' in str(raised.value)
    assert delays == []


def test_connection_refused_by_a_closed_port() -> None:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    delays: list[float] = []
    client = Client(retries=3, backoff=0, sleep=delays.append)
    with pytest.raises(HTTPError) as raised:
        client.get(f'http://127.0.0.1:{port}/x')
    assert raised.value.status == 0
    assert delays == []


@pytest.fixture
def truncating_server() -> Any:
    """A server that answers the first `truncated` requests with fewer bytes than announced"""
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    state = {'requests': 0, 'truncated': 1}

    def serve() -> None:
        while True:
            try:
                connection, _ = listener.accept()
            except OSError:
                return
            with connection:
                connection.recv(65536)
                state['requests'] += 1
                body = b'abc' if state['requests'] <= state['truncated'] else b'abcdefghij'
                connection.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 10\r\nConnection: close\r\n\r\n' + body)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{listener.getsockname()[1]}/x', state
    listener.close()
    thread.join(5)


def test_incomplete_response_is_retried(truncating_server: Any, tmp_path: Path) -> None:
    url, state = truncating_server
    delays: list[float] = []
    client = Client(retries=4, backoff=0, sleep=delays.append)
    assert client.get(url).body == b'abcdefghij'
    assert len(delays) == 1
    state['requests'], state['truncated'] = 0, 1
    target = tmp_path / 'file'
    client.download(url, str(target))
    assert target.read_bytes() == b'abcdefghij'


def test_incomplete_responses_fail_after_retries(truncating_server: Any) -> None:
    url, state = truncating_server
    state['truncated'] = 100
    delays: list[float] = []
    client = Client(retries=2, backoff=0, sleep=delays.append)
    with pytest.raises(HTTPError) as raised:
        client.get(url)
    assert raised.value.status == 0
    assert state['requests'] == 3
    assert len(delays) == 2


def test_default_timeout() -> None:
    assert Client().timeout == 30.0


def test_network_error_then_success(server: FakeServer, monkeypatch: pytest.MonkeyPatch) -> None:
    server.route('/x', b'ok')
    real = urllib.request.urlopen
    calls = []

    def urlopen(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionResetError('reset')
        return real(*args, **kwargs)

    monkeypatch.setattr(urllib.request, 'urlopen', urlopen)
    client, delays = recording_client(server)
    assert client.get(f'{server.url}/x').body == b'ok'
    assert len(delays) == 1


def test_user_agent_and_token_for_api_host(server: FakeServer, client: Client) -> None:
    server.route('/x', b'')
    client.get(f'{server.url}/x', headers={'Accept': 'text/plain'})
    request = server.requests[-1]
    assert request.headers['User-Agent'] == USER_AGENT
    assert request.headers['Accept'] == 'text/plain'
    assert request.headers['Authorization'] == 'Bearer test-token'
    assert {name.lower(): value for name, value in request.headers.items()}['x-github-api-version'] == '2022-11-28'


def test_token_not_sent_to_other_hosts(server: FakeServer) -> None:
    server.route('/x', b'')
    client = Client(token='secret', api_url='https://api.example.com', sleep=lambda _: None)
    client.get(f'{server.url}/x')
    assert 'Authorization' not in server.requests[-1].headers
    other = Client(token='secret', api_url=server.url.replace('127.0.0.1', 'localhost'), sleep=lambda _: None)
    other.get(f'{server.url}/x')
    assert 'Authorization' not in server.requests[-1].headers


def test_no_token_no_header(server: FakeServer) -> None:
    server.route('/x', b'')
    Client(api_url=server.url).get(f'{server.url}/x')
    assert 'Authorization' not in server.requests[-1].headers


def test_token_not_sent_along_redirects(server: FakeServer, client: Client) -> None:
    server.route('/asset', Reply(302, headers={'Location': f'{server.url}/storage'}))
    server.route('/storage', b'content')
    assert client.get(f'{server.url}/asset').body == b'content'
    first, second = server.requests
    assert first.headers['Authorization'] == 'Bearer test-token'
    assert 'Authorization' not in second.headers
    assert second.path == '/storage'


def test_get_json_sends_accept(server: FakeServer, client: Client) -> None:
    server.route('/x', json.dumps({'a': 1}))
    assert client.get_json(f'{server.url}/x') == {'a': 1}
    assert server.requests[-1].headers['Accept'] == 'application/vnd.github+json'


def test_exists(server: FakeServer, client: Client) -> None:
    server.route('/x', b'yes')
    server.route('/gone', Reply(410))
    response = client.exists(f'{server.url}/x')
    assert response is not None
    assert response.body == b'yes'
    assert client.exists(f'{server.url}/missing') is None
    assert client.exists(f'{server.url}/gone') is None


def test_download_streams_to_file(
    server: FakeServer, client: Client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr('goproxy_gh_pages.net._CHUNK', 7)
    content = bytes(range(256)) * 10
    server.route('/file', content)
    target = tmp_path / 'file.bin'
    digest = client.download(f'{server.url}/file', str(target), headers={'Accept': 'application/octet-stream'})
    assert digest == hashlib.sha256(content).hexdigest()
    assert target.read_bytes() == content
    assert not (tmp_path / 'file.bin.part').exists()
    assert server.requests[-1].headers['Accept'] == 'application/octet-stream'


def test_download_failure_leaves_no_files(server: FakeServer, client: Client, tmp_path: Path) -> None:
    target = tmp_path / 'file.bin'
    with pytest.raises(HTTPError):
        client.download(f'{server.url}/missing', str(target))
    assert list(tmp_path.iterdir()) == []


def test_download_removes_partial_file(server: FakeServer, client: Client, tmp_path: Path) -> None:
    server.route('/file', b'content')
    target = tmp_path / 'directory'
    target.mkdir()
    (target / 'keep').write_text('')
    with pytest.raises(OSError):  # noqa: PT011 the error differs between systems
        client.download(f'{server.url}/file', str(target))
    assert not (tmp_path / 'directory.part').exists()


def test_drain(server: FakeServer, client: Client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('goproxy_gh_pages.net._CHUNK', 3)
    server.route('/x', b'0123456789')
    assert client.drain(f'{server.url}/x') == 200
    assert client.drain(f'{server.url}/missing', ok=(404,)) == 404


def _page(items: list[int], next_url: str | None, extra: str = '') -> Reply:
    links = [extra] if extra else []
    if next_url:
        links.append(f'<{next_url}>; rel="next"')
    headers = {'Link': ', '.join(links)} if links else {}
    return Reply(body=json.dumps(items).encode(), headers=headers)


def test_paginate_follows_links(server: FakeServer, client: Client) -> None:
    server.route(
        '/items?per_page=100', _page([1, 2], f'{server.url}/items?page=2', f'<{server.url}/items?page=3>; rel="last"')
    )
    server.route('/items?page=2', _page([3, 4], f'{server.url}/items?page=3'))
    server.route('/items?page=3', _page([5], None, f'<{server.url}/items?page=1>; rel="first"'))
    assert list(client.paginate(f'{server.url}/items')) == [1, 2, 3, 4, 5]
    assert all(request.headers['Accept'] == 'application/vnd.github+json' for request in server.requests)


def test_paginate_fetches_pages_as_needed(server: FakeServer, client: Client) -> None:
    server.route('/items?state=all&per_page=100', _page([1, 2, 3], f'{server.url}/items?page=2'))
    server.route('/items?page=2', _page([4, 5, 6], None))
    assert list(itertools.islice(client.paginate(f'{server.url}/items?state=all'), 3)) == [1, 2, 3]
    assert server.paths() == ['/items?state=all&per_page=100']
    assert list(itertools.islice(client.paginate(f'{server.url}/items?state=all'), 4)) == [1, 2, 3, 4]


def test_paginate_lower_case_link_header(monkeypatch: pytest.MonkeyPatch) -> None:
    client = Client(sleep=lambda _: None)
    pages = {
        'https://example.com/x?per_page=100': ({'link': '<https://example.com/x?page=2>; rel="next"'}, b'[1]'),
        'https://example.com/x?page=2': ({}, b'[2]'),
    }

    def open_(url: str, **_: Any) -> tuple[int, dict[str, str], bytes]:
        headers, body = pages[url]
        return 200, headers, body

    monkeypatch.setattr(client, 'open', open_)
    assert list(client.paginate('https://example.com/x')) == [1, 2]
