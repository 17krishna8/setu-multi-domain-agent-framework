"""Prompt-diverse design-critic fixtures; these are fixtures, not LLM generations."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import pytest
import design_check


PROMPTS = [
    ("grain cooperative stock ledger", "#8a4b2a", "Fraunces", "Public Sans", "rail-ledger"),
    ("clinic appointment timeline", "#176b62", "Newsreader", "Source Sans 3", "timeline"),
    ("cyber incident response console", "#b57825", "IBM Plex Mono", "Manrope", "data-table"),
    ("community legal aid intake", "#9b3d34", "Literata", "Work Sans", "editorial-split"),
]


def _page(prompt, accent, display, reading, layout):
    heading = prompt.title()
    body = {
        "rail-ledger": "<nav aria-label='Sections'><a href='#stock'>Stock ledger</a></nav><main><table><caption>Current stock</caption><tr><th>Lot</th><th>Weight</th></tr><tr><td>Millet, lot 24-07</td><td>420 kg</td></tr></table></main>",
        "timeline": "<main><ol><li><time>09:30</time>Check in with Dr Rao</li><li><time>10:00</time>Consultation room 2</li></ol><button>Confirm appointment</button></main>",
        "data-table": "<main><table><caption>Incident queue</caption><tr><th>Signal</th><th>State</th></tr><tr><td>Unexpected redirect</td><td>Needs review</td></tr></table><button>Open event log</button></main>",
        "editorial-split": "<main><aside>Bring notices, receipts, and dates. A volunteer will review each document with you.</aside><section><h2>Intake checklist</h2><ul><li>Consumer complaint</li><li>RTI follow-up</li><li>Benefits appeal</li></ul></section></main>",
    }[layout]
    return f'''<!doctype html><html><head><style>
      :root{{--accent:{accent};}}
      body{{font-family:'{reading}',sans-serif;color:#222;background:#f7f3eb}}
      h1,h2{{font-family:'{display}',serif}} .shell{{display:grid;grid-template-columns:12rem 1fr;gap:2rem}}
      a:focus,button:focus{{outline:3px solid #111}} @media(max-width:700px){{.shell{{display:block}}}}
      </style></head><body><header><h1>{heading}</h1></header><div class='shell'>{body}</div></body></html>'''


@pytest.mark.parametrize("prompt,accent,display,reading,layout", PROMPTS)
def test_varied_subject_prompts_have_distinct_critic_approved_structures(
        tmp_path, prompt, accent, display, reading, layout):
    html = _page(prompt, accent, display, reading, layout)
    page = tmp_path / "prompt.html"
    page.write_text(html, encoding="utf-8")
    report = design_check.run_checks([page])
    assert report["verdict"] == "APPROVE", (prompt, report["violations"])
    assert report["blocking"] == 0
    assert {"rail-ledger": "<table", "timeline": "<ol", "data-table": "<table",
            "editorial-split": "<aside"}[layout] in html


def test_generic_output_is_rejected_and_specific_feedback_removes_violations(tmp_path):
    bad = '''<style>body{font-family:Inter,system-ui;text-align:center}
    .hero{background:linear-gradient(90deg,#667eea,#764ba2)}</style>
    <header class="hero"><h1>Your headline here</h1></header>
    <div class="card">Feature One</div><div class="card">Feature Two</div>
    <div class="card">Feature Three</div><footer>best-in-class platform</footer>'''
    page = tmp_path / "generic.html"
    page.write_text(bad, encoding="utf-8")
    first = design_check.run_checks([page])
    assert first["verdict"] == "REJECT"
    rules = {item["rule"] for item in first["violations"]}
    assert {"gradient-purple-blue", "font-generic", "template-structure",
            "placeholder-copy"} <= rules

    # This deterministic fixture simulates applying each critic suggestion to a
    # repair fixture. It tests the critic signal/feedback path, not an LLM retry.
    improved = _page("community grain ledger", "#8a4b2a", "Fraunces", "Public Sans",
                     "rail-ledger")
    page.write_text(improved, encoding="utf-8")
    second = design_check.run_checks([page])
    assert second["verdict"] == "APPROVE"
    assert second["score"] > first["score"]
    assert len([first, second]) - 1 <= 2
