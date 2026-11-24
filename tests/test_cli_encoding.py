"""Windows console regressions for advertised health-check commands."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_setu_selftest_succeeds_with_legacy_windows_console_encoding():
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "cp1252"
    proc = subprocess.run(
        [sys.executable, "scripts/setu.py", "selftest"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 0, proc.stderr
    assert "11/11 gates hold" in proc.stdout
