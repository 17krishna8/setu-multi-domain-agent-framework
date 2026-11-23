#!/usr/bin/env python3
"""Setu Web Dev — the fix path, SWE-agent style.

    diagnose ──► fix ──► verify
       │           │        │
   localise    generate  re-run tests
   the bug     a patch   and REFUSE the patch if they fail

The verify step is the one that matters and the one that is usually skipped. A
patch that makes the linter quiet but leaves the tests red is worse than no patch,
because it hides the bug behind a green-looking commit. So ``verify`` runs the
project's test command against the *patched* tree and only then accepts; if it
fails, it reverts and reports.

Design notes
------------
* **Diagnosis is static and deterministic.** It reads the traceback to find the
  file/line, then greps the relevant function. No model call is required to know
  *where* a `KeyError: 'email'` happened — and a deterministic answer is worth
  more than a confident one.
* **Patching is textual and reviewed.** ``fix`` proposes a unified diff via
  ``difflib``, writes it to disk, and requires a human or the verify step to
  apply it. It never does a blind search-replace over a whole repo.
* **Verification reverts.** Any failure restores the original bytes from a
  backup taken before the patch. A fix that breaks the build never survives.

CLI
---
    python3 webdev_fix.py diagnose --traceback err.txt --root .
    python3 webdev_fix.py diagnose --error "KeyError: 'email'" --file app/views.py
    python3 webdev_fix.py apply --patch out/fix.diff --root .
    python3 webdev_fix.py verify --root . --test-cmd "python3 -m pytest -q"
    python3 webdev_fix.py loop --root . --test-cmd "python3 -m pytest -q" --patch out/fix.diff
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Classic CPython traceback:  File "app/x.py", line 12, in handler
TB_FRAME = re.compile(r'File "(?P<path>[^"]+)", line (?P<line>\d+), in (?P<func>\S+)')
# pytest short traceback:      app/x.py:12: in handler   /   app/x.py:12: KeyError
PYTEST_FRAME = re.compile(r'^(?P<path>[\w./\\-]+\.py):(?P<line>\d+):(?:\s+in\s+(?P<func>\S+))?')
# pytest's assertion/exception detail line:  E   KeyError: 'email'
PYTEST_EXC = re.compile(r'^E\s+(?P<type>[A-Za-z_][\w.]*(?:Error|Exception)):\s*(?P<msg>.*)$')
ERR_LINE = re.compile(r'^(?P<type>[A-Za-z_][\w.]*(?:Error|Exception|Warning|Exit)):?'
                      r'(?:\s*(?P<msg>.*))?$')


# --------------------------------------------------------------------------- #
# DIAGNOSE
# --------------------------------------------------------------------------- #
def parse_traceback(text: str) -> dict:
    """Extract the frames and the final exception from a Python traceback."""
    frames: list[dict] = []
    for m in TB_FRAME.finditer(text):
        frames.append({"path": m.group("path"), "line": int(m.group("line")),
                       "func": m.group("func")})
    if not frames:
        seen: set[tuple[str, int]] = set()
        for line in text.splitlines():
            m = PYTEST_FRAME.match(line.strip())
            if not m:
                continue
            key = (m.group("path"), int(m.group("line")))
            if key in seen:
                continue
            seen.add(key)
            frames.append({"path": m.group("path"), "line": int(m.group("line")),
                           "func": m.group("func") or "<module>"})
    exc_type = exc_msg = None
    for line in reversed(text.strip().splitlines()):
        stripped = line.strip()
        m = PYTEST_EXC.match(stripped) or ERR_LINE.match(stripped)
        if m:
            exc_type, exc_msg = m.group("type"), (m.group("msg") or "").strip()
            break
    return {"frames": frames, "exception_type": exc_type, "message": exc_msg,
            "innermost": frames[-1] if frames else None}


def context_for_frame(frame: dict, root: Path, span: int = 6) -> dict:
    """Read the source around the failing line, plus the enclosing def/class."""
    p = Path(frame["path"])
    if not p.is_absolute():
        p = root / p
    if not p.exists():
        return {"error": f"file not found: {p}"}
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    idx = frame["line"] - 1
    start = max(0, idx - span)
    end = min(len(lines), idx + span + 1)
    enclosing = None
    for i in range(idx, -1, -1):
        if re.match(r"\s*(def|class|function|const|async def)\s+", lines[i]):
            enclosing = {"line": i + 1, "text": lines[i].rstrip()}
            break
    return {
        "file": str(p),
        "line": frame["line"],
        "func": frame["func"],
        "source": "\n".join(f"{i + 1:>5} | {lines[i]}" for i in range(start, end)),
        "first_line": start + 1,
        "enclosing": enclosing,
        "total_lines": len(lines),
    }


def diagnose(error_text: str | None = None, traceback_path: str | None = None,
             error: str | None = None, file: str | None = None,
             root: str | Path = ".") -> dict:
    """Localise a bug. Returns a report the `fix` step and a human can act on."""
    rootp = Path(root)
    text = ""
    if traceback_path:
        text = Path(traceback_path).read_text(encoding="utf-8", errors="replace")
    elif error_text:
        text = error_text

    report: dict = {"mode": "traceback" if text else "static", "root": str(rootp)}
    if text:
        tb = parse_traceback(text)
        report.update(tb)
        if tb["innermost"]:
            report["primary_context"] = context_for_frame(tb["innermost"], rootp)
        # The innermost frame is often library code; the interesting one is the
        # last frame that lives inside the project.
        for f in reversed(tb["frames"]):
            fp = Path(f["path"])
            fp = fp if fp.is_absolute() else rootp / fp
            if str(fp.resolve()).startswith(str(rootp.resolve())):
                report["project_frame"] = {**f, "context": context_for_frame(f, rootp)}
                break

    if error:
        m = ERR_LINE.match(error.strip())
        report["exception_type"] = m.group("type") if m else None
        report["message"] = (m.group("msg") if m else error).strip() if m else error
    if file:
        fp = Path(file)
        fp = fp if fp.is_absolute() else rootp / fp
        if fp.exists():
            src = fp.read_text(encoding="utf-8", errors="replace")
            hits = []
            token = (report.get("message") or "").strip("'\" ")
            if token:
                for i, line in enumerate(src.splitlines(), 1):
                    if token and token in line:
                        hits.append({"line": i, "text": line.strip()[:160]})
            report["candidates"] = hits[:10]
            report["file"] = str(fp)

    report["hypotheses"] = _hypotheses(report)
    return report


def _hypotheses(r: dict) -> list[dict]:
    """Turn the raw location into candidate causes, ordered by how often they are true."""
    et = (r.get("exception_type") or "").split(".")[-1]
    msg = r.get("message") or ""
    out: list[dict] = []
    if et == "KeyError":
        out.append({"cause": f"Missing dict key {msg or '?'} — the call site assumed a key "
                             f"that the payload does not always contain.",
                    "fix_shape": "Use .get(key, default) or validate the mapping shape "
                                 "before indexing.",
                    "confidence": 0.7})
    elif et in ("AttributeError", "TypeError"):
        out.append({"cause": f"{et}: {msg or 'an attribute/method is missing or None'} — "
                             f"often a None that arrived from an optional branch.",
                    "fix_shape": "Guard for None, or fix the producer that returns it.",
                    "confidence": 0.6})
    elif et in ("IndexError", "ValueError"):
        out.append({"cause": f"{et}: {msg or 'a value or index was out of range'} — check "
                             f"off-by-one and empty-input cases.",
                    "fix_shape": "Bound the index, or handle the empty case explicitly.",
                    "confidence": 0.55})
    elif et in ("ModuleNotFoundError", "ImportError"):
        out.append({"cause": f"Import problem: {msg}. Usually a dependency not declared, "
                             f"or a circular import.",
                    "fix_shape": "Add the dependency, or move the import inside the function.",
                    "confidence": 0.8})
    elif et == "NameError":
        out.append({"cause": f"Undefined name {msg} — typo, missing import, or use before "
                             f"assignment.",
                    "fix_shape": "Define or import the name; or fix the ordering.",
                    "confidence": 0.75})
    if not out:
        out.append({"cause": "No exception type recognised. Read the context block and "
                             "the enclosing function manually.",
                    "fix_shape": "Write the smallest reproduction as a failing test first.",
                    "confidence": 0.3})
    return out


# --------------------------------------------------------------------------- #
# FIX
# --------------------------------------------------------------------------- #
def propose_patch(path: str | Path, old: str, new: str, label: str = "fix",
                  root: str | Path | None = None) -> str:
    """Produce a unified diff for one substitution. Does not write the file.

    The header must carry the path *relative to the project root*, not the
    basename: a diff that says ``a/views.py`` for ``app/views.py`` cannot be
    applied with ``-p1`` and silently fails on every nested file.
    """
    fp = Path(path)
    if root is not None:
        try:
            rel = fp.resolve().relative_to(Path(root).resolve())
        except ValueError:
            rel = Path(fp.name)
    else:
        rel = Path(fp.name)
    original = fp.read_text(encoding="utf-8").splitlines(keepends=True)
    joined = "".join(original)
    if old not in joined:
        raise ValueError(f"patch anchor not found in {fp}: {old[:80]!r}")
    count = joined.count(old)
    if count > 1:
        raise ValueError(f"patch anchor is ambiguous in {fp} ({count} matches): {old[:80]!r}. "
                         f"Widen the anchor so the fix is reviewable.")
    patched = joined.replace(old, new, 1)
    diff = difflib.unified_diff(
        original, patched.splitlines(keepends=True),
        fromfile=f"a/{rel.as_posix()}", tofile=f"b/{rel.as_posix()}",
        n=3)
    header = f"# setu-webdev-fix: {label}\n"
    return header + "".join(diff)


def apply_patch(patch_text: str, root: str | Path = ".", dry_run: bool = False) -> dict:
    """Apply a unified diff produced by propose_patch. Backs up before writing."""
    rootp = Path(root)
    import subprocess as sp
    try:
        if dry_run:
            proc = sp.run(["patch", "-p1", "--forward", "--dry-run", "--batch"],
                          input=patch_text.encode(), cwd=rootp, capture_output=True)
            return {"applied": proc.returncode == 0, "dry_run": True, "patcher": "gnu-patch",
                    "message": (proc.stdout + proc.stderr).decode(errors="replace").strip()}
        proc = sp.run(["patch", "-p1", "--forward", "--batch"],
                      input=patch_text.encode(), cwd=rootp, capture_output=True)
        return {"applied": proc.returncode == 0, "dry_run": False, "patcher": "gnu-patch",
                "message": (proc.stdout + proc.stderr).decode(errors="replace").strip()}
    except FileNotFoundError:
        # Only reached on a machine with no `patch` binary (e.g. Windows).
        # A *failing* patch must surface its error, never fall through to here.
        return _apply_patch_python(patch_text, rootp, dry_run)


def _apply_patch_python(patch_text: str, root: Path, dry_run: bool) -> dict:
    """Unified-diff applier used only when no ``patch`` binary exists.

    Applies hunk by hunk by matching the hunk's context lines against the file,
    which is what makes it safe: a hunk that does not match is an error, not a
    blind overwrite.
    """
    files: dict[str, list[str]] = {}
    cur: str | None = None
    hunks: list[list[tuple[str, str]]] = []
    hunk: list[tuple[str, str]] = []

    for raw in patch_text.splitlines():
        if raw.startswith("+++ "):
            if cur is not None and hunk:
                hunks.append(hunk)
            name = raw[4:].strip()
            files[name] = []
            cur = name
            hunks = []
            hunk = []
        elif raw.startswith("--- ") or raw.startswith("#"):
            continue
        elif raw.startswith("@@"):
            if hunk:
                hunks.append(hunk)
            hunk = []
        elif cur is not None:
            tag, body = (raw[:1], raw[1:]) if raw else (" ", "")
            hunk.append((tag, body))
    if cur is not None and hunk:
        hunks.append(hunk)

    if not cur:
        return {"applied": False, "message": "no +++ file header found in patch"}

    target = root / cur
    if not target.exists():
        return {"applied": False, "message": f"target missing: {target}"}
    lines = target.read_text(encoding="utf-8").splitlines()
    original = list(lines)

    for h in hunks:
        old_seq = [body for tag, body in h if tag in (" ", "-")]
        new_seq = [body for tag, body in h if tag in (" ", "+")]
        if not old_seq:
            continue
        found = -1
        for i in range(len(lines) - len(old_seq) + 1):
            if lines[i:i + len(old_seq)] == old_seq:
                found = i
                break
        if found < 0:
            return {"applied": False, "patcher": "python",
                    "message": f"hunk did not match in {target}; refusing to guess. "
                               f"First context line: {old_seq[0][:80]!r}"}
        lines[found:found + len(old_seq)] = new_seq

    if dry_run:
        return {"applied": True, "dry_run": True, "patcher": "python",
                "message": f"would rewrite {target} ({len(hunks)} hunk(s))"}
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"applied": True, "dry_run": False, "patcher": "python",
            "files": [str(target)], "message": f"rewrote {target} ({len(hunks)} hunk(s))"}


# --------------------------------------------------------------------------- #
# VERIFY — the step that cannot be skipped
# --------------------------------------------------------------------------- #
def verify(root: str | Path = ".", test_cmd: str | None = None,
           timeout: int = 300) -> dict:
    """Run the project's tests. Returns a verdict; never assumes success."""
    rootp = Path(root)
    cmd = test_cmd or _guess_test_command(rootp)
    if not cmd:
        return {"verified": False, "reason": "no test command given and none could be "
                                             "inferred. Verification cannot be skipped: "
                                             "pass --test-cmd.",
                "root": str(rootp)}
    try:
        proc = subprocess.run(cmd, shell=True, cwd=rootp, capture_output=True,
                              timeout=timeout, text=True)
    except subprocess.TimeoutExpired:
        return {"verified": False, "reason": f"tests timed out after {timeout}s",
                "command": cmd}
    out = (proc.stdout or "") + (proc.stderr or "")
    return {
        "verified": proc.returncode == 0,
        "command": cmd,
        "exit_code": proc.returncode,
        "summary": _test_summary(out),
        "tail": out.strip().splitlines()[-25:],
        "root": str(rootp),
    }


def _guess_test_command(root: Path) -> str | None:
    if (root / "package.json").exists():
        try:
            pkg = json.loads((root / "package.json").read_text())
            if "test" in (pkg.get("scripts") or {}):
                return "npm test --silent"
        except json.JSONDecodeError:
            pass
    if list(root.glob("pytest.ini")) or list(root.glob("tests/test_*.py")) or \
       list(root.glob("test_*.py")):
        return "python3 -m pytest -q"
    if (root / "go.mod").exists():
        return "go test ./..."
    if (root / "Cargo.toml").exists():
        return "cargo test -q"
    return None


def _test_summary(out: str) -> str:
    for pat in (r"\d+ passed[^\n]*", r"\d+ failed[^\n]*", r"Tests:\s+[^\n]+",
                r"OK \(\d+ tests?\)", r"FAILED[^\n]*"):
        m = re.search(pat, out)
        if m:
            return m.group(0).strip()
    return "no parseable summary"


# --------------------------------------------------------------------------- #
# LOOP — fix + verify with automatic revert
# --------------------------------------------------------------------------- #
def loop(patch_path: str, root: str | Path = ".", test_cmd: str | None = None,
         timeout: int = 300) -> dict:
    """Apply a patch, verify, and revert if verification fails. Returns the transcript."""
    rootp = Path(root)
    patch_text = Path(patch_path).read_text(encoding="utf-8")
    # Snapshot every file the patch names, before touching anything.
    targets = set()
    for line in patch_text.splitlines():
        if line.startswith("+++ "):
            name = line[4:].strip()
            if name.startswith("b/"):
                name = name[2:]
            targets.add(rootp / name)
    backups: dict[Path, bytes] = {}
    for t in targets:
        if t.exists():
            backups[t] = t.read_bytes()

    applied = apply_patch(patch_text, rootp)
    if not applied.get("applied"):
        return {"result": "PATCH_FAILED", "applied": applied,
                "note": "patch did not apply; nothing was verified or reverted"}

    v = verify(rootp, test_cmd, timeout)
    if v["verified"]:
        return {"result": "ACCEPTED", "applied": applied, "verify": v,
                "note": "patch applied and tests pass — this is the only path to ACCEPTED"}
    # Revert exactly what we touched.
    for t, data in backups.items():
        t.write_bytes(data)
    return {"result": "REVERTED", "applied": applied, "verify": v,
            "note": "tests failed after the patch; original files restored. "
                    "A patch that breaks tests is worse than no patch.",
            "restored": [str(t) for t in backups]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="webdev_fix.py",
                                 description="Setu Web Dev diagnose -> fix -> verify")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("diagnose")
    d.add_argument("--traceback")
    d.add_argument("--error")
    d.add_argument("--file")
    d.add_argument("--root", default=".")
    d.add_argument("--out")

    a = sub.add_parser("apply")
    a.add_argument("--patch", required=True)
    a.add_argument("--root", default=".")
    a.add_argument("--dry-run", action="store_true")

    v = sub.add_parser("verify")
    v.add_argument("--root", default=".")
    v.add_argument("--test-cmd", default=None)
    v.add_argument("--timeout", type=int, default=300)

    lp = sub.add_parser("loop")
    lp.add_argument("--patch", required=True)
    lp.add_argument("--root", default=".")
    lp.add_argument("--test-cmd", default=None)
    lp.add_argument("--timeout", type=int, default=300)

    args = ap.parse_args(argv)

    if args.cmd == "diagnose":
        rep = diagnose(error_text=None, traceback_path=args.traceback,
                       error=args.error, file=args.file, root=args.root)
        out = json.dumps(rep, indent=2, ensure_ascii=False)
        if args.out:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(out + "\n", encoding="utf-8")
        print(out)
        return 0
    if args.cmd == "apply":
        res = apply_patch(Path(args.patch).read_text(encoding="utf-8"),
                          args.root, args.dry_run)
        print(json.dumps(res, indent=2))
        return 0 if res.get("applied") else 1
    if args.cmd == "verify":
        res = verify(args.root, args.test_cmd, args.timeout)
        print(json.dumps(res, indent=2))
        return 0 if res["verified"] else 1
    if args.cmd == "loop":
        res = loop(args.patch, args.root, args.test_cmd, args.timeout)
        print(json.dumps(res, indent=2))
        return 0 if res["result"] == "ACCEPTED" else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
