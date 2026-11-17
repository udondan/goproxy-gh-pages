"""Helpers to make module zips and fetched versions for the source and site tests"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from goproxy_gh_pages.modzip import ZipResult, create_zip
from goproxy_gh_pages.sources import Fetched, read_local
from tests.unit.conftest import write_module

TIME = datetime(2024, 5, 6, 7, 8, 9, tzinfo=timezone.utc)


def make_zip(
    tmp_path: Path,
    module: str,
    version: str,
    files: dict[str, str | bytes] | None = None,
    name: str | None = None,
    time: datetime = TIME,
) -> ZipResult:
    """Writes a module directory and its zip under tmp_path"""
    safe = module.replace('/', '_') + '@' + version
    module_dir = write_module(tmp_path / 'src' / safe, module, files)
    output = tmp_path / 'zips' / (name or f'{safe}.zip')
    output.parent.mkdir(parents=True, exist_ok=True)
    return create_zip(str(module_dir), version, str(output), time)


def make_fetched(tmp_path: Path, module: str, version: str, files: dict[str, str | bytes] | None = None) -> Fetched:
    result = make_zip(tmp_path, module, version, files)
    return read_local(result.path)[1]
