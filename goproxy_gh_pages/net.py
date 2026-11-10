"""A small HTTP client on urllib: retries with backoff, streaming downloads, and a GitHub token
that is only sent to the GitHub API and never along redirects."""

from __future__ import annotations

import hashlib
import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import IO, Any

from goproxy_gh_pages import __version__

USER_AGENT = f'goproxy-gh-pages/{__version__} (+https://github.com/udondan/goproxy-gh-pages)'
_TOKEN_HOSTS = {'api.github.com', 'uploads.github.com'}
_RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}
_CHUNK = 1 << 20


class HTTPError(Exception):
    def __init__(self, url: str, status: int, message: str) -> None:
        super().__init__(f'{status} {message}: {url}')
        self.url = url
        self.status = status


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)

    def text(self) -> str:
        return self.body.decode('utf-8', 'replace')


class Client:
    def __init__(
        self,
        token: str | None = None,
        retries: int = 4,
        backoff: float = 1.0,
        timeout: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        api_url: str | None = None,
    ) -> None:
        self.token = token or None
        self.retries = retries
        self.backoff = backoff
        self.timeout = timeout
        self.sleep = sleep
        self.api_url = (api_url or os.environ.get('GITHUB_API_URL') or 'https://api.github.com').rstrip('/')
        self._token_hosts = set(_TOKEN_HOSTS) | {urllib.parse.urlsplit(self.api_url).hostname or ''}

    def _request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None,
        data: bytes | IO[bytes] | None,
    ) -> urllib.request.Request:
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header('User-Agent', USER_AGENT)
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        if self.token and urllib.parse.urlsplit(url).hostname in self._token_hosts:
            # unredirected: release asset downloads redirect to storage that must not get the token
            request.add_unredirected_header('Authorization', f'Bearer {self.token}')
            request.add_header('X-GitHub-Api-Version', '2022-11-28')
        return request

    def _wait(self, attempt: int, error: urllib.error.HTTPError | None) -> None:
        delay = self.backoff * (2**attempt) * (0.5 + random.random())
        if error is not None:
            retry_after = error.headers.get('Retry-After') if error.headers else None
            if retry_after and retry_after.isdigit():
                delay = max(delay, float(retry_after))
            reset = error.headers.get('X-RateLimit-Reset') if error.headers else None
            if (
                error.code in (403, 429)
                and reset
                and reset.isdigit()
                and error.headers.get('X-RateLimit-Remaining') == '0'
            ):
                delay = max(delay, min(float(reset) - time.time() + 1, 300))
        self.sleep(delay)

    def open(
        self,
        url: str,
        method: str = 'GET',
        headers: dict[str, str] | None = None,
        data: bytes | None = None,
        ok: tuple[int, ...] = (),
        consume: Callable[[Any], Any] | None = None,
    ) -> tuple[int, dict[str, str], Any]:
        """Sends a request, with retries for network errors and transient status codes. Status
        codes in `ok` are returned instead of raised. `consume` reads the response body."""
        attempt = 0
        while True:
            try:
                with urllib.request.urlopen(
                    self._request(method, url, headers, data), timeout=self.timeout
                ) as response:
                    result = consume(response) if consume else response.read()
                    return response.status, dict(response.headers.items()), result
            except urllib.error.HTTPError as error:
                if error.code in ok:
                    body = error.read()
                    return error.code, dict(error.headers.items()) if error.headers else {}, body
                rate_limited = error.code == 403 and error.headers and error.headers.get('X-RateLimit-Remaining') == '0'
                if (error.code in _RETRY_STATUS or rate_limited) and attempt < self.retries:
                    self._wait(attempt, error)
                    attempt += 1
                    continue
                raise HTTPError(url, error.code, str(error.reason)) from None
            except (urllib.error.URLError, OSError) as error:
                # nothing listens there, so waiting doesn't help
                refused = isinstance(error, ConnectionRefusedError) or isinstance(
                    getattr(error, 'reason', None), ConnectionRefusedError
                )
                if not refused and attempt < self.retries:
                    self._wait(attempt, None)
                    attempt += 1
                    continue
                raise HTTPError(url, 0, str(getattr(error, 'reason', error))) from None

    def get(self, url: str, headers: dict[str, str] | None = None, ok: tuple[int, ...] = ()) -> Response:
        status, response_headers, body = self.open(url, headers=headers, ok=ok)
        return Response(status, response_headers, body)

    def get_json(self, url: str) -> Any:
        return self.get(url, headers={'Accept': 'application/vnd.github+json'}).json()

    def exists(self, url: str) -> Response | None:
        """GETs a URL and returns the response, or None if it is not found"""
        response = self.get(url, ok=(404, 410))
        return None if response.status in (404, 410) else response

    def download(self, url: str, path: str, headers: dict[str, str] | None = None) -> str:
        """Streams a URL into a file and returns the sha256 of the content"""
        temporary = path + '.part'

        def consume(response: IO[bytes]) -> str:
            digest = hashlib.sha256()
            with open(temporary, 'wb') as file:
                while chunk := response.read(_CHUNK):
                    digest.update(chunk)
                    file.write(chunk)
            return digest.hexdigest()

        try:
            _, _, digest = self.open(url, headers=headers, consume=consume)
            os.replace(temporary, path)
            return str(digest)
        finally:
            if os.path.exists(temporary):
                os.remove(temporary)

    def drain(self, url: str, ok: tuple[int, ...] = ()) -> int:
        """GETs a URL and discards the body, without keeping it in memory. Returns the status."""

        def consume(response: IO[bytes]) -> None:
            while response.read(_CHUNK):
                pass

        status, _, _ = self.open(url, ok=ok, consume=consume)
        return status

    def paginate(self, url: str) -> Iterator[Any]:
        """The items of a GitHub API list. Pages are fetched as the items are used, so a caller
        that stops early doesn't fetch the rest."""
        separator = '&' if '?' in url else '?'
        next_url: str | None = f'{url}{separator}per_page=100'
        while next_url:
            _, headers, body = self.open(next_url, headers={'Accept': 'application/vnd.github+json'})
            yield from json.loads(body)
            next_url = None
            for link in headers.get('Link', headers.get('link', '')).split(','):
                url_part, _, rel = link.partition(';')
                if 'rel="next"' in rel:
                    next_url = url_part.strip().strip('<>')
