"""SSOT contract tests: ``MOLECULE_RESOLVED_PROVIDER`` is the TOP-PRECEDENCE
explicit provider for the codex adapter.

The workspace provisioner resolves the LLM provider ONCE (core Go
``manifest.DeriveProvider``) and publishes the resolved registry arm name in the
single env var ``MOLECULE_RESOLVED_PROVIDER``. Every downstream layer READS it,
never re-derives. For codex this means: when the var is set it wins over
``LLM_PROVIDER``/``MODEL_PROVIDER`` and the model-derived subscription
auto-detection, and the adapter selects exactly that registry arm (proxy
config.toml for ``platform``; the built-in / vendor path for a byok arm). When
the var is absent the adapter falls back to the legacy resolution (back-compat).

These drive the real ``CodexAdapter.setup()`` provider-resolution + config.toml
render. The heavy post-resolution steps (plugin install, system-prompt build)
are stubbed so the test isolates the provider decision — they run AFTER the
config.toml is written and are exercised by other suites.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

pytest.importorskip("molecule_runtime.adapters.base")


@pytest.fixture()
def _setup_harness(monkeypatch, tmp_path):
    """Return a callable that runs ``CodexAdapter.setup()`` with the plugin /
    prompt steps stubbed and returns the rendered ``~/.codex/config.toml`` text
    (empty string when no override was written — the built-in OpenAI path)."""
    from adapter import CodexAdapter
    from molecule_runtime.adapters.base import AdapterConfig
    import molecule_runtime.plugins as mr_plugins
    import molecule_runtime.prompt as mr_prompt

    # Stub the post-resolution steps so setup() completes on the provider
    # decision alone (these run AFTER config.toml is written).
    async def _noop_install(self, config, plugins):
        return None

    class _Plugins:
        rules = None
        prompt_fragments = []

    monkeypatch.setattr(CodexAdapter, "install_plugins_via_registry", _noop_install)
    monkeypatch.setattr(mr_plugins, "load_plugins", lambda **kw: _Plugins())
    monkeypatch.setattr(mr_prompt, "build_system_prompt", lambda *a, **k: "")

    codex_home = tmp_path / ".codex"

    async def _run(env: dict, model: str = "gpt-5.4"):
        # Credential preflight is provider-independent; satisfy it.
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        for k in ("MOLECULE_RESOLVED_PROVIDER", "LLM_PROVIDER", "MODEL_PROVIDER"):
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        adapter = CodexAdapter()
        await adapter.setup(AdapterConfig(
            model=model,
            runtime_config={"model": model},
            config_path=str(_ROOT),  # load the real config.yaml (has platform arm)
        ))
        toml = codex_home / "config.toml"
        return toml.read_text() if toml.exists() else ""

    return _run


@pytest.mark.asyncio
async def test_resolved_provider_platform_selects_platform_arm(_setup_harness):
    """MOLECULE_RESOLVED_PROVIDER=platform selects the platform proxy arm —
    config.toml pins ``model_provider = "platform"`` + the proxy base_url."""
    body = await _setup_harness({"MOLECULE_RESOLVED_PROVIDER": "platform"})
    assert 'model_provider = "platform"' in body
    assert "[model_providers.platform]" in body


@pytest.mark.asyncio
async def test_resolved_provider_beats_legacy_llm_provider(_setup_harness):
    """TOP PRECEDENCE: MOLECULE_RESOLVED_PROVIDER=platform wins even when the
    legacy LLM_PROVIDER names a different (byok) arm."""
    body = await _setup_harness({
        "MOLECULE_RESOLVED_PROVIDER": "platform",
        "LLM_PROVIDER": "openai-api",
        "MODEL_PROVIDER": "openai-api",
    })
    assert 'model_provider = "platform"' in body


@pytest.mark.asyncio
async def test_resolved_provider_byok_arm_selected_by_name(_setup_harness):
    """A byok resolved arm (openai-api, a CLI built-in OpenAI path) writes NO
    model_provider override — selected by name, not re-derived from the model.
    The platform proxy block must NOT appear."""
    body = await _setup_harness({"MOLECULE_RESOLVED_PROVIDER": "openai-api"})
    assert 'model_provider = "platform"' not in body
    assert "[model_providers.platform]" not in body


@pytest.mark.asyncio
async def test_absent_signal_falls_back_to_legacy_llm_provider(_setup_harness):
    """Back-compat: with MOLECULE_RESOLVED_PROVIDER absent, the legacy
    LLM_PROVIDER=platform still selects the platform arm."""
    body = await _setup_harness({"LLM_PROVIDER": "platform"})
    assert 'model_provider = "platform"' in body
