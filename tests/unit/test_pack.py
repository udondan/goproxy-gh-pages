from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from goproxy_gh_pages.modzip import ZipError, file_sha256, read_zip
from goproxy_gh_pages.net import Client
from goproxy_gh_pages.pack import DEFAULT_OUTPUT, normalize_version, output_name, pack, version_time
from tests.unit.conftest import FakeServer, Reply, Request, write_module

MODULE = 'example.com/m'


@pytest.mark.parametrize(
    ('text', 'version'),
    [('1.2.3', 'v1.2.3'), ('v1.2.3', 'v1.2.3'), (' 2.0.0-rc.1\n', 'v2.0.0-rc.1'), ('refs/tags/v1.0.0', 'v1.0.0')],
)
def test_normalize_version(text: str, version: str) -> None:
    assert normalize_version(text) == version


def test_version_time_input_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('SOURCE_DATE_EPOCH', '0')
    assert version_time('2024-01-02T03:04:05Z', str(tmp_path)) == datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert version_time('1700000000', str(tmp_path)) == datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)


def test_version_time_source_date_epoch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('SOURCE_DATE_EPOCH', ' 1600000000 ')
    assert version_time(None, str(tmp_path)) == datetime(2020, 9, 13, 12, 26, 40, tzinfo=timezone.utc)
    monkeypatch.setenv('SOURCE_DATE_EPOCH', '')
    monkeypatch.setattr('goproxy_gh_pages.pack.subprocess.run', _fail_run)
    before = datetime.now(timezone.utc)
    assert before <= version_time('', str(tmp_path)) <= datetime.now(timezone.utc)


def _fail_run(*args: Any, **kwargs: Any) -> Any:
    raise OSError('no git')


@pytest.mark.skipif(shutil.which('git') is None, reason='git is not installed')
def test_version_time_head_commit(tmp_path: Path) -> None:
    env = {
        **os.environ,
        'GIT_AUTHOR_NAME': 'test',
        'GIT_AUTHOR_EMAIL': 'test@example.com',
        'GIT_COMMITTER_NAME': 'test',
        'GIT_COMMITTER_EMAIL': 'test@example.com',
        'GIT_AUTHOR_DATE': '2001-02-03T04:05:06Z',
        'GIT_COMMITTER_DATE': '2002-03-04T05:06:07Z',
    }
    root = write_module(tmp_path / 'repo', MODULE)
    for command in (
        ['init', '-q'],
        ['add', '.'],
        ['-c', 'commit.gpgsign=false', 'commit', '--no-verify', '-q', '-m', 'init'],
    ):
        subprocess.run(['git', *command], cwd=root, env=env, check=True)
    (root / 'sub').mkdir()
    # the committer time, also from a subdirectory of the repository
    assert version_time(None, str(root / 'sub')) == datetime(2002, 3, 4, 5, 6, 7, tzinfo=timezone.utc)


def test_version_time_falls_back_to_now(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def empty(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout='\n', stderr='')

    def failing(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.CalledProcessError(128, 'git')

    for run in (empty, failing, _fail_run):
        monkeypatch.setattr('goproxy_gh_pages.pack.subprocess.run', run)
        before = datetime.now(timezone.utc)
        assert before <= version_time(None, str(tmp_path)) <= datetime.now(timezone.utc)


@pytest.mark.parametrize(
    ('template', 'module', 'version', 'repository', 'name'),
    [
        (DEFAULT_OUTPUT, MODULE, 'v1.2.3', 'owner/repo', 'repo-go-module-v1.2.3.zip'),
        (DEFAULT_OUTPUT, 'example.com/a/lib', 'v1.2.3', None, 'lib-go-module-v1.2.3.zip'),
        (DEFAULT_OUTPUT, 'example.com/a/lib/v2', 'v2.0.0', None, 'lib-go-module-v2.0.0.zip'),
        (DEFAULT_OUTPUT, 'gopkg.in/yaml.v3', 'v3.0.1', '', 'yaml-go-module-v3.0.1.zip'),
        (DEFAULT_OUTPUT, 'example.com/lib/v1', 'v1.0.0', 'noslash', 'v1-go-module-v1.0.0.zip'),
        (
            'dist/{module}@{version}.zip',
            'github.com/Owner/Lib/v2',
            'v2.1.0',
            None,
            'dist/github.com_!owner_!lib_v2@v2.1.0.zip',
        ),
        ('{name}/{major}/{version}.zip', 'example.com/x', 'v0.1.0-rc.1', 'o/r', 'r/v0/v0.1.0-rc.1.zip'),
    ],
)
def test_output_name(template: str, module: str, version: str, repository: str | None, name: str) -> None:
    assert output_name(template, module, version, repository) == name


def test_pack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    root = write_module(tmp_path / 'm', MODULE, {'m.go': 'package m\n', 'vendor/x/y.go': 'package y\n'})
    monkeypatch.chdir(tmp_path)
    result = pack(str(root), '1.0.0', time='2024-01-01T00:00:00Z', compression_level=1)
    assert result.upload is None
    assert result.zip.path == 'm-go-module-v1.0.0.zip'
    assert (result.zip.module, result.zip.version, result.zip.time) == (MODULE, 'v1.0.0', '2024-01-01T00:00:00Z')
    assert read_zip(str(tmp_path / 'm-go-module-v1.0.0.zip')).h1 == result.zip.h1
    output = capsys.readouterr().err
    assert f'Wrote m-go-module-v1.0.0.zip: {MODULE}@v1.0.0, 2 files' in output
    assert 'left out vendor/x/y.go: file is in vendor directory' in output


def test_pack_errors(tmp_path: Path) -> None:
    with pytest.raises(ZipError, match=r'has no go\.mod file'):
        pack(str(tmp_path), 'v1.0.0')
    root = write_module(tmp_path / 'm', MODULE)
    with pytest.raises(ValueError, match='needs the repository and a token'):
        pack(str(root), 'v1.0.0', output=str(tmp_path / 'm.zip'), time='0', release_tag='v1.0.0')


def _release(server: FakeServer, assets: list[dict[str, Any]], draft: bool = True) -> dict[str, Any]:
    return {
        'id': 1,
        'tag_name': 'v1.0.0',
        'draft': draft,
        'published_at': None,
        'upload_url': f'{server.url}/upload/1{{?name,label}}',
        'assets': assets,
    }


def test_pack_uploads_to_release(tmp_path: Path, server: FakeServer, client: Client) -> None:
    root = write_module(tmp_path / 'm', MODULE)
    other = {'id': 2, 'name': 'other.zip', 'size': 1, 'url': f'{server.url}/asset/2'}
    server.route(
        '/repos/owner/repo/releases?per_page=100',
        json.dumps([{**_release(server, []), 'id': 0, 'tag_name': 'v0.9.0'}, _release(server, [other])]),
    )
    server.route('/upload/1?name=lib-v1.0.0.zip', Reply(201, b'{}'), method='POST')
    result = pack(
        str(root),
        'v1.0.0',
        output=str(tmp_path / 'out.zip'),
        time='0',
        repository='owner/repo',
        release_tag='v1.0.0',
        asset_name='lib-{version}.zip',
        client=client,
    )
    assert result.upload == 'uploaded'
    uploads = [request for request in server.requests if request.method == 'POST']
    assert len(uploads) == 1
    assert uploads[0].body == (tmp_path / 'out.zip').read_bytes()
    assert uploads[0].headers['Content-Type'] == 'application/zip'
    assert uploads[0].headers['Authorization'] == 'Bearer test-token'


def test_pack_upload_skips_an_unchanged_asset(tmp_path: Path, server: FakeServer, client: Client) -> None:
    root = write_module(tmp_path / 'm', MODULE)
    output = tmp_path / 'repo-go-module-v1.0.0.zip'
    pack(str(root), 'v1.0.0', output=str(output), time='0')
    asset = {
        'id': 3,
        'name': output.name,
        'size': output.stat().st_size,
        'url': f'{server.url}/asset/3',
        'digest': f'sha256:{file_sha256(str(output))}',
    }
    server.route('/repos/owner/repo/releases?per_page=100', json.dumps([_release(server, [asset], draft=False)]))
    result = pack(
        str(root),
        'v1.0.0',
        output=str(tmp_path / '{name}-go-module-{version}.zip'),
        time='0',
        repository='owner/repo',
        release_tag='v1.0.0',
        client=client,
    )
    assert result.upload == 'unchanged'
    assert server.paths('POST') == []


def test_pack_upload_needs_the_release(tmp_path: Path, server: FakeServer, client: Client) -> None:
    root = write_module(tmp_path / 'm', MODULE)
    server.route('/repos/owner/repo/releases?per_page=100', '[]')

    def unexpected(request: Request) -> Reply:  # pragma: no cover
        raise AssertionError(request.path)

    server.route('/upload/1?name=x', unexpected, method='POST')
    with pytest.raises(LookupError, match=r'owner/repo has no release v1\.0\.0'):
        pack(
            str(root),
            'v1.0.0',
            output=str(tmp_path / 'x.zip'),
            time='0',
            repository='owner/repo',
            release_tag='v1.0.0',
            client=client,
        )
