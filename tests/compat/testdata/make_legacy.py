"""Writes the legacy zips in testdata/legacy: module zips as a simple script makes them, without the
archive comment, in an unsorted order and with a directory entry. Run it only to change the zips;
they are committed, so the tests prove that zips made before keep working."""

from __future__ import annotations

import os
import sys
import zipfile

TIMESTAMP = (2023, 5, 6, 7, 8, 9)

ZIPS: dict[str, tuple[str, str, list[tuple[str, bytes | None]]]] = {
    'example.com-legacy-v1.4.0.zip': (
        'example.com/legacy',
        'v1.4.0',
        [
            ('legacy.go', b'package legacy\n\nconst Version = "1.4.0"\n'),
            ('sub/', None),
            ('sub/sub.go', b'package sub\n'),
            ('LICENSE', b'license text\n'),
            ('go.mod', b'module example.com/legacy\n\ngo 1.20\n'),
        ],
    ),
    'github.com-Owner-legacy-v2-v2.0.1.zip': (
        'github.com/Owner/legacy/v2',
        'v2.0.1',
        [
            ('go.mod', b'module github.com/Owner/legacy/v2\n\ngo 1.20\n'),
            ('z.go', b'package legacy\n'),
            ('a.go', b'package legacy\n'),
        ],
    ),
}


def main() -> None:
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'legacy')
    os.makedirs(out_dir, exist_ok=True)
    for name, (module, version, files) in ZIPS.items():
        path = os.path.join(out_dir, name)
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for file, content in files:
                info = zipfile.ZipInfo(f'{module}@{version}/{file}', TIMESTAMP)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (0o755 if content is None else 0o644) << 16
                archive.writestr(info, content or b'', compresslevel=9)
        sys.stdout.write(f'{path}\n')


if __name__ == '__main__':
    main()
