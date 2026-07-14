"""Codex CLI adapter — runs OpenAI Codex (`@openai/codex`) inside the workspace.

This template wraps OpenAI's Codex CLI as a Molecule workspace runtime.
The actual A2A bridge lives in ``executor.py`` — this file is just the
``BaseAdapter`` shell: name, display metadata, config schema, executor
factory, and an ``OPENAI_API_KEY`` reachability check at setup.

Architecture in one paragraph: each workspace session holds one
long-lived ``codex app-server`` child (spawned by ``executor.py`` on
first turn) plus one Codex thread. A2A messages become ``turn/start``
RPCs against that thread, giving us session continuity + queued
mid-turn handling. See
``docs/integrations/codex-app-server-adapter-design.md`` in
molecule-core for the full design.

We deliberately do NOT run a separate daemon here (unlike hermes,
where a long-running gateway listens on :8642 from container boot).
``codex app-server`` is a stdio child of the executor, not a network
service — fewer moving parts, no port to configure, no health endpoint
to wait on at start time.
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from molecule_runtime.adapters.base import BaseAdapter, AdapterConfig

logger = logging.getLogger(__name__)

# ===========================================================================
# ADR-004 adapter-socket: the CODEX per-runtime shape, OWNED HERE.
# ===========================================================================
# Per ADR-004 (SDK owns the adapter contract; the shared engine holds zero
# per-runtime dispatch), the codex-specific MCP-render / read / present /
# persona shape moves OUT of molecule_runtime's _RUNTIME_SPECS / _RUNTIME_READERS
# / _RUNTIME_PERSONA dispatch tables and INTO this adapter. The functions below
# are copied FAITHFULLY (byte-identical native-config output) from the engine's
# mcp_render.py / persona_render.py codex branches so this adapter renders the
# EXACT same ~/.codex/config.toml + AGENTS.md the engine produces today — the
# golden-parity invariant the migration must preserve. The engine still holds
# an identical codex branch (deletion is a later phase); this adapter only ADDS
# the socket methods so both render identically.
#
# Codex reads MCP servers from ~/.codex/config.toml under the [mcp_servers.<name>]
# table; identity from AGENTS.md (the AAIF convention) in its project directory.

# Codex reads MCP servers from this TOML table (mcp_render.CODEX_MCP_TABLE).
_CODEX_MCP_TABLE = "mcp_servers"

# Codex reads the AAIF-standard AGENTS.md as its native identity file
# (persona_render.CODEX_PERSONA_FILE).
_CODEX_PERSONA_FILE = "AGENTS.md"


def _codex_path(config_path) -> Path:
    """Absolute native MCP-config file codex reads (mcp_render._codex_path).

    Codex reads ~/.codex/config.toml. ``config_path`` is unused (the codex CLI
    resolves ``$HOME``), but the signature is uniform across the socket.
    """
    return Path(os.path.expanduser("~")) / ".codex" / "config.toml"


def _codex_persona_path(config_path) -> Path:
    return Path(config_path) / _CODEX_PERSONA_FILE


# --- TOML rendering (verbatim from mcp_render: _toml_escape/_toml_value/
# _render_codex_table/_codex_markers/render_codex_config) ---

def _toml_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _toml_value(v) -> str:
    """Render a scalar/list value as a TOML literal. Scoped to the value
    shapes an MCP spec carries (str, list[str], and nested str->str env dict
    handled by the caller)."""
    if isinstance(v, str):
        return f'"{_toml_escape(v)}"'
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    # Fallback: stringify (defensive — MCP specs shouldn't reach here).
    return f'"{_toml_escape(str(v))}"'


def _render_codex_table(name: str, spec: dict) -> str:
    """Emit the ``[mcp_servers.<name>]`` TOML block for a single server.

    We keep ``env`` (a str->str map) in its own sub-table, which is the
    unambiguous TOML form and avoids inline-table escaping edge cases.
    """
    lines = [f"[{_CODEX_MCP_TABLE}.{name}]"]
    env = None
    for k, v in spec.items():
        if k == "env" and isinstance(v, dict):
            env = v
            continue
        lines.append(f"{k} = {_toml_value(v)}")
    if env:
        lines.append("")
        lines.append(f"[{_CODEX_MCP_TABLE}.{name}.env]")
        for ek, ev in env.items():
            lines.append(f"{ek} = {_toml_value(ev)}")
    return "\n".join(lines) + "\n"


def _codex_markers(name: str) -> tuple[str, str]:
    begin = f"# >>> molecule-mcp:{name} >>>"
    end = f"# <<< molecule-mcp:{name} <<<"
    return begin, end


def _render_codex_config(config_path: Path, name: str, spec: dict) -> None:
    """Additively merge ``name -> spec`` into the codex ``config.toml``
    ``[mcp_servers.<name>]`` table. Idempotent; preserves the rest of the file.

    Manages each server as a marker-delimited block: on re-install we strip the
    prior block for THIS server name and re-append the freshly rendered one,
    leaving every other server (and any hand-written config) intact.
    """
    config_path = Path(config_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)

    existing = config_path.read_text() if config_path.is_file() else ""
    begin, end = _codex_markers(name)

    # Strip any prior managed block for this server (idempotent re-install).
    if begin in existing and end in existing:
        head, _, rest = existing.partition(begin)
        _, _, tail = rest.partition(end)
        existing = head.rstrip("\n") + ("\n" + tail.lstrip("\n") if tail.strip() else "")

    block = f"{begin}\n{_render_codex_table(name, spec)}{end}\n"
    sep = "" if existing == "" or existing.endswith("\n") else "\n"
    config_path.write_text(existing + sep + block)


def _codex_config_has(config_path: Path, name: str) -> bool:
    """True when the codex config.toml declares ``[mcp_servers.<name>]``.

    Fail-closed by construction: a missing/unreadable/malformed config yields
    False, so a genuinely MCP-less codex concierge stays degraded at the gate.
    Uses stdlib ``tomllib`` (read-only; 3.11+, our floor).
    """
    import tomllib

    try:
        data = tomllib.loads(Path(config_path).read_text())
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return False
    table = data.get(_CODEX_MCP_TABLE)
    return isinstance(table, dict) and name in table


def _read_codex_mcp_servers(config_path: Path) -> dict:
    """Read the ``[mcp_servers.<name>]`` tables from codex's config.toml.

    The inverse of the renderer — returns the FULL ``{name: spec}`` map the
    enumerate seam probes. Fail-closed: ``{}`` on any unreadable/malformed
    config so enumeration degrades safely (never crashes boot).
    """
    import tomllib

    try:
        data = tomllib.loads(Path(config_path).read_text())
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return {}
    table = data.get(_CODEX_MCP_TABLE)
    return {k: v for k, v in table.items() if isinstance(v, dict)} if isinstance(table, dict) else {}


def _materialize_codex_persona(config_path: Path, persona: str) -> Path:
    """Codex — write the persona to ``<configs>/AGENTS.md`` (the AAIF convention
    codex reads from its project directory). Verbatim from
    persona_render.materialize_codex_persona + _write_persona_file."""
    target = Path(config_path) / _CODEX_PERSONA_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    body = persona if persona.endswith("\n") else persona + "\n"
    target.write_text(body, encoding="utf-8")
    return target


class CodexAdapter(BaseAdapter):
    """Adapter that proxies A2A turns to a persistent codex app-server."""

    @staticmethod
    def name() -> str:
        return "codex"

    @staticmethod
    def display_name() -> str:
        return "OpenAI Codex CLI"

    @staticmethod
    def description() -> str:
        return (
            "Runs the OpenAI Codex CLI (@openai/codex) with native session "
            "continuity. Each A2A message becomes a turn against a "
            "long-lived codex thread — same UX shape as hermes/openclaw, "
            "MCP-native push parity with claude-code."
        )

    @staticmethod
    def get_config_schema() -> dict:
        return {
            "model": {
                "type": "string",
                "description": (
                    "Codex model. Pass through to `thread/start`. May-2026 "
                    "roster: 'gpt-5.5' (default), 'gpt-5.4', 'gpt-5.4-mini', "
                    "'gpt-5.3-codex', 'gpt-5.3-codex-spark', 'gpt-5.2'. "
                    "Empty = codex default (gpt-5.5)."
                ),
            },
            "provider": {
                "type": "string",
                "description": (
                    "Optional codex provider id from the `providers:` "
                    "registry in config.yaml (e.g. 'openai-subscription', "
                    "'openai-api', 'minimax-token-plan'). Empty = "
                    "auto-resolve from model + env credentials."
                ),
            },
        }

    async def setup(self, config: AdapterConfig) -> None:
        """Verify the codex binary is on PATH and a credential is set, then
        render ``~/.codex/config.toml`` from the providers registry.

        We do NOT spawn the app-server here — that happens lazily on
        the first turn inside the executor. Failing fast at setup
        time with a clear message beats a confusing ``FileNotFoundError``
        from the executor's first ``asyncio.create_subprocess_exec``.

        Provider resolution (see ``provider_config.resolve_provider``):
          1. Explicit ``provider`` field in ``runtime_config`` /
             ``MODEL_PROVIDER`` env wins.
          2. Else, if any ``chatgpt_subscription`` provider's auth_env
             is set (``CODEX_AUTH_JSON`` / ``CODEX_CHATGPT_AUTH_JSON``),
             pick it — preserves the verified prod behavior where the
             subscription beats a co-set vendor key.
          3. Else, model-prefix / alias match against the registry.
          4. Else, first credential-satisfied entry, with the registry's
             first entry as the final fallback.

        The resolved provider is then rendered to ``~/.codex/config.toml``:
        built-in modes (subscription, openai_api) emit NO override (the
        CLI's native OpenAI/Responses provider handles them); compat
        providers emit ``[model_providers.<slug>]`` + ``model_provider``.
        """
        if not shutil.which("codex"):
            raise RuntimeError(
                "codex binary not on PATH. The Dockerfile installs "
                "@openai/codex globally via npm — if you're running "
                "outside the container, install it with: "
                "`npm install -g @openai/codex`"
            )
        # Auth: codex resolves credentials in three ways and any one
        # is sufficient. Mirror that here so setup() does not
        # false-fail a validly-authed workspace:
        #   A. OPENAI_API_KEY  — direct OpenAI path (codex default).
        #   B. MINIMAX_API_KEY — MiniMax chat-wire route
        #      (codex_minimax_config.sh writes config.toml).
        #   C. $CODEX_HOME/auth.json — an injected ChatGPT/Codex
        #      -subscription credential (auth_mode:"chatgpt"),
        #      materialized by start.sh from the CODEX_AUTH_JSON env
        #      var (Infisical SSOT /shared/codex-oauth, key
        #      CODEX_AUTH_JSON, env=prod; CODEX_CHATGPT_AUTH_JSON is a
        #      backward-compat alias) for a SINGLE runner. This mirrors
        #      OpenClaw's openai-codex auth.order: prefer an injected
        #      subscription auth.json over the pay-as-you-go API key.
        #      Codex prefers auth.json over env keys. The
        #      OPENAI_API_KEY path (A) is retained as the documented
        #      fallback and is intentionally NOT removed.
        # CODEX_HOME defaults to ~/.codex; honor an explicit override
        # so a non-default home is still detected.
        codex_home = os.environ.get("CODEX_HOME") or os.path.join(
            os.path.expanduser("~"), ".codex"
        )
        auth_json = Path(codex_home) / "auth.json"
        has_auth_json = auth_json.is_file() and auth_json.stat().st_size > 0
        if not (
            os.environ.get("OPENAI_API_KEY")
            or os.environ.get("MINIMAX_API_KEY")
            or has_auth_json
        ):
            raise RuntimeError(
                "No codex credential found. Codex needs exactly one "
                "of: OPENAI_API_KEY (direct OpenAI), MINIMAX_API_KEY "
                "(MiniMax token-plan codex route), or an injected "
                "ChatGPT/Codex-subscription auth.json at "
                f"{auth_json} (set CODEX_AUTH_JSON for a single-runner "
                "workspace). Configure via the canvas Config tab."
            )

        # --- Provider resolution + config.toml rendering ---
        # Pull the picked model + (optional) explicit provider from
        # runtime_config (the canvas Config tab writes here on Save).
        rc = getattr(config, "runtime_config", None)
        if isinstance(rc, dict):
            yaml_model = rc.get("model") or ""
            yaml_provider = rc.get("provider") or ""
        else:
            yaml_model = getattr(rc, "model", None) or getattr(config, "model", "") or ""
            yaml_provider = getattr(rc, "provider", None) or ""

        try:
            from provider_config import (
                assert_model_is_not_provider_name,
                load_providers, resolve_provider, write_config_toml,
            )
        except ImportError as exc:
            # Defensive: fall back to the legacy shell-script path
            # below if the module can't be imported (e.g. a partial
            # install). The credential preflight above has already
            # gated; codex will boot off OPENAI_API_KEY or auth.json
            # using the CLI defaults.
            logger.warning(
                "codex: provider_config import failed (%s); "
                "skipping registry-driven config.toml render",
                exc,
            )
            return

        providers = load_providers(
            workspace_config_path=getattr(config, "config_path", "") or "",
        )

        # Provider selection is flag-free. ``MOLECULE_RESOLVED_PROVIDER`` is the
        # SSOT signal: core's workspace provisioner resolves the provider ONCE
        # (Go ``manifest.DeriveProvider``) and publishes the resolved registry
        # arm name in this single env var for every downstream layer to READ,
        # never re-derive. It is the TOP-PRECEDENCE explicit provider — when
        # present it wins over ``LLM_PROVIDER``/``MODEL_PROVIDER`` and the
        # model-derived subscription auto-detection, so codex selects exactly the
        # arm core resolved (``platform`` for the metered proxy, a byok arm
        # otherwise). ``LLM_PROVIDER`` (the legacy platform-routed signal core
        # injected) and ``MODEL_PROVIDER`` (the legacy alias) remain as
        # back-compat fallbacks for old provisioners that predate the SSOT
        # signal — consumed only when ``MOLECULE_RESOLVED_PROVIDER`` is absent.
        #
        # A value that names a provider the registry accepts — INCLUDING
        # ``platform`` — is taken as the explicit provider, so the platform arm
        # is selected exactly like any other arm (provider==platform) rather
        # than via a billing-mode env.
        #
        # MODEL_PROVIDER is historically overloaded in the platform stack: old
        # provisioners used it for a model id, while newer config paths use it
        # for a provider name. Treat it as explicit only when it names a
        # provider the registry actually accepts. A leaked value like "gpt-5.5"
        # must not override the subscription auto-detection path. (The SSOT
        # ``MOLECULE_RESOLVED_PROVIDER`` always carries a registry arm name, so
        # the same registry-name validation applies to it uniformly.)
        env_provider = (
            os.environ.get("MOLECULE_RESOLVED_PROVIDER")
            or os.environ.get("LLM_PROVIDER")
            or os.environ.get("MODEL_PROVIDER")
            or ""
        ).strip()
        provider_names = {p["name"].lower() for p in providers}
        if env_provider and env_provider.lower() in provider_names:
            explicit_provider = env_provider
        elif env_provider:
            explicit_provider = yaml_provider or None
            logger.warning(
                "codex adapter: ignoring legacy MODEL_PROVIDER=%r because "
                "it is not a provider registry name",
                env_provider,
            )
        else:
            explicit_provider = yaml_provider or None

        # The `platform` provider (proxy Responses surface) is selected the
        # same way every other arm is — by the resolved provider above, i.e.
        # explicit_provider=="platform" coming from LLM_PROVIDER/MODEL_PROVIDER
        # (core injects LLM_PROVIDER=platform for platform-routed workspaces) or
        # the YAML provider. When it IS the picked arm, the tenant has no BYOK
        # key (the workspace-server strips them); the proxy owns the keys +
        # usage billing, and the base_url is overridden below with the injected
        # MOLECULE_LLM_BASE_URL (per-env). codex POSTs {base_url}/responses
        # (wire_api=responses). There is NO billing-mode env gating this.

        # Defense-in-depth for the CP workspace-config writer bug
        # (2026-05-18 Reviewer + Researcher wedge): if the upstream
        # writer stamped a PROVIDER name into the YAML `model:` field
        # (e.g. model: 'openai-subscription'), refuse to boot rather
        # than letting codex thread/start accept the garbage and wedge.
        # Either side alone closes the bug — see
        # `assert_model_is_not_provider_name` doc + the structural fix
        # in molecule-controlplane's workspace-config writer.
        assert_model_is_not_provider_name(yaml_model, providers)

        try:
            picked = resolve_provider(
                yaml_model, providers,
                explicit_provider=explicit_provider,
            )
        except ValueError:
            # Re-raise with the actionable message intact — silent
            # fallback to providers[0] when the operator picked an
            # unknown name would route them through the wrong
            # base_url + env key (the analog #180 in claude-code).
            raise

        # Platform arm: prefer the injected per-env proxy base over the
        # registry's static (prod) base_url, so staging routes to staging.
        # Keyed on the RESOLVED provider (provider=="platform"), never on a
        # billing-mode env.
        if picked["name"] == "platform":
            base = (os.environ.get("MOLECULE_LLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or "").strip()
            if base:
                picked = {**picked, "base_url": base.rstrip("/")}

        # Render + write config.toml. For built-in OpenAI auth modes
        # (subscription, openai_api) this writes NOTHING and clears
        # any stale auto-generated override — exactly the verified
        # device-logged codex-0.130 shape that the prod-Reviewer /
        # prod-Researcher path requires.
        codex_home = os.environ.get("CODEX_HOME") or os.path.join(
            os.path.expanduser("~"), ".codex"
        )
        try:
            written = write_config_toml(
                picked, model=yaml_model or None, codex_home=codex_home,
            )
        except ValueError as exc:
            # Misconfigured registry entry (missing base_url / vendor
            # env). Fail closed so the operator sees the YAML defect.
            raise RuntimeError(
                f"codex provider registry: {exc}"
            ) from exc

        logger.info(
            "codex adapter: provider=%s auth_mode=%s wrote=%s",
            picked["name"], picked["auth_mode"],
            str(written) if written else "<no override>",
        )

        # --- Plugin pipeline: render declared plugins into ~/.codex/config.toml ---
        # Drive the base per-runtime plugin adaptor pipeline. For an MCP-server
        # plugin (e.g. the privileged ``molecule-platform-mcp`` the concierge
        # declares) this resolves to MCPServerAdaptor, which calls
        # ``register_mcp_server_hook`` — our override below — to render the
        # ``[mcp_servers.<name>]`` table into the codex config.toml the running
        # CLI reads. Without this call the declared management MCP is NEVER
        # written for codex, so a codex concierge boots without create_workspace
        # (the #3159 class of bug — wired-to-a-file-the-runtime-never-reads, here
        # specifically: not wired at all). Mirrors the claude-code adapter's
        # ``install_plugins_via_registry`` call from its own setup().
        from molecule_runtime.plugins import load_plugins
        workspace_plugins_dir = os.path.join(config.config_path, "plugins")
        plugins = load_plugins(
            workspace_plugins_dir=workspace_plugins_dir,
            shared_plugins_dir=os.environ.get("PLUGINS_DIR", "/plugins"),
        )
        await self.install_plugins_via_registry(config, plugins)

        # --- SSOT: publish the single base-built system prompt onto config ---
        # The codex executor consumes ``config.system_prompt`` as
        # ``developerInstructions`` (executor.py). That field is BASE-OWNED and
        # is None until something fills it. Build it HERE via the one canonical
        # builder (``build_system_prompt``), which honors ``config.prompt_files``
        # (with the legacy ``system-prompt.md`` fallback baked in) — so the codex
        # concierge gets the SAME prompt-file resolution every other runtime
        # gets, instead of an empty ``developerInstructions``. This closes the
        # per-runtime prompt drift (the executor must never re-read
        # /configs/system-prompt.md itself and ignore prompt_files). Plugin
        # rules/prompts already loaded above are folded in so the assembled
        # prompt matches the base ``_common_setup`` shape.
        from molecule_runtime.prompt import build_system_prompt
        config.system_prompt = build_system_prompt(
            config.config_path,
            config.workspace_id,
            [],  # skills: codex does not load LangChain skills into the prompt
            [],  # peers: fetched live per-turn by the platform tools, not baked
            prompt_files=config.prompt_files,
            plugin_rules=getattr(plugins, "rules", None),
            plugin_prompts=list(getattr(plugins, "prompt_fragments", []) or []),
        )

    def register_mcp_server_hook(self, config, name, spec):
        """Codex MCP-wiring PORT override: inject literal molecule-* env values.

        Codex's MCP-child env whitelist (codex-rs/rmcp-client/src/utils.rs) only
        forwards a small set (HOME / PATH / LANG / …) and DROPS the molecule-
        specific runtime env — so an MCP server spawned by codex never inherits
        ``MOLECULE_CP_URL`` / ``MOLECULE_ADMIN_TOKEN`` / ``PLATFORM_URL`` / etc.
        from the parent process the way Claude Code's MCP child does. The
        management MCP (``@molecule-ai/mcp-server``) reads MOLECULE_CP_URL +
        MOLECULE_ADMIN_TOKEN to reach the controlplane; without them
        create_workspace 401s/no-ops even though the server is declared.

        Fix: resolve those values at install time and merge them as LITERALS
        into the spec's ``env`` block before the codex renderer writes the codex
        ``[mcp_servers.<name>.env]`` sub-table — the exact pattern the hardcoded
        ``[mcp_servers.molecule]`` a2a block uses for WORKSPACE_ID / PLATFORM_URL
        (codex_mcp_config.sh). Values already present in the plugin descriptor's
        env (e.g. MOLECULE_MCP_MODE) are preserved and win, so this only fills
        the whitelist-dropped gaps.

        ADR-004: this override now renders codex's config.toml DIRECTLY via the
        adapter-owned :func:`_render_codex_config` (was ``super()`` → engine
        mcp_render dispatch). The adapter is self-contained — it owns its
        renderer so the codex install works without the engine's per-runtime
        dispatch table. The privileged-env enrichment
        (``inject_privileged_env``) that the base funnel applies for the
        management MCP is preserved here explicitly (no-op for non-management
        names; idempotent + descriptor-wins), so a direct caller of this hook
        (e.g. the ensure-management-MCP self-heal) still gets the enriched spec.
        """
        from molecule_runtime.privileged_mcp_env import inject_privileged_env

        # Belt-and-suspenders: enrich the privileged MCP spec for any caller that
        # invokes this hook DIRECTLY (not only via install_plugins_via_registry's
        # funnel). No-op for non-management names; idempotent + descriptor-wins.
        spec = inject_privileged_env(name, spec)

        spec = dict(spec)
        descriptor_env = dict(spec.get("env") or {})

        # The molecule-* runtime env codex would otherwise drop. Only keys whose
        # value is actually present in this process are injected (an empty string
        # written into the TOML would shadow nothing useful and could confuse the
        # MCP server's "is this configured?" checks). Descriptor-declared keys
        # are NOT overwritten.
        for key in (
            "MOLECULE_CP_URL",
            "MOLECULE_ADMIN_TOKEN",
            "PLATFORM_URL",
            "WORKSPACE_ID",
            "MOLECULE_ORG_ID",
        ):
            if key in descriptor_env:
                continue
            val = os.environ.get(key)
            if val:
                descriptor_env[key] = val

        if descriptor_env:
            spec["env"] = descriptor_env

        target = _codex_path(config.config_path)
        _render_codex_config(target, name, spec)
        logger.info(
            "register_mcp_server_hook: wired MCP %r into %s (runtime=codex)",
            name, target,
        )

    # ------------------------------------------------------------------
    # ADR-004 adapter-socket: the rest of the codex MCP + persona seam,
    # owned by this adapter (relocated from mcp_render / persona_render).
    # ------------------------------------------------------------------
    def mcp_settings_path(self, config: AdapterConfig) -> str:
        """Absolute native MCP-config file codex reads (~/.codex/config.toml).

        Adapter-owned (was mcp_render.mcp_settings_path_for dispatch). ``config``
        is accepted for socket uniformity; codex resolves ``$HOME`` and ignores
        ``config.config_path``."""
        return str(_codex_path(config.config_path))

    def management_mcp_present(self, config: AdapterConfig) -> bool:
        """True when the ``molecule-platform`` management MCP is declared in
        codex's native config.toml (the RCA#2970 online-gate probe for codex).

        Adapter-owned (was mcp_render.management_mcp_present_for dispatch).
        Fail-CLOSED: a missing/unreadable/malformed config → False."""
        from molecule_runtime.platform_agent_identity import MANAGEMENT_MCP_NAME

        return _codex_config_has(_codex_path(config.config_path), MANAGEMENT_MCP_NAME)

    async def enumerate_loaded_mcp_tools(self, config: AdapterConfig):
        """Enumerate the LOADED MCP tool ids from codex's native config.toml.

        Adapter-owned (was the base default's mcp_render.read_mcp_servers_for
        switch). Reads codex's own ``[mcp_servers.*]`` map and hands the resolved
        ``{name: spec}`` to the shared boot-safe stdio probe engine
        (``enumerate_from_specs_async``) — the generic, runtime-name-free engine
        the platform keeps. Preserves the TRI-STATE (None / [] / [ids]) +
        never-raise + boot-safety guarantees (they live in the shared engine)."""
        from molecule_runtime.loaded_mcp_tools_probe import (
            enumerate_from_specs_async,
        )

        try:
            servers = _read_codex_mcp_servers(_codex_path(config.config_path))
        except Exception:  # noqa: BLE001 — enumeration must never crash boot
            logger.warning(
                "codex enumerate_loaded_mcp_tools: could not read declared MCP "
                "servers — leaving producer unset (grace window applies)",
                exc_info=True,
            )
            return None
        return await enumerate_from_specs_async(servers)

    def materialize_persona(self, config: AdapterConfig):
        """Materialize the canonical persona into codex's native AGENTS.md.

        Adapter-owned (was persona_render.materialize_persona_for dispatch). The
        persona is read runtime-agnostically from the delivered
        ``config.prompt_files`` via the shared engine helper
        ``read_canonical_persona`` (a generic, runtime-name-free helper the
        engine keeps). Best-effort: returns ``None`` (no-op) when no persona is
        delivered, never clobbering codex's baked default with an empty
        identity."""
        from molecule_runtime import persona_render

        persona = persona_render.read_canonical_persona(
            config.config_path, config.prompt_files
        )
        if not (persona or "").strip():
            logger.info(
                "materialize_persona: no canonical persona delivered for codex "
                "— leaving AGENTS.md untouched",
            )
            return None
        target = _materialize_codex_persona(Path(config.config_path), persona)
        logger.info(
            "materialize_persona: wrote codex persona (%d chars) to %s",
            len(persona), target,
        )
        return target

    async def create_executor(self, config: AdapterConfig):
        from executor import CodexAppServerExecutor
        return CodexAppServerExecutor(config)


Adapter = CodexAdapter
