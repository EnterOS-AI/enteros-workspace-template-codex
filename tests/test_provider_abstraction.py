"""Tests for the provider-abstraction layer (provider_config.py).

Pins the contract introduced in feat/multi-provider-abstraction:
the previous hardcoded ``codex_minimax_config.sh`` is replaced by a
YAML-driven registry + dispatch. Adding a new codex-compatible
provider is now a one-entry config.yaml edit instead of a code change.

Five groups:
  1. ``load_providers`` reads ``config.yaml`` and falls back to builtins.
  2. ``resolve_provider`` honors subscription precedence + explicit name
     + credential-aware model-prefix matching.
  3. ``render_config_toml`` emits NOTHING for built-in OpenAI modes
     (the verified prod shape for the subscription / OPENAI_API_KEY
     paths — codex's native provider handles them).
  4. ``render_config_toml`` emits a ``[model_providers.<slug>]`` block
     for ``openai_compat_responses`` with ``wire_api = "responses"``
     (CLI 0.130 contract).
  5. ``write_config_toml`` is idempotent and clears stale
     auto-generated overrides when the picked provider is built-in.

These tests are pure-Python — no codex binary, no subprocess, no
container — so they run cleanly in the same CI lane as the existing
``test_modernization_pr1.py`` group.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# Ensure the template root is importable so the module under test
# imports cleanly without depending on molecule-runtime being
# installed. Mirrors test_modernization_pr1.py's sys.path setup.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture
def pc(monkeypatch):
    """Import ``provider_config`` with all env auth vars cleared.

    Each test starts from a known-empty credential slate so subscription
    auto-detection (which is ENV-driven) doesn't leak between tests.
    """
    for ev in (
        "CODEX_AUTH_JSON", "CODEX_CHATGPT_AUTH_JSON",
        "OPENAI_API_KEY", "MINIMAX_API_KEY", "MODEL_PROVIDER", "MODEL",
    ):
        monkeypatch.delenv(ev, raising=False)
    sys.modules.pop("provider_config", None)
    spec = importlib.util.spec_from_file_location(
        "provider_config", _ROOT / "provider_config.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- Group 1: load_providers ----------------------------------------------

def test_load_providers_reads_shipped_yaml(pc):
    """The shipped config.yaml carries exactly the registry this PR ships."""
    providers = pc.load_providers(workspace_config_path=str(_ROOT))
    names = [p["name"] for p in providers]
    assert "openai-subscription" in names
    assert "openai-api" in names
    assert "minimax-token-plan" in names
    # Subscription MUST appear before openai-api so the subscription
    # auto-detection step finds it first when both creds are present
    # (preserves the verified prod precedence).
    assert names.index("openai-subscription") < names.index("openai-api")


def test_load_providers_falls_back_to_builtins_when_yaml_missing(pc, tmp_path):
    """No YAML anywhere → registry is the built-in pair (subscription + api)."""
    monkey = pytest.MonkeyPatch()
    monkey.setattr(pc, "_TEMPLATE_DIR", tmp_path)
    monkey.setattr(pc, "_CANONICAL_ADAPTER_DIR", tmp_path / "nope")
    try:
        providers = pc.load_providers(workspace_config_path=str(tmp_path))
        names = [p["name"] for p in providers]
        assert names == ["openai-subscription", "openai-api"]
    finally:
        monkey.undo()


def test_load_providers_drops_invalid_entries(pc, tmp_path):
    """A malformed YAML entry doesn't poison the whole registry."""
    yaml_mod = pytest.importorskip("yaml")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml_mod.safe_dump({
        "providers": [
            {"name": "good", "auth_mode": "openai_api",
             "model_prefixes": ["gpt-"], "auth_env": ["OPENAI_API_KEY"]},
            {"auth_mode": "openai_api"},  # missing name — drop
            {"name": "bogus", "auth_mode": "no-such-mode"},  # unknown mode
        ],
    }))
    monkey = pytest.MonkeyPatch()
    monkey.setattr(pc, "_TEMPLATE_DIR", tmp_path)
    monkey.setattr(pc, "_CANONICAL_ADAPTER_DIR", tmp_path / "nope")
    try:
        providers = pc.load_providers(workspace_config_path=str(tmp_path))
        names = [p["name"] for p in providers]
        assert names == ["good"]
    finally:
        monkey.undo()


# --- Group 2: resolve_provider --------------------------------------------

def test_subscription_wins_over_minimax_when_both_set(pc):
    """The headline contract from PR#11 (internal#513): with both
    CODEX_AUTH_JSON and MINIMAX_API_KEY set, subscription wins even if
    the model id matches the MiniMax prefix. This is the prod
    Reviewer/Researcher path."""
    providers = pc.load_providers(workspace_config_path=str(_ROOT))
    env = {
        "CODEX_AUTH_JSON": '{"auth_mode":"chatgpt"}',
        "MINIMAX_API_KEY": "sk-cp-test",
    }
    picked = pc.resolve_provider(
        model="codex-MiniMax-M2.7", providers=providers, env=env,
    )
    assert picked["name"] == "openai-subscription"


def test_alias_credential_also_triggers_subscription(pc):
    """CODEX_CHATGPT_AUTH_JSON (backward-compat alias) also satisfies."""
    providers = pc.load_providers(workspace_config_path=str(_ROOT))
    env = {"CODEX_CHATGPT_AUTH_JSON": '{"auth_mode":"chatgpt"}'}
    picked = pc.resolve_provider(model="gpt-5.5", providers=providers, env=env)
    assert picked["name"] == "openai-subscription"


def test_openai_api_key_routes_to_openai_api_not_subscription(pc):
    """OPENAI_API_KEY-only on a gpt-* model → openai-api (NOT subscription).
    Without this, the subscription provider — which renders NO config
    override — would be picked and codex would auth-fail at first turn
    because auth.json isn't present."""
    providers = pc.load_providers(workspace_config_path=str(_ROOT))
    env = {"OPENAI_API_KEY": "sk-fake-openai"}
    picked = pc.resolve_provider(model="gpt-5.5", providers=providers, env=env)
    assert picked["name"] == "openai-api"


def test_minimax_model_id_routes_to_minimax_provider(pc):
    """A codex-minimax-* model id routes to the MiniMax provider when
    MINIMAX_API_KEY is the only credential present."""
    providers = pc.load_providers(workspace_config_path=str(_ROOT))
    env = {"MINIMAX_API_KEY": "sk-cp-test"}
    picked = pc.resolve_provider(
        model="codex-minimax-m2.7", providers=providers, env=env,
    )
    assert picked["name"] == "minimax-token-plan"


def test_explicit_provider_name_wins(pc):
    """Explicit `provider:` from canvas Config tab beats model-prefix."""
    providers = pc.load_providers(workspace_config_path=str(_ROOT))
    env = {"OPENAI_API_KEY": "sk-fake", "MINIMAX_API_KEY": "sk-fake-mm"}
    picked = pc.resolve_provider(
        model="gpt-5.5", providers=providers,
        explicit_provider="minimax-token-plan", env=env,
    )
    assert picked["name"] == "minimax-token-plan"


def test_explicit_provider_unknown_raises_actionable(pc):
    """Picking a provider not in the registry raises a ValueError that
    names the available providers and points at the YAML fix path
    (silent fallback to providers[0] was the analog #180 bug)."""
    providers = pc.load_providers(workspace_config_path=str(_ROOT))
    with pytest.raises(ValueError) as exc:
        pc.resolve_provider(
            model="gpt-5.5", providers=providers,
            explicit_provider="not-real-provider",
        )
    msg = str(exc.value)
    assert "not-real-provider" in msg
    assert "openai-subscription" in msg
    assert "openai_compat_responses" in msg


# --- internal#728 Bug 2: canonical 'openai' SSOT name acceptance ----------

def test_canonical_openai_with_api_key_maps_to_openai_api(pc):
    """internal#728 Bug 2 repro (agents-team Researcher + CR2, codex/gpt-5.5,
    2026-05-28). The controlplane providers.yaml derives the canonical
    ``openai`` for codex/gpt-*; pre-fix the adapter rejected it with
    `provider='openai' but it is not in the providers registry` → setup()
    failure → JSON-RPC -32603 / A2A 503.

    With OPENAI_API_KEY present and no subscription cred, the canonical
    ``openai`` alias must map to the built-in ``openai-api`` (the credential
    that is actually available)."""
    providers = pc.load_providers(workspace_config_path=str(_ROOT))
    env = {"OPENAI_API_KEY": "sk-fake-openai"}
    picked = pc.resolve_provider(
        model="gpt-5.5", providers=providers,
        explicit_provider="openai", env=env,
    )
    assert picked["name"] == "openai-api"


def test_canonical_openai_with_subscription_maps_to_subscription(pc):
    """The canonical ``openai`` alias maps to ``openai-subscription`` when a
    subscription credential (CODEX_AUTH_JSON) is present — mirrors
    resolve_provider's subscription-first precedence so the alias and the
    auto-detect path agree."""
    providers = pc.load_providers(workspace_config_path=str(_ROOT))
    env = {"CODEX_AUTH_JSON": '{"auth_mode":"chatgpt"}'}
    picked = pc.resolve_provider(
        model="gpt-5.5", providers=providers,
        explicit_provider="openai", env=env,
    )
    assert picked["name"] == "openai-subscription"


def test_canonical_openai_no_cred_defaults_to_openai_api(pc):
    """Canonical ``openai`` with no credential present still RESOLVES (to the
    openai-api built-in) rather than raising — codex's native provider then
    surfaces the auth error at first turn with an actionable message, instead
    of the adapter wedging at setup() / A2A 503. The decisive fix: setup()
    must not crash on a derivable canonical provider name."""
    providers = pc.load_providers(workspace_config_path=str(_ROOT))
    picked = pc.resolve_provider(
        model="gpt-5.5", providers=providers,
        explicit_provider="openai", env={},
    )
    assert picked["name"] == "openai-api"


def test_canonical_openai_is_case_insensitive(pc):
    """The alias match is case-insensitive (parity with the existing
    explicit-name match)."""
    providers = pc.load_providers(workspace_config_path=str(_ROOT))
    env = {"OPENAI_API_KEY": "sk-fake"}
    picked = pc.resolve_provider(
        model="gpt-5.5", providers=providers,
        explicit_provider="OpenAI", env=env,
    )
    assert picked["name"] == "openai-api"


def test_genuinely_unknown_provider_still_raises(pc):
    """The ``openai`` alias is the ONLY new acceptance — a genuinely
    unregistered name still raises the actionable error (the alias must not
    have widened the gate to any name)."""
    providers = pc.load_providers(workspace_config_path=str(_ROOT))
    with pytest.raises(ValueError) as exc:
        pc.resolve_provider(
            model="gpt-5.5", providers=providers,
            explicit_provider="anthropic-oauth", env={"OPENAI_API_KEY": "x"},
        )
    assert "anthropic-oauth" in str(exc.value)


# --- Group 3: render_config_toml — built-in modes -------------------------

def test_render_subscription_emits_nothing(pc):
    """The verified working device-logged codex-0.130 shape has NO
    model_provider override; emitting one would route the subscription
    auth to the override's base_url and 404 (internal#513 live blocker)."""
    providers = pc.load_providers(workspace_config_path=str(_ROOT))
    sub = next(p for p in providers if p["name"] == "openai-subscription")
    body = pc.render_config_toml(sub, model="gpt-5.5")
    assert body == ""


def test_render_openai_api_emits_nothing(pc):
    """Same shape for the direct OPENAI_API_KEY path — codex's built-in
    OpenAI provider handles it natively."""
    providers = pc.load_providers(workspace_config_path=str(_ROOT))
    api = next(p for p in providers if p["name"] == "openai-api")
    body = pc.render_config_toml(api, model="gpt-5.5")
    assert body == ""


# --- Group 4: render_config_toml — openai_compat_responses ----------------

def test_render_minimax_emits_responses_wire(pc):
    """MiniMax provider entry → [model_providers.minimax] block with
    wire_api = "responses" (CLI 0.130 contract; "chat" was removed)."""
    providers = pc.load_providers(workspace_config_path=str(_ROOT))
    mm = next(p for p in providers if p["name"] == "minimax-token-plan")
    body = pc.render_config_toml(mm, model="codex-minimax-m2.7")
    # Top-level pin
    assert 'model_provider = "minimax"' in body
    # Provider block
    assert "[model_providers.minimax]" in body
    assert 'base_url = "https://api.minimax.io/v1"' in body
    assert 'env_key = "MINIMAX_API_KEY"' in body
    # wire_api: only "responses" is parse-valid on CLI 0.130. "chat"
    # is a regression.
    assert 'wire_api = "responses"' in body
    assert 'wire_api = "chat"' not in body


def test_render_compat_uses_model_id_override(pc):
    """The MiniMax YAML entry sets ``model_id_override: codex-MiniMax-M2.7``
    so the wire-protocol model name (uppercase + period) is used in
    config.toml regardless of how the canvas surfaces it."""
    providers = pc.load_providers(workspace_config_path=str(_ROOT))
    mm = next(p for p in providers if p["name"] == "minimax-token-plan")
    body = pc.render_config_toml(mm, model="codex-minimax-m2.7")
    assert 'model = "codex-MiniMax-M2.7"' in body


def test_render_compat_requires_base_url(pc):
    """A misconfigured registry entry (compat mode but no base_url)
    must fail closed — silently writing an empty base_url would
    produce a config.toml the CLI hard-rejects at parse."""
    bad = {
        "name": "broken",
        "auth_mode": pc.AUTH_MODE_OPENAI_COMPAT_RESPONSES,
        "base_url": None,
        "auth_env": ("BROKEN_KEY",),
        "model_prefixes": (),
        "model_aliases": (),
        "wire_api": "responses",
        "model_provider_slug": None,
        "model_id_override": None,
    }
    with pytest.raises(ValueError, match="no base_url"):
        pc.render_config_toml(bad, model="x")


# --- Group 5: write_config_toml — idempotent + cleanup --------------------

def test_write_subscription_clears_stale_override(pc, tmp_path):
    """When the picked provider is subscription/openai-api, an existing
    auto-generated config.toml MUST be removed — otherwise a stale
    MiniMax block survives a provider switch and codex authenticates
    off the subscription but POSTs to api.minimax.io (live blocker
    fixed in PR#11)."""
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    stale = codex_home / "config.toml"
    stale.write_text(
        "# Auto-generated by provider_config.render_config_toml\n"
        'model_provider = "minimax"\n'
    )
    providers = pc.load_providers(workspace_config_path=str(_ROOT))
    sub = next(p for p in providers if p["name"] == "openai-subscription")
    written = pc.write_config_toml(
        sub, model="gpt-5.5", codex_home=str(codex_home),
    )
    assert written is None
    assert not stale.exists()


def test_write_compat_writes_file_with_responses_wire(pc, tmp_path):
    """Roundtrip: write_config_toml for MiniMax actually creates the file
    and the bytes carry wire_api="responses"."""
    codex_home = tmp_path / ".codex"
    providers = pc.load_providers(workspace_config_path=str(_ROOT))
    mm = next(p for p in providers if p["name"] == "minimax-token-plan")
    written = pc.write_config_toml(
        mm, model="codex-minimax-m2.7", codex_home=str(codex_home),
    )
    assert written is not None
    assert written.exists()
    body = written.read_text()
    assert 'wire_api = "responses"' in body
    assert 'wire_api = "chat"' not in body
    assert "api.minimax.io" in body


# --- Group 6: assert_model_is_not_provider_name ---------------------------
#
# Defense-in-depth: when the upstream workspace-config writer (CP
# provisioner) gets confused and stamps a PROVIDER name (e.g.
# "openai-subscription") into the YAML `model:` field, codex's
# thread/start would silently take "openai-subscription" as a model id
# and either 4xx-loop or wedge. We catch this at adapter setup() and
# abort with a structured 422-style error pointing at the writer.
# Pairs with the CP-side fix; either side alone closes the bug, both
# together is defense-in-depth.

def test_assert_model_is_not_provider_name_passes_for_real_model(pc):
    """Real model ids in the model: field do NOT raise."""
    providers = pc.load_providers(workspace_config_path=str(_ROOT))
    # Real codex roster (verified May-2026 in test_modernization_pr1):
    for model_id in ("gpt-5.5", "gpt-5.4", "codex-minimax-m2.7", ""):
        # Should not raise.
        pc.assert_model_is_not_provider_name(model_id, providers)


def test_assert_model_is_not_provider_name_passes_for_none(pc):
    """``None`` (no model picked) does NOT raise — the workspace can boot
    with codex's thread/start default."""
    providers = pc.load_providers(workspace_config_path=str(_ROOT))
    pc.assert_model_is_not_provider_name(None, providers)


def test_assert_model_is_not_provider_name_raises_on_openai_subscription(pc):
    """The exact bug shape from the field reports (Reviewer + Researcher
    wedge 2026-05-18/19): MODEL_PROVIDER='openai-subscription' got
    stamped into the YAML `model:` field by the CP writer. Adapter
    setup() must abort BEFORE codex thread/start sees the garbage."""
    providers = pc.load_providers(workspace_config_path=str(_ROOT))
    with pytest.raises(RuntimeError) as exc:
        pc.assert_model_is_not_provider_name(
            "openai-subscription", providers,
        )
    msg = str(exc.value)
    # Names the bad value verbatim so the operator sees the bug.
    assert "openai-subscription" in msg
    # Names the registry entry it collided with so the operator can map
    # back to which provider this value belongs in.
    assert "provider name" in msg.lower()
    # Points at the writer (the CP provisioner) — the actual root cause.
    assert "workspace-config writer" in msg.lower() or "provisioner" in msg.lower()


def test_assert_model_is_not_provider_name_raises_on_openai_api(pc):
    """Same shape for the openai-api provider name."""
    providers = pc.load_providers(workspace_config_path=str(_ROOT))
    with pytest.raises(RuntimeError, match="openai-api"):
        pc.assert_model_is_not_provider_name(
            "openai-api", providers,
        )


def test_assert_model_is_not_provider_name_raises_on_minimax_token_plan(pc):
    """Same shape for the minimax-token-plan provider name."""
    providers = pc.load_providers(workspace_config_path=str(_ROOT))
    with pytest.raises(RuntimeError, match="minimax-token-plan"):
        pc.assert_model_is_not_provider_name(
            "minimax-token-plan", providers,
        )


def test_assert_model_is_not_provider_name_is_case_insensitive(pc):
    """The provider registry's name is lowercased on the match path so a
    capitalization typo in the writer (OpenAI-Subscription) still
    raises — matching ``resolve_provider``'s case-insensitive shape."""
    providers = pc.load_providers(workspace_config_path=str(_ROOT))
    with pytest.raises(RuntimeError, match="(?i)openai-subscription"):
        pc.assert_model_is_not_provider_name(
            "OpenAI-Subscription", providers,
        )


# --- Group: platform-managed provider (proxy Responses surface) -----------

def test_platform_provider_present_in_shipped_yaml(pc):
    """The `platform` provider must be in the registry so the adapter's
    provider-driven selection (explicit_provider='platform', i.e.
    provider==platform — NOT a billing-mode env) can resolve it."""
    providers = pc.load_providers(workspace_config_path=str(_ROOT))
    plat = [p for p in providers if p["name"] == "platform"]
    assert plat, "platform provider missing from config.yaml providers registry"
    p = plat[0]
    assert p["auth_mode"] == pc.AUTH_MODE_OPENAI_COMPAT_RESPONSES
    assert p["wire_api"] == "responses", "codex CLI 0.130+ is Responses-API-only"
    assert p.get("model_provider_slug") == "platform"
    assert "MOLECULE_LLM_USAGE_TOKEN" in p["auth_env"]


def test_platform_auth_env_is_usage_token_only(pc):
    """#2250 exact-equality gate: the codex `platform` leg authenticates
    with the injected MOLECULE_LLM_USAGE_TOKEN bearer ONLY.

    render_config_toml picks the FIRST non-builtin auth_env entry as the
    config.toml ``env_key``. The platform arm previously carried a trailing
    ``ANTHROPIC_API_KEY`` (cross-runtime drift copied from the claude-code
    platform arm). It was inert on the happy path — MOLECULE_LLM_USAGE_TOKEN
    sorts first — but if that token were ever unset, env_key would silently
    fall through to ANTHROPIC_API_KEY and the codex CLI would POST an
    Anthropic key to the OpenAI-compat proxy leg (the #2250 mis-auth
    footgun). Pin EXACT equality so any future re-addition fails CI.
    """
    providers = pc.load_providers(workspace_config_path=str(_ROOT))
    plat = next(p for p in providers if p["name"] == "platform")
    assert list(plat["auth_env"]) == ["MOLECULE_LLM_USAGE_TOKEN"], (
        f"codex platform auth_env drifted: {list(plat['auth_env'])!r}; "
        "must be exactly [MOLECULE_LLM_USAGE_TOKEN] (#2250). A vendor key "
        "here can become the rendered env_key when the usage token is absent."
    )
    # And the rendered config.toml env_key must be the usage token — the
    # consequence the exact-equality gate protects.
    toml = pc.render_config_toml(plat, model="gpt-5.5")
    assert 'env_key = "MOLECULE_LLM_USAGE_TOKEN"' in toml, (
        "platform config.toml must read the usage token as its bearer env_key"
    )


def test_no_codex_provider_carries_anthropic_key(pc):
    """No codex provider may carry an Anthropic key in auth_env. codex is an
    OpenAI-family runtime; an ANTHROPIC_* env in ANY provider's auth_env is
    cross-runtime drift (#2250) — it can be rendered as the bearer env_key
    for an openai_compat_responses leg. Scan the whole shipped registry."""
    providers = pc.load_providers(workspace_config_path=str(_ROOT))
    offenders = {
        p["name"]: list(p["auth_env"])
        for p in providers
        if any("ANTHROPIC" in ev.upper() for ev in p["auth_env"])
    }
    assert not offenders, (
        f"codex providers must not carry an Anthropic auth_env key (#2250): "
        f"{offenders!r}"
    )


def test_resolve_explicit_platform_returns_platform(pc):
    """resolve_provider with explicit_provider='platform' (what the adapter
    derives when the resolved provider is platform — LLM_PROVIDER=platform /
    MODEL_PROVIDER=platform / yaml provider, NOT a billing-mode env) returns
    the platform entry — not a subscription/openai fallback."""
    providers = pc.load_providers(workspace_config_path=str(_ROOT))
    picked = pc.resolve_provider(
        model="openai/gpt-5.4", providers=providers,
        explicit_provider="platform", env={},
    )
    assert picked["name"] == "platform"
    assert picked["auth_mode"] == pc.AUTH_MODE_OPENAI_COMPAT_RESPONSES
    assert picked["wire_api"] == "responses"
