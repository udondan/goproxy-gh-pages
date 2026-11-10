"""Shared fixtures: a fake HTTP server for the GitHub API, the live site and the public proxy, and
helpers to write module directories."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from goproxy_gh_pages.net import Client


@dataclass
class Reply:
    status: int = 200
    body: bytes = b''
    headers: dict[str, str] = field(default_factory=dict)
    ranges: bool = True


@dataclass
class Request:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


Handler = Callable[[Request], Reply]
Route = Reply | Handler | bytes | str


class FakeServer:
    """Serves routes by method and path (with query). A route is a Reply, bytes or str (200), or a
    function of the request. Unknown paths are 404. Range requests on byte bodies are answered with
    206, unless the reply disables them."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Route] = {}
        self.requests: list[Request] = []
        self._lock = threading.Lock()
        server = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, format: str, *args: Any) -> None:
                pass

            def _handle(self) -> None:
                length = int(self.headers.get('Content-Length') or 0)
                body = self.rfile.read(length) if length else b''
                request = Request(self.command, self.path, dict(self.headers.items()), body)
                with server._lock:
                    server.requests.append(request)
                reply = server.reply(request)
                data = reply.body
                headers = dict(reply.headers)
                status = reply.status
                range_header = request.headers.get('Range')
                if status == 200 and range_header and reply.ranges and range_header.startswith('bytes='):
                    start_text, _, end_text = range_header[6:].partition('-')
                    if start_text:
                        start = int(start_text)
                        end = min(int(end_text), len(data) - 1) if end_text else len(data) - 1
                    else:
                        start = max(len(data) - int(end_text), 0)
                        end = len(data) - 1
                    headers['Content-Range'] = f'bytes {start}-{end}/{len(data)}'
                    data = data[start : end + 1]
                    status = 206
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                if self.command != 'HEAD':
                    self.wfile.write(data)

            do_GET = do_POST = do_HEAD = do_PUT = do_DELETE = _handle

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        self.httpd.daemon_threads = True
        self.url = f'http://127.0.0.1:{self.httpd.server_address[1]}'
        self._thread = threading.Thread(target=self.httpd.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        self._thread.start()

    def route(self, path: str, reply: Route, method: str = 'GET') -> None:
        self.routes[(method, path)] = reply

    def reply(self, request: Request) -> Reply:
        route = self.routes.get((request.method, request.path))
        if route is None and request.method == 'HEAD':
            route = self.routes.get(('GET', request.path))
        if route is None:
            return Reply(404, b'not found')
        if isinstance(route, Reply):
            return route
        if isinstance(route, bytes):
            return Reply(body=route)
        if isinstance(route, str):
            return Reply(body=route.encode())
        return route(request)

    def paths(self, method: str = 'GET') -> list[str]:
        return [request.path for request in self.requests if request.method == method]

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server() -> Iterator[FakeServer]:
    fake = FakeServer()
    yield fake
    fake.close()


@pytest.fixture
def client(server: FakeServer) -> Client:
    """A client that doesn't sleep between retries and sends the token to the fake API"""
    return Client(token='test-token', retries=2, backoff=0, timeout=10, sleep=lambda _: None, api_url=server.url)


def write_module(root: Path, module: str, files: Mapping[str, str | bytes] | None = None) -> Path:
    """Writes a module directory with a go.mod and the given files"""
    root.mkdir(parents=True, exist_ok=True)
    (root / 'go.mod').write_text(f'module {module}\n\ngo 1.21\n')
    for name, content in (files if files is not None else {'lib.go': 'package lib\n'}).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content)
    return root


@pytest.fixture(autouse=True)
def _no_actions_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Tests run like outside of GitHub Actions, unless a test sets the variables"""
    for name in list(os.environ):
        if name.startswith(('GITHUB_', 'INPUT_', 'RUNNER_')) or name == 'SOURCE_DATE_EPOCH':
            monkeypatch.delenv(name, raising=False)
