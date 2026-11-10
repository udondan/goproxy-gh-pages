"""Writes the static site: the files of the GOPROXY protocol, go-import pages and the manifest."""

from __future__ import annotations

import html
import json
import os
import urllib.parse
from dataclasses import dataclass, field

from goproxy_gh_pages import __version__
from goproxy_gh_pages.module import ModuleError, check_path, escape, latest_version, sort_versions
from goproxy_gh_pages.modzip import link_or_copy
from goproxy_gh_pages.sources import MANIFEST, Fetched

VCS = ('git', 'hg', 'svn', 'bzr', 'fossil')


@dataclass
class Rule:
    """A line of the `modules` input: an import path prefix and how it is served"""

    prefix: str
    mode: str  # 'proxy', 'mod' or a version control system
    url: str | None = None
    subdir: str | None = None

    def covers(self, module: str) -> bool:
        return module == self.prefix or module.startswith(self.prefix + '/')

    @property
    def go_import(self) -> str:
        fields = [self.prefix, self.mode, self.url or '']
        if self.subdir:
            fields.append(self.subdir)
        return ' '.join(fields)


def parse_rules(text: str) -> list[Rule]:
    """Parses the `modules` input. Each line is a module path served by this site, optionally
    followed by `proxy`, or the content of a go-import meta tag: `<prefix> <vcs> <repo url>
    [<subdir>]` for an import path backed by a repository, `<prefix> mod <proxy url>` for a module
    served by another proxy. `#` starts a comment."""
    rules: list[Rule] = []
    seen: set[str] = set()
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split(' #', 1)[0].strip()
        if not line or line.startswith('#'):
            continue
        fields = line.split()
        prefix = fields[0]
        try:
            check_path(prefix)
        except ModuleError as error:
            raise ModuleError(f'modules, line {number}: {error}') from None
        if prefix in seen:
            raise ModuleError(f'modules, line {number}: {prefix} is listed twice')
        seen.add(prefix)
        if len(fields) == 1 or fields[1:] == ['proxy']:
            rules.append(Rule(prefix, 'proxy'))
            continue
        mode = fields[1]
        if mode not in (*VCS, 'mod') or len(fields) not in (3, 4) or (mode == 'mod' and len(fields) == 4):
            raise ModuleError(
                f'modules, line {number}: expected "<module>", "<prefix> <vcs> <repo url> [<subdir>]" '
                f'or "<prefix> mod <proxy url>", got {line!r}'
            )
        url = fields[2]
        if urllib.parse.urlsplit(url).scheme not in ('https', 'http', 'ssh', 'git', 'svn', 'bzr', 'hg'):
            raise ModuleError(f'modules, line {number}: {url!r} is not a URL')
        rules.append(Rule(prefix, mode, url.rstrip('/'), fields[3].strip('/') if len(fields) == 4 else None))
    return rules


def site_path(base_url: str) -> str:
    """The import path prefix that maps to the base URL: host and path, like example.com/repo"""
    parts = urllib.parse.urlsplit(base_url)
    return ((parts.hostname or '') + parts.path).rstrip('/')


def page_dir(site: str, import_path: str) -> str | None:
    """The directory of the page for an import path, relative to the site, or None if the site
    can't serve pages for it"""
    if import_path == site:
        return ''
    if import_path.startswith(site + '/'):
        return import_path[len(site) + 1 :]
    return None


def _go_source(rule: Rule) -> str | None:
    """A go-source meta tag for repositories on GitHub, which links pkg.go.dev to the source"""
    if rule.mode != 'git' or not rule.url:
        return None
    parts = urllib.parse.urlsplit(rule.url)
    if parts.hostname != 'github.com':
        return None
    home = rule.url[:-4] if rule.url.endswith('.git') else rule.url
    sub = f'/{rule.subdir}' if rule.subdir else ''
    return f'{rule.prefix} {home} {home}/tree/HEAD{sub}{{/dir}} {home}/blob/HEAD{sub}{{/dir}}/{{file}}#L{{line}}'


def render_page(import_path: str, rules: list[Rule], home_url: str | None) -> str:
    metas = []
    for rule in rules:
        metas.append(f'    <meta name="go-import" content="{html.escape(rule.go_import)}">')
        source = _go_source(rule)
        if source:
            metas.append(f'    <meta name="go-source" content="{html.escape(source)}">')
    links = []
    if import_path:
        links.append(f'<a href="https://pkg.go.dev/{html.escape(import_path)}">Documentation</a>')
    if home_url:
        links.append(f'<a href="{html.escape(home_url)}">Home</a>')
    title = html.escape(import_path or 'Go modules')
    body = f'    <p><code>go get {html.escape(import_path)}</code></p>\n' if import_path else ''
    if links:
        body += f'    <p>{" · ".join(links)}</p>\n'
    return (
        '<!doctype html>\n<html lang="en">\n  <head>\n    <meta charset="utf-8">\n'
        + '\n'.join(metas)
        + ('\n' if metas else '')
        + f'    <title>{title}</title>\n  </head>\n  <body>\n{body}  </body>\n</html>\n'
    )


def render_redirect(url: str) -> str:
    target = html.escape(url)
    return (
        '<!doctype html>\n<html lang="en">\n  <head>\n    <meta charset="utf-8">\n'
        f'    <meta http-equiv="refresh" content="0; url={target}">\n'
        f'    <link rel="canonical" href="{target}">\n    <title>Redirecting</title>\n  </head>\n'
        f'  <body>\n    <p><a href="{target}">{target}</a></p>\n  </body>\n</html>\n'
    )


@dataclass
class ServedModule:
    module: str
    versions: dict[str, Fetched]
    packages: set[str] = field(default_factory=set)

    @property
    def latest(self) -> str | None:
        return latest_version(list(self.versions))


@dataclass
class SiteResult:
    proxy_dir: str
    pages: int
    files: int
    size: int
    warnings: list[str]


def _write(path: str, data: str | bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if isinstance(data, str):
        data = data.encode('utf-8')
    with open(path, 'wb') as file:
        file.write(data)


def _info(fetched: Fetched) -> bytes:
    return json.dumps({'Version': fetched.version, 'Time': fetched.time}, separators=(',', ':')).encode() + b'\n'


def write_site(
    output: str,
    proxy_path: str,
    base_url: str,
    proxy_url: str,
    served: list[ServedModule],
    rules: list[Rule],
    home_url: str | None,
    redirect_root: bool,
    not_found_page: bool,
    sumdb_verified: dict[tuple[str, str], bool],
    asset_ids: dict[tuple[str, str], int],
    merge: bool,
) -> SiteResult:
    """Writes the site into `output`: the proxy under `proxy_path`, go-import pages under the import
    paths relative to the base URL, the manifest, and the root and 404 pages"""
    warnings: list[str] = []
    site = site_path(base_url)
    proxy_dir = os.path.join(output, *[part for part in proxy_path.split('/') if part])
    manifest: dict[str, object] = {'generator': f'goproxy-gh-pages {__version__}', 'proxy': proxy_url, 'modules': {}}
    modules: dict[str, object] = {}
    # page dir → (module path or prefix, rule); longer prefixes win, as the go command picks the
    # module with the longest path
    pages: dict[str, tuple[str, Rule, str]] = {}

    other_rules = [rule for rule in rules if rule.mode != 'proxy']

    def add_page(import_path: str, rule: Rule) -> None:
        directory = page_dir(site, import_path)
        if directory is None:
            return
        # a package under a more specific rule belongs to that rule
        for other_rule in other_rules:
            if other_rule.covers(import_path) and len(other_rule.prefix) > len(rule.prefix):
                rule = other_rule
        other = pages.get(directory)
        if other is None or len(rule.prefix) > len(other[1].prefix):
            pages[directory] = (rule.prefix, rule, import_path)

    for module in sorted(served, key=lambda served_module: served_module.module):
        if escape(module.module).split('/', 1)[0] == MANIFEST:
            raise ModuleError(f'the module path {module.module} clashes with the manifest {MANIFEST}')
        module_dir = os.path.join(proxy_dir, *escape(module.module).split('/'))
        versions_dir = os.path.join(module_dir, '@v')
        os.makedirs(versions_dir, exist_ok=True)
        ordered = sort_versions(list(module.versions))
        for version in ordered:
            fetched = module.versions[version]
            name = escape(version)
            link_or_copy(fetched.path, os.path.join(versions_dir, f'{name}.zip'))
            _write(os.path.join(versions_dir, f'{name}.mod'), fetched.go_mod)
            _write(os.path.join(versions_dir, f'{name}.info'), _info(fetched))
        _write(os.path.join(versions_dir, 'list'), ''.join(f'{version}\n' for version in ordered))
        latest = module.latest
        if latest:
            _write(os.path.join(module_dir, '@latest'), _info(module.versions[latest]))
        modules[module.module] = {
            'mode': 'proxy',
            'latest': latest,
            'versions': {
                version: {
                    'time': fetched.time,
                    'h1': fetched.h1,
                    'mod_h1': fetched.mod_h1,
                    'sha256': fetched.sha256,
                    'size': fetched.size,
                    **({'asset': asset_ids[(module.module, version)]} if (module.module, version) in asset_ids else {}),
                    **({'sumdb': True} if sumdb_verified.get((module.module, version)) else {}),
                }
                for version, fetched in ((version, module.versions[version]) for version in ordered)
            },
        }
        rule = Rule(module.module, 'mod', proxy_url)
        if page_dir(site, module.module) is None:
            warnings.append(
                f'{module.module} is not under {site}, so the site has no go-import page for it; '
                f'use it with GOPROXY={proxy_url}'
            )
        for package in sorted(module.packages | {''}):
            add_page(f'{module.module}/{package}'.rstrip('/'), rule)

    for rule in other_rules:
        if page_dir(site, rule.prefix) is None:
            raise ModuleError(f'{rule.prefix} is not under {site}, so the site can not serve its go-import page')
        add_page(rule.prefix, rule)
        modules[rule.prefix] = {'mode': rule.mode, 'url': rule.url, **({'subdir': rule.subdir} if rule.subdir else {})}

    manifest['modules'] = modules
    _write(os.path.join(proxy_dir, MANIFEST), json.dumps(manifest, indent=1, sort_keys=True) + '\n')

    for directory, (_, rule, import_path) in pages.items():
        path = os.path.join(output, *[part for part in directory.split('/') if part], 'index.html')
        if directory == '' and redirect_root and home_url:
            # the root is a module: keep the go-import tag, but send browsers home
            page = render_page(import_path, [rule], home_url).replace(
                '    <title>',
                f'    <meta http-equiv="refresh" content="0; url={html.escape(home_url)}">\n    <title>',
                1,
            )
            _write(path, page)
            continue
        _write(path, render_page(import_path, [rule], home_url))

    root = os.path.join(output, 'index.html')
    if '' not in pages:
        if merge and os.path.exists(root):
            pass
        elif redirect_root and home_url:
            _write(root, render_redirect(home_url))
        else:
            _write(root, _render_index(served, rules, proxy_url, home_url))

    if not_found_page:
        not_found = os.path.join(output, '404.html')
        if merge and os.path.exists(not_found):
            warnings.append(
                'the output has a 404.html already, so import paths without a page of their own may not resolve'
            )
        else:
            _write(not_found, render_page('', _not_found_rules(served, rules, proxy_url), home_url))

    _write(os.path.join(output, '.nojekyll'), b'')
    files, size = _usage(output)
    return SiteResult(proxy_dir=proxy_dir, pages=len(pages), files=files, size=size, warnings=warnings)


def _not_found_rules(served: list[ServedModule], rules: list[Rule], proxy_url: str) -> list[Rule]:
    """go-import tags for the 404 page, which GitHub Pages serves for paths without a page, like
    packages of repositories. The go command reads the tags of 404 pages, but fails if two tags
    match an import path, so a prefix of another one is left out."""
    candidates = [Rule(module.module, 'mod', proxy_url) for module in served]
    candidates += [rule for rule in rules if rule.mode != 'proxy']
    return sorted(
        (rule for rule in candidates if not any(other.prefix.startswith(rule.prefix + '/') for other in candidates)),
        key=lambda rule: rule.prefix,
    )


def _render_index(served: list[ServedModule], rules: list[Rule], proxy_url: str, home_url: str | None) -> str:
    items = []
    for module in sorted(served, key=lambda served_module: served_module.module):
        items.append(f'      <li><code>{html.escape(module.module)}</code> {html.escape(module.latest or "")}</li>')
    for rule in rules:
        if rule.mode != 'proxy':
            items.append(f'      <li><code>{html.escape(rule.prefix)}</code> ({html.escape(rule.mode)})</li>')
    home = f'    <p><a href="{html.escape(home_url)}">Home</a></p>\n' if home_url else ''
    return (
        '<!doctype html>\n<html lang="en">\n  <head>\n    <meta charset="utf-8">\n    <title>Go modules</title>\n'
        '  </head>\n  <body>\n    <h1>Go modules</h1>\n    <ul>\n'
        + '\n'.join(items)
        + f'\n    </ul>\n    <p>GOPROXY: <code>{html.escape(proxy_url)}</code></p>\n{home}  </body>\n</html>\n'
    )


def _usage(directory: str) -> tuple[int, int]:
    """Files and bytes in a directory. Hard links count once per name, as in the Pages artifact."""
    files = size = 0
    for root, _, names in os.walk(directory):
        for name in names:
            files += 1
            size += os.path.getsize(os.path.join(root, name))
    return files, size
