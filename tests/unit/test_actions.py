"""The build action is in build/ next to pack/ and seed/. The Marketplace lists only the action at the
root of the repository, which is the same action and must not drift from it."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_root_action_is_the_build_action() -> None:
    build = (ROOT / 'build' / 'action.yml').read_text(encoding='utf-8')
    root = (ROOT / 'action.yml').read_text(encoding='utf-8')
    assert '"$GITHUB_ACTION_PATH/../run.sh" action build' in build
    assert root == build.replace('"$GITHUB_ACTION_PATH/../run.sh"', '"$GITHUB_ACTION_PATH/run.sh"')
