"""CLI entry point — render ``~/.codex/config.toml`` from the providers registry.

Invoked from ``start.sh`` (replacing the old hardcoded
``codex_minimax_config.sh`` invocation). The actual logic lives in
``provider_config.py`` so the adapter and the boot script share one
implementation; this file is just an ``argparse``-free wrapper that
loads the YAML registry, resolves the provider against the current
env, and writes the config.toml.

Exit codes:
  0 — wrote a config.toml (compat provider with model_provider override).
  0 — wrote nothing (built-in OpenAI mode; codex uses its native default).
  2 — registry / env misconfig (raised ValueError); we print the message
      so ``start.sh`` can surface it to ``docker logs`` and the operator.

Usage:
  python3 render_provider_toml.py                 # auto-resolve
  python3 render_provider_toml.py --provider X    # explicit provider
  python3 render_provider_toml.py --model Y       # explicit model
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# Allow running from /usr/local/bin or /app via either copy.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("CODEX_PROVIDER_LOG_LEVEL", "INFO"),
        format="[codex-provider] %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="Render ~/.codex/config.toml from the providers registry"
    )
    parser.add_argument("--provider", default="", help="explicit provider name")
    parser.add_argument("--model", default="", help="explicit model id")
    parser.add_argument(
        "--codex-home",
        default=os.environ.get("CODEX_HOME") or "",
        help="override $CODEX_HOME (default: $CODEX_HOME or ~/.codex)",
    )
    parser.add_argument(
        "--workspace-config",
        default=os.environ.get("WORKSPACE_CONFIG_PATH", "/configs"),
        help="workspace config dir (for the per-workspace YAML fallback)",
    )
    args = parser.parse_args()

    try:
        from provider_config import (
            load_providers, resolve_provider, write_config_toml,
        )
    except ImportError as exc:
        print(f"[codex-provider] FATAL: provider_config import failed: {exc}",
              file=sys.stderr)
        return 2

    providers = load_providers(workspace_config_path=args.workspace_config)
    explicit_provider = args.provider or os.environ.get("MODEL_PROVIDER", "")
    model = args.model or os.environ.get("MODEL", "")

    try:
        picked = resolve_provider(
            model or None, providers,
            explicit_provider=explicit_provider or None,
        )
    except ValueError as exc:
        print(f"[codex-provider] {exc}", file=sys.stderr)
        return 2

    try:
        written = write_config_toml(
            picked,
            model=model or None,
            codex_home=args.codex_home or None,
        )
    except ValueError as exc:
        print(f"[codex-provider] {exc}", file=sys.stderr)
        return 2

    if written:
        print(
            f"[codex-provider] wrote {written} provider={picked['name']} "
            f"auth_mode={picked['auth_mode']}"
        )
    else:
        print(
            f"[codex-provider] no config.toml override needed "
            f"(provider={picked['name']} auth_mode={picked['auth_mode']}); "
            "codex will use its built-in OpenAI/Responses path"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
