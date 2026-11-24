"""Uninstall must remove exactly what install wrote, and give back what it moved aside."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import install_state  # noqa: E402


def _entry(root: Path, files: list[Path]) -> dict:
    return {"target": "claude", "root": str(root), "files": [str(f) for f in files],
            "shared_files": [], "status": "installed"}


def test_uninstall_restores_user_file_moved_aside_and_removes_setu_copy(tmp_path):
    root = tmp_path / ".claude"
    agents = root / "agents"
    agents.mkdir(parents=True)
    original = agents / "helper.md"
    backup = agents / "helper.md.setu-bak"
    original.write_text("setu copy", encoding="utf-8")
    backup.write_text("user's own edits", encoding="utf-8")

    entry = _entry(root, [backup, original])  # install records the backup, then the copy
    install_state._uninstall_entries([entry], dry_run=False)

    assert original.read_text(encoding="utf-8") == "user's own edits"
    assert not backup.exists()


def test_uninstall_dry_run_changes_nothing(tmp_path):
    root = tmp_path / ".claude"
    root.mkdir()
    original = root / "helper.md"
    backup = root / "helper.md.setu-bak"
    original.write_text("setu copy", encoding="utf-8")
    backup.write_text("user's own edits", encoding="utf-8")

    install_state._uninstall_entries([_entry(root, [backup, original])], dry_run=True)

    assert original.read_text(encoding="utf-8") == "setu copy"
    assert backup.read_text(encoding="utf-8") == "user's own edits"
