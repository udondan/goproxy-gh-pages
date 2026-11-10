from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

from goproxy_gh_pages import __version__
from goproxy_gh_pages.module import ModuleError
from goproxy_gh_pages.site import (
    Rule,
    ServedModule,
    page_dir,
    parse_rules,
    render_page,
    render_redirect,
    site_path,
    write_site,
)
from tests.unit.sitehelpers import make_fetched

BASE = 'https://owner.github.io/repo'
SITE = 'owner.github.io/repo'
PROXY = f'{BASE}/go'


# rules


def test_parse_rules_all_forms() -> None:
    text = """
# a comment
owner.github.io/repo/a
owner.github.io/repo/b proxy   # served here
owner.github.io/repo/c git https://github.com/x/y/ sub/dir/
owner.github.io/repo/d hg https://hg.example.com/d
owner.github.io/repo/e mod https://proxy.example.com/
owner.github.io/repo/f svn svn://svn.example.com/f
"""
    assert parse_rules(text) == [
        Rule('owner.github.io/repo/a', 'proxy'),
        Rule('owner.github.io/repo/b', 'proxy'),
        Rule('owner.github.io/repo/c', 'git', 'https://github.com/x/y', 'sub/dir'),
        Rule('owner.github.io/repo/d', 'hg', 'https://hg.example.com/d'),
        Rule('owner.github.io/repo/e', 'mod', 'https://proxy.example.com'),
        Rule('owner.github.io/repo/f', 'svn', 'svn://svn.example.com/f'),
    ]
    assert parse_rules('') == []


@pytest.mark.parametrize(
    ('text', 'message'),
    [
        ('not a path', 'line 1'),
        ('-bad.example.com', 'line 1'),
        ('example.com/a\nexample.com/a git https://x.com/a', 'line 2: example.com/a is listed twice'),
        ('example.com/a cvs https://x.com/a', 'expected'),
        ('example.com/a git', 'expected'),
        ('example.com/a git https://x.com/a sub extra', 'expected'),
        ('example.com/a mod https://proxy.example.com sub', 'expected'),
        ('example.com/a git github.com/x/y', "'github.com/x/y' is not a URL"),
        ('example.com/a mod ftp://proxy.example.com', 'is not a URL'),
    ],
)
def test_parse_rules_errors(text: str, message: str) -> None:
    with pytest.raises(ModuleError, match=re.escape(message)):
        parse_rules(text)


def test_rule_covers_and_go_import() -> None:
    rule = Rule('example.com/a', 'git', 'https://github.com/x/y', 'sub')
    assert rule.covers('example.com/a')
    assert rule.covers('example.com/a/b')
    assert not rule.covers('example.com/ab')
    assert rule.go_import == 'example.com/a git https://github.com/x/y sub'
    assert Rule('example.com/a', 'mod', 'https://p.example.com').go_import == 'example.com/a mod https://p.example.com'
    assert Rule('example.com/a', 'proxy').go_import == 'example.com/a proxy '


def test_site_path() -> None:
    assert site_path('https://Owner.GitHub.io/repo/') == 'owner.github.io/repo'
    assert site_path('https://example.com') == 'example.com'
    assert site_path('https://example.com/a/b') == 'example.com/a/b'


def test_page_dir() -> None:
    assert page_dir(SITE, SITE) == ''
    assert page_dir(SITE, f'{SITE}/mod/sub') == 'mod/sub'
    assert page_dir(SITE, 'owner.github.io/repository') is None
    assert page_dir(SITE, 'example.com/mod') is None


# pages


def metas(page: str, name: str) -> list[str]:
    return re.findall(rf'<meta name="{name}" content="([^"]*)">', page)


def test_render_page_with_github_repository() -> None:
    rules = [Rule(f'{SITE}/x', 'git', 'https://github.com/o/x.git', 'go')]
    page = render_page(f'{SITE}/x/pkg', rules, 'https://example.com/?a=1&b=2')
    assert metas(page, 'go-import') == [f'{SITE}/x git https://github.com/o/x.git go']
    assert metas(page, 'go-source') == [
        f'{SITE}/x https://github.com/o/x https://github.com/o/x/tree/HEAD/go{{/dir}} '
        'https://github.com/o/x/blob/HEAD/go{/dir}/{file}#L{line}'
    ]
    assert f'<title>{SITE}/x/pkg</title>' in page
    assert f'<code>go get {SITE}/x/pkg</code>' in page
    assert f'<a href="https://pkg.go.dev/{SITE}/x/pkg">Documentation</a>' in page
    assert '<a href="https://example.com/?a=1&amp;b=2">Home</a>' in page


def test_render_page_go_source_only_for_github_git() -> None:
    no_subdir = render_page('a.com/x', [Rule('a.com/x', 'git', 'https://github.com/o/x')], None)
    assert metas(no_subdir, 'go-source') == [
        'a.com/x https://github.com/o/x https://github.com/o/x/tree/HEAD{/dir} '
        'https://github.com/o/x/blob/HEAD{/dir}/{file}#L{line}'
    ]
    for rule in (
        Rule('a.com/x', 'git', 'https://gitlab.com/o/x'),
        Rule('a.com/x', 'hg', 'https://github.com/o/x'),
        Rule('a.com/x', 'mod', 'https://proxy.example.com'),
        Rule('a.com/x', 'git'),
    ):
        assert metas(render_page('a.com/x', [rule], None), 'go-source') == []


def test_render_page_escapes_html() -> None:
    page = render_page('a.com/<x>', [Rule('a.com/"x"', 'mod', 'https://p.example.com/?a&b')], None)
    assert metas(page, 'go-import') == ['a.com/&quot;x&quot; mod https://p.example.com/?a&amp;b']
    assert '<title>a.com/&lt;x&gt;</title>' in page
    assert 'Home' not in page


def test_render_page_without_import_path_and_rules() -> None:
    page = render_page('', [], None)
    assert '<title>Go modules</title>' in page
    assert '<meta name=' not in page
    assert '<body>\n  </body>' in page
    assert '<p><a href="https://h.example.com">Home</a></p>' in render_page('', [], 'https://h.example.com')


def test_render_redirect() -> None:
    page = render_redirect('https://example.com/?a=1&b="2"')
    target = 'https://example.com/?a=1&amp;b=&quot;2&quot;'
    assert f'<meta http-equiv="refresh" content="0; url={target}">' in page
    assert f'<link rel="canonical" href="{target}">' in page
    assert f'<a href="{target}">{target}</a>' in page


def test_served_module_latest(tmp_path: Path) -> None:
    assert ServedModule('a.com/x', {}).latest is None
    versions = {version: make_fetched(tmp_path, 'a.com/x', version) for version in ('v1.0.0', 'v1.2.0', 'v1.10.0-rc.1')}
    assert ServedModule('a.com/x', versions).latest == 'v1.2.0'


# the site


def served(
    tmp_path: Path, module: str, versions: list[str], files: dict[str, str | bytes] | None = None
) -> ServedModule:
    fetched = {version: make_fetched(tmp_path, module, version, files) for version in versions}
    packages = {package for item in fetched.values() for package in item.packages}
    return ServedModule(module, fetched, packages)


def build_site(output: Path, modules: list[ServedModule], **overrides: Any) -> Any:
    options: dict[str, Any] = {
        'output': str(output),
        'proxy_path': 'go',
        'base_url': BASE,
        'proxy_url': PROXY,
        'served': modules,
        'rules': [],
        'home_url': 'https://github.com/owner/repo',
        'redirect_root': True,
        'not_found_page': True,
        'sumdb_verified': {},
        'asset_ids': {},
        'merge': False,
    }
    options.update(overrides)
    return write_site(**options)


def read(path: Path) -> str:
    return path.read_text(encoding='utf-8')


def test_write_site_proxy_files(tmp_path: Path) -> None:
    module = f'{SITE}/Mod'
    mod = served(
        tmp_path, module, ['v1.0.0', 'v1.1.0-RC.1', 'v1.0.1'], {'a.go': 'package a\n', 'sub/b.go': 'package b\n'}
    )
    output = tmp_path / 'site'
    result = build_site(
        output,
        [mod],
        sumdb_verified={(module, 'v1.0.0'): True, (module, 'v1.0.1'): False},
        asset_ids={(module, 'v1.0.1'): 42},
    )
    proxy = output / 'go'
    versions = proxy / 'owner.github.io' / 'repo' / '!mod' / '@v'
    assert result.proxy_dir == str(proxy)
    assert read(versions / 'list') == 'v1.0.0\nv1.0.1\nv1.1.0-RC.1\n'
    for version, name in (('v1.0.0', 'v1.0.0'), ('v1.0.1', 'v1.0.1'), ('v1.1.0-RC.1', 'v1.1.0-!r!c.1')):
        fetched = mod.versions[version]
        assert json.loads(read(versions / f'{name}.info')) == {'Version': version, 'Time': '2024-05-06T07:08:09Z'}
        assert (versions / f'{name}.mod').read_bytes() == fetched.go_mod
        assert (versions / f'{name}.zip').read_bytes() == Path(fetched.path).read_bytes()
        assert os.path.samefile(versions / f'{name}.zip', fetched.path)
    assert read(versions / 'v1.0.0.info') == '{"Version":"v1.0.0","Time":"2024-05-06T07:08:09Z"}\n'
    assert json.loads(read(versions.parent / '@latest'))['Version'] == 'v1.0.1'

    manifest = json.loads(read(proxy / 'index.json'))
    assert manifest['generator'] == f'goproxy-gh-pages {__version__}'
    assert manifest['proxy'] == PROXY
    entry = manifest['modules'][module]
    assert entry['mode'] == 'proxy'
    assert entry['latest'] == 'v1.0.1'
    assert list(entry['versions']) == ['v1.0.0', 'v1.0.1', 'v1.1.0-RC.1']
    first = mod.versions['v1.0.0']
    assert entry['versions']['v1.0.0'] == {
        'time': first.time,
        'h1': first.h1,
        'mod_h1': first.mod_h1,
        'sha256': first.sha256,
        'size': first.size,
        'sumdb': True,
    }
    assert entry['versions']['v1.0.1']['asset'] == 42
    assert 'sumdb' not in entry['versions']['v1.0.1']
    assert 'asset' not in entry['versions']['v1.1.0-RC.1']

    # pages for the module and its packages, under the unescaped import path
    for directory, import_path in (('Mod', module), ('Mod/sub', f'{module}/sub')):
        page = read(output / directory / 'index.html')
        assert metas(page, 'go-import') == [f'{module} mod {PROXY}']
        assert f'<title>{import_path}</title>' in page
    assert result.pages == 2
    assert result.warnings == []
    assert (output / '.nojekyll').read_bytes() == b''
    files = [path for path in output.rglob('*') if path.is_file()]
    assert result.files == len(files)
    assert result.size == sum(path.stat().st_size for path in files)


def test_write_site_longest_prefix_wins(tmp_path: Path) -> None:
    outer = served(tmp_path, f'{SITE}/mod', ['v1.0.0'], {'a.go': 'package a\n', 'inner/b.go': 'package b\n'})
    inner = served(tmp_path, f'{SITE}/mod/inner', ['v0.1.0'], {'c.go': 'package c\n', 'deep/d.go': 'package d\n'})
    output = tmp_path / 'site'
    rules = [Rule(f'{SITE}/mod', 'git', 'https://github.com/o/mod'), Rule(f'{SITE}/mod/inner', 'proxy')]
    result = build_site(output, [inner, outer], rules=rules)
    assert metas(read(output / 'mod' / 'index.html'), 'go-import') == [f'{SITE}/mod mod {PROXY}']
    assert metas(read(output / 'mod' / 'inner' / 'index.html'), 'go-import') == [f'{SITE}/mod/inner mod {PROXY}']
    deep = read(output / 'mod' / 'inner' / 'deep' / 'index.html')
    assert metas(deep, 'go-import') == [f'{SITE}/mod/inner mod {PROXY}']
    assert f'<title>{SITE}/mod/inner/deep</title>' in deep
    assert result.pages == 3
    # 404 page: a prefix of another prefix would make two tags match, so it is left out
    not_found = read(output / '404.html')
    assert metas(not_found, 'go-import') == [f'{SITE}/mod/inner mod {PROXY}']
    manifest = json.loads(read(output / 'go' / 'index.json'))
    assert manifest['modules'][f'{SITE}/mod'] == {'mode': 'git', 'url': 'https://github.com/o/mod'}


def test_write_site_vanity_and_external_rules(tmp_path: Path) -> None:
    output = tmp_path / 'site'
    rules = [
        Rule(f'{SITE}/vanity', 'git', 'https://github.com/o/vanity', 'go'),
        Rule(f'{SITE}/external', 'mod', 'https://proxy.example.com'),
        Rule(f'{SITE}/unused', 'proxy'),
    ]
    result = build_site(output, [], rules=rules)
    vanity = read(output / 'vanity' / 'index.html')
    assert metas(vanity, 'go-import') == [f'{SITE}/vanity git https://github.com/o/vanity go']
    assert len(metas(vanity, 'go-source')) == 1
    external = read(output / 'external' / 'index.html')
    assert metas(external, 'go-import') == [f'{SITE}/external mod https://proxy.example.com']
    assert not (output / 'unused').exists()
    manifest = json.loads(read(output / 'go' / 'index.json'))
    assert manifest['modules'] == {
        f'{SITE}/vanity': {'mode': 'git', 'url': 'https://github.com/o/vanity', 'subdir': 'go'},
        f'{SITE}/external': {'mode': 'mod', 'url': 'https://proxy.example.com'},
    }
    assert sorted(metas(read(output / '404.html'), 'go-import')) == [
        f'{SITE}/external mod https://proxy.example.com',
        f'{SITE}/vanity git https://github.com/o/vanity go',
    ]
    assert result.pages == 2


def test_write_site_rule_outside_site(tmp_path: Path) -> None:
    with pytest.raises(ModuleError, match=re.escape('example.com/x is not under owner.github.io/repo')):
        build_site(tmp_path / 'site', [], rules=[Rule('example.com/x', 'git', 'https://github.com/o/x')])


def test_write_site_module_outside_site(tmp_path: Path) -> None:
    output = tmp_path / 'site'
    result = build_site(output, [served(tmp_path, 'example.com/x', ['v1.0.0'])])
    assert result.pages == 0
    assert result.warnings == [
        f'example.com/x is not under {SITE}, so the site has no go-import page for it; use it with GOPROXY={PROXY}'
    ]
    assert (output / 'go' / 'example.com' / 'x' / '@v' / 'v1.0.0.zip').is_file()
    assert not (output / 'example.com').exists()


def test_write_site_manifest_clash(tmp_path: Path) -> None:
    module = ServedModule('index.json/x', {})
    with pytest.raises(ModuleError, match='clashes with the manifest'):
        build_site(tmp_path / 'site', [module])


def test_write_site_root_redirect(tmp_path: Path) -> None:
    output = tmp_path / 'site'
    build_site(output, [served(tmp_path, f'{SITE}/mod', ['v1.0.0'])])
    root = read(output / 'index.html')
    assert '<meta http-equiv="refresh" content="0; url=https://github.com/owner/repo">' in root
    assert '<meta name="go-import"' not in root


def test_write_site_root_index(tmp_path: Path) -> None:
    output = tmp_path / 'site'
    mod = served(tmp_path, f'{SITE}/mod', ['v1.0.0', 'v1.1.0'])
    rules = [Rule(f'{SITE}/vanity', 'git', 'https://github.com/o/vanity'), Rule(f'{SITE}/mod', 'proxy')]
    build_site(output, [mod], rules=rules, redirect_root=False)
    root = read(output / 'index.html')
    assert f'<li><code>{SITE}/mod</code> v1.1.0</li>' in root
    assert f'<li><code>{SITE}/vanity</code> (git)</li>' in root
    assert f'GOPROXY: <code>{PROXY}</code>' in root
    assert '<a href="https://github.com/owner/repo">Home</a>' in root
    assert 'http-equiv' not in root

    build_site(output, [mod], redirect_root=True, home_url=None)
    root = read(output / 'index.html')
    assert 'Home' not in root
    assert '<h1>Go modules</h1>' in root


def test_write_site_module_at_root(tmp_path: Path) -> None:
    output = tmp_path / 'site'
    mod = served(tmp_path, SITE, ['v1.0.0'])
    build_site(output, [mod])
    root = read(output / 'index.html')
    assert metas(root, 'go-import') == [f'{SITE} mod {PROXY}']
    assert '<meta http-equiv="refresh" content="0; url=https://github.com/owner/repo">\n    <title>' in root

    build_site(output, [mod], home_url=None)
    root = read(output / 'index.html')
    assert metas(root, 'go-import') == [f'{SITE} mod {PROXY}']
    assert 'http-equiv' not in root

    build_site(output, [mod], redirect_root=False)
    assert 'http-equiv' not in read(output / 'index.html')


def test_write_site_merge_keeps_files(tmp_path: Path) -> None:
    output = tmp_path / 'site'
    output.mkdir()
    (output / 'index.html').write_text('docs')
    (output / '404.html').write_text('custom 404')
    (output / 'docs.css').write_text('css')
    result = build_site(output, [served(tmp_path, f'{SITE}/mod', ['v1.0.0'])], merge=True)
    assert read(output / 'index.html') == 'docs'
    assert read(output / '404.html') == 'custom 404'
    assert read(output / 'docs.css') == 'css'
    assert result.warnings == [
        'the output has a 404.html already, so import paths without a page of their own may not resolve'
    ]


def test_write_site_merge_writes_missing_root_pages(tmp_path: Path) -> None:
    output = tmp_path / 'site'
    build_site(output, [served(tmp_path, f'{SITE}/mod', ['v1.0.0'])], merge=True)
    assert 'http-equiv="refresh"' in read(output / 'index.html')
    assert metas(read(output / '404.html'), 'go-import') == [f'{SITE}/mod mod {PROXY}']


def test_write_site_without_merge_overwrites_root_pages(tmp_path: Path) -> None:
    output = tmp_path / 'site'
    output.mkdir()
    (output / 'index.html').write_text('old')
    (output / '404.html').write_text('old')
    result = build_site(output, [served(tmp_path, f'{SITE}/mod', ['v1.0.0'])])
    assert read(output / 'index.html') != 'old'
    assert read(output / '404.html') != 'old'
    assert result.warnings == []


def test_write_site_without_404_page(tmp_path: Path) -> None:
    output = tmp_path / 'site'
    build_site(output, [served(tmp_path, f'{SITE}/mod', ['v1.0.0'])], not_found_page=False)
    assert not (output / '404.html').exists()


def test_write_site_nested_proxy_path_and_rebuild(tmp_path: Path) -> None:
    output = tmp_path / 'site'
    mod = served(tmp_path, f'{SITE}/mod', ['v1.0.0'])
    first = build_site(output, [mod], proxy_path='/a/b/')
    zip_path = output / 'a' / 'b' / 'owner.github.io' / 'repo' / 'mod' / '@v' / 'v1.0.0.zip'
    assert first.proxy_dir == str(output / 'a' / 'b')
    assert zip_path.is_file()
    # writing again over the same output replaces the files
    second = build_site(output, [mod], proxy_path='/a/b/')
    assert zip_path.read_bytes() == Path(mod.versions['v1.0.0'].path).read_bytes()
    assert (second.files, second.size) == (first.files, first.size)


def test_write_site_module_without_versions(tmp_path: Path) -> None:
    output = tmp_path / 'site'
    build_site(output, [ServedModule(f'{SITE}/empty', {})])
    versions = output / 'go' / 'owner.github.io' / 'repo' / 'empty' / '@v'
    assert read(versions / 'list') == ''
    assert not (versions.parent / '@latest').exists()
    manifest = json.loads(read(output / 'go' / 'index.json'))
    assert manifest['modules'][f'{SITE}/empty'] == {'mode': 'proxy', 'latest': None, 'versions': {}}
