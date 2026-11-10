"""Benchmark of a build: many large versions of one module as release assets, served by a local
fake of the GitHub API, first into an empty site and then incrementally from the live site.

    uv run python tests/bench/bench.py [--versions 30] [--size-mb 10] [--parallelism 8]
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlsplit

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))

from goproxy_gh_pages.build import BuildOptions, BuildResult, build
from goproxy_gh_pages.modzip import create_zip, file_sha256, parse_time
from goproxy_gh_pages.net import Client

MODULE = 'example.com/bench'
REPOSITORY = 'bench/repo'


class Fake:
    """The GitHub API with releases and their assets, and the live site from a directory"""

    def __init__(self) -> None:
        self.releases: list[dict[str, Any]] = []
        self.assets: dict[int, str] = {}
        self.site_dir: str | None = None
        self.requests = 0
        self.bytes_sent = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                fake.requests += 1
                path = unquote(urlsplit(self.path).path)
                if path == f'/api/repos/{REPOSITORY}/releases':
                    self._send(json.dumps(fake.releases).encode(), 'application/json')
                elif path.startswith('/api/assets/'):
                    self._file(fake.assets.get(int(path.rsplit('/', 1)[1])))
                elif path.startswith('/site/') and fake.site_dir:
                    local = os.path.join(fake.site_dir, *path[len('/site/') :].split('/'))
                    self._file(local if os.path.isfile(local) else None)
                else:
                    self._send(b'not found', 'text/plain', 404)

            def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                fake.bytes_sent += len(body)

            def _file(self, path: str | None) -> None:
                if path is None:
                    self._send(b'not found', 'text/plain', 404)
                    return
                size = os.path.getsize(path)
                start, end, status = 0, size - 1, 200
                range_header = self.headers.get('Range', '')
                if range_header.startswith('bytes='):
                    first, _, last = range_header[6:].partition('-')
                    if first:
                        start, end = int(first), min(int(last or size - 1), size - 1)
                    else:
                        start = max(size - int(last), 0)
                    status = 206
                self.send_response(status)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Length', str(end - start + 1))
                if status == 206:
                    self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
                self.end_headers()
                with open(path, 'rb') as file:
                    file.seek(start)
                    remaining = end - start + 1
                    while remaining > 0:
                        chunk = file.read(min(remaining, 1 << 20))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                        fake.bytes_sent += len(chunk)

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f'http://127.0.0.1:{self.httpd.server_address[1]}'
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def add_release(self, tag: str, zip_path: str) -> None:
        asset_id = len(self.assets) + 1
        self.assets[asset_id] = zip_path
        self.releases.insert(
            0,
            {
                'id': asset_id,
                'tag_name': tag,
                'draft': False,
                'published_at': '2024-01-01T00:00:00Z',
                'upload_url': f'{self.url}/upload{{?name,label}}',
                'assets': [
                    {
                        'id': asset_id,
                        'name': os.path.basename(zip_path),
                        'size': os.path.getsize(zip_path),
                        'url': f'{self.url}/api/assets/{asset_id}',
                        'digest': f'sha256:{file_sha256(zip_path)}',
                    }
                ],
            },
        )


def make_zips(work: str, versions: int, size: int) -> list[tuple[str, str]]:
    """Module zips with incompressible content, the same for the same arguments"""
    zips = []
    for index in range(versions):
        version = f'v1.{index}.0'
        path = os.path.join(work, 'zips', f'bench-go-module-{version}.zip')
        zips.append((version, path))
        if os.path.exists(path):
            continue
        module_dir = os.path.join(work, 'src', version)
        os.makedirs(module_dir, exist_ok=True)
        with open(os.path.join(module_dir, 'go.mod'), 'w') as file:
            file.write(f'module {MODULE}\n\ngo 1.21\n')
        with open(os.path.join(module_dir, 'bench.go'), 'w') as file:
            file.write(f'package bench\n\nconst Version = "{version}"\n')
        data = random.Random(index).randbytes(size)
        with open(os.path.join(module_dir, 'data.bin'), 'wb') as file:
            file.write(data)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        create_zip(module_dir, version, path, parse_time('2024-01-01T00:00:00Z'), compression_level=1)
        shutil.rmtree(module_dir)
    return zips


def run(fake: Fake, work: str, name: str, parallelism: int) -> tuple[float, BuildResult]:
    output = os.path.join(work, name)
    shutil.rmtree(output, ignore_errors=True)
    os.makedirs(os.path.join(work, f'{name}-tmp'), exist_ok=True)
    client = Client(token='bench', api_url=f'{fake.url}/api')
    options = BuildOptions(
        output=output,
        base_url=f'{fake.url}/site',
        repository=REPOSITORY,
        fallback_proxy=None,
        verify_sumdb=False,
        parallelism=parallelism,
        work_dir=os.path.join(work, f'{name}-tmp'),
    )
    fake.requests = fake.bytes_sent = 0
    start = time.monotonic()
    result = build(options, client)
    return time.monotonic() - start, result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--versions', type=int, default=30)
    parser.add_argument('--size-mb', type=float, default=10)
    parser.add_argument('--parallelism', type=int, default=8)
    parser.add_argument('--work-dir', help='keeps the generated zips for the next run')
    args = parser.parse_args()

    work = args.work_dir or tempfile.mkdtemp(prefix='goproxy-bench-')
    try:
        start = time.monotonic()
        zips = make_zips(work, args.versions, int(args.size_mb * (1 << 20)))
        total = sum(os.path.getsize(path) for _, path in zips)
        print(f'## {len(zips)} zips, {total / (1 << 20):.1f} MiB ({time.monotonic() - start:.1f}s to prepare)\n')

        fake = Fake()
        for version, path in zips:
            fake.add_release(version, path)

        results = []
        print('## Cold build from release assets')
        seconds, result = run(fake, work, 'cold', args.parallelism)
        results.append(('cold', seconds, fake.requests, fake.bytes_sent, result))

        fake.site_dir = result.output
        print('\n## Incremental build from the live site')
        seconds, result = run(fake, work, 'incremental', args.parallelism)
        results.append(('incremental', seconds, fake.requests, fake.bytes_sent, result))

        print('\n| Build | Time | Requests | Downloaded | Throughput | Versions |')
        print('| --- | --- | --- | --- | --- | --- |')
        for name, seconds, requests, sent, built in results:
            mib = sent / (1 << 20)
            versions = sum(len(versions) for versions in built.modules.values())
            print(f'| {name} | {seconds:.2f}s | {requests} | {mib:.1f} MiB | {mib / seconds:.0f} MiB/s | {versions} |')
        expected = {MODULE: [version for version, _ in zips]}
        for name, _, _, _, built in results:
            if built.modules != expected:
                sys.exit(f'{name}: unexpected modules {built.modules}')
    finally:
        if not args.work_dir:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    main()
