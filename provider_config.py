"""Codex provider abstraction — YAML-driven registry + ``config.toml`` writer.

Replaces the previous hardcoded ``codex_minimax_config.sh`` flow with a
data-driven layer modeled on the claude-code template's provider
registry (``adapter.py:_load_providers``/``_resolve_provider``/
``_project_vendor_auth``). Adding a new codex-compatible provider is now
a one-entry YAML edit instead of a code change in the boot scripts.

Codex differs from claude-code's wire shape: the ``codex`` CLI reads
credentials and provider routing from on-disk files (``~/.codex/auth.json``
+ ``~/.codex/config.toml``) rather than from ``ANTHROPIC_BASE_URL``-style
env vars. So this module's job is to render the right ``config.toml``
fragment for the picked provider, not to mutate ``os.environ``.

Three auth modes are supported:

1. ``chatgpt_subscription`` — a codex/ChatGPT subscription ``auth.json``
   blob is injected via the ``CODEX_AUTH_JSON`` env var (the older
   ``CODEX_CHATGPT_AUTH_JSON`` is accepted as a backward-compat alias).
   The CLI's built-in OpenAI/Responses provider serves this — we MUST
   NOT write a ``model_provider`` override, otherwise codex would
   authenticate off the subscription but route requests to the
   override's base_url (this was the live A2A blocker observed before
   PR #11; preserved here as a hard contract).

2. ``openai_api`` — direct OpenAI API key via ``OPENAI_API_KEY``. Also
   handled by the CLI's built-in provider with no override needed.

3. ``openai_compat_responses`` — third-party endpoint that speaks the
   OpenAI ``Responses`` API on a vendor-specific base_url + env key
   (e.g. MiniMax token-plan, future grok-codex). We emit a
   ``[model_providers.<slug>]`` block + ``model_provider = "<slug>"``
   so the CLI POSTs to the vendor's endpoint while reading the env
   key. ``wire_api`` is pinned to ``"responses"`` (CLI 0.130 removed
   the ``"chat"`` variant — see the registry comment on the MiniMax
   entry for the known Chat-vs-Responses gap there).

The registry lives in ``config.yaml`` under ``providers:`` (same as the
claude-code template). The shipped entries are open-source-safe — all
vendor base URLs are public endpoints (api.openai.com, api.minimax.io)
and no deployer-specific identifiers are baked in. Operators add a
custom provider by appending one entry to ``providers:`` and putting
the env key into the workspace's secrets — no fork-and-edit needed.

The legacy ``codex_minimax_config.sh`` and the inline mode-C block in
``start.sh`` remain the file-write *mechanism* (for backward-compat
with the existing tests that exercise the shell scripts directly). The
adapter's ``setup()`` calls into this module first, before ``executor``
spawns the codex child, so the routing decision is made in Python and
the shell scripts become thin no-op fallbacks when a registry-driven
config has already been written.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

# --- Auth-mode constants ---------------------------------------------------

AUTH_MODE_CHATGPT_SUBSCRIPTION = "chatgpt_subscription"
AUTH_MODE_OPENAI_API = "openai_api"
AUTH_MODE_OPENAI_COMPAT_RESPONSES = "openai_compat_responses"

# Canonical SSOT provider name (internal#718 / internal#728 Bug 2).
# molecule-controlplane's providers.yaml registry derives the OpenAI BYOK
# provider for the codex runtime as the canonical ``openai`` (DeriveProvider
# returns provider.Name == "openai"; the registry comment on the codex runtime
# block notes "both [subscription + API key] map to the `openai` manifest
# provider"). The codex adapter's OWN registry splits that into two
# auth-mode-specific built-ins (``openai-subscription`` / ``openai-api``) because
# codex authenticates differently per mode. Those internal names are an
# adapter-private detail the SSOT must not have to know — so the adapter accepts
# the canonical ``openai`` as an alias and selects the right built-in from the
# available credential. Keeping the alias here (not in the registry) keeps the
# SSOT canonical: the registry stays adapter-agnostic.
CANONICAL_OPENAI_PROVIDER = "openai"

_BUILTIN_AUTH_MODES = frozenset({
    AUTH_MODE_CHATGPT_SUBSCRIPTION,
    AUTH_MODE_OPENAI_API,
    AUTH_MODE_OPENAI_COMPAT_RESPONSES,
})


# --- Built-in registry -----------------------------------------------------
# Fallback when ``config.yaml`` has no ``providers:`` section or every
# entry was rejected. Mirrors the claude-code template's ``_BUILTIN_PROVIDERS``
# pattern: the canonical registry is the YAML; this exists so a bare-bones
# workspace still boots with sane defaults.
#
# Order matters for ``_resolve_provider``'s fallback selection: the FIRST
# entry whose credential is available wins when no explicit provider is
# picked. Subscription is preferred over the pay-as-you-go API key.

_BUILTIN_PROVIDERS = (
    {
        "name": "openai-subscription",
        "auth_mode": AUTH_MODE_CHATGPT_SUBSCRIPTION,
        "model_prefixes": ("gpt-",),
        "model_aliases": (),
        "base_url": None,
        "auth_env": ("CODEX_AUTH_JSON", "CODEX_CHATGPT_AUTH_JSON"),
        "wire_api": None,
        "model_provider_slug": None,
    },
    {
        "name": "openai-api",
        "auth_mode": AUTH_MODE_OPENAI_API,
        "model_prefixes": ("gpt-",),
        "model_aliases": (),
        "base_url": None,
        "auth_env": ("OPENAI_API_KEY",),
        "wire_api": None,
        "model_provider_slug": None,
    },
)


# --- YAML parsing ----------------------------------------------------------

def _coerce_string_list(value, lowercase: bool = False) -> tuple:
    """Defensive: coerce a YAML scalar/list field into a tuple of strings.

    Same shape as the claude-code template's helper of the same name.
    Forgotten brackets (``model_prefixes: gpt-`` → string, not list)
    used to iterate over characters and silently match every model. A
    mixed list (``[gpt-, 123]``) used to raise mid-comprehension and
    drop the whole registry to builtins. Neither happens now.
    """
    if not isinstance(value, list):
        return ()
    out = []
    for item in value:
        if not isinstance(item, str):
            logger.warning(
                "providers: skipping non-string list item %r (type %s)",
                item, type(item).__name__,
            )
            continue
        out.append(item.lower() if lowercase else item)
    return tuple(out)


def _normalize_provider(entry) -> Optional[dict]:
    """Normalize one YAML provider entry; return ``None`` if unusable."""
    if not isinstance(entry, dict):
        return None
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        logger.warning("providers: skipping entry without a string name: %r", entry)
        return None
    auth_mode = entry.get("auth_mode")
    if auth_mode not in _BUILTIN_AUTH_MODES:
        logger.warning(
            "providers: entry %r has unknown auth_mode=%r; skipping",
            name, auth_mode,
        )
        return None
    return {
        "name": name,
        "auth_mode": auth_mode,
        "model_prefixes": _coerce_string_list(entry.get("model_prefixes"), lowercase=True),
        "model_aliases": _coerce_string_list(entry.get("model_aliases"), lowercase=True),
        "base_url": entry.get("base_url") or None,
        # Env-var names are case-sensitive; preserve.
        "auth_env": _coerce_string_list(entry.get("auth_env"), lowercase=False),
        # CLI 0.130 only parses ``responses`` on third-party blocks; we
        # still take the value from YAML so a future ``chat``-shimmed
        # provider can be added without touching this module.
        "wire_api": (
            entry.get("wire_api")
            if isinstance(entry.get("wire_api"), str) and entry.get("wire_api").strip()
            else None
        ),
        # For ``openai_compat_responses``: the TOML slug used in
        # ``[model_providers.<slug>]`` + ``model_provider = "<slug>"``.
        # Defaults to the provider ``name`` if not specified.
        "model_provider_slug": (
            entry.get("model_provider_slug")
            if isinstance(entry.get("model_provider_slug"), str)
            and entry.get("model_provider_slug").strip()
            else None
        ),
        # Optional CLI model id override (used when the wire model name
        # differs from the canvas-shown model id, e.g. ``codex-MiniMax-M2.7``).
        "model_id_override": (
            entry.get("model_id_override")
            if isinstance(entry.get("model_id_override"), str)
            and entry.get("model_id_override").strip()
            else None
        ),
    }


_TEMPLATE_DIR = Path(__file__).resolve().parent
_CANONICAL_ADAPTER_DIR = Path("/opt/adapter")


# --- Defense-in-depth: catch the CP workspace-config writer bug -----------
#
# Field-observed bug shape (prod-Reviewer + prod-Researcher wedge,
# 2026-05-18/19): the upstream CP provisioner's workspace-config writer
# conflated the ``MODEL`` env var (a model id like ``gpt-5.5``) with the
# ``MODEL_PROVIDER`` env var (a provider name like ``openai-subscription``)
# and stamped the PROVIDER name into the YAML ``model:`` field. Codex
# thread/start then takes ``"openai-subscription"`` as a model id and
# either 4xx-loops or silently wedges (the executor's reader thread
# blocks in wait4 — see
# ``reference_codex_prod_reviewer_researcher_wedge_in_executor_not_codex_2026_05_18``).
#
# The structural fix lives in the CP provisioner. This helper is the
# template-side defense-in-depth: at adapter.setup() the template
# refuses to boot when it sees a provider name in the model field, and
# emits a structured error pointing operators at the writer. Either
# side alone closes the bug; both together is the class-fix.

def assert_model_is_not_provider_name(
    model: Optional[str],
    providers: Sequence[dict],
) -> None:
    """Raise ``RuntimeError`` when ``model`` matches a provider registry name.

    No-op when ``model`` is ``None``, empty, or a non-matching string.
    Case-insensitive against the registry's ``name`` field (matching
    ``resolve_provider``'s shape so a capitalization typo in the
    upstream writer doesn't slip through).
    """
    if not model:
        return
    m = model.strip().lower()
    if not m:
        return
    for provider in providers:
        if provider["name"].lower() == m:
            known = ", ".join(p["name"] for p in providers)
            raise RuntimeError(
                f"codex adapter: refusing to boot — MODEL value "
                f"{model!r} is a PROVIDER NAME, not a model id. "
                f"This is the workspace-config writer bug (CP "
                f"provisioner stamped MODEL_PROVIDER into the YAML "
                f"`model:` field). Codex thread/start would silently "
                f"accept this garbage and either 4xx-loop or wedge."
                f"\n\n"
                f"Provider registry names (do NOT pass these as "
                f"`model:`): {known}\n"
                f"\n"
                f"Fix path: update the molecule-controlplane "
                f"workspace-config writer to write the MODEL env value "
                f"(real model id, e.g. 'gpt-5.5') into `model:` and "
                f"the MODEL_PROVIDER env value (registry provider "
                f"name, e.g. {provider['name']!r}) into `provider:` "
                f"separately."
            )


def load_providers(workspace_config_path: str = "") -> tuple:
    """Read the provider registry from the template's ``config.yaml``.

    Resolution order mirrors the claude-code template's ``_load_providers``:
      1. ``/opt/adapter/config.yaml`` — compatibility path for older and
         explicitly self-managed installs.
      2. Adjacent to this file's ``__file__`` — current published-image
         path (``/app/config.yaml``) and the normal dev/test path.
      3. Per-workspace ``<workspace_config_path>/config.yaml`` — operator
         override on private deployments.
      4. ``_BUILTIN_PROVIDERS`` — last-resort fallback so a bare-bones
         install still boots.
    """
    try:
        import yaml  # transitive dep via molecule-ai-workspace-runtime
    except ImportError:
        logger.warning("providers: yaml import failed; using builtins")
        return _BUILTIN_PROVIDERS

    candidates = []
    seen = set()
    for path in (
        _CANONICAL_ADAPTER_DIR / "config.yaml",
        _TEMPLATE_DIR / "config.yaml",
        Path(workspace_config_path) / "config.yaml" if workspace_config_path else None,
    ):
        if path is None:
            continue
        if path not in seen:
            seen.add(path)
            candidates.append(path)

    raw = None
    chosen_path = None
    for yaml_path in candidates:
        try:
            with open(yaml_path, "r") as f:
                data = yaml.safe_load(f) or {}
        except FileNotFoundError:
            continue
        except Exception as exc:  # noqa: BLE001 — never block boot on YAML error
            logger.warning(
                "providers: failed to load %s (%s); trying next candidate",
                yaml_path, exc,
            )
            continue
        candidate_raw = data.get("providers") if isinstance(data, dict) else None
        if isinstance(candidate_raw, list) and candidate_raw:
            raw = candidate_raw
            chosen_path = yaml_path
            break

    if raw is None:
        logger.info(
            "providers: no providers section in %s; using builtins",
            " or ".join(str(p) for p in candidates),
        )
        return _BUILTIN_PROVIDERS

    parsed = []
    for entry in raw:
        try:
            normalized = _normalize_provider(entry)
        except Exception as exc:  # noqa: BLE001 — per-entry isolation
            logger.warning("providers: dropping unparseable entry %r (%s)", entry, exc)
            continue
        if normalized is not None:
            parsed.append(normalized)

    if not parsed:
        logger.warning("providers: no valid entries in %s; using builtins", chosen_path)
        return _BUILTIN_PROVIDERS
    logger.info("providers: loaded %d entries from %s", len(parsed), chosen_path)
    return tuple(parsed)


# --- Provider resolution ---------------------------------------------------

def resolve_provider(
    model: Optional[str],
    providers: Sequence[dict],
    explicit_provider: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> dict:
    """Return the provider entry matching this model id + available env.

    Precedence:
      1. ``explicit_provider`` (set via canvas Config tab or env
         ``MODEL_PROVIDER``) — if named entry isn't in the registry,
         raise ``ValueError`` with an actionable message (silent
         fallback is the bug that motivated the analog #180 in
         claude-code).
      2. Subscription auto-detection: if any provider with
         ``auth_mode == chatgpt_subscription`` has its ``auth_env``
         present, prefer it over a model-prefix match. This preserves
         the verified production behavior where prod-Reviewer /
         prod-Researcher have ``CODEX_AUTH_JSON`` set alongside the
         (unused-by-them) ``MINIMAX_API_KEY`` — subscription wins.
      3. ``model`` matched against ``model_prefixes`` then
         ``model_aliases`` (case-insensitive).
      4. First entry whose credential is satisfied.
      5. ``providers[0]`` — last-resort fallback (open-source-safe:
         ``_BUILTIN_PROVIDERS[0]`` is the subscription path, which
         no-ops cleanly when the credential is absent).
    """
    if not providers:
        raise ValueError("resolve_provider called with empty providers tuple")
    if env is None:
        env = os.environ

    # 1. Explicit name.
    if explicit_provider:
        ep_lower = explicit_provider.lower()
        for provider in providers:
            if provider["name"].lower() == ep_lower:
                return provider

        # internal#728 Bug 2: accept the canonical SSOT name ``openai`` as an
        # alias for codex's auth-mode-specific OpenAI built-ins. The
        # controlplane providers.yaml derives ``openai`` for codex/gpt-* (BYOK);
        # the adapter's registry only has ``openai-subscription`` /
        # ``openai-api``. Without this, a re-provisioned codex workspace whose
        # config carries provider='openai' (or MODEL_PROVIDER=openai) fails
        # adapter.setup() -> JSON-RPC -32603 / A2A 503 (agents-team Researcher +
        # CR2, gpt-5.5; live-confirmed 2026-05-28, comment 52493).
        #
        # Map ``openai`` to the right built-in by available credential:
        # CODEX_AUTH_JSON / CODEX_CHATGPT_AUTH_JSON present -> subscription
        # (chatgpt_subscription); else -> the openai-api (OPENAI_API_KEY) path.
        # This mirrors resolve_provider's own subscription-first precedence
        # (#2/#3 below) so the alias and the auto-detect path agree. If neither
        # built-in exists in the registry (a deployer pruned them), fall through
        # to the actionable raise rather than guessing.
        if ep_lower == CANONICAL_OPENAI_PROVIDER:
            sub = next(
                (p for p in providers
                 if p["auth_mode"] == AUTH_MODE_CHATGPT_SUBSCRIPTION
                 and any(env.get(ev) for ev in p["auth_env"])),
                None,
            )
            if sub is not None:
                logger.info(
                    "resolve_provider: canonical provider 'openai' + subscription "
                    "credential present -> mapping to built-in %s",
                    sub["name"],
                )
                return sub
            api = next(
                (p for p in providers if p["auth_mode"] == AUTH_MODE_OPENAI_API),
                None,
            )
            if api is not None:
                logger.info(
                    "resolve_provider: canonical provider 'openai' -> mapping to "
                    "built-in %s (no subscription credential present)",
                    api["name"],
                )
                return api

        known = ", ".join(p["name"] for p in providers)
        raise ValueError(
            f"codex adapter: workspace config picks "
            f"provider='{explicit_provider}' but it is not in the "
            f"providers registry.\n"
            f"\n"
            f"Known providers: {known}\n"
            f"\n"
            f"Fix: add an entry to /configs/config.yaml `providers:` "
            f"with auth_mode in {{chatgpt_subscription, openai_api, "
            f"openai_compat_responses}}, plus base_url + auth_env."
        )

    # 2. Subscription auto-detection — any subscription provider with
    # its auth_env present wins. The auth.json materialization
    # downstream is what actually authenticates; we just have to make
    # sure config.toml doesn't pin a vendor override that would route
    # away from the subscription's Responses endpoint.
    for provider in providers:
        if provider["auth_mode"] != AUTH_MODE_CHATGPT_SUBSCRIPTION:
            continue
        for ev in provider["auth_env"]:
            if env.get(ev):
                logger.info(
                    "resolve_provider: subscription credential %s present; "
                    "picking provider=%s (overrides model-prefix match)",
                    ev, provider["name"],
                )
                return provider

    # 3. Model-prefix / alias match, but PREFER credential-satisfied
    # matches first. Multiple providers can advertise the same prefix
    # (gpt-* belongs to both openai-subscription and openai-api): the
    # one whose auth_env is actually present wins, so an
    # OPENAI_API_KEY-only workspace on model=gpt-5.5 picks openai-api
    # rather than openai-subscription (which would no-op the config
    # and then auth-fail at first turn).
    if model:
        m = model.lower()
        matched: list[dict] = []
        for provider in providers:
            for prefix in provider["model_prefixes"]:
                if prefix and m.startswith(prefix):
                    matched.append(provider)
                    break
            else:
                if m in provider["model_aliases"]:
                    matched.append(provider)
        # First credential-satisfied prefix/alias match wins.
        for provider in matched:
            if any(env.get(ev) for ev in provider["auth_env"]):
                return provider
        # No credential satisfied — fall back to the first match so the
        # downstream auth error message names the provider the operator
        # actually picked by model id.
        if matched:
            return matched[0]

    # 4. First credential-satisfied entry across the whole registry.
    for provider in providers:
        if any(env.get(ev) for ev in provider["auth_env"]):
            return provider

    # 5. Last resort.
    return providers[0]


# --- config.toml rendering -------------------------------------------------

def render_config_toml(provider: dict, model: Optional[str] = None) -> str:
    """Render the ``~/.codex/config.toml`` body for the picked provider.

    Returns ``""`` for the built-in CLI providers (subscription,
    openai-api) — codex resolves both natively with NO config override.
    Returns a ``[model_providers.<slug>]`` block + top-level
    ``model_provider`` pin for ``openai_compat_responses`` so the CLI
    POSTs to the vendor's base_url and reads the vendor's env key.
    """
    mode = provider["auth_mode"]
    if mode in (AUTH_MODE_CHATGPT_SUBSCRIPTION, AUTH_MODE_OPENAI_API):
        # Built-in OpenAI provider; no override emitted. The
        # subscription's ``auth.json`` (written elsewhere by start.sh's
        # mode-C block) is sufficient; the openai-api path uses the
        # CLI's default env-key reader.
        return ""

    if mode == AUTH_MODE_OPENAI_COMPAT_RESPONSES:
        slug = provider.get("model_provider_slug") or provider["name"]
        # CLI 0.130 removed wire_api="chat"; default to "responses"
        # (only parse-valid value). Read from registry so a future
        # shim that re-introduces a chat-style wire can flip one YAML
        # field without touching this module.
        wire_api = provider.get("wire_api") or "responses"
        base_url = provider["base_url"]
        if not base_url:
            raise ValueError(
                f"provider {provider['name']!r} has auth_mode="
                f"{AUTH_MODE_OPENAI_COMPAT_RESPONSES} but no base_url"
            )
        # Pick the env-var name we will tell codex to read for the
        # bearer key. ``auth_env`` is ordered; the first entry that's
        # not one of the codex/openai built-ins is the vendor key.
        builtin_env = {"CODEX_AUTH_JSON", "CODEX_CHATGPT_AUTH_JSON", "OPENAI_API_KEY"}
        env_key = next(
            (e for e in provider["auth_env"] if e not in builtin_env),
            None,
        )
        if env_key is None:
            raise ValueError(
                f"provider {provider['name']!r} has no vendor-specific "
                "auth_env entry (all entries are built-in OpenAI keys)"
            )
        cli_model = provider.get("model_id_override") or model or ""

        lines = [
            "# Auto-generated by provider_config.render_config_toml — do not edit",
            "# by hand; regenerated on every boot from the YAML providers registry.",
        ]
        if cli_model:
            lines.append(f'model = "{cli_model}"')
        lines.append(f'model_provider = "{slug}"')
        lines.append("")
        lines.append(f"[model_providers.{slug}]")
        lines.append(f'name = "{provider["name"]}"')
        lines.append(f'base_url = "{base_url}"')
        lines.append(f'env_key = "{env_key}"')
        lines.append(f'wire_api = "{wire_api}"')
        lines.append("requires_openai_auth = false")
        lines.append("request_max_retries = 4")
        lines.append("stream_max_retries = 10")
        lines.append("stream_idle_timeout_ms = 300000")
        return "\n".join(lines) + "\n"

    raise ValueError(f"unsupported auth_mode {mode!r}")


def write_config_toml(
    provider: dict,
    model: Optional[str] = None,
    codex_home: Optional[str] = None,
) -> Optional[Path]:
    """Render + write ``$CODEX_HOME/config.toml``. Returns the path written, or ``None``.

    Idempotent: an existing ``config.toml`` is overwritten. If the
    provider needs no override (subscription / openai-api built-in),
    NO file is written and any pre-existing override is also removed —
    that mirrors the verified-working device-logged codex 0.130 shape
    (no ``model_provider`` block).
    """
    home_str = codex_home or os.environ.get("CODEX_HOME") or os.path.join(
        os.path.expanduser("~"), ".codex"
    )
    home = Path(home_str)
    home.mkdir(parents=True, exist_ok=True)
    config_toml = home / "config.toml"

    body = render_config_toml(provider, model=model)
    if not body:
        # Built-in provider: ensure NO stale override is left behind.
        # Preserves the subscription/openai-api paths' "no
        # model_provider override" contract.
        if config_toml.exists():
            existing = config_toml.read_text()
            # Strip ONLY auto-generated content; preserve hand-edits
            # under a sentinel marker. Conservative: only rewrite when
            # the file is fully auto-generated (starts with our header).
            if existing.startswith("# Auto-generated by provider_config"):
                config_toml.unlink()
        return None

    config_toml.write_text(body)
    try:
        owner = home.stat()
        os.chown(config_toml, owner.st_uid, owner.st_gid)
    except (PermissionError, OSError):
        pass
    return config_toml
