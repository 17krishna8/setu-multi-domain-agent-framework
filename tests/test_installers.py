#!/usr/bin/env python3
"""Installer dry-run and selection smoke tests."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _bash() -> Path | None:
    git = shutil.which("git")
    candidates = [Path(git).resolve().parents[1] / "bin" / "bash.exe"] if git else []
    candidates += [Path(r"C:\Program Files\Git\bin\bash.exe"),
                   Path(r"C:\Program Files\DeepAI Code\resources\git\bin\bash.exe")]
    return next((path for path in candidates if path.is_file()), None)


def test_shell_installer_dry_run_does_not_create_target_root(tmp_path):
    bash = _bash()
    if bash is None:
        pytest.skip("Git Bash is not installed; WSL bash wrapper is not an interpreter")
    target = tmp_path / "not-created" / "claude-home"
    env = os.environ.copy()
    env["PYTHON"] = os.sys.executable
    env["CLAUDE_HOME"] = str(target)

    result = subprocess.run([str(bash), str(ROOT / "install.sh"), "--dry-run",
                             "--profile", "minimal"], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=30)

    assert result.returncode == 0, result.stderr
    assert "DRY RUN" in result.stdout
    assert "would:" in result.stdout
    assert not target.exists()
    assert not target.parent.exists()


def test_powershell_installer_dry_run_does_not_create_target_root(tmp_path):
    if os.name != "nt":
        pytest.skip("PowerShell installer smoke test requires Windows")
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        pytest.skip("Windows PowerShell is unavailable")
    target = tmp_path / "not-created" / "cursor-home"
    env = os.environ.copy()
    env["CURSOR_HOME"] = str(target)

    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         str(ROOT / "install.ps1"), "-DryRun", "-Target", "cursor", "-Profile", "minimal"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "DRY RUN" in result.stdout
    assert "setu\\scripts\\finance.py" in result.stdout
    assert not target.exists()
    assert not target.parent.exists()


def test_shell_installer_selects_only_the_education_pack(tmp_path):
    bash = _bash()
    if bash is None:
        pytest.skip("Git Bash is not installed; WSL bash wrapper is not an interpreter")
    target = tmp_path / "claude-home"
    ledger = tmp_path / "install-ledger.json"
    env = os.environ.copy()
    env.update({"PYTHON": os.sys.executable, "CLAUDE_HOME": str(target),
                "SETU_LEDGER": str(ledger)})

    result = subprocess.run(
        [str(bash), str(ROOT / "install.sh"), "--target", "claude",
         "--domains", "education"], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert (target / "setu/scripts/education.py").is_file()
    assert (target / "skills/setu-education/SKILL.md").is_file()
    assert (target / "agents/education-study-coach.md").is_file()
    assert not (target / "setu/scripts/finance.py").exists()
    assert not (target / "skills/setu-finance/SKILL.md").exists()
