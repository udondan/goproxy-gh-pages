from __future__ import annotations

from goproxy_gh_pages.net import Client
from tests.unit.conftest import FakeServer, Reply


def test_fake_server_serves_routes_and_ranges(server: FakeServer, client: Client) -> None:
    server.route('/a', b'0123456789')
    server.route('/b', Reply(500))
    assert client.get(f'{server.url}/a').body == b'0123456789'
    status, headers, body = client.open(f'{server.url}/a', headers={'Range': 'bytes=-3'})
    assert (status, body) == (206, b'789')
    assert headers['Content-Range'] == 'bytes 7-9/10'
    assert client.exists(f'{server.url}/missing') is None
    assert client.get(f'{server.url}/b', ok=(500,)).status == 500
