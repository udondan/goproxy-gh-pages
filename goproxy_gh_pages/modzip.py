"""Module zip files, following golang.org/x/mod/zip and the h1 hash of x/mod/sumdb/dirhash.

The go command accepts a module zip only if all its files are under `<module>@<version>/`, have
valid paths, don't collide when compared case-insensitively and stay within the size limits.
Files of nested modules, vendored packages and VCS directories are left out, as `go mod download`
leaves them out.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import posixpath
import re
import shutil
import stat
import zipfile
import zlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import IO

from goproxy_gh_pages.module import (
    ModuleError,
    check_file_path,
    check_version,
    module_path_of_go_mod,
)

MAX_ZIP_FILE = 500 << 20
MAX_GO_MOD = 16 << 20
MAX_LICENSE = 16 << 20
_VCS_DIRS = {'.bzr', '.git', '.hg', '.svn'}
_CHUNK = 1 << 20
# The earliest time a zip file can store
_ZIP_EPOCH = datetime(1980, 1, 1, tzinfo=timezone.utc)


class ZipError(ValueError):
    """A module zip, or the files for one, that the go command would not accept"""


_GO_DIRECTIVE = re.compile(rb'^[ \t]*go[ \t]+"?(\d+)(?:\.(\d+))?', re.MULTILINE)


def vendor_rules_of(go_mod: bytes | None) -> bool:
    """Whether a module uses the vendor rules of Go 1.24 and later, which depend on the go
    directive of its go.mod file"""
    match = _GO_DIRECTIVE.search(go_mod or b'')
    if not match:
        return False
    return (int(match[1]), int(match[2] or 0)) >= (1, 24)


def is_vendored_package(name: str, go_1_24: bool = False) -> bool:
    """Whether a file is in a vendored package, which module zips leave out. Before Go 1.24,
    vendor/modules.txt stays and the offset for nested vendor directories is wrong: the go command
    keeps that behavior for older modules so their hashes don't change (golang.org/issue/37397)."""
    if go_1_24 and name == 'vendor/modules.txt':
        return True
    if name.startswith('vendor/'):
        index = len('vendor/')
    elif (position := name.find('/vendor/')) >= 0:
        index = position + len('/vendor/') if go_1_24 else len('/vendor/')
    else:
        return False
    return '/' in name[index:]


# Pairs that Go's simple case folding joins but Python's case folding doesn't
_FOLD_EXTRA = {'ΐ': 'ΐ', 'ΰ': 'ΰ', 'ﬆ': 'ﬅ'}


def _fold_char(char: str) -> str:
    char = _FOLD_EXTRA.get(char, char)
    folded = char.casefold()
    if len(folded) == 1:
        return folded
    # full case folding maps some letters to several, which simple folding doesn't (ß and ss)
    lower = char.lower()
    return lower if len(lower) == 1 else char


def _fold(path: str) -> str:
    """A key that is the same for paths Go treats as equal ignoring case (strings.EqualFold)"""
    if path.isascii():
        return path.lower()
    return ''.join(_fold_char(char) for char in path)


class _CollisionChecker:
    def __init__(self) -> None:
        self.seen: dict[str, tuple[str, bool]] = {}

    def check(self, path: str, is_dir: bool) -> None:
        fold = _fold(path)
        other = self.seen.get(fold)
        if other:
            if other[0] != path:
                raise ZipError(f'case-insensitive file name collision: {other[0]!r} and {path!r}')
            if other[1] != is_dir:
                raise ZipError(f'entry {path!r} is both a file and a directory')
            if not is_dir:
                raise ZipError(f'multiple entries for file {path!r}')
            return
        self.seen[fold] = (path, is_dir)
        parent = path.rsplit('/', 1)[0] if '/' in path else ''
        if parent:
            self.check(parent, True)


@dataclass
class CheckedFiles:
    """The files of a module by whether they go into the zip, like zip.CheckedFiles"""

    valid: list[str] = field(default_factory=list)
    omitted: list[tuple[str, str]] = field(default_factory=list)
    invalid: list[tuple[str, str]] = field(default_factory=list)
    size_error: str | None = None

    def raise_for_errors(self) -> None:
        problems = [f'{path}: {reason}' for path, reason in self.invalid]
        if self.size_error:
            problems.append(self.size_error)
        if problems:
            raise ZipError('invalid module files:\n  ' + '\n  '.join(problems))


def check_files(
    files: Iterable[tuple[str, int, bool]], go_mod: bytes | None = None, in_zip: bool = False
) -> CheckedFiles:
    """Checks the files of a module, given as (slash path, size, is regular file), like
    zip.CheckFiles, or like zip.CheckZip with in_zip, which leaves out nothing. go_mod is the
    module's go.mod file, which decides the vendor rules."""
    files = list(files)
    result = CheckedFiles()
    if in_zip:
        _check_zip_files(files, result)
        return result
    go_1_24 = vendor_rules_of(go_mod)
    have_go_mod = {
        path.rsplit('/', 1)[0] + '/' if '/' in path else ''
        for path, _, regular in files
        if regular and path.rsplit('/', 1)[-1].lower() == 'go.mod'
    }

    def in_submodule(path: str) -> bool:
        while '/' in path:
            path = path.rsplit('/', 1)[0]
            if path + '/' in have_go_mod:
                return True
        return False

    collisions = _CollisionChecker()
    remaining = MAX_ZIP_FILE
    for path, size, regular in files:
        if path != posixpath.normpath(path) or path == '..' or path.startswith('../'):
            result.invalid.append((path, 'file path is not clean'))
            continue
        if path.startswith('/'):
            result.invalid.append((path, 'file path is not relative'))
            continue
        if is_vendored_package(path, go_1_24):
            result.omitted.append((path, 'file is in vendor directory'))
            continue
        if in_submodule(path):
            result.omitted.append((path, 'file is in another module'))
            continue
        if path == '.hg_archival.txt':
            result.omitted.append((path, 'file is inserted by "hg archive" and is always omitted'))
            continue
        try:
            check_file_path(path)
        except ModuleError as error:
            result.invalid.append((path, str(error)))
            continue
        if path.lower() == 'go.mod' and path != 'go.mod':
            result.invalid.append((path, 'go.mod files must have lowercase names'))
            continue
        try:
            collisions.check(path, False)
        except ZipError as error:
            result.invalid.append((path, str(error)))
            continue
        if not regular:
            result.omitted.append((path, 'not a regular file'))
            continue
        if 0 <= size <= remaining:
            remaining -= size
        elif result.size_error is None:
            result.size_error = f'module source tree too large (max size is {MAX_ZIP_FILE} bytes)'
        if path == 'go.mod' and size > MAX_GO_MOD:
            result.invalid.append((path, f'go.mod file too large (max size is {MAX_GO_MOD} bytes)'))
            continue
        if path == 'LICENSE' and size > MAX_LICENSE:
            result.invalid.append((path, f'LICENSE file too large (max size is {MAX_LICENSE} bytes)'))
            continue
        result.valid.append(path)
    return result


def _check_zip_files(files: list[tuple[str, int, bool]], result: CheckedFiles) -> None:
    """The checks of zip.CheckZip, which accepts vendored files and the like. The third field of a
    file is False for a directory entry, which is checked but not valid content."""
    collisions = _CollisionChecker()
    remaining = MAX_ZIP_FILE
    for path, size, is_file in files:
        if path != posixpath.normpath(path) or path == '..' or path.startswith(('../', '/')):
            result.invalid.append((path, 'file path is not clean'))
            continue
        try:
            check_file_path(path)
            collisions.check(path, not is_file)
        except (ModuleError, ZipError) as error:
            result.invalid.append((path, str(error)))
            continue
        if not is_file:
            continue
        base = path.rsplit('/', 1)[-1]
        if base.lower() == 'go.mod':
            if base != path:
                result.invalid.append((path, 'go.mod file not in module root directory'))
                continue
            if path != 'go.mod':
                result.invalid.append((path, 'go.mod files must have lowercase names'))
                continue
        if 0 <= size <= remaining:
            remaining -= size
        elif result.size_error is None:
            result.size_error = (
                f'total uncompressed size of module contents too large (max size is {MAX_ZIP_FILE} bytes)'
            )
        if path == 'go.mod' and size > MAX_GO_MOD:
            result.invalid.append((path, f'go.mod file too large (max size is {MAX_GO_MOD} bytes)'))
            continue
        if path == 'LICENSE' and size > MAX_LICENSE:
            result.invalid.append((path, f'LICENSE file too large (max size is {MAX_LICENSE} bytes)'))
            continue
        result.valid.append(path)


def list_dir(module_dir: str, exclude: Iterable[str] = ()) -> tuple[list[tuple[str, int, bool]], list[tuple[str, str]]]:
    """The files of a module directory in walk order, without VCS directories and nested modules,
    like the file listing of zip.CreateFromDir. Symbolic links are not followed. exclude holds
    slash paths to leave out, such as the zip being written."""
    files: list[tuple[str, int, bool]] = []
    omitted: list[tuple[str, str]] = []
    excluded = set(exclude)
    try:
        with open(os.path.join(module_dir, 'go.mod'), 'rb') as file:
            go_1_24 = vendor_rules_of(file.read())
    except OSError:
        go_1_24 = False

    def walk(directory: str, prefix: str) -> None:
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda entry: entry.name):
                path = prefix + entry.name
                if path in excluded:
                    omitted.append((path, 'output of this command'))
                    continue
                if is_vendored_package(path, go_1_24):
                    omitted.append((path, 'file is in vendor directory'))
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.name in _VCS_DIRS:
                        omitted.append((path, 'directory is a version control repository'))
                        continue
                    go_mod = os.path.join(entry.path, 'go.mod')
                    if os.path.lexists(go_mod) and not stat.S_ISDIR(os.lstat(go_mod).st_mode):
                        omitted.append((path, 'directory is in another module'))
                        continue
                    walk(entry.path, path + '/')
                    continue
                info = entry.stat(follow_symlinks=False)
                regular = stat.S_ISREG(info.st_mode)
                if not regular:
                    omitted.append((path, 'not a regular file'))
                    continue
                files.append((path, info.st_size, True))

    walk(module_dir, '')
    return files, omitted


@dataclass
class ZipResult:
    path: str
    module: str
    version: str
    time: str
    files: int
    size: int
    h1: str
    mod_h1: str
    omitted: list[tuple[str, str]]


def format_time(time: datetime) -> str:
    return time.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def parse_time(text: str) -> datetime:
    """Parses RFC 3339 or seconds since the epoch"""
    text = text.strip()
    if text.isdigit():
        return datetime.fromtimestamp(int(text), timezone.utc)
    text = text.replace('Z', '+00:00').replace('z', '+00:00')
    # Python 3.10 accepts only 3 or 6 digits of fractional seconds
    text = re.sub(r'\.(\d+)', lambda m: '.' + (m.group(1) + '000000')[:6], text, count=1)
    value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _new_info(name: str, date_time: tuple[int, int, int, int, int, int], level: int, size: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time)
    info.compress_type = zipfile.ZIP_DEFLATED
    # the same on every OS, so the zip is the same everywhere
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    info.file_size = size
    # compress_level since Python 3.13, _compresslevel before
    setattr(info, 'compress_level' if hasattr(info, 'compress_level') else '_compresslevel', level)
    return info


def create_zip(
    module_dir: str,
    version: str,
    output: str,
    time: datetime,
    compression_level: int = 9,
) -> ZipResult:
    """Writes the module zip of a module directory. All files have the given time, which is also the
    time of the version in the archive comment, so a zip of the same files is the same zip."""
    go_mod = os.path.join(module_dir, 'go.mod')
    if not os.path.isfile(go_mod):
        raise ZipError(f'{module_dir} has no go.mod file')
    with open(go_mod, 'rb') as file:
        go_mod_data = file.read()
    module = module_path_of_go_mod(go_mod_data)
    check_version(module, version)
    # an output in the module directory, such as from an earlier run, is not part of the module
    relative = os.path.relpath(os.path.abspath(output), os.path.abspath(module_dir)).replace(os.sep, '/')
    files, omitted = list_dir(module_dir, exclude=(relative, relative + '.tmp'))
    checked = check_files(files, go_mod_data)
    checked.raise_for_errors()
    omitted += checked.omitted
    time = max(time.astimezone(timezone.utc), _ZIP_EPOCH).replace(microsecond=0)
    date_time = (time.year, time.month, time.day, time.hour, time.minute, time.second)
    prefix = f'{module}@{version}/'
    sizes = {path: size for path, size, _ in files}
    directory = os.path.dirname(os.path.abspath(output))
    os.makedirs(directory, exist_ok=True)
    temporary = output + '.tmp'
    hashes: dict[str, str] = {}
    try:
        with zipfile.ZipFile(temporary, 'w') as archive:
            archive.comment = json.dumps({'Version': version, 'Time': format_time(time)}).encode()
            for path in checked.valid:
                info = _new_info(prefix + path, date_time, compression_level, sizes[path])
                digest = hashlib.sha256()
                with (
                    open(os.path.join(module_dir, *path.split('/')), 'rb') as source,
                    archive.open(info, 'w') as target,
                ):
                    while chunk := source.read(_CHUNK):
                        digest.update(chunk)
                        target.write(chunk)
                hashes[prefix + path] = digest.hexdigest()
        size = os.path.getsize(temporary)
        if size > MAX_ZIP_FILE:
            raise ZipError(f'module zip too large: {size} bytes (max size is {MAX_ZIP_FILE} bytes)')
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
    mod_h1 = hash_go_mod(go_mod_data)
    return ZipResult(
        path=output,
        module=module,
        version=version,
        time=format_time(time),
        files=len(checked.valid),
        size=size,
        h1=_hash1(hashes.items()),
        mod_h1=mod_h1,
        omitted=omitted,
    )


def _hash1(hashes: Iterable[tuple[str, str]]) -> str:
    """dirhash.Hash1 of (name, sha256 hex) pairs. A name that is in a zip more than once, which
    zips can have for directories, counts as often as it is there."""
    summary = hashlib.sha256()
    for name, digest in sorted(hashes):
        if '\n' in name:
            raise ZipError('file names with newlines are not supported')
        summary.update(f'{digest}  {name}\n'.encode())
    return 'h1:' + base64.b64encode(summary.digest()).decode()


def hash_go_mod(data: bytes) -> str:
    """The h1 hash of a go.mod file, which sum.golang.org has as `<module> <version>/go.mod`"""
    return _hash1([('go.mod', hashlib.sha256(data).hexdigest())])


@dataclass
class ModuleZip:
    """The module version of a module zip and what the proxy serves for it"""

    module: str
    version: str
    time: str | None
    go_mod: bytes
    h1: str
    mod_h1: str
    files: list[str]


def _read_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> str:
    digest = hashlib.sha256()
    with archive.open(info) as member:
        while chunk := member.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def read_zip(path: str | IO[bytes], expect: tuple[str, str] | None = None, hash_files: bool = True) -> ModuleZip:
    """Checks a module zip the way the go command checks downloaded zips and returns its module
    version, go.mod file and h1 hash. The version time is the one in the archive comment, or the
    time of go.mod in the zip."""
    if isinstance(path, str) and os.path.getsize(path) > MAX_ZIP_FILE:
        raise ZipError(f'module zip too large (max size is {MAX_ZIP_FILE} bytes)')
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, ValueError, EOFError) as error:
        raise ZipError(f'not a zip file: {error}') from None
    try:
        with archive:
            return _read_zip(archive, expect, hash_files)
    except ZipError:
        raise
    except (ValueError, zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError) as error:
        # corrupt or unsupported data in the archive, or a go.mod without a module path
        raise ZipError(f'invalid module zip: {error}') from None


_UTF8_FLAG = 0x800
# the compression methods archive/zip can read
_GO_METHODS = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)


def _entry_name(info: zipfile.ZipInfo) -> str:
    """The name of a zip entry as Go reads it: the stored bytes as UTF-8, with or without the
    UTF-8 flag. Python decodes names without the flag as CP437 and, on Windows, replaces
    backslashes, so this undoes both."""
    name = info.orig_filename
    if info.flag_bits & _UTF8_FLAG:
        return name
    raw = name.encode('cp437')
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        raise ZipError(f'{raw!r}: malformed file path: invalid UTF-8') from None


def _read_zip(archive: zipfile.ZipFile, expect: tuple[str, str] | None, hash_files: bool) -> ModuleZip:
    infos = archive.infolist()
    if not infos:
        raise ZipError('empty zip file')
    names = [_entry_name(info) for info in infos]
    module, sep, rest = names[0].partition('@')
    version = rest.split('/', 1)[0]
    if not sep or '/' not in rest:
        raise ZipError(f'not a module zip: {names[0]!r} is not in a <module>@<version>/ directory')
    prefix = f'{module}@{version}/'
    try:
        check_version(module, version)
    except ModuleError as error:
        raise ZipError(f'not a module zip: {error}') from None
    if expect and expect != (module, version):
        raise ZipError(f'zip has {module}@{version}, expected {expect[0]}@{expect[1]}')
    # (name, size, is a file): directories are checked like files, as CheckZip does
    files: list[tuple[str, int, bool]] = []
    for info, entry in zip(infos, names, strict=True):
        if not entry.startswith(prefix):
            raise ZipError(f'{entry!r} is not in {prefix}')
        if info.compress_type not in _GO_METHODS:
            raise ZipError(
                f'{entry!r}: unsupported compression method {info.compress_type}; '
                'the go command reads only stored and deflated files'
            )
        name = entry[len(prefix) :]
        if not name:
            continue
        is_dir = name.endswith('/')
        files.append((name[:-1] if is_dir else name, info.file_size, not is_dir))
    # also checks the total uncompressed size
    checked = check_files(files, in_zip=True)
    checked.raise_for_errors()
    go_mod = archive.read(prefix + 'go.mod') if 'go.mod' in checked.valid else None
    if go_mod is not None and module_path_of_go_mod(go_mod) != module:
        raise ZipError(f'go.mod in the zip is not the module {module}')
    time = None
    if archive.comment:
        try:
            # the .info file needs RFC 3339, which is all the go command reads
            time = format_time(parse_time(json.loads(archive.comment.decode())['Time']))
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError, OSError):
            time = None
    if time is None:
        info = archive.getinfo(prefix + 'go.mod') if go_mod is not None else infos[0]
        try:
            time = format_time(datetime(*info.date_time, tzinfo=timezone.utc))
        except ValueError:
            # zips made by golang.org/x/mod, which proxy.golang.org serves, have a zero DOS date
            time = None
    if go_mod is None:
        # the go command makes up a go.mod file for a module without one
        go_mod = f'module {module}\n'.encode()
    h1 = ''
    if hash_files:
        # like dirhash.HashZip: every entry, with the content of the last entry of its name
        digests = {entry: _read_member(archive, info) for info, entry in zip(infos, names, strict=True)}
        h1 = _hash1((entry, digests[entry]) for entry in names)
    return ModuleZip(
        module=module,
        version=version,
        time=time,
        go_mod=go_mod,
        h1=h1,
        mod_h1=hash_go_mod(go_mod),
        files=[name for name, _, is_file in files if is_file],
    )


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as file:
        while chunk := file.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def link_or_copy(source: str, target: str) -> None:
    """Hard links a file, or copies it if it is on another file system"""
    if os.path.exists(target):
        os.remove(target)
    try:
        os.link(source, target)
    except OSError:
        shutil.copyfile(source, target)
