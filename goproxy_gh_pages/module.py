"""Module paths, versions and go.mod files, following golang.org/x/mod/module and x/mod/semver."""

from __future__ import annotations

import re
from functools import total_ordering
from typing import NamedTuple


class ModuleError(ValueError):
    """An invalid module path, file path, version or go.mod file"""


_BAD_WINDOWS_NAMES = {
    'con',
    'prn',
    'aux',
    'nul',
    *(f'com{i}' for i in range(1, 10)),
    *(f'lpt{i}' for i in range(1, 10)),
}
_FILE_PUNCT = set('!#$%&()+,-.=@[]^_{}~ ')
_MOD_PUNCT = set('-._~')


def _mod_char_ok(char: str) -> bool:
    return char.isascii() and (char.isalnum() or char in _MOD_PUNCT)


def _file_char_ok(char: str) -> bool:
    if char.isascii():
        return char.isalnum() or char in _FILE_PUNCT
    return char.isalpha()


def _check_elem(elem: str, kind: str) -> None:
    if elem == '':
        raise ModuleError('empty path element')
    if elem.count('.') == len(elem):
        raise ModuleError(f'invalid path element {elem!r}')
    if elem[0] == '.' and kind == 'module':
        raise ModuleError('leading dot in path element')
    if elem[-1] == '.':
        raise ModuleError('trailing dot in path element')
    char_ok = _file_char_ok if kind == 'file' else _mod_char_ok
    for char in elem:
        if not char_ok(char):
            raise ModuleError(f'invalid char {char!r}')
    short = elem.split('.', 1)[0]
    if short.lower() in _BAD_WINDOWS_NAMES:
        raise ModuleError(f'{short!r} disallowed as path element component on Windows')
    if kind == 'file':
        return
    # path elements that look like Windows short names: a tilde followed by digits
    tilde = short.rfind('~')
    if 0 <= tilde < len(short) - 1 and short[tilde + 1 :].isdigit() and short[tilde + 1 :].isascii():
        raise ModuleError(f'trailing tilde and digits in path element {elem!r}')


def _check_path(path: str, kind: str) -> None:
    if path == '':
        raise ModuleError('empty string')
    if path[0] == '-' and kind != 'file':
        raise ModuleError('leading dash')
    if '//' in path:
        raise ModuleError('double slash')
    if path[-1] == '/':
        raise ModuleError('trailing slash')
    for elem in path.split('/'):
        _check_elem(elem, kind)


def check_path(path: str) -> None:
    """Checks that a module path is valid, like module.CheckPath"""
    try:
        _check_path(path, 'module')
        host = path.split('/', 1)[0]
        if '.' not in host:
            raise ModuleError('missing dot in first path element')
        # a leading dash is rejected by _check_path already
        for char in host:
            if not (char in '-.' or ('0' <= char <= '9') or ('a' <= char <= 'z')):
                raise ModuleError(f'invalid char {char!r} in first path element')
        if split_path_version(path) is None:
            raise ModuleError('invalid version suffix')
    except ModuleError as error:
        raise ModuleError(f'malformed module path {path!r}: {error}') from None


def check_file_path(path: str) -> None:
    """Checks that a path of a file in a module is valid, like module.CheckFilePath"""
    try:
        _check_path(path, 'file')
    except ModuleError as error:
        raise ModuleError(f'malformed file path {path!r}: {error}') from None


def split_path_version(path: str) -> tuple[str, str] | None:
    """Splits a module path into prefix and major version suffix (`/vN`, or `.vN` for gopkg.in),
    like module.SplitPathVersion. Returns None for an invalid suffix like /v0, /v1 or /v02, and for
    gopkg.in paths without `.vN`."""
    if path.startswith('gopkg.in/'):
        gopkg = re.search(r'\.v([0-9]+)(?:-unstable)?$', path)
        if not gopkg or (gopkg.group(1)[0] == '0' and gopkg.group(0) != '.v0'):
            return None
        return path[: gopkg.start()], path[gopkg.start() :]
    match = re.search(r'/v([0-9.]+)$', path)
    if not match or match.start() == 0:
        return path, ''
    number = match.group(1)
    if '.' in number or number[0] == '0' or number == '1':
        return None
    return path[: match.start()], path[match.start() :]


def escape(value: str) -> str:
    """Escapes a module path or version for URLs and file names: upper case letters become
    `!` and the lower case letter, like module.EscapePath"""
    if '!' in value:
        raise ModuleError(f'invalid char "!" in {value!r}')
    return re.sub('[A-Z]', lambda match: '!' + match.group(0).lower(), value)


def unescape(value: str) -> str:
    """Reverts escape()"""
    if re.search('[A-Z]|![^a-z]|!$', value):
        raise ModuleError(f'invalid escaped string {value!r}')
    return re.sub('!([a-z])', lambda match: match.group(1).upper(), value)


_SEMVER = re.compile(
    r'v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)'
    r'(?:-((?:0|[1-9][0-9]*|[0-9]*[a-zA-Z-][0-9a-zA-Z-]*)'
    r'(?:\.(?:0|[1-9][0-9]*|[0-9]*[a-zA-Z-][0-9a-zA-Z-]*))*))?'
)


@total_ordering
class Version:
    """A canonical semantic version as Go modules use it: vMAJOR.MINOR.PATCH[-PRERELEASE]"""

    __slots__ = ('major', 'minor', 'patch', 'prerelease', 'text')

    def __init__(self, text: str) -> None:
        match = _SEMVER.fullmatch(text)
        if not match:
            raise ModuleError(f'invalid version {text!r}: not a canonical semantic version')
        self.text = text
        self.major = int(match.group(1))
        self.minor = int(match.group(2))
        self.patch = int(match.group(3))
        self.prerelease: tuple[str, ...] = tuple(match.group(4).split('.')) if match.group(4) else ()

    def _key(self) -> tuple[int, int, int, int, tuple[tuple[int, int, str], ...]]:
        # a release sorts after all of its prereleases; numeric identifiers sort before others
        pre = tuple((0, int(part), '') if part.isdigit() else (1, 0, part) for part in self.prerelease)
        return (self.major, self.minor, self.patch, 0 if self.prerelease else 1, pre)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._key() < other._key()

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Version) and self.text == other.text

    def __hash__(self) -> int:
        return hash(self.text)

    def __str__(self) -> str:
        return self.text

    def __repr__(self) -> str:
        return f'Version({self.text!r})'


def sort_versions(versions: list[str]) -> list[str]:
    """Sorts versions by semantic version precedence, oldest first"""
    return [str(version) for version in sorted(Version(text) for text in versions)]


_PSEUDO = re.compile(r'v[0-9]+\.(?:0\.0-|\d+\.\d+-(?:[^+]*\.)?0\.)(\d{14})-[A-Za-z0-9]+')


def is_pseudo_version(version: str) -> bool:
    """Whether a version is a pseudo-version, like v0.0.0-20240102030405-abcdef123456"""
    return bool(_PSEUDO.fullmatch(version))


def latest_version(versions: list[str]) -> str | None:
    """The version `@latest` resolves to: the highest release, else the highest tagged prerelease,
    else the pseudo-version of the newest commit, as the go command picks it from a list without
    tagged versions"""
    if not versions:
        return None
    parsed = sorted(Version(text) for text in versions)
    releases = [version for version in parsed if not version.prerelease]
    tagged = [version for version in parsed if not is_pseudo_version(version.text)]
    if releases or tagged:
        return str((releases or tagged)[-1])
    # the newest commit time; of versions with the same time, the first in the list, as Go picks it
    return str(max(parsed, key=lambda version: _pseudo_time(version.text)))


def _pseudo_time(version: str) -> str:
    match = _PSEUDO.fullmatch(version)
    return match[1] if match else ''


def check_version(path: str, version: str) -> None:
    """Checks that a version is valid for a module path, like module.Check: the version must be
    canonical and match the major version suffix of the path. +incompatible versions are not
    supported, because they have no go.mod file."""
    check_path(path)
    parsed = Version(version)
    split = split_path_version(path)
    suffix = split[1] if split else ''
    major = suffix[2:].replace('-unstable', '')
    if suffix.startswith('.v') and major == '1' and version.startswith('v0.0.0-'):
        return  # old pseudo-versions of gopkg.in/x.v1, which the go command accepts
    if suffix:
        if parsed.major != int(major):
            raise ModuleError(f'{path}@{version}: invalid version: should be v{major}, not v{parsed.major}')
    elif parsed.major > 1:
        raise ModuleError(
            f'{path}@{version}: invalid version: should be v0 or v1, not v{parsed.major}; '
            f'the module path of v{parsed.major} needs the suffix /v{parsed.major}'
        )


class ModuleVersion(NamedTuple):
    path: str
    version: str

    def __str__(self) -> str:
        return f'{self.path}@{self.version}'

    @classmethod
    def parse(cls, text: str) -> ModuleVersion:
        path, sep, version = text.strip().rpartition('@')
        if not sep:
            raise ModuleError(f'not a module@version: {text!r}')
        check_version(path, version)
        return cls(path, version)


_GO_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"|`([^`]*)`')


def _unquote(token: str) -> str:
    match = _GO_STRING.fullmatch(token)
    if not match:
        return token
    if match.group(2) is not None:
        return match.group(2)
    return match.group(1).encode('latin-1', 'backslashreplace').decode('unicode_escape')


def module_path_of_go_mod(data: bytes | str) -> str:
    """The path of the module directive of a go.mod file"""
    text = data.decode('utf-8') if isinstance(data, bytes) else data
    # strip comments, which can't be part of a module path
    lines = [re.sub(r'//.*$', '', line).strip() for line in text.splitlines()]
    for index, line in enumerate(lines):
        match = re.match(r'module(?:\s*\(\s*$|\s+(\S.*))', line)
        if not match:
            continue
        if match.group(1) is None:
            # module (
            #     path
            # )
            for inner in lines[index + 1 :]:
                if inner == ')':
                    break
                if inner:
                    return _unquote(inner)
            break
        return _unquote(match.group(1).strip())
    raise ModuleError('go.mod has no module directive')
