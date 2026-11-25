#!/usr/bin/env python3
"""Setu model wrapper — one function, many backends.

    from model_wrapper import call_model
    text = call_model("summarise these findings", model="claude")

Backends are selected by a single config variable, ``SETU_MODEL``:

    claude | claude-haiku | claude-sonnet | claude-opus
    gpt    | gpt-mini   | gpt-4o
    ollama | ollama:<model-name>      # e.g. ollama:qwen2.5-coder:7b

Precedence for choosing a backend, highest first:
    1. explicit ``model=`` argument
    2. ``SETU_MODEL`` environment variable
    3. ``"claude"``

Design constraints this file deliberately honours:

* **No hard dependency.** Anthropic / OpenAI SDKs are imported lazily inside the
  backend. If a key or package is missing you get a clear ``ModelConfigError``,
  not an ImportError at import time — so `--target cursor` installs still work
  on a machine with no SDKs at all.
* **Deterministic offline fallback.** ``SETU_OFFLINE=1`` (or no credentials at
  all) routes to a local extractive stub so every script in Setu can be
  exercised end-to-end in CI without network or spend. The stub is clearly
  labelled ``[offline-stub]`` in its output — it never pretends to be a model.
* **Never generate numbers.** Setu's finance pack needs the LLM kept away from
  arithmetic; ``call_model`` therefore refuses prompts that look like a request
  to compute a number unless ``allow_math=True`` is passed. (Cheap guardrail,
  documented in finance-emi-calc.)

CLI:
    python3 model_wrapper.py --prompt "hello" --model ollama:llama3
    python3 model_wrapper.py --selftest
"""
from __future__ import annotations

import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
DEFAULT_MODEL = "claude"

# name -> (backend, concrete model id, default max tokens)
MODEL_ALIASES: dict[str, tuple[str, str, int]] = {
    # Claude
    "claude":        ("anthropic", "claude-sonnet-4-20250514", 4096),
    "claude-haiku":  ("anthropic", "claude-haiku-4-5-20251001", 2048),
    "claude-sonnet": ("anthropic", "claude-sonnet-4-20250514", 4096),
    "claude-opus":   ("anthropic", "claude-opus-4-20250514", 8192),
    # OpenAI
    "gpt":           ("openai", "gpt-4o", 4096),
    "gpt-4o":        ("openai", "gpt-4o", 4096),
    "gpt-mini":      ("openai", "gpt-4o-mini", 2048),
    # Local
    "ollama":        ("ollama", "", 4096),
    "local":         ("ollama", "", 4096),
}

# Agent frontmatter uses bare model tiers (haiku/opus/sonnet). Map them, so an
# agent file's `model: haiku` works whether the team runs Claude, GPT or Ollama.
TIER_MAP = {
    "haiku": "claude-haiku", "small": "claude-haiku", "fast": "claude-haiku",
    "sonnet": "claude-sonnet", "medium": "claude-sonnet", "default": "claude-sonnet",
    "opus": "claude-opus", "large": "claude-opus", "deep": "claude-opus",
}


class ModelConfigError(RuntimeError):
    """Raised when the requested backend cannot be used on this machine."""


class ModelCallError(RuntimeError):
    """Raised when the backend was reachable but the call failed."""


@dataclass
class ModelResponse:
    text: str
    model: str
    backend: str
    usage: dict = field(default_factory=dict)
    fallback: bool = False

    def __str__(self) -> str:      # so callers can treat it as a string
        return self.text

    def as_dict(self) -> dict:
        return {"text": self.text, "model": self.model, "backend": self.backend,
                "usage": self.usage, "fallback": self.fallback}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def resolve_model(model: str | None = None) -> tuple[str, str, str, int]:
    """(alias, backend, concrete_model, max_tokens)"""
    name = (model or os.environ.get("SETU_MODEL") or DEFAULT_MODEL).strip()
    tier = TIER_MAP.get(name.lower())
    if tier:
        name = tier
    if name.lower() in MODEL_ALIASES:
        backend, concrete, mt = MODEL_ALIASES[name.lower()]
        return name, backend, concrete, mt
    # ollama:<model>
    if ":" in name:
        head, _, tail = name.partition(":")
        if head.lower() in {"ollama", "local"}:
            return name, "ollama", tail or os.environ.get("SETU_OLLAMA_MODEL", "llama3"), 4096
    # Unknown name: treat as a raw pass-through for the default backend family
    family = os.environ.get("SETU_MODEL_FAMILY", "anthropic").lower()
    return name, ("openai" if family.startswith("open") else "anthropic"), name, 4096


# Refuse explicit requests to derive or compute values, not prose that merely
# mentions EMI, rates, payments, or already-computed amounts. Keep explanatory
# output available to finance.explain_emi and other normal chat workflows.
_MATH_HINT = re.compile(
    r"\b(calculate|compute|solve|derive|work\s+out|how\s+much|what\s+is\s+\d)\b|"
    r"\d+\s*[+\-*/^%]\s*\d+", re.I)


def _looks_like_math(prompt: str) -> bool:
    return bool(_MATH_HINT.search(prompt or ""))


def _timeout() -> float:
    try:
        return float(os.environ.get("SETU_TIMEOUT", "120"))
    except ValueError:
        return 120.0


# --------------------------------------------------------------------------- #
# backends
# --------------------------------------------------------------------------- #
def _call_anthropic(prompt: str, concrete: str, max_tokens: int,
                    system: str | None) -> ModelResponse:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise ModelConfigError("ANTHROPIC_API_KEY is not set")
    base = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    body: dict[str, Any] = {
        "model": concrete, "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        body["system"] = system
    req = urllib.request.Request(
        f"{base.rstrip('/')}/v1/messages",
        data=json.dumps(body).encode(),
        headers={
            "content-type": "application/json",
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_timeout(),
                                    context=ssl.create_default_context()) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise ModelCallError(f"anthropic HTTP {e.code}: {e.read()[:400].decode(errors='replace')}")
    except (urllib.error.URLError, TimeoutError) as e:
        raise ModelCallError(f"anthropic unreachable: {e}")
    text = "".join(b.get("text", "") for b in data.get("content", []) if isinstance(b, dict))
    return ModelResponse(text=text, model=concrete, backend="anthropic",
                         usage=data.get("usage", {}))


def _call_openai(prompt: str, concrete: str, max_tokens: int,
                 system: str | None) -> ModelResponse:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise ModelConfigError("OPENAI_API_KEY is not set")
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    msgs = ([{"role": "system", "content": system}] if system else []) + \
           [{"role": "user", "content": prompt}]
    body = {"model": concrete, "messages": msgs, "max_tokens": max_tokens}
    req = urllib.request.Request(
        f"{base.rstrip('/')}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json", "authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_timeout()) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise ModelCallError(f"openai HTTP {e.code}: {e.read()[:400].decode(errors='replace')}")
    except (urllib.error.URLError, TimeoutError) as e:
        raise ModelCallError(f"openai unreachable: {e}")
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        raise ModelCallError(f"openai unexpected shape: {json.dumps(data)[:300]}")
    return ModelResponse(text=text, model=concrete, backend="openai",
                         usage=data.get("usage", {}))


def _call_ollama(prompt: str, concrete: str, max_tokens: int,
                 system: str | None) -> ModelResponse:
    base = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
    if not base.startswith("http"):
        base = f"http://{base}"
    model = concrete or os.environ.get("SETU_OLLAMA_MODEL", "llama3")
    body = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"num_predict": max_tokens},
    }
    if system:
        body["system"] = system
    req = urllib.request.Request(
        f"{base.rstrip('/')}/api/generate",
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_timeout()) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise ModelCallError(f"ollama HTTP {e.code} (is the model pulled?): "
                             f"{e.read()[:300].decode(errors='replace')}")
    except (urllib.error.URLError, TimeoutError) as e:
        raise ModelConfigError(f"ollama unreachable at {base}: {e}")
    return ModelResponse(text=data.get("response", ""), model=model, backend="ollama",
                         usage={"eval_count": data.get("eval_count")})


def _offline_stub(prompt: str, model_name: str) -> ModelResponse:
    """Extractive, deterministic, obviously-not-a-model output.

    The point is that every script in Setu runs in CI with zero credentials.
    It echoes the task and the first keywords it finds, tagged so no human or
    downstream parser can mistake it for a real answer.
    """
    words = re.findall(r"[A-Za-z]{4,}", prompt or "")
    seen, keys = set(), []
    for w in words:
        lw = w.lower()
        if lw not in seen:
            seen.add(lw)
            keys.append(lw)
        if len(keys) >= 8:
            break
    head = (prompt or "").strip().splitlines()
    first = head[0][:160] if head else ""
    return ModelResponse(
        text=("[offline-stub] SETU_OFFLINE is set or no credentials were found. "
              f"task={first!r} keys={','.join(keys) or 'none'}"),
        model=model_name, backend="offline", usage={}, fallback=True,
    )


_BACKENDS = {"anthropic": _call_anthropic, "openai": _call_openai, "ollama": _call_ollama}


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def call_model(prompt: str, model: str | None = None, *, system: str | None = None,
               max_tokens: int | None = None, allow_math: bool = False,
               temperature: float | None = None,
               fallback: bool = True) -> str:
    """Send `prompt` to whichever backend `model`/SETU_MODEL selects.

    Returns the model's text. Raises ModelConfigError if the backend is not
    usable and `fallback=False`; with `fallback=True` (default) it degrades to
    the offline stub so a whole pipeline can still be dry-run.

    `allow_math=False` (default) rejects prompts that ask the model to compute a
    number. Deterministic arithmetic belongs in Python (see finance-emi-calc).
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("call_model: prompt must be a non-empty string")
    if not allow_math and _looks_like_math(prompt):
        raise ValueError(
            "call_model: refusing a math-shaped prompt. Compute the number in "
            "Python and pass the result in; set allow_math=True only for "
            "explanatory prose about a number you already computed."
        )

    alias, backend, concrete, default_mt = resolve_model(model)
    mt = max_tokens or default_mt

    if os.environ.get("SETU_OFFLINE") in {"1", "true", "TRUE", "yes"}:
        return _offline_stub(prompt, alias).text

    fn = _BACKENDS.get(backend)
    if fn is None:
        raise ModelConfigError(f"unknown backend '{backend}' for model '{alias}'")
    try:
        return fn(prompt, concrete, mt, system).text
    except ModelConfigError:
        if fallback:
            return _offline_stub(prompt, alias).text
        raise
    except ModelCallError:
        raise


def call_model_full(prompt: str, model: str | None = None, **kw) -> ModelResponse:
    """Same as call_model but returns the ModelResponse (usage, backend, fallback)."""
    kw.setdefault("fallback", True)
    text = call_model(prompt, model, **kw)
    alias, backend, concrete, _ = resolve_model(model)
    return ModelResponse(text=text, model=concrete or alias, backend=backend,
                         fallback=text.startswith("[offline-stub]"))


def available_backends() -> dict[str, bool]:
    """Which backends look usable right now — useful for `setu doctor`."""
    return {
        "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "openai": bool(os.environ.get("OPENAI_API_KEY")),
        "ollama": _ollama_alive(),
        "offline": True,
    }


def _ollama_alive() -> bool:
    base = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
    if not base.startswith("http"):
        base = f"http://{base}"
    try:
        with urllib.request.urlopen(f"{base.rstrip('/')}/api/tags", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def _selftest() -> int:
    print("resolved:", resolve_model())
    print("from tier 'haiku':", resolve_model("haiku"))
    print("from tier 'opus':", resolve_model("opus"))
    print("ollama pin:", resolve_model("ollama:qwen2.5-coder:7b"))
    print("env SETU_MODEL =", os.environ.get("SETU_MODEL", "(unset)"))
    print("backends:", json.dumps(available_backends()))
    out = call_model("Write one sentence about bridges.", model="offline")
    print("sample call ->", out[:120])
    try:
        call_model("calculate the EMI for 500000 at 9% for 60 months")
        print("MATH GUARD: FAILED (should have raised)")
        return 1
    except ValueError as e:
        print("math guard: OK ->", str(e)[:80])
    return 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(prog="model_wrapper.py")
    ap.add_argument("--prompt")
    ap.add_argument("--model", default=None)
    ap.add_argument("--system", default=None)
    ap.add_argument("--max-tokens", type=int, default=None)
    ap.add_argument("--allow-math", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        raise SystemExit(_selftest())
    if not a.prompt:
        ap.error("pass --prompt or --selftest")
    try:
        print(call_model(a.prompt, a.model, system=a.system,
                         max_tokens=a.max_tokens, allow_math=a.allow_math))
    except (ModelConfigError, ModelCallError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        raise SystemExit(2)
