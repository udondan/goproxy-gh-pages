"""GitHub Actions workflow commands and the parts of the GitHub REST API the actions use."""

from __future__ import annotations

import fnmatch
import os
import sys
import urllib.parse
import uuid
from dataclasses import dataclass
from typing import Any

from goproxy_gh_pages.net import Client, HTTPError


def log(message: str) -> None:
    """Writes to stderr, so stdout stays free for machine-readable output.
    The Actions runner reads workflow commands from both streams."""
    print(message, file=sys.stderr, flush=True)


def _escape_data(value: str) -> str:
    return value.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')


def warning(message: str) -> None:
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        log(f'::warning::{_escape_data(message)}')
    else:
        log(f'warning: {message}')


def notice(message: str) -> None:
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        log(f'::notice::{_escape_data(message)}')
    else:
        log(message)


def set_output(name: str, value: str) -> None:
    path = os.environ.get('GITHUB_OUTPUT')
    if not path:
        return
    delimiter = f'ghadelimiter_{uuid.uuid4()}'
    with open(path, 'a', encoding='utf-8') as file:
        file.write(f'{name}<<{delimiter}\n{value}\n{delimiter}\n')


def add_summary(markdown: str) -> None:
    path = os.environ.get('GITHUB_STEP_SUMMARY')
    if not path:
        return
    with open(path, 'a', encoding='utf-8') as file:
        file.write(markdown.rstrip('\n') + '\n')


def repository() -> str | None:
    return os.environ.get('GITHUB_REPOSITORY') or None


def default_base_url(client: Client, repo: str | None) -> str | None:
    """The URL of the Pages site of a repository: from the Pages API, which knows custom domains,
    or else the URL of a site on github.io"""
    if not repo:
        return None
    try:
        site = client.get_json(f'{client.api_url}/repos/{repo}/pages')
        if site.get('html_url'):
            return str(site['html_url']).rstrip('/')
    except HTTPError:
        pass
    owner, name = repo.split('/', 1)
    host = f'{owner.lower()}.github.io'
    if name.lower() == host:
        return f'https://{host}'
    return f'https://{host}/{name}'


@dataclass
class Asset:
    id: int
    name: str
    size: int
    url: str
    digest: str | None
    release: str
    published_at: str | None


@dataclass
class Release:
    id: int
    tag: str
    draft: bool
    published_at: str | None
    upload_url: str
    assets: list[Asset]


def _release(data: dict[str, Any]) -> Release:
    return Release(
        id=data['id'],
        tag=data['tag_name'],
        draft=bool(data.get('draft')),
        published_at=data.get('published_at'),
        upload_url=data.get('upload_url', '').split('{', 1)[0],
        assets=[
            Asset(
                id=asset['id'],
                name=asset['name'],
                size=asset.get('size', 0),
                url=asset['url'],
                digest=asset.get('digest'),
                release=data['tag_name'],
                published_at=data.get('published_at'),
            )
            for asset in data.get('assets', [])
        ],
    )


def list_releases(client: Client, repo: str, limit: int, include_drafts: bool) -> list[Release]:
    """The newest releases, by creation. Drafts are only listed for tokens with write access."""
    releases = []
    for data in client.paginate(f'{client.api_url}/repos/{repo}/releases'):
        if data.get('draft') and not include_drafts:
            continue
        releases.append(_release(data))
        if len(releases) >= limit:
            break
    return releases


def find_release(client: Client, repo: str, tag: str) -> Release:
    """A release by tag, including drafts, which the tag endpoint doesn't find"""
    for data in client.paginate(f'{client.api_url}/repos/{repo}/releases'):
        if data['tag_name'] == tag:
            return _release(data)
    raise LookupError(f'{repo} has no release {tag}')


def matching_assets(release: Release, patterns: list[str]) -> list[Asset]:
    return [asset for asset in release.assets if any(fnmatch.fnmatchcase(asset.name, pattern) for pattern in patterns)]


def download_asset(client: Client, asset: Asset, path: str) -> str:
    return client.download(asset.url, path, headers={'Accept': 'application/octet-stream'})


def upload_asset(client: Client, release: Release, path: str, name: str, sha256: str) -> str:
    """Uploads a file to a release, unless the release has the same file already. A different file
    of the same name is replaced, which GitHub only allows while the release is a draft."""
    for asset in release.assets:
        if asset.name != name:
            continue
        if asset.digest == f'sha256:{sha256}':
            return 'unchanged'
        if not release.draft:
            raise RuntimeError(
                f'release {release.tag} has a different {name} and is published; '
                'assets of published releases can not be replaced'
            )
        client.open(asset.url, method='DELETE')
    url = f'{release.upload_url}?name={urllib.parse.quote(name)}'
    with open(path, 'rb') as file:
        data = file.read()
    client.open(url, method='POST', headers={'Content-Type': 'application/zip'}, data=data)
    return 'uploaded'
