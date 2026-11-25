from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _agent(name):
    return (ROOT / "agents" / f"{name}.md").read_text(encoding="utf-8").lower()


def test_recon_agents_define_scope_and_passive_vs_active_boundary():
    for name in ("cyber-subdomain-enum", "cyber-endpoint-discovery"):
        text = _agent(name)
        assert "## scope and completion" in text
        assert "authorization" in text
        assert "## guardrails" in text
        assert "## done when" in text
        assert "skipped" in text


def test_builder_defines_prompt_diversity_and_deterministic_critic_limit():
    text = _agent("webdev-builder")
    assert "stress-test expectations" in text
    assert "at least four" in text
    assert "not as evidence that an llm built" in text
    assert "never exceed two retries" in text
