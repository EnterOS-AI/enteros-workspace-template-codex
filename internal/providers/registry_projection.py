"""Registry-projection artifact generator + subset-relation gate.

Path B of internal#718 P4 codegen scope. CTO direction 2026-05-27:
- Each template's hand-authored authoritative source (`providers:` block for
  claude-code/hermes/codex; `runtime_config.models[]` for openclaw;
  top-level `models[]` for langgraph) STAYS authoritative and ships unchanged.
- This module reads the SYNCED copy of the canonical providers registry
  (`internal/providers/providers.yaml`, kept in sync with molecule-controlplane
  by the `.gitea/workflows/sync-providers-yaml.yml` workflow) and emits a
  REGISTRY-PROJECTION ARTIFACT (`internal/providers/registry-projection.json`)
  describing the registry's view of THIS template's runtime.
- A CI drift gate (`.gitea/workflows/verify-providers-projection.yml`) asserts
  two invariants:
    1. The checked-in projection is byte-identical to what regeneration would
       emit (catches a stale artifact + a hand-edit of the projection).
    2. The projection is a **SUBSET** of the template's hand-authored
       authoritative source — every (provider, model) pair the registry
       claims this runtime can serve must also be servable by the template's
       shipping config. The hand-authored block stays authoritative; this
       gate only stops the registry from over-claiming.

Federation contract: if the registry does not list this template's runtime,
the gate fails OPEN (langgraph). Mirrors molecule-ci's
`validate-workspace-template.py` `check_full_providers_block` fail-open path.

Path B does NOT enforce the inverse direction (template ⊆ registry); that is
already enforced by molecule-ci's full-providers-block gate. Path B closes
the federation-vs-registry over-claim hole orthogonally.

Run modes:
    python -m internal.providers.registry_projection generate
        Regenerate registry-projection.json from the synced providers.yaml.
    python -m internal.providers.registry_projection verify
        Regenerate in memory and exit 1 if the artifact has drifted.
    python -m internal.providers.registry_projection subset
        Run only the subset assertion (no artifact regen).
    python -m internal.providers.registry_projection check
        Both verify (artifact drift) AND subset (registry ⊆ template).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml

# Each PR sets RUNTIME_NAME via the per-repo wrapper script. This module is
# template-shape-agnostic; the wrapper passes the runtime id + the template's
# config.yaml path.


def load_manifest(providers_yaml_path: str | os.PathLike) -> dict:
    """Load the synced providers.yaml manifest."""
    with open(providers_yaml_path) as f:
        return yaml.safe_load(f)


def project_runtime(manifest: dict, runtime: str) -> dict:
    """Extract the registry's view of `runtime` as a sorted, stable JSON shape.

    Returns the canonical projection. If the runtime is absent from the
    manifest, returns a federation-fail-open marker:
        {"runtime": <name>, "in_registry": False, "providers": []}
    """
    runtimes = (manifest.get("runtimes") or {})
    if runtime not in runtimes:
        return {
            "runtime": runtime,
            "in_registry": False,
            "providers": [],
        }
    view = runtimes[runtime] or {}
    # Sort providers + their model lists for stable bytes.
    sorted_providers = []
    for ref in view.get("providers") or []:
        name = ref.get("name")
        models = sorted(ref.get("models") or [])
        sorted_providers.append({"name": name, "models": models})
    sorted_providers.sort(key=lambda p: p["name"])
    return {
        "runtime": runtime,
        "in_registry": True,
        "providers": sorted_providers,
    }


def render_projection(projection: dict) -> str:
    """Render the projection as the canonical JSON shape the artifact uses.

    Stable bytes are mandatory — the verify gate byte-compares. The
    artifact is pure JSON (parseable by any tool) — provenance lives in
    internal/providers/README.md, NOT in a JSON-with-comments header,
    so `jq .` works out of the box. The leading `_provenance` field
    embeds the DO-NOT-EDIT marker in the data itself.
    """
    framed = {
        "_provenance": (
            "AUTO-GENERATED - DO NOT EDIT. Regenerated from the synced "
            "internal/providers/providers.yaml by "
            "`python internal/providers/registry_projection.py generate <runtime>`. "
            "Hand-authored config.yaml stays authoritative; this is the "
            "registry's informational projection of this runtime."
        ),
        **projection,
    }
    # ensure_ascii=False keeps the artifact human-readable (no \uXXXX
    # escapes); indent=2 + sort_keys=True gives stable bytes the verify
    # gate can byte-compare.
    return json.dumps(framed, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


# -----------------------------------------------------------------------------
# Per-template authoritative-source readers + servability predicates.
#
# Each predicate answers: given the template's config.yaml, can the template
# serve (provider, model)? Semantics differ by template shape.
# -----------------------------------------------------------------------------

def _claude_code_servable(cfg: dict, provider: str, model: str) -> tuple[bool, str]:
    """claude-code: top-level `providers:` block with model_prefixes +
    model_aliases. Adapter strips `anthropic:`/`claude:` colon prefixes before
    resolving (see adapter.py `_strip_provider_prefix`). Any other prefix is
    passed through verbatim, so colon-form ids on non-anthropic providers
    don't auto-resolve."""
    tprov = {p["name"]: p for p in (cfg.get("providers") or [])}
    if provider not in tprov:
        return False, f"provider {provider!r} not in template providers block"
    entry = tprov[provider]
    prefixes = tuple(str(p).lower() for p in (entry.get("model_prefixes") or []))
    aliases = set(str(a).lower() for a in (entry.get("model_aliases") or []))
    m = model.lower()
    for sp in ("anthropic:", "claude:"):
        if m.startswith(sp):
            m = m[len(sp):]
            break
    if m in aliases:
        return True, f"alias hit on {provider!r}"
    for pref in prefixes:
        if pref and m.startswith(pref):
            return True, f"prefix {pref!r} hit on {provider!r}"
    return False, (
        f"after colon-strip={m!r}, no alias/prefix on {provider!r} "
        f"(aliases={sorted(aliases)}, prefixes={list(prefixes)})"
    )


def _prefix_only_servable(cfg: dict, provider: str, model: str) -> tuple[bool, str]:
    """hermes-shape (and codex, with platform-force handling): top-level
    `providers:` block, prefix-only resolution; no colon strip.
    The `platform` provider entry typically has empty model_prefixes by
    design — the adapter force-selects it via explicit provider= under
    platform-managed billing. We treat a present `platform` entry as
    servable for any platform-namespaced model id."""
    tprov = {p["name"]: p for p in (cfg.get("providers") or [])}
    if provider not in tprov:
        # codex special-case: registry's "openai" might map to any template
        # provider that prefix-matches (openai-subscription / openai-api).
        for tname, t in tprov.items():
            prefixes = tuple(str(p).lower() for p in (t.get("model_prefixes") or []))
            if any(model.lower().startswith(pr) for pr in prefixes if pr):
                return True, (
                    f"registry provider {provider!r} not in template, but "
                    f"template provider {tname!r} prefix-matches {model!r}"
                )
        return False, f"provider {provider!r} not in template providers block"
    entry = tprov[provider]
    prefixes = tuple(str(p).lower() for p in (entry.get("model_prefixes") or []))
    if provider == "platform":
        # Platform entry exists; explicit-provider force handles routing.
        return True, "platform entry present; routed via explicit provider= force"
    if any(model.lower().startswith(pr) for pr in prefixes if pr):
        return True, f"prefix hit on {provider!r}"
    return False, (
        f"no prefix in {list(prefixes)} matches {model!r} on {provider!r}"
    )


def _models_list_servable(cfg: dict, provider: str, model: str) -> tuple[bool, str]:
    """openclaw-shape: runtime_config.models[] is the authoritative source.
    Pair is servable iff the literal model id is in the list AND (for
    platform pairs) the entry is marked provider: platform."""
    rc = cfg.get("runtime_config") or {}
    models = rc.get("models") or []
    byok_ids = set()
    plat_ids = set()
    for m in models:
        if not isinstance(m, dict):
            continue
        mid = m.get("id")
        if not mid:
            continue
        if m.get("provider") == "platform":
            plat_ids.add(mid)
        else:
            byok_ids.add(mid)
    if provider == "platform":
        return (model in plat_ids), (
            "in template platform models" if model in plat_ids
            else "not in template platform models"
        )
    return (model in byok_ids), (
        "in template BYOK models" if model in byok_ids
        else f"not in template BYOK models (have {sorted(byok_ids)})"
    )


def _toplevel_models_list_servable(cfg: dict, provider: str, model: str) -> tuple[bool, str]:
    """langgraph-shape: top-level models[] is the authoritative source.
    Same semantics as openclaw but at the top level."""
    models = cfg.get("models") or []
    byok_ids = set()
    plat_ids = set()
    for m in models:
        if not isinstance(m, dict):
            continue
        mid = m.get("id")
        if not mid:
            continue
        if m.get("provider") == "platform":
            plat_ids.add(mid)
        else:
            byok_ids.add(mid)
    if provider == "platform":
        return (model in plat_ids), (
            "in template platform models" if model in plat_ids
            else "not in template platform models"
        )
    return (model in byok_ids), (
        "in template BYOK models" if model in byok_ids
        else f"not in template BYOK models (have {sorted(byok_ids)})"
    )


SERVABILITY_BY_RUNTIME = {
    "claude-code": _claude_code_servable,
    "hermes": _prefix_only_servable,
    "codex": _prefix_only_servable,
    "openclaw": _models_list_servable,
    "langgraph": _toplevel_models_list_servable,
}


def check_subset(
    projection: dict,
    template_config: dict,
    runtime: str,
) -> tuple[bool, list[tuple[str, str, str]]]:
    """Assert that every (provider, model) in the projection is servable by
    the template's hand-authored authoritative source.

    Returns (ok, violations). Each violation is (provider, model, reason).

    Fail-open contract: if the projection says in_registry=False (runtime not
    in registry — federation), returns (True, []) — drift gate stays GREEN
    so non-first-party runtimes can ship without registry coverage.
    """
    if not projection.get("in_registry", False):
        return True, []
    pred = SERVABILITY_BY_RUNTIME.get(runtime)
    if pred is None:
        # Unknown runtime — defensive fail-open with a clear marker so the
        # gate doesn't silently false-green on a typo.
        return True, [("<runtime>", runtime, "no servability predicate registered; fail-open")]
    violations: list[tuple[str, str, str]] = []
    for prov in projection.get("providers") or []:
        pname = prov["name"]
        for m in prov.get("models") or []:
            ok, reason = pred(template_config, pname, m)
            if not ok:
                violations.append((pname, m, reason))
    return (not violations), violations


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def _here() -> Path:
    return Path(__file__).resolve().parent


def _read_template_config(template_root: Path) -> dict:
    with open(template_root / "config.yaml") as f:
        return yaml.safe_load(f)


def _resolve_paths(template_root: Path) -> tuple[Path, Path]:
    providers_yaml = template_root / "internal" / "providers" / "providers.yaml"
    artifact = template_root / "internal" / "providers" / "registry-projection.json"
    return providers_yaml, artifact


def main(argv: list[str]) -> int:
    """CLI dispatch. Argv shape:
        registry_projection.py <generate|verify|subset|check> <runtime> [template_root]
    """
    if len(argv) < 3:
        print(
            "usage: registry_projection.py <generate|verify|subset|check> "
            "<runtime> [template_root]",
            file=sys.stderr,
        )
        return 2
    cmd, runtime = argv[1], argv[2]
    template_root = Path(argv[3]) if len(argv) >= 4 else _here().parent.parent
    providers_yaml, artifact = _resolve_paths(template_root)

    if not providers_yaml.is_file():
        print(f"ERROR: {providers_yaml} not found (synced copy of the canonical providers registry).", file=sys.stderr)
        return 1

    manifest = load_manifest(providers_yaml)
    projection = project_runtime(manifest, runtime)
    rendered = render_projection(projection)

    if cmd == "generate":
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(rendered)
        print(f"wrote {artifact}")
        return 0

    if cmd in ("verify", "check"):
        if not artifact.is_file():
            print(
                f"ERROR: {artifact} missing. Run "
                f"`python -m internal.providers.registry_projection generate {runtime}` "
                f"and commit the result.",
                file=sys.stderr,
            )
            return 1
        existing = artifact.read_text()
        if existing != rendered:
            print(
                f"ERROR: {artifact} drifted from regeneration. "
                f"Run `python -m internal.providers.registry_projection generate {runtime}` "
                f"and commit the result.",
                file=sys.stderr,
            )
            return 1
        print(f"OK — {artifact} is in sync with the synced providers.yaml.")
        if cmd == "verify":
            return 0

    if cmd in ("subset", "check"):
        cfg = _read_template_config(template_root)
        ok, violations = check_subset(projection, cfg, runtime)
        if not ok:
            print(
                "ERROR: registry-projection subset violation — the registry's view of "
                f"runtime {runtime!r} claims (provider, model) pairs the template's "
                f"hand-authored authoritative block does NOT list:",
                file=sys.stderr,
            )
            for prov, model, reason in violations:
                print(
                    f"  - ({prov}, {model}): {reason}\n"
                    f"    FIX: either add this pair to the template's authoritative source, "
                    f"    or retire it from the registry (controlplane "
                    f"internal/providers/providers.yaml `runtimes:` block).",
                    file=sys.stderr,
                )
            return 1
        marker = "fail-open (runtime not in registry)" if not projection.get("in_registry") else "subset holds"
        print(f"OK — registry ⊆ template authoritative source for {runtime!r} ({marker}).")
        return 0

    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
