from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from goproxy_gh_pages import github
from goproxy_gh_pages.github import Asset, Release
from goproxy_gh_pages.net import Client
from tests.unit.conftest import FakeServer, Reply


def test_log(capsys: pytest.CaptureFixture[str]) -> None:
    github.log('hello')
    assert capsys.readouterr() == ('', 'hello\n')


def test_warning_and_notice_outside_actions(capsys: pytest.CaptureFixture[str]) -> None:
    github.warning('50% done\nnext')
    github.notice('note')
    assert capsys.readouterr() == ('', 'warning: 50% done\nnext\nnote\n')


def test_warning_and_notice_in_actions(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    github.warning('50% done\r\nnext')
    github.notice('a\nb')
    assert capsys.readouterr() == ('', '::warning::50%25 done%0D%0Anext\n::notice::a%0Ab\n')


def test_set_output_without_file_does_nothing(tmp_path: Path) -> None:
    github.set_output('name', 'value')
    assert list(tmp_path.iterdir()) == []


def test_set_output_writes_multiline_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / 'output'
    output.write_text('existing=1\n')
    monkeypatch.setenv('GITHUB_OUTPUT', str(output))
    github.set_output('one', 'a\nb')
    github.set_output('two', '["x"]')
    lines = output.read_text().splitlines()
    assert lines[0] == 'existing=1'
    name, _, delimiter = lines[1].partition('<<')
    assert name == 'one'
    assert delimiter.startswith('ghadelimiter_')
    assert lines[2:5] == ['a', 'b', delimiter]
    second = lines[5].partition('<<')[2]
    assert second != delimiter
    assert lines[5:] == [f'two<<{second}', '["x"]', second]


def test_add_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    github.add_summary('ignored')
    summary = tmp_path / 'summary.md'
    monkeypatch.setenv('GITHUB_STEP_SUMMARY', str(summary))
    github.add_summary('# One\n\n')
    github.add_summary('two')
    assert summary.read_text() == '# One\ntwo\n'


def test_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    assert github.repository() is None
    monkeypatch.setenv('GITHUB_REPOSITORY', 'owner/repo')
    assert github.repository() == 'owner/repo'
    monkeypatch.setenv('GITHUB_REPOSITORY', '')
    assert github.repository() is None


def test_default_base_url_without_repository(client: Client) -> None:
    assert github.default_base_url(client, None) is None


def test_default_base_url_from_pages_api(server: FakeServer, client: Client) -> None:
    server.route('/repos/Owner/repo/pages', json.dumps({'html_url': 'https://docs.example.com/'}))
    assert github.default_base_url(client, 'Owner/repo') == 'https://docs.example.com'


def test_default_base_url_without_html_url(server: FakeServer, client: Client) -> None:
    server.route('/repos/Owner/Repo/pages', json.dumps({'html_url': None}))
    assert github.default_base_url(client, 'Owner/Repo') == 'https://owner.github.io/Repo'


def test_default_base_url_without_pages(client: Client) -> None:
    assert github.default_base_url(client, 'Owner/Repo') == 'https://owner.github.io/Repo'


def test_default_base_url_of_user_site(client: Client) -> None:
    assert github.default_base_url(client, 'Owner/Owner.GitHub.io') == 'https://owner.github.io'


def release_data(
    server: FakeServer, number: int, draft: bool = False, assets: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        'id': number,
        'tag_name': f'v1.0.{number}',
        'draft': draft,
        'published_at': None if draft else '2024-01-01T00:00:00Z',
        'upload_url': f'{server.url}/upload/{number}/assets{{?name,label}}',
        'assets': assets
        if assets is not None
        else [
            {
                'id': number * 10,
                'name': f'module-v1.0.{number}.zip',
                'size': 123,
                'url': f'{server.url}/assets/{number * 10}',
                'digest': 'sha256:abc',
            },
            {'id': number * 10 + 1, 'name': 'notes.txt', 'url': f'{server.url}/assets/{number * 10 + 1}'},
        ],
    }


def test_list_releases(server: FakeServer, client: Client) -> None:
    data = [release_data(server, 3, draft=True), release_data(server, 2), release_data(server, 1)]
    server.route('/repos/o/r/releases?per_page=100', json.dumps(data))
    releases = github.list_releases(client, 'o/r', limit=10, include_drafts=True)
    assert [release.tag for release in releases] == ['v1.0.3', 'v1.0.2', 'v1.0.1']
    draft = releases[0]
    assert draft.draft
    assert draft.published_at is None
    assert draft.upload_url == f'{server.url}/upload/3/assets'
    assert draft.assets[0] == Asset(
        30, 'module-v1.0.3.zip', 123, f'{server.url}/assets/30', 'sha256:abc', 'v1.0.3', None
    )
    assert draft.assets[1].size == 0
    assert draft.assets[1].digest is None

    published = github.list_releases(client, 'o/r', limit=10, include_drafts=False)
    assert [release.tag for release in published] == ['v1.0.2', 'v1.0.1']
    assert [release.tag for release in github.list_releases(client, 'o/r', limit=1, include_drafts=False)] == ['v1.0.2']


def test_list_releases_stops_at_the_limit(server: FakeServer, client: Client) -> None:
    data = [release_data(server, 2), release_data(server, 1)]
    next_page = {'Link': f'<{server.url}/repos/o/r/releases?page=2>; rel="next"'}
    server.route('/repos/o/r/releases?per_page=100', Reply(body=json.dumps(data).encode(), headers=next_page))
    server.route('/repos/o/r/releases?page=2', Reply(status=404))
    releases = github.list_releases(client, 'o/r', limit=2, include_drafts=False)
    assert [release.tag for release in releases] == ['v1.0.2', 'v1.0.1']
    assert server.paths() == ['/repos/o/r/releases?per_page=100']


def test_release_without_optional_fields(server: FakeServer, client: Client) -> None:
    server.route('/repos/o/r/releases?per_page=100', json.dumps([{'id': 1, 'tag_name': 'v1'}]))
    (release,) = github.list_releases(client, 'o/r', limit=5, include_drafts=False)
    assert release == Release(1, 'v1', False, None, '', [])


def test_find_release(server: FakeServer, client: Client) -> None:
    data = [release_data(server, 2, draft=True), release_data(server, 1)]
    server.route('/repos/o/r/releases?per_page=100', json.dumps(data))
    assert github.find_release(client, 'o/r', 'v1.0.2').draft
    assert github.find_release(client, 'o/r', 'v1.0.1').id == 1
    with pytest.raises(LookupError, match='o/r has no release v9'):
        github.find_release(client, 'o/r', 'v9')


def make_release(server: FakeServer, draft: bool, assets: list[Asset]) -> Release:
    return Release(1, 'v1.0.0', draft, None, f'{server.url}/upload', assets)


def make_asset(server: FakeServer, name: str, digest: str | None = None) -> Asset:
    return Asset(5, name, 10, f'{server.url}/assets/5', digest, 'v1.0.0', None)


def test_matching_assets(server: FakeServer) -> None:
    assets = [make_asset(server, name) for name in ('a.zip', 'b.ZIP', 'c.tar.gz', 'go-v1.zip')]
    release = make_release(server, False, assets)
    assert [asset.name for asset in github.matching_assets(release, ['*.zip'])] == ['a.zip', 'go-v1.zip']
    assert [asset.name for asset in github.matching_assets(release, ['go-*', '*.gz'])] == ['c.tar.gz', 'go-v1.zip']
    assert github.matching_assets(release, []) == []


def test_download_asset(server: FakeServer, client: Client, tmp_path: Path) -> None:
    server.route('/assets/5', b'zip content')
    asset = make_asset(server, 'a.zip')
    target = tmp_path / 'a.zip'
    assert github.download_asset(client, asset, str(target)) == hashlib.sha256(b'zip content').hexdigest()
    assert target.read_bytes() == b'zip content'
    assert server.requests[-1].headers['Accept'] == 'application/octet-stream'


@pytest.fixture
def upload_file(tmp_path: Path) -> tuple[str, str]:
    path = tmp_path / 'module.zip'
    path.write_bytes(b'new content')
    return str(path), hashlib.sha256(b'new content').hexdigest()


def test_upload_new_asset(server: FakeServer, client: Client, upload_file: tuple[str, str]) -> None:
    path, sha256 = upload_file
    server.route('/upload?name=my%20module.zip', Reply(201, b'{}'), method='POST')
    release = make_release(server, False, [make_asset(server, 'other.zip')])
    assert github.upload_asset(client, release, path, 'my module.zip', sha256) == 'uploaded'
    (request,) = server.requests
    assert request.method == 'POST'
    assert request.body == b'new content'
    assert request.headers['Content-Type'] == 'application/zip'


def test_upload_unchanged_asset(server: FakeServer, client: Client, upload_file: tuple[str, str]) -> None:
    path, sha256 = upload_file
    release = make_release(server, False, [make_asset(server, 'module.zip', f'sha256:{sha256}')])
    assert github.upload_asset(client, release, path, 'module.zip', sha256) == 'unchanged'
    assert server.requests == []


def test_upload_replaces_asset_of_draft(
    server: FakeServer, client: Client, upload_file: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, sha256 = upload_file
    server.route('/upload?name=module.zip', Reply(201, b'{}'), method='POST')
    # the fake server has no DELETE, so the delete is recorded on the client
    calls: list[tuple[str, str]] = []
    real_open = client.open

    def open_(url: str, method: str = 'GET', **kwargs: Any) -> tuple[int, dict[str, str], Any]:
        calls.append((method, url))
        if method == 'DELETE':
            return 204, {}, b''
        return real_open(url, method=method, **kwargs)

    monkeypatch.setattr(client, 'open', open_)
    release = make_release(server, True, [make_asset(server, 'module.zip', 'sha256:old')])
    assert github.upload_asset(client, release, path, 'module.zip', sha256) == 'uploaded'
    assert calls == [('DELETE', f'{server.url}/assets/5'), ('POST', f'{server.url}/upload?name=module.zip')]
    assert server.paths('POST') == ['/upload?name=module.zip']


def test_upload_refuses_to_replace_published_asset(
    server: FakeServer, client: Client, upload_file: tuple[str, str]
) -> None:
    path, sha256 = upload_file
    release = make_release(server, False, [make_asset(server, 'module.zip', None)])
    with pytest.raises(RuntimeError, match='published'):
        github.upload_asset(client, release, path, 'module.zip', sha256)
    assert server.requests == []
