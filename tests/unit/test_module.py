from __future__ import annotations

import pytest

from goproxy_gh_pages.module import (
    ModuleError,
    ModuleVersion,
    Version,
    check_file_path,
    check_path,
    check_version,
    escape,
    latest_version,
    module_path_of_go_mod,
    sort_versions,
    split_path_version,
    unescape,
)


@pytest.mark.parametrize(
    'path',
    [
        'example.com/m',
        'example.com',
        'github.com/Owner/Repo',
        'example.com/a-b/c_d/e~f/g.h',
        'example.com/m/v2',
        'example.com/m/v10',
        'example.com/m/v1x',
        'example.com/v',
        'example.com/x.v2',
        'xn--e1a.example/m',
        'example.com/~a',
        'gopkg.in/yaml.v2',
        'gopkg.in/check.v1',
        'gopkg.in/x.v0',
        'gopkg.in/x.v3-unstable',
        'gopkg.in/user/pkg.v1',
    ],
)
def test_check_path_valid(path: str) -> None:
    check_path(path)


@pytest.mark.parametrize(
    ('path', 'reason'),
    [
        ('', 'empty string'),
        ('-example.com/m', 'leading dash'),
        ('example.com//m', 'double slash'),
        ('example.com/m/', 'trailing slash'),
        ('/example.com', 'empty path element'),
        ('example.com/./m', "invalid path element '.'"),
        ('example.com/../m', "invalid path element '..'"),
        ('example.com/.m', 'leading dot'),
        ('example.com/m.', 'trailing dot'),
        ('example.com/a b', 'invalid char'),
        ('example.com/a@b', 'invalid char'),
        ('example.com/é', 'invalid char'),
        ('example.com/con', 'disallowed as path element component on Windows'),
        ('example.com/LPT1.txt', 'disallowed as path element component on Windows'),
        ('example.com/abc~1', 'trailing tilde and digits'),
        ('example.com/abc~1.go', 'trailing tilde and digits'),
        ('example/m', 'missing dot in first path element'),
        ('Example.com/m', "invalid char 'E' in first path element"),
        ('exa_mple.com/m', "invalid char '_' in first path element"),
        ('example.com/m/v0', 'invalid version suffix'),
        ('example.com/m/v1', 'invalid version suffix'),
        ('example.com/m/v02', 'invalid version suffix'),
        ('example.com/m/v2.0', 'invalid version suffix'),
        ('gopkg.in/yaml', 'invalid version suffix'),
        ('gopkg.in/yaml.v', 'invalid version suffix'),
        ('gopkg.in/yaml.v02', 'invalid version suffix'),
        ('gopkg.in/yaml.v0-unstable', 'invalid version suffix'),
        ('gopkg.in/yaml/v2', 'invalid version suffix'),
    ],
)
def test_check_path_invalid(path: str, reason: str) -> None:
    with pytest.raises(ModuleError, match='malformed module path') as error:
        check_path(path)
    assert reason in str(error.value)


@pytest.mark.parametrize(
    'path',
    ['go.mod', 'a/b/c.go', '.hidden', 'a/.b/c', 'Ä/ö.go', 'a!#$%&()+,-.=@[]^_{}~ b.go', 'abc~1.go', '-dash.go'],
)
def test_check_file_path_valid(path: str) -> None:
    check_file_path(path)


@pytest.mark.parametrize(
    ('path', 'reason'),
    [
        ('', 'empty string'),
        ('a//b', 'double slash'),
        ('a/', 'trailing slash'),
        ('..', 'invalid path element'),
        ('a/../b', 'invalid path element'),
        ('a.', 'trailing dot'),
        ('a:b', 'invalid char'),
        ("a'b", 'invalid char'),
        ('a\\b', 'invalid char'),
        ('a\nb', 'invalid char'),
        ('a/€', 'invalid char'),
        ('aux.go', 'disallowed as path element component on Windows'),
        ('x/Com3', 'disallowed as path element component on Windows'),
    ],
)
def test_check_file_path_invalid(path: str, reason: str) -> None:
    with pytest.raises(ModuleError, match='malformed file path') as error:
        check_file_path(path)
    assert reason in str(error.value)


@pytest.mark.parametrize(
    ('path', 'expected'),
    [
        ('example.com/m', ('example.com/m', '')),
        ('example.com/m/v2', ('example.com/m', '/v2')),
        ('example.com/m/v123', ('example.com/m', '/v123')),
        ('example.com/m/v1', None),
        ('example.com/m/v0', None),
        ('example.com/m/v01', None),
        ('example.com/m/v2.1', None),
        ('example.com/m/v', ('example.com/m/v', '')),
        ('/v2', ('/v2', '')),
        ('gopkg.in/yaml.v2', ('gopkg.in/yaml', '.v2')),
        ('gopkg.in/yaml.v0', ('gopkg.in/yaml', '.v0')),
        ('gopkg.in/yaml.v3-unstable', ('gopkg.in/yaml', '.v3-unstable')),
        ('gopkg.in/yaml', None),
        ('gopkg.in/yaml.v01', None),
    ],
)
def test_split_path_version(path: str, expected: tuple[str, str] | None) -> None:
    assert split_path_version(path) == expected


@pytest.mark.parametrize(
    ('value', 'escaped'),
    [
        ('github.com/Owner/Repo', 'github.com/!owner/!repo'),
        ('example.com/m', 'example.com/m'),
        ('v1.0.0-RC1', 'v1.0.0-!r!c1'),
    ],
)
def test_escape_roundtrip(value: str, escaped: str) -> None:
    assert escape(value) == escaped
    assert unescape(escaped) == value


def test_escape_rejects_exclamation_mark() -> None:
    with pytest.raises(ModuleError, match='invalid char'):
        escape('example.com/a!b')


@pytest.mark.parametrize('value', ['Example.com', 'a!', 'a!B', 'a!1', 'a!!b'])
def test_unescape_invalid(value: str) -> None:
    with pytest.raises(ModuleError, match='invalid escaped string'):
        unescape(value)


def test_version_parse() -> None:
    version = Version('v1.2.3-rc.1.beta')
    assert (version.major, version.minor, version.patch) == (1, 2, 3)
    assert version.prerelease == ('rc', '1', 'beta')
    assert str(version) == 'v1.2.3-rc.1.beta'
    assert repr(version) == "Version('v1.2.3-rc.1.beta')"
    assert Version('v0.0.0').prerelease == ()


@pytest.mark.parametrize(
    'text',
    [
        '1.2.3',
        'v1.2',
        'v1',
        'v01.2.3',
        'v1.02.3',
        'v1.2.03',
        'v1.2.3-',
        'v1.2.3-01',
        'v1.2.3-a..b',
        'v1.2.3+meta',
        'v2.0.0+incompatible',
        'v1.2.3 ',
        'latest',
    ],
)
def test_version_invalid(text: str) -> None:
    with pytest.raises(ModuleError, match='not a canonical semantic version'):
        Version(text)


def test_version_equality_and_hash() -> None:
    assert Version('v1.0.0') == Version('v1.0.0')
    assert Version('v1.0.0') != Version('v1.0.1')
    assert Version('v1.0.0') != 'v1.0.0'
    assert len({Version('v1.0.0'), Version('v1.0.0')}) == 1
    assert Version('v1.0.0') < Version('v1.0.1')
    assert Version('v1.0.1') >= Version('v1.0.0')
    with pytest.raises(TypeError):
        assert Version('v1.0.0') < 'v1.0.1'


def test_sort_versions_semver_precedence() -> None:
    # the example order of the semver specification, plus numeric fields with several digits
    ordered = [
        'v0.9.0',
        'v1.0.0-0.3.7',
        'v1.0.0-alpha',
        'v1.0.0-alpha.1',
        'v1.0.0-alpha.beta',
        'v1.0.0-beta',
        'v1.0.0-beta.2',
        'v1.0.0-beta.11',
        'v1.0.0-rc.1',
        'v1.0.0',
        'v1.2.0',
        'v1.10.0',
        'v2.0.0',
        'v10.0.0',
    ]
    assert sort_versions(list(reversed(ordered))) == ordered
    assert sort_versions([]) == []


@pytest.mark.parametrize(
    ('versions', 'latest'),
    [
        ([], None),
        (['v1.0.0', 'v1.1.0-rc.1', 'v0.9.0'], 'v1.0.0'),
        (['v1.1.0-rc.1', 'v1.1.0-beta'], 'v1.1.0-rc.1'),
        (['v1.0.0', 'v1.10.0', 'v1.9.0'], 'v1.10.0'),
    ],
)
def test_latest_version(versions: list[str], latest: str | None) -> None:
    assert latest_version(versions) == latest


@pytest.mark.parametrize(
    ('path', 'version'),
    [
        ('example.com/m', 'v0.1.0'),
        ('example.com/m', 'v1.2.3-rc.1'),
        ('example.com/m/v2', 'v2.0.0'),
        ('example.com/m/v3', 'v3.1.0-pre'),
        ('gopkg.in/yaml.v2', 'v2.4.0'),
        ('gopkg.in/yaml.v3-unstable', 'v3.0.0'),
        ('gopkg.in/check.v1', 'v0.0.0-20200227125254-8fa46927fb4f'),
        ('gopkg.in/x.v0', 'v0.1.0'),
    ],
)
def test_check_version_valid(path: str, version: str) -> None:
    check_version(path, version)


@pytest.mark.parametrize(
    ('path', 'version', 'reason'),
    [
        ('example.com/m', 'v2.0.0', 'needs the suffix /v2'),
        ('example.com/m/v2', 'v1.0.0', 'should be v2, not v1'),
        ('example.com/m/v2', 'v3.0.0', 'should be v2, not v3'),
        ('gopkg.in/yaml.v2', 'v3.0.0', 'should be v2, not v3'),
        ('gopkg.in/yaml.v2', 'v0.0.0-20200227125254-8fa46927fb4f', 'should be v2, not v0'),
        ('example.com/m', 'v2.0.0+incompatible', 'not a canonical semantic version'),
        ('example.com/m', 'v1.0', 'not a canonical semantic version'),
        ('example', 'v1.0.0', 'malformed module path'),
    ],
)
def test_check_version_invalid(path: str, version: str, reason: str) -> None:
    with pytest.raises(ModuleError, match=reason):
        check_version(path, version)


def test_module_version() -> None:
    module_version = ModuleVersion.parse(' example.com/m/v2@v2.1.0\n')
    assert module_version == ModuleVersion('example.com/m/v2', 'v2.1.0')
    assert str(module_version) == 'example.com/m/v2@v2.1.0'
    with pytest.raises(ModuleError, match='not a module@version'):
        ModuleVersion.parse('example.com/m')
    with pytest.raises(ModuleError):
        ModuleVersion.parse('example.com/m@v2.0.0')


@pytest.mark.parametrize(
    'go_mod',
    [
        b'module example.com/m\n',
        'module example.com/m',
        b'// a comment\n\nmodule   example.com/m   // trailing\n\ngo 1.21\n',
        b'module "example.com/m"\n',
        b'module "example.com/\\x6d"\n',
        b'module `example.com/m`\n',
        b'module (\n\texample.com/m\n)\n',
        b'module(\n\n  // the path\n  example.com/m\n)\n',
        b'\xef\xbb\xbf\nmodule\texample.com/m\r\n'[3:],
        b'go 1.21\nrequire example.com/other v1.0.0\nmodule example.com/m\n',
    ],
)
def test_module_path_of_go_mod(go_mod: bytes | str) -> None:
    assert module_path_of_go_mod(go_mod) == 'example.com/m'


def test_module_path_of_go_mod_unicode_escape() -> None:
    assert module_path_of_go_mod('module "example.com/\\u00e4"\n') == 'example.com/ä'
    assert module_path_of_go_mod('module "example.com/ä"\n') == 'example.com/ä'


@pytest.mark.parametrize(
    'go_mod',
    [
        b'',
        b'go 1.21\n',
        b'// module example.com/m\n',
        b'module\n',
        b'modules example.com/m\n',
        b'module (\n)\nmodule example.com/m\n',
        b'module (\n\n',
    ],
)
def test_module_path_of_go_mod_missing(go_mod: bytes) -> None:
    with pytest.raises(ModuleError, match='no module directive'):
        module_path_of_go_mod(go_mod)
