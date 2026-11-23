#!/usr/bin/env python3
"""Setu shared memory layer.

The "self-learning" mechanism is deliberately cheap: no fine-tuning, no vector
database, no daemon. Every operation is read-file → compute → write-file. There
is no in-memory state kept between calls, so the process can be a fresh
interpreter every time (low-memory requirement satisfied by construction).

Storage: one append-only-ish JSON file, default `~/.setu/memory.json`
(override with SETU_MEMORY). Shape:

    {
      "version": 1,
      "entries": [
        {
          "id": "8f3c1a2b",
          "ts": "2026-11-22T10:31:02+00:00",
          "pack": "cyber",
          "input_summary": "enumerate subdomains for example.com",
          "output_summary": "12 findings, 3 high severity, report emitted",
          "was_helpful": true,
          "tokens": {"input": ["enumerate","subdomains","example","com"]}
        }
      ]
    }

Relevance for few-shot retrieval is a dependency-free hybrid:
  lexical overlap (IDF-weighted)  x  recency decay  x  helpfulness prior

Public API:
    log_feedback(pack, input_summary, output_summary, was_helpful) -> dict
    get_few_shot_examples(pack, n=3, query=None) -> list[dict]
    usage_stats() -> dict
    pack_score(pack) -> dict

CLI:
    python3 memory.py log  --pack cyber --input "..." --output "..." --helpful true
    python3 memory.py few-shot --pack cyber --n 3 [--query "..."] [--json]
    python3 memory.py scores
    python3 memory.py stats
    python3 memory.py prune --keep 500
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

MEMORY_PATH = Path(os.environ.get("SETU_MEMORY", Path.home() / ".setu" / "memory.json"))
MAX_ENTRIES = int(os.environ.get("SETU_MEMORY_MAX", "2000"))
RECENCY_HALF_LIFE_DAYS = 45.0

_STOP = {
    "the", "a", "an", "and", "or", "of", "to", "for", "in", "on", "at", "is",
    "it", "this", "that", "with", "my", "me", "i", "we", "you", "please",
    "can", "could", "would", "should", "do", "does", "how", "what", "why",
}


# --------------------------------------------------------------------------- #
# storage — stateless read/modify/write
# --------------------------------------------------------------------------- #
def _load() -> dict:
    """Read the whole store. Missing/corrupt file => empty store, never an exception."""
    if not MEMORY_PATH.exists():
        return {"version": 1, "entries": []}
    try:
        data = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "entries": []}
    if not isinstance(data, dict):
        return {"version": 1, "entries": []}
    data.setdefault("version", 1)
    if not isinstance(data.get("entries"), list):
        data["entries"] = []
    return data


def _save(data: dict) -> None:
    """Atomic write: temp file in the same dir, then rename. Survives a crash mid-write."""
    MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    entries = data.get("entries", [])
    if len(entries) > MAX_ENTRIES:
        # keep the most recent MAX_ENTRIES; the log is a hint cache, not an archive
        data["entries"] = entries[-MAX_ENTRIES:]
    fd, tmp = tempfile.mkstemp(dir=str(MEMORY_PATH.parent), prefix=".memory.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, MEMORY_PATH)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# --------------------------------------------------------------------------- #
# tokenising + relevance
# --------------------------------------------------------------------------- #
def _tokens(text: str) -> list[str]:
    raw = re.findall(r"[a-z0-9]+", (text or "").lower())
    return [t for t in raw if len(t) > 1 and t not in _STOP]


def _idf(entries: list[dict]) -> dict[str, float]:
    """Inverse document frequency over the corpus, so 'example' beats 'site'."""
    df: dict[str, int] = {}
    for e in entries:
        for t in set(e.get("tokens", {}).get("input", [])):
            df[t] = df.get(t, 0) + 1
    n = max(len(entries), 1)
    return {t: math.log((n + 1) / (c + 0.5)) for t, c in df.items()}


def _relevance(entry: dict, query_tokens: set[str], idf: dict[str, float]) -> float:
    """Cosine-ish overlap between query and entry input tokens, IDF weighted."""
    et = set(entry.get("tokens", {}).get("input", []))
    if not query_tokens or not et:
        return 0.0
    inter = query_tokens & et
    num = sum(idf.get(t, 1.0) for t in inter)
    denom = math.sqrt(sum(idf.get(t, 1.0) ** 2 for t in query_tokens) *
                      sum(idf.get(t, 1.0) ** 2 for t in et)) or 1.0
    return num / denom


def _recency(entry: dict, now: datetime | None = None) -> float:
    """Exponential decay, half-life RECENCY_HALF_LIFE_DAYS."""
    now = now or datetime.now(timezone.utc)
    try:
        ts = datetime.fromisoformat(entry.get("ts", ""))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except ValueError:
        return 0.5
    age_days = max((now - ts).total_seconds() / 86400.0, 0.0)
    return 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def log_feedback(pack: str, input_summary: str, output_summary: str,
                 was_helpful: bool, meta: dict | None = None) -> dict:
    """Append one feedback record. Returns the record as stored.

    Stateless: loads the file, appends, writes, returns. No globals touched.
    """
    if not pack:
        raise ValueError("log_feedback: pack is required")
    entry = {
        "id": hashlib.sha1(
            f"{time.time_ns()}{pack}{input_summary}".encode()
        ).hexdigest()[:8],
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pack": pack,
        "input_summary": (input_summary or "").strip()[:500],
        "output_summary": (output_summary or "").strip()[:1000],
        "was_helpful": bool(was_helpful),
        "tokens": {"input": _tokens(input_summary)},
    }
    if meta:
        entry["meta"] = meta
    data = _load()
    data["entries"].append(entry)
    _save(data)
    return entry


def get_few_shot_examples(pack: str, n: int = 3, query: str | None = None,
                          include_unhelpful: bool = False) -> list[dict]:
    """Return the n most relevant past *helpful* examples for `pack`.

    Relevance = IDF-weighted overlap x recency decay x helpfulness prior.
    If `query` is None, ranking is recency x helpfulness only.

    These are meant to be injected as few-shot context:
        shots = get_few_shot_examples("cyber", 3, query=request)
        context = "\\n\\n".join(
            f"INPUT: {s['input_summary']}\\nGOOD OUTPUT: {s['output_summary']}" for s in shots)

    Unhelpful records are excluded by default — we do not teach the model to
    repeat a failure. Set include_unhelpful=True to use them as negative
    examples instead.
    """
    data = _load()
    pool = [e for e in data["entries"]
            if e.get("pack") == pack and (include_unhelpful or e.get("was_helpful"))]
    if not pool:
        return []
    idf = _idf(data["entries"])
    qt = set(_tokens(query)) if query else set()
    now = datetime.now(timezone.utc)

    scored = []
    for e in pool:
        rel = _relevance(e, qt, idf) if qt else 0.0
        prior = 1.0 if e.get("was_helpful") else 0.35
        # When a query is given, relevance dominates; without one, recency does.
        score = (0.65 * min(rel, 1.0) + 0.35 * _recency(e, now)) * prior if qt else _recency(e, now) * prior
        scored.append((score, e))
    scored.sort(key=lambda p: p[0], reverse=True)
    # a fixed seed per (pack, query) keeps ordering deterministic for same scores
    out = []
    for score, e in scored[:max(int(n), 0)]:
        out.append({
            "id": e.get("id"),
            "ts": e.get("ts"),
            "input_summary": e.get("input_summary"),
            "output_summary": e.get("output_summary"),
            "was_helpful": e.get("was_helpful"),
            "score": round(score, 4),
        })
    return out


def pack_score(pack: str) -> dict:
    """Helpfulness stats for one pack."""
    entries = [e for e in _load()["entries"] if e.get("pack") == pack]
    total = len(entries)
    helpful = sum(1 for e in entries if e.get("was_helpful"))
    return {
        "pack": pack,
        "samples": total,
        "helpful": helpful,
        "rate": round(helpful / total, 3) if total else None,
        "last_seen": entries[-1]["ts"] if entries else None,
        "warn": bool(total >= 3 and helpful / total < 0.5),
    }


def usage_stats() -> dict:
    """Per-pack usage + helpfulness, plus the overall totals. This is the
    'per-pack usage stats' view the orchestrator reads before routing."""
    data = _load()
    entries = data["entries"]
    packs: dict[str, dict] = {}
    for e in entries:
        p = e.get("pack", "unknown")
        s = packs.setdefault(p, {"samples": 0, "helpful": 0, "last_seen": None})
        s["samples"] += 1
        s["helpful"] += 1 if e.get("was_helpful") else 0
        s["last_seen"] = e.get("ts")
    for s in packs.values():
        s["rate"] = round(s["helpful"] / s["samples"], 3) if s["samples"] else None
    return {
        "memory_path": str(MEMORY_PATH),
        "entries": len(entries),
        "packs": dict(sorted(packs.items())),
        "overall_rate": round(sum(1 for e in entries if e.get("was_helpful")) / len(entries), 3)
        if entries else None,
    }


def prune(keep: int = 500) -> int:
    """Keep only the newest `keep` entries. Returns how many were dropped."""
    data = _load()
    before = len(data["entries"])
    if before <= keep:
        return 0
    data["entries"] = data["entries"][-keep:]
    _save(data)
    return before - keep


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _as_bool(v: str) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "y", "helpful", "up"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="memory.py", description="Setu shared memory layer")
    ap.add_argument("--memory", default=None, help="override memory file path")
    sub = ap.add_subparsers(dest="cmd", required=True)

    lg = sub.add_parser("log", help="record feedback")
    lg.add_argument("--pack", required=True)
    lg.add_argument("--input", default="", dest="inp")
    lg.add_argument("--output", default="", dest="out")
    lg.add_argument("--helpful", default="true")

    fs = sub.add_parser("few-shot", help="retrieve few-shot examples")
    fs.add_argument("--pack", required=True)
    fs.add_argument("--n", type=int, default=3)
    fs.add_argument("--query", default=None)
    fs.add_argument("--include-unhelpful", action="store_true")
    fs.add_argument("--json", action="store_true")

    sc = sub.add_parser("scores", help="per-pack helpfulness")
    sc.add_argument("--json", action="store_true")

    st = sub.add_parser("stats", help="full usage stats")
    pr = sub.add_parser("prune", help="trim the log")
    pr.add_argument("--keep", type=int, default=500)

    args = ap.parse_args(argv)
    if args.memory:
        global MEMORY_PATH
        MEMORY_PATH = Path(args.memory).expanduser()

    if args.cmd == "log":
        e = log_feedback(args.pack, args.inp, args.out, _as_bool(args.helpful))
        print(f"logged {e['id']} pack={e['pack']} helpful={e['was_helpful']}")
    elif args.cmd == "few-shot":
        shots = get_few_shot_examples(args.pack, args.n, args.query, args.include_unhelpful)
        if args.json:
            print(json.dumps(shots, indent=2))
        elif not shots:
            print(f"(no examples yet for pack '{args.pack}')")
        else:
            for s in shots:
                print(f"- [{s['score']}] {s['input_summary']}\n    -> {s['output_summary'][:160]}")
    elif args.cmd == "scores":
        stats = usage_stats()
        if args.json:
            print(json.dumps(stats, indent=2))
        else:
            for p, s in stats["packs"].items():
                flag = "  <-- weak" if (s["samples"] >= 3 and (s["rate"] or 0) < 0.5) else ""
                print(f"{p:10s} samples={s['samples']:<4d} rate={s['rate']}{flag}")
            if not stats["packs"]:
                print("(memory is empty)")
    elif args.cmd == "stats":
        print(json.dumps(usage_stats(), indent=2))
    elif args.cmd == "prune":
        print(f"pruned {prune(args.keep)} entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
