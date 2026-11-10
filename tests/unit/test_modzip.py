from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import zipfile
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from goproxy_gh_pages import modzip
from goproxy_gh_pages.module import ModuleError
from goproxy_gh_pages.modzip import (
    CheckedFiles,
    ZipError,
    check_files,
    create_zip,
    file_sha256,
    format_time,
    hash_go_mod,
    is_vendored_package,
    link_or_copy,
    list_dir,
    parse_time,
    read_zip,
    vendor_rules_of,
)
from tests.unit.conftest import write_module

TIME = datetime(2024, 5, 6, 7, 8, 9, tzinfo=timezone.utc)
MODULE = 'example.com/m'


def hash1(files: dict[str, bytes]) -> str:
    """dirhash.Hash1, written from its definition: the sha256 of the sorted lines
    `<sha256 hex of the file>  <name>\\n`, base64 encoded, with the prefix h1:"""
    lines = ''.join(f'{hashlib.sha256(data).hexdigest()}  {name}\n' for name, data in sorted(files.items()))
    return 'h1:' + base64.b64encode(hashlib.sha256(lines.encode()).digest()).decode()


def make_zip(
    entries: Sequence[tuple[str, bytes] | str],
    comment: bytes = b'',
    date_time: tuple[int, int, int, int, int, int] = (2020, 1, 2, 3, 4, 6),
) -> bytes:
    """A zip with the given entries, in order; names ending in / are directories"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for entry in entries:
            name, data = (entry, b'') if isinstance(entry, str) else entry
            archive.writestr(zipfile.ZipInfo(name, date_time), data)
        archive.comment = comment
    return buffer.getvalue()


def write_zip(tmp_path: Path, data: bytes, name: str = 'm.zip') -> str:
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def symlinks_supported(tmp_path: Path) -> bool:
    try:
        os.symlink(tmp_path / 'missing', tmp_path / '.probe')
    except (OSError, NotImplementedError):
        return False
    os.remove(tmp_path / '.probe')
    return True


# is_vendored_package


@pytest.mark.parametrize(
    ('name', 'before_1_24', 'since_1_24'),
    [
        ('vendor/modules.txt', False, True),
        ('vendor/a.go', False, False),
        ('vendor/a/b.go', True, True),
        ('vendor/a/b/c.go', True, True),
        ('a/vendor/b.go', True, False),  # the offset bug of older go commands, golang.org/issue/37397
        ('pkg/vendor/vendor.go', True, False),
        ('pkg/vendor/foo/foo.go', True, True),
        ('a/vendor/modules.txt', True, False),
        ('/vendor/b.go', False, False),
        ('vendorx/a/b.go', False, False),
        ('a/b.go', False, False),
    ],
)
def test_is_vendored_package(name: str, before_1_24: bool, since_1_24: bool) -> None:
    assert is_vendored_package(name) is before_1_24
    assert is_vendored_package(name, go_1_24=True) is since_1_24


@pytest.mark.parametrize(
    ('go_mod', 'expected'),
    [
        (None, False),
        (b'module m\n', False),
        (b'module m\n\ngo 1.23\n', False),
        (b'module m\n\ngo 1.23.4\n', False),
        (b'module m\n\ngo 1.24\n', True),
        (b'module m\n\ngo 1.24rc1\n', True),
        (b'module m\n\ngo 1.26.0 // comment\n', True),
        (b'module m\n\n  go 1.25\n', True),
        (b'module m\n\ngo 2\n', True),
        (b'module m\n\ngo "1.24"\n', True),
        (b'module m\n// go 1.24\n', False),
        (b'module m\ntoolchain go1.25.0\n', False),
    ],
)
def test_vendor_rules_of(go_mod: bytes | None, expected: bool) -> None:
    assert vendor_rules_of(go_mod) is expected


# check_files


def files(*paths: str, size: int = 1, regular: bool = True) -> list[tuple[str, int, bool]]:
    return [(path, size, regular) for path in paths]


def test_check_files_valid_and_omitted() -> None:
    result = check_files(
        files(
            'go.mod',
            'a.go',
            'vendor/modules.txt',
            'vendor/x/y.go',
            'sub/go.mod',
            'sub/a.go',
            'sub/deep/b.go',
            'other/GO.MOD',
            'other/c.go',
            '.hg_archival.txt',
            'LICENSE',
        )
        + files('link', regular=False)
        + files('dir/go.mod', regular=False)
        + files('dir/d.go')
    )
    assert result.valid == ['go.mod', 'a.go', 'vendor/modules.txt', 'LICENSE', 'dir/d.go']
    assert result.omitted == [
        ('vendor/x/y.go', 'file is in vendor directory'),
        ('sub/go.mod', 'file is in another module'),
        ('sub/a.go', 'file is in another module'),
        ('sub/deep/b.go', 'file is in another module'),
        ('other/GO.MOD', 'file is in another module'),
        ('other/c.go', 'file is in another module'),
        ('.hg_archival.txt', 'file is inserted by "hg archive" and is always omitted'),
        ('link', 'not a regular file'),
        ('dir/go.mod', 'not a regular file'),
    ]
    assert result.invalid == []
    assert result.size_error is None
    result.raise_for_errors()


@pytest.mark.parametrize(
    ('path', 'reason'),
    [
        ('./a.go', 'file path is not clean'),
        ('a/../b.go', 'file path is not clean'),
        ('a//b.go', 'file path is not clean'),
        ('..', 'file path is not clean'),
        ('../a.go', 'file path is not clean'),
        ('/a.go', 'file path is not relative'),
        ('.', 'invalid path element'),
        ('a:b.go', 'invalid char'),
        ('nul.go', 'disallowed as path element component on Windows'),
        ('GO.MOD', 'go.mod files must have lowercase names'),
        ('Go.mod', 'go.mod files must have lowercase names'),
    ],
)
def test_check_files_invalid(path: str, reason: str) -> None:
    result = check_files(files(path))
    assert result.valid == []
    assert len(result.invalid) == 1
    assert result.invalid[0][0] == path
    assert reason in result.invalid[0][1]
    with pytest.raises(ZipError, match='invalid module files'):
        result.raise_for_errors()


@pytest.mark.parametrize(
    ('paths', 'reason'),
    [
        (('a.go', 'A.go'), "case-insensitive file name collision: 'a.go' and 'A.go'"),
        (('dir/a.go', 'DIR/b.go'), "case-insensitive file name collision: 'dir' and 'DIR'"),
        (('a.go', 'a.go'), "multiple entries for file 'a.go'"),
        (('dir', 'dir/a.go'), "entry 'dir' is both a file and a directory"),
        (('dir/a.go', 'dir'), "entry 'dir' is both a file and a directory"),
        (('σ.go', 'ς.go'), "case-insensitive file name collision: 'σ.go' and 'ς.go'"),
        (('k.go', 'K.go'), "case-insensitive file name collision: 'k.go' and 'K.go'"),
        (('ß.go', 'ẞ.go'), "case-insensitive file name collision: 'ß.go' and 'ẞ.go'"),
        (('ΐ.go', 'ΐ.go'), "case-insensitive file name collision: 'ΐ.go' and 'ΐ.go'"),
    ],
)
def test_check_files_collisions(paths: tuple[str, str], reason: str) -> None:
    result = check_files(files(*paths))
    assert result.valid == [paths[0]]
    assert result.invalid == [(paths[1], reason)]


def test_check_files_no_collision_with_full_case_folding() -> None:
    # full case folding maps ß to ss, Go's simple folding doesn't
    assert check_files(files('ß.go', 'ss.go', 'İ.go', 'i.go')).invalid == []


def test_check_files_in_zip_size_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(modzip, 'MAX_ZIP_FILE', 10)
    monkeypatch.setattr(modzip, 'MAX_GO_MOD', 3)
    monkeypatch.setattr(modzip, 'MAX_LICENSE', 2)
    result = check_files([('go.mod', 4, True), ('LICENSE', 3, True), ('a.go', 6, True), ('b.go', 6, True)], in_zip=True)
    assert result.valid == ['a.go', 'b.go']
    assert [path for path, _ in result.invalid] == ['go.mod', 'LICENSE']
    assert result.size_error == 'total uncompressed size of module contents too large (max size is 10 bytes)'


def test_check_files_in_zip_keeps_what_modules_leave_out() -> None:
    result = check_files(files('vendor/a/b.go', '.hg_archival.txt', 'sub/a.go'), in_zip=True)
    assert result.valid == ['vendor/a/b.go', '.hg_archival.txt', 'sub/a.go']
    assert result.omitted == []


def test_check_files_same_directory_twice() -> None:
    assert check_files(files('a/b/c.go', 'a/b/d.go', 'a/e.go')).valid == ['a/b/c.go', 'a/b/d.go', 'a/e.go']


def test_check_files_size_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(modzip, 'MAX_ZIP_FILE', 10)
    monkeypatch.setattr(modzip, 'MAX_GO_MOD', 3)
    monkeypatch.setattr(modzip, 'MAX_LICENSE', 2)
    result = check_files([('a.go', 6, True), ('b.go', 6, True), ('c.go', 6, True)])
    assert result.valid == ['a.go', 'b.go', 'c.go']
    assert result.size_error == 'module source tree too large (max size is 10 bytes)'
    with pytest.raises(ZipError, match='module source tree too large'):
        result.raise_for_errors()

    assert check_files([('a.go', -1, True)]).size_error is not None
    assert check_files([('a.go', 10, True)]).size_error is None

    result = check_files([('go.mod', 4, True), ('LICENSE', 3, True), ('sub/LICENSE', 3, True)])
    assert result.invalid == [
        ('go.mod', 'go.mod file too large (max size is 3 bytes)'),
        ('LICENSE', 'LICENSE file too large (max size is 2 bytes)'),
    ]
    assert result.valid == ['sub/LICENSE']


def test_raise_for_errors_lists_all_problems() -> None:
    checked = CheckedFiles(invalid=[('a', 'one'), ('b', 'two')], size_error='too large')
    with pytest.raises(ZipError) as error:
        checked.raise_for_errors()
    assert str(error.value) == 'invalid module files:\n  a: one\n  b: two\n  too large'


# list_dir


def test_list_dir(tmp_path: Path) -> None:
    root = write_module(
        tmp_path / 'm',
        MODULE,
        {
            'b.go': 'package m\n',
            'a/x.go': 'package a\n',
            'vendor/modules.txt': '',
            'vendor/dep/dep.go': 'package dep\n',
            '.git/config': '',
            'pkg/.hg/store': '',
            'sub/go.mod': 'module example.com/m/sub\n',
            'sub/s.go': 'package sub\n',
            'notsub/go.mod/file': '',
            'notsub/n.go': 'package notsub\n',
        },
    )
    listed, omitted = list_dir(str(root))
    assert listed == [
        ('a/x.go', 10, True),
        ('b.go', 10, True),
        ('go.mod', (root / 'go.mod').stat().st_size, True),
        ('notsub/go.mod/file', 0, True),
        ('notsub/n.go', 15, True),
        ('vendor/modules.txt', 0, True),
    ]
    assert omitted == [
        ('.git', 'directory is a version control repository'),
        ('pkg/.hg', 'directory is a version control repository'),
        ('sub', 'directory is in another module'),
        ('vendor/dep/dep.go', 'file is in vendor directory'),
    ]


def test_list_dir_without_go_mod(tmp_path: Path) -> None:
    (tmp_path / 'vendor').mkdir()
    (tmp_path / 'vendor' / 'modules.txt').write_bytes(b'')
    listed, omitted = list_dir(str(tmp_path))
    assert listed == [('vendor/modules.txt', 0, True)]
    assert omitted == []


def test_list_dir_symlinks(tmp_path: Path) -> None:
    if not symlinks_supported(tmp_path):
        pytest.skip('symbolic links are not supported')
    root = write_module(tmp_path / 'm', MODULE, {'a.go': 'package m\n', 'target/t.go': 'package target\n'})
    os.symlink(root / 'a.go', root / 'link.go')
    os.symlink(root / 'target', root / 'linkdir', target_is_directory=True)
    listed, omitted = list_dir(str(root))
    assert [path for path, _, _ in listed] == ['a.go', 'go.mod', 'target/t.go']
    assert omitted == [('link.go', 'not a regular file'), ('linkdir', 'not a regular file')]


def test_list_dir_symlinked_go_mod_makes_a_submodule(tmp_path: Path) -> None:
    if not symlinks_supported(tmp_path):
        pytest.skip('symbolic links are not supported')
    root = write_module(tmp_path / 'm', MODULE, {'sub/a.go': 'package sub\n'})
    os.symlink(tmp_path / 'missing', root / 'sub' / 'go.mod')
    _, omitted = list_dir(str(root))
    assert omitted == [('sub', 'directory is in another module')]


# create_zip


def test_create_zip(tmp_path: Path) -> None:
    root = write_module(
        tmp_path / 'm',
        MODULE,
        {
            'm.go': 'package m\n',
            'pkg/p.go': 'package pkg\n',
            'vendor/dep/dep.go': 'package dep\n',
            '.git/HEAD': 'ref\n',
            'LICENSE': 'license\n',
        },
    )
    output = tmp_path / 'out' / 'nested' / 'm.zip'
    result = create_zip(str(root), 'v1.2.3', str(output), TIME, compression_level=9)
    assert result.path == str(output)
    assert (result.module, result.version, result.time) == (MODULE, 'v1.2.3', '2024-05-06T07:08:09Z')
    assert result.files == 4
    assert result.size == output.stat().st_size
    assert result.omitted == [
        ('.git', 'directory is a version control repository'),
        ('vendor/dep/dep.go', 'file is in vendor directory'),
    ]
    contents = {
        f'{MODULE}@v1.2.3/LICENSE': b'license\n',
        f'{MODULE}@v1.2.3/go.mod': (root / 'go.mod').read_bytes(),
        f'{MODULE}@v1.2.3/m.go': b'package m\n',
        f'{MODULE}@v1.2.3/pkg/p.go': b'package pkg\n',
    }
    assert result.h1 == hash1(contents)
    assert result.mod_h1 == hash1({'go.mod': (root / 'go.mod').read_bytes()})
    assert not (tmp_path / 'out' / 'nested' / 'm.zip.tmp').exists()

    with zipfile.ZipFile(output) as archive:
        assert json.loads(archive.comment) == {'Version': 'v1.2.3', 'Time': '2024-05-06T07:08:09Z'}
        infos = archive.infolist()
        assert [info.filename for info in infos] == sorted(contents)
        for info in infos:
            assert info.date_time == (2024, 5, 6, 7, 8, 8)  # zip times have a resolution of 2 seconds
            assert info.compress_type == zipfile.ZIP_DEFLATED
            assert info.create_system == 3
            assert info.external_attr >> 16 == 0o100644
            assert archive.read(info) == contents[info.filename]


def test_create_zip_is_reproducible(tmp_path: Path) -> None:
    files_: dict[str, str | bytes] = {'b.go': 'package m\n', 'a/a.go': 'package a\n' * 1000, 'z.txt': 'z'}
    first = write_module(tmp_path / 'one', MODULE, files_)
    second = write_module(tmp_path / 'two', MODULE, dict(reversed(files_.items())))
    os.utime(second / 'b.go', (0, 0))
    one = create_zip(str(first), 'v1.0.0', str(tmp_path / 'one.zip'), TIME)
    two = create_zip(str(second), 'v1.0.0', str(tmp_path / 'two.zip'), TIME.astimezone(timezone(timedelta(hours=5))))
    assert (tmp_path / 'one.zip').read_bytes() == (tmp_path / 'two.zip').read_bytes()
    assert one.h1 == two.h1
    again = create_zip(str(first), 'v1.0.0', str(tmp_path / 'one.zip'), TIME)
    assert again.size == one.size
    assert file_sha256(str(tmp_path / 'one.zip')) == file_sha256(str(tmp_path / 'two.zip'))


def test_create_zip_compression_level(tmp_path: Path) -> None:
    root = write_module(tmp_path / 'm', MODULE, {'a.go': 'package m\n' + '// padding\n' * 2000})
    stored = create_zip(str(root), 'v1.0.0', str(tmp_path / '0.zip'), TIME, compression_level=0)
    best = create_zip(str(root), 'v1.0.0', str(tmp_path / '9.zip'), TIME, compression_level=9)
    assert best.size < stored.size
    assert best.h1 == stored.h1


def test_create_zip_time(tmp_path: Path) -> None:
    root = write_module(tmp_path / 'm', MODULE)
    early = create_zip(str(root), 'v1.0.0', str(tmp_path / 'a.zip'), datetime(1970, 1, 1, tzinfo=timezone.utc))
    assert early.time == '1980-01-01T00:00:00Z'
    precise = create_zip(
        str(root), 'v1.0.0', str(tmp_path / 'b.zip'), datetime(2024, 1, 1, 0, 0, 1, 999999, tzinfo=timezone.utc)
    )
    assert precise.time == '2024-01-01T00:00:01Z'


def test_create_zip_major_version(tmp_path: Path) -> None:
    root = write_module(tmp_path / 'm', 'example.com/m/v2')
    result = create_zip(str(root), 'v2.0.0', str(tmp_path / 'm.zip'), TIME)
    assert result.module == 'example.com/m/v2'
    with pytest.raises(ModuleError, match='should be v2, not v1'):
        create_zip(str(root), 'v1.0.0', str(tmp_path / 'x.zip'), TIME)


def test_create_zip_errors(tmp_path: Path) -> None:
    with pytest.raises(ZipError, match=r'has no go\.mod file'):
        create_zip(str(tmp_path), 'v1.0.0', str(tmp_path / 'm.zip'), TIME)
    root = write_module(tmp_path / 'm', MODULE, {"it's.go": 'package m\n'})
    with pytest.raises(ZipError, match='invalid char'):
        create_zip(str(root), 'v1.0.0', str(tmp_path / 'm.zip'), TIME)
    assert not (tmp_path / 'm.zip').exists()


def test_create_zip_too_large(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = write_module(tmp_path / 'm', MODULE, {'a.bin': os.urandom(100)})
    monkeypatch.setattr(modzip, 'MAX_ZIP_FILE', 200)
    with pytest.raises(ZipError, match='module zip too large'):
        create_zip(str(root), 'v1.0.0', str(tmp_path / 'm.zip'), TIME, compression_level=0)
    assert not (tmp_path / 'm.zip').exists()
    assert not (tmp_path / 'm.zip.tmp').exists()


def test_create_zip_output_in_module_dir(tmp_path: Path) -> None:
    root = write_module(tmp_path / 'm', MODULE)
    output = root / 'dist' / 'm.zip'
    first = create_zip(str(root), 'v1.0.0', str(output), TIME)
    (root / 'dist' / 'm.zip.tmp').write_bytes(b'leftover')
    first_bytes = output.read_bytes()
    second = create_zip(str(root), 'v1.0.0', str(output), TIME)
    assert second.h1 == first.h1
    assert output.read_bytes() == first_bytes
    assert ('dist/m.zip', 'output of this command') in second.omitted
    assert read_zip(str(output)).files == ['go.mod', 'lib.go']


def test_create_zip_vendor_rules_follow_go_version(tmp_path: Path) -> None:
    module_files: dict[str, str | bytes] = {
        'vendor/modules.txt': '',
        'pkg/vendor/vendor.go': 'package vendor\n',
        'pkg/vendor/foo/foo.go': 'package foo\n',
    }
    old = write_module(tmp_path / 'old', MODULE, module_files)
    assert create_zip(str(old), 'v1.0.0', str(tmp_path / 'old.zip'), TIME).files == 2
    assert read_zip(str(tmp_path / 'old.zip')).files == ['go.mod', 'vendor/modules.txt']
    new = write_module(tmp_path / 'new', MODULE, module_files)
    (new / 'go.mod').write_bytes(f'module {MODULE}\n\ngo 1.24\n'.encode())
    assert create_zip(str(new), 'v1.0.0', str(tmp_path / 'new.zip'), TIME).files == 2
    assert read_zip(str(tmp_path / 'new.zip')).files == ['go.mod', 'pkg/vendor/vendor.go']


def test_create_zip_replaces_output(tmp_path: Path) -> None:
    root = write_module(tmp_path / 'm', MODULE)
    output = tmp_path / 'm.zip'
    output.write_bytes(b'old')
    create_zip(str(root), 'v1.0.0', str(output), TIME)
    assert read_zip(str(output)).version == 'v1.0.0'


# hashes


def test_hash_go_mod() -> None:
    data = b'module example.com/m\n'
    assert hash_go_mod(data) == hash1({'go.mod': data})
    # sum.golang.org has this hash for golang.org/x/text v0.3.0/go.mod
    assert hash_go_mod(b'module golang.org/x/text\n') == 'h1:NqM8EUOU14njkJ3fqMW+pc6Ldnwhi/IjpwHt7yyuwOQ='


def test_hash1_rejects_newlines() -> None:
    with pytest.raises(ZipError, match='newlines'):
        modzip._hash1({'a\nb': '00'})


# read_zip


def test_read_zip_roundtrip(tmp_path: Path) -> None:
    root = write_module(tmp_path / 'm', MODULE, {'m.go': 'package m\n', 'pkg/p.go': 'package pkg\n'})
    created = create_zip(str(root), 'v1.2.3', str(tmp_path / 'm.zip'), TIME)
    module_zip = read_zip(str(tmp_path / 'm.zip'), expect=(MODULE, 'v1.2.3'))
    assert (module_zip.module, module_zip.version, module_zip.time) == (MODULE, 'v1.2.3', created.time)
    assert module_zip.go_mod == (root / 'go.mod').read_bytes()
    assert module_zip.h1 == created.h1
    assert module_zip.mod_h1 == created.mod_h1
    assert module_zip.files == ['go.mod', 'm.go', 'pkg/p.go']

    unhashed = read_zip(str(tmp_path / 'm.zip'), hash_files=False)
    assert unhashed.h1 == ''
    assert unhashed.mod_h1 == created.mod_h1

    with open(tmp_path / 'm.zip', 'rb') as file:
        assert read_zip(file).h1 == created.h1


def test_read_zip_legacy_without_comment(tmp_path: Path) -> None:
    prefix = f'{MODULE}@v1.0.0/'
    data = make_zip([(prefix + 'a.go', b'package m\n')], date_time=(2019, 1, 1, 0, 0, 0))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr(zipfile.ZipInfo(prefix + 'a.go', (2019, 1, 1, 0, 0, 0)), b'package m\n')
        archive.writestr(zipfile.ZipInfo(prefix + 'go.mod', (2021, 2, 3, 4, 5, 6)), b'module example.com/m\n')
    module_zip = read_zip(write_zip(tmp_path, buffer.getvalue()))
    assert module_zip.time == '2021-02-03T04:05:06Z'
    assert module_zip.h1 == hash1({prefix + 'a.go': b'package m\n', prefix + 'go.mod': b'module example.com/m\n'})

    # without go.mod: the time of the first file, and the go.mod the go command makes up
    module_zip = read_zip(write_zip(tmp_path, data, 'nomod.zip'))
    assert module_zip.time == '2019-01-01T00:00:00Z'
    assert module_zip.go_mod == b'module example.com/m\n'
    assert module_zip.files == ['a.go']


@pytest.mark.parametrize(
    'comment',
    [b'not json', b'{"Version": "v1.0.0"}', b'{"Time": "yesterday"}', b'["Time"]', b'{"Time": 5}', b'\xff'],
)
def test_read_zip_ignores_invalid_comments(tmp_path: Path, comment: bytes) -> None:
    data = make_zip(
        [(f'{MODULE}@v1.0.0/go.mod', b'module example.com/m\n')], comment=comment, date_time=(2022, 2, 2, 2, 2, 2)
    )
    try:
        assert read_zip(write_zip(tmp_path, data)).time == '2022-02-02T02:02:02Z'
    except UnicodeDecodeError:  # pragma: no cover
        pytest.fail('comment that is not UTF-8 is not ignored')


def test_read_zip_includes_directory_entries_in_the_hash(tmp_path: Path) -> None:
    prefix = f'{MODULE}@v1.0.0/'
    data = make_zip([prefix, (prefix + 'go.mod', b'module example.com/m\n'), prefix + 'pkg/'])
    module_zip = read_zip(write_zip(tmp_path, data))
    assert module_zip.files == ['go.mod']
    assert module_zip.h1 == hash1({prefix: b'', prefix + 'go.mod': b'module example.com/m\n', prefix + 'pkg/': b''})


@pytest.mark.parametrize(
    ('entries', 'error'),
    [
        ([('example.com/m/go.mod', b'')], 'not in a <module>@<version>/ directory'),
        ([('example.com/m@v1.0.0', b'')], 'not in a <module>@<version>/ directory'),
        ([('example.com/m@1.0/go.mod', b'')], 'not a module zip: invalid version'),
        ([('example@v1.0.0/go.mod', b'')], 'not a module zip: malformed module path'),
        ([('example.com/m@v2.0.0/a.go', b'')], 'not a module zip:'),
        (
            [('example.com/m@v1.0.0/a.go', b''), ('example.com/n@v1.0.0/b.go', b'')],
            "'example.com/n@v1.0.0/b.go' is not in example.com/m@v1.0.0/",
        ),
        ([('example.com/m@v1.0.0/a.go', b''), ('example.com/m@v1.0.0/A.go', b'')], 'case-insensitive'),
        ([('example.com/m@v1.0.0/a:b.go', b'')], 'invalid char'),
        ([('example.com/m@v1.0.0/a/go.mod', b'')], 'go.mod file not in module root directory'),
        ([('example.com/m@v1.0.0/GO.MOD', b'')], 'go.mod files must have lowercase names'),
        ([('example.com/m@v1.0.0/σ.go', b''), ('example.com/m@v1.0.0/ς.go', b'')], 'case-insensitive'),
        ([('example.com/m@v1.0.0/../a.go', b'')], 'not clean'),
        ([('example.com/m@v1.0.0/go.mod', b'module example.com/other\n')], 'is not the module example.com/m'),
        ([('example.com/m@v1.0.0/go.mod', b'go 1.21\n')], 'no module directive'),
    ],
)
def test_read_zip_invalid(tmp_path: Path, entries: list[tuple[str, bytes]], error: str) -> None:
    with pytest.raises(ZipError, match=error):
        read_zip(write_zip(tmp_path, make_zip(entries)))


def test_read_zip_zero_dos_date(tmp_path: Path) -> None:
    # golang.org/x/mod, which makes the zips of proxy.golang.org, writes no comment and a zero DOS date
    prefix = f'{MODULE}@v1.0.0/'
    entries = [(prefix + 'go.mod', b'module example.com/m\n'), (prefix + 'm.go', b'package m\n')]
    data = make_zip(entries, date_time=(1980, 0, 0, 0, 0, 0))
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.infolist()[0].date_time == (1980, 0, 0, 0, 0, 0)
    module_zip = read_zip(write_zip(tmp_path, data))
    assert module_zip.time is None
    assert module_zip.h1 == hash1(dict(entries))
    # without go.mod, the time of the first file
    assert read_zip(write_zip(tmp_path, make_zip(entries[1:], date_time=(1980, 0, 0, 0, 0, 0)), 'n.zip')).time is None


def test_read_zip_corrupt_data(tmp_path: Path) -> None:
    contents = b'package m\n' * 100
    data = make_zip([(f'{MODULE}@v1.0.0/go.mod', b'module example.com/m\n'), (f'{MODULE}@v1.0.0/m.go', contents)])
    # a stored file with other bytes than its CRC says
    start = data.index(contents)
    path = write_zip(tmp_path, data[:start] + b'X' + data[start + 1 :])
    with pytest.raises(ZipError, match='invalid module zip: Bad CRC-32'):
        read_zip(path)
    assert read_zip(path, hash_files=False).files == ['go.mod', 'm.go']
    # a compression method zipfile doesn't support
    header = data.index(b'PK\x01\x02')  # the central directory entry of go.mod
    unsupported = bytearray(data)
    unsupported[header + 10 : header + 12] = (99).to_bytes(2, 'little')
    with pytest.raises(ZipError, match='invalid module zip'):
        read_zip(write_zip(tmp_path, bytes(unsupported), 'method.zip'))


def test_read_zip_expect(tmp_path: Path) -> None:
    path = write_zip(tmp_path, make_zip([(f'{MODULE}@v1.0.0/go.mod', b'module example.com/m\n')]))
    assert read_zip(path, expect=(MODULE, 'v1.0.0')).version == 'v1.0.0'
    with pytest.raises(ZipError, match=re.escape('zip has example.com/m@v1.0.0, expected example.com/m@v1.0.1')):
        read_zip(path, expect=(MODULE, 'v1.0.1'))


def test_read_zip_not_a_zip(tmp_path: Path) -> None:
    with pytest.raises(ZipError, match='not a zip file'):
        read_zip(write_zip(tmp_path, b'not a zip'))
    with pytest.raises(ZipError, match='empty zip file'):
        read_zip(write_zip(tmp_path, make_zip([]), 'empty.zip'))


def test_read_zip_size_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_zip(tmp_path, make_zip([(f'{MODULE}@v1.0.0/a.go', b'x' * 1000)]))
    monkeypatch.setattr(modzip, 'MAX_ZIP_FILE', os.path.getsize(path) - 1)
    with pytest.raises(ZipError, match='module zip too large'):
        read_zip(path)
    monkeypatch.setattr(modzip, 'MAX_ZIP_FILE', 999)
    with (
        open(path, 'rb') as file,
        pytest.raises(ZipError, match='total uncompressed size of module contents too large'),
    ):
        read_zip(file)


# times and files


@pytest.mark.parametrize(
    ('text', 'expected'),
    [
        ('1700000000', datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)),
        (' 0 ', datetime(1970, 1, 1, tzinfo=timezone.utc)),
        ('2024-05-06T07:08:09Z', TIME),
        ('2024-05-06T07:08:09', TIME),
        ('2024-05-06T09:08:09+02:00', TIME),
        ('2024-05-06 07:08:09.5Z', TIME.replace(microsecond=500000)),
    ],
)
def test_parse_time(text: str, expected: datetime) -> None:
    parsed = parse_time(text)
    assert parsed == expected
    assert parsed.tzinfo == timezone.utc


def test_parse_time_invalid() -> None:
    with pytest.raises(ValueError, match='yesterday'):
        parse_time('yesterday')


def test_format_time() -> None:
    assert format_time(TIME.astimezone(timezone(timedelta(hours=-3)))) == '2024-05-06T07:08:09Z'


def test_file_sha256(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(modzip, '_CHUNK', 3)
    path = tmp_path / 'f'
    path.write_bytes(b'0123456789')
    assert file_sha256(str(path)) == hashlib.sha256(b'0123456789').hexdigest()


def test_link_or_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / 'source'
    source.write_bytes(b'data')
    target = tmp_path / 'target'
    target.write_bytes(b'old')
    link_or_copy(str(source), str(target))
    assert target.read_bytes() == b'data'

    def no_link(source: str, target: str) -> None:
        raise OSError('cross-device link')

    monkeypatch.setattr(os, 'link', no_link)
    copy = tmp_path / 'copy'
    link_or_copy(str(source), str(copy))
    assert copy.read_bytes() == b'data'
    assert not os.path.samefile(source, copy)
