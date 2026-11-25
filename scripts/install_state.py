#!/usr/bin/env python3
"""Setu install ledger.

Records *exactly* which files an install wrote, so `uninstall` can remove those
files and nothing else. The ledger lives outside the plugin directory so an
uninstall still works after the source tree is deleted.

    ~/.setu/install-ledger.json

Schema:
    {
      "version": 1,
      "installs": [
        {
          "target": "claude",
          "root": "/home/you/.claude",
          "domains": ["cyber", "webdev"],
          "installed_at": "2026-11-22T10:30:00Z",
          "files": ["/home/you/.claude/skills/...", "..."],
          "shared_files": ["/your/project/AGENTS.md"],
          "status": "installed"          # or "removed"
        }
      ]

``files`` are OURS: created by the installer, safe to unlink on uninstall.
``shared_files`` are files we EDITED (AGENTS.md), which existed for their own
reasons before Setu arrived. Uninstalling must remove only the managed block
between the Setu markers and leave every other byte alone — deleting a user's
AGENTS.md would be data loss caused by an uninstall, which is the worst possible
bug for an install script to have. A ``.setu-bak`` taken before the edit is
preferred when present, since that is the exact pre-Setu content.
    }

CLI (called from install.sh / install.ps1):
    python3 scripts/install_state.py record   --target claude --root ~/.claude \
            --domains cyber,webdev --manifest /tmp/setu-manifest.txt
    python3 scripts/install_state.py list
    python3 scripts/install_state.py uninstall --target claude
    python3 scripts/install_state.py uninstall --all --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

BACKUP_SUFFIX = ".setu-bak"
LEDGER = Path(os.environ.get("SETU_LEDGER", Path.home() / ".setu" / "install-ledger.json"))


def _load() -> dict:
    if not LEDGER.exists():
        return {"version": 1, "installs": []}
    try:
        data = json.loads(LEDGER.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # A corrupt ledger must never block an uninstall; start clean.
        data = {"version": 1, "installs": []}
    data.setdefault("version", 1)
    data.setdefault("installs", [])
    return data


def _save(data: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(LEDGER)


def _read_manifest(path: str | None, extra: list[str]) -> list[str]:
    files: list[str] = []
    if path:
        p = Path(path).expanduser()
        if p.exists():
            files += [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    files += extra
    # de-dupe, keep order, store absolute
    seen, out = set(), []
    for f in files:
        a = str(Path(f).expanduser().resolve())
        if a not in seen:
            seen.add(a)
            out.append(a)
    return out


def cmd_record(args: argparse.Namespace) -> int:
    data = _load()
    files = _read_manifest(args.manifest, args.file or [])
    shared = [str(Path(f).expanduser().resolve()) for f in (args.shared or [])]
    if not files:
        print("install_state: no files recorded (empty manifest)", file=sys.stderr)
        return 1
    entry = {
        "target": args.target,
        "root": str(Path(args.root).expanduser().resolve()) if args.root else None,
        "domains": [d.strip() for d in (args.domains or "").split(",") if d.strip()],
        "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "files": files,
        "shared_files": shared,
        "status": "installed",
    }
    data["installs"].append(entry)
    _save(data)
    print(f"recorded install: target={entry['target']} files={len(files)} ledger={LEDGER}")
    return 0


SETU_BEGIN = "<!-- BEGIN SETU (managed block"
SETU_END = "<!-- END SETU -->"


def _strip_managed_block(path: Path, dry_run: bool) -> str:
    """Remove only the Setu-managed block from a shared file. Returns an action log."""
    bak = Path(str(path) + ".setu-bak")
    if bak.exists():
        if dry_run:
            return f"would restore {path} from {bak.name}"
        path.write_text(bak.read_text(encoding="utf-8"), encoding="utf-8")
        bak.unlink()
        return f"restored {path} from backup"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return f"skip {path}: {e}"
    if SETU_BEGIN not in text:
        return f"left {path} alone (no Setu block found)"
    start = text.index(SETU_BEGIN)
    try:
        stop = text.index(SETU_END, start) + len(SETU_END)
    except ValueError:
        return f"left {path} alone (unterminated block; not guessing)"
    new_text = (text[:start].rstrip() + "\n" + text[stop:].lstrip()).strip()
    if dry_run:
        return f"would strip the Setu block from {path}"
    if not new_text:
        path.unlink()
        return f"deleted {path} (it contained only the Setu block)"
    path.write_text(new_text + "\n", encoding="utf-8")
    return f"stripped the Setu block from {path} (your own content kept)"


def _uninstall_entries(entries: list[dict], dry_run: bool) -> int:
    removed, missing = 0, 0
    for entry in entries:
        for shared in entry.get("shared_files", []) or []:
            p = Path(shared)
            if p.exists():
                print("  " + _strip_managed_block(p, dry_run))
            else:
                print(f"  shared file already gone: {p}")
    restores: list[tuple[Path, Path]] = []
    for entry in entries:
        for f in entry.get("files", []):
            p = Path(f)
            # A .setu-bak is the user's own file, moved aside on install. It is restored
            # after the Setu copy is removed, never deleted.
            if f.endswith(BACKUP_SUFFIX):
                restores.append((p, Path(f[:-len(BACKUP_SUFFIX)])))
                continue
            if p.exists() or p.is_symlink():
                if dry_run:
                    print(f"would remove {p}")
                else:
                    try:
                        p.unlink()
                    except OSError as e:
                        print(f"skip {p}: {e}", file=sys.stderr)
                        continue
                    removed += 1
            else:
                missing += 1
                if dry_run:
                    print(f"already gone {p}")
    for bak, original in restores:
        if not bak.exists():
            missing += 1
            continue
        if dry_run:
            print(f"would restore {original} from {bak.name}")
            continue
        try:
            os.replace(bak, original)
        except OSError as e:
            print(f"skip restore {original}: {e}", file=sys.stderr)
            continue
        print(f"restored {original}")
        removed += 1
    for entry in entries:
        # prune now-empty parent dirs, stopping at the recorded root
        if not dry_run:
            root = Path(entry["root"]).resolve() if entry.get("root") else None
            for f in entry.get("files", []):
                d = Path(f).parent
                while d.exists() and (root is None or (root in d.parents or d == root)):
                    try:
                        d.rmdir()
                    except OSError:
                        break
                    if root is not None and d == root:
                        break
                    d = d.parent
        if not dry_run:
            entry["status"] = "removed"
            entry["removed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"{'would remove' if dry_run else 'removed'} {removed if not dry_run else len([1 for e in entries for _ in e.get('files', [])])} file(s); {missing} already absent")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    data = _load()
    live = [e for e in data["installs"] if e.get("status") == "installed"]
    if args.all:
        targets = live
    else:
        targets = [e for e in live if e.get("target") == args.target]
        if not targets:
            print(f"nothing installed for target '{args.target}'", file=sys.stderr)
            return 1
    rc = _uninstall_entries(targets, args.dry_run)
    if not args.dry_run:
        _save(data)
    return rc


def cmd_list(_: argparse.Namespace) -> int:
    data = _load()
    if not data["installs"]:
        print("no installs recorded")
        return 0
    for i, e in enumerate(data["installs"]):
        print(f"[{i}] {e.get('status'):9s} target={e.get('target')} "
              f"domains={','.join(e.get('domains') or ['all'])} "
              f"files={len(e.get('files', []))} at={e.get('installed_at')}")
        print(f"     root={e.get('root')}  ledger={LEDGER}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="install_state")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("record", help="record what an install wrote")
    r.add_argument("--target", required=True)
    r.add_argument("--root", default=None)
    r.add_argument("--domains", default="")
    r.add_argument("--manifest", default=None, help="file containing one absolute path per line")
    r.add_argument("--file", action="append", default=[])
    r.add_argument("--shared", action="append", default=[],
                   help="file we edited in place (not owned) — uninstall strips only our block")
    r.set_defaults(func=cmd_record)

    u = sub.add_parser("uninstall", help="remove exactly what was installed")
    u.add_argument("--target", default=None)
    u.add_argument("--all", action="store_true")
    u.add_argument("--dry-run", action="store_true")
    u.set_defaults(func=cmd_uninstall)

    l = sub.add_parser("list", help="show recorded installs")
    l.set_defaults(func=cmd_list)

    args = ap.parse_args(argv)
    if args.cmd == "uninstall" and not args.target and not args.all:
        u.error("pass --target <name> or --all")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
