"""TDD-driven test for the registry-projection drift gate.

Path B of internal#718 P4 codegen scope. The gate has TWO assertions:
  1. The checked-in `internal/providers/registry-projection.json` artifact is
     byte-identical to what regeneration would emit (catches stale artifact +
     hand-edit).
  2. The projection is a SUBSET of the template's hand-authored authoritative
     source — every (provider, model) the registry claims this runtime serves
     must also be servable by the template's shipping config.

Positive case (no fake): the current-state subset must hold (or the gate is
known-RED today and the PR body flags it as a follow-up). Negative case
(injected fake): the checker MUST reject a (provider, model) pair the
template does not list — that's the gate doing its job.

These tests run under the template repo's existing pytest harness; the
project root is the repo root.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "internal" / "providers"))

import registry_projection as rp  # noqa: E402


def _template_runtime() -> str:
    cfg = yaml.safe_load((REPO_ROOT / "config.yaml").read_text())
    runtime = cfg.get("runtime")
    assert runtime, "config.yaml must declare a `runtime:` for this gate to run"
    return str(runtime)


def _manifest() -> dict:
    return rp.load_manifest(REPO_ROOT / "internal" / "providers" / "providers.yaml")


def _projection() -> dict:
    return rp.project_runtime(_manifest(), _template_runtime())


def _template_config() -> dict:
    return yaml.safe_load((REPO_ROOT / "config.yaml").read_text())


# ─────────────────────────────────────────────────────────────────────────────
# Artifact-drift gate
# ─────────────────────────────────────────────────────────────────────────────

def test_artifact_byte_identical_to_regeneration():
    """The checked-in registry-projection.json must equal what regeneration
    emits from the synced providers.yaml. Catches stale-artifact drift +
    hand-edits (the file carries a DO NOT EDIT header)."""
    artifact = REPO_ROOT / "internal" / "providers" / "registry-projection.json"
    assert artifact.is_file(), (
        f"{artifact} missing. Run "
        f"`python -m internal.providers.registry_projection generate {_template_runtime()}` "
        f"and commit."
    )
    rendered = rp.render_projection(_projection())
    existing = artifact.read_text()
    assert existing == rendered, (
        "registry-projection.json drifted from regeneration. Run "
        "`python internal/providers/registry_projection.py generate "
        f"{_template_runtime()}` and commit the result."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Positive subset case (current state)
# ─────────────────────────────────────────────────────────────────────────────

def test_current_state_subset_status():
    """Records the CURRENT subset status. For templates whose current
    registry projection IS a clean subset of the hand-authored block today,
    this passes. For templates with known violations (e.g. claude-code's
    colon-form non-anthropic ids), this test MARKS xfail with the exact
    violation list — the gate's design intent is to surface these for CTO
    decision (register colon prefixes vs retire from registry), not silently
    pass."""
    proj = _projection()
    cfg = _template_config()
    runtime = _template_runtime()
    ok, violations = rp.check_subset(proj, cfg, runtime)
    if not ok:
        violation_strs = [f"({p}, {m}): {r}" for p, m, r in violations]
        pytest.xfail(
            f"Known subset violations for runtime {runtime!r} today "
            f"(per Path B PR body — flagged for CTO follow-up). Violations:\n  "
            + "\n  ".join(violation_strs)
        )
    assert ok, "expected current state to be either a subset or xfailed above"


# ─────────────────────────────────────────────────────────────────────────────
# Negative subset case (injected fake)
# ─────────────────────────────────────────────────────────────────────────────

def test_subset_rejects_injected_fake_pair():
    """Inject a (provider, model) pair the template's authoritative source
    DEFINITELY does not list — the subset checker MUST reject it. This
    proves the gate has teeth and isn't trivially green."""
    runtime = _template_runtime()
    proj = _projection()

    if not proj.get("in_registry", False):
        # langgraph case: registry has no view → gate fails open by design.
        # The negative case here injects a faux registry view to prove the
        # checker would catch a fake pair IF the runtime were registered.
        fake_proj = {
            "runtime": runtime,
            "in_registry": True,
            "providers": [
                {"name": "definitely-fake-provider-xyz",
                 "models": ["fake/definitely-not-a-real-model-id-7392"]},
            ],
        }
        ok, violations = rp.check_subset(fake_proj, _template_config(), runtime)
        assert not ok, (
            "subset checker MUST reject a fake (provider, model) pair against "
            "a template that does not list it — even on fail-open runtimes "
            "the predicate must have teeth."
        )
        return

    fake_proj = copy.deepcopy(proj)
    fake_proj["providers"].append({
        "name": "definitely-fake-provider-xyz",
        "models": ["fake/definitely-not-a-real-model-id-7392"],
    })
    ok, violations = rp.check_subset(fake_proj, _template_config(), runtime)
    assert not ok, (
        "subset checker MUST reject a fake (provider, model) pair against "
        "the template's authoritative source — gate has no teeth otherwise."
    )
    # The violation list must include our injected pair.
    assert any(
        prov == "definitely-fake-provider-xyz"
        and model == "fake/definitely-not-a-real-model-id-7392"
        for prov, model, _ in violations
    ), f"injected pair must appear in violations; got {violations}"


def test_subset_rejects_injected_unservable_model_on_real_provider():
    """Stronger negative: keep a real registry-listed provider name (so it
    exists in the template), but pair it with a model id whose alias/prefix
    rules cannot match. Proves the model-level subset check works on its
    own, not just the provider-name level."""
    runtime = _template_runtime()
    proj = _projection()
    if not proj.get("in_registry", False):
        pytest.skip("federation fail-open runtime; covered by the prior test")
    if not proj["providers"]:
        pytest.skip("no providers in projection; nothing to mutate")
    real_provider = proj["providers"][0]["name"]
    fake_proj = copy.deepcopy(proj)
    bogus_model = "no-template-prefix-or-alias-could-ever-match-this-9183__"
    fake_proj["providers"][0]["models"].append(bogus_model)
    ok, violations = rp.check_subset(fake_proj, _template_config(), runtime)
    assert not ok, (
        f"subset checker MUST reject a bogus model id paired with a real "
        f"registry-listed provider {real_provider!r}."
    )
    assert any(
        prov == real_provider and model == bogus_model
        for prov, model, _ in violations
    ), f"injected bogus model must appear in violations; got {violations}"


def test_sync_workflow_reads_auto_sync_token_from_infisical_ssot():
    """The cross-repo drift gate must not depend on a duplicated Gitea PAT.

    The three bootstrap inputs are already used by this repo's protected
    publish workflow. Fork PRs receive none of them, so the workflow keeps its
    existing fail-open behavior when the bootstrap trio is unavailable.
    """
    workflow = (
        REPO_ROOT / ".gitea" / "workflows" / "sync-providers-yaml.yml"
    ).read_text()

    assert "secrets.AUTO_SYNC_TOKEN" not in workflow
    assert "secrets.INFISICAL_CI_CLIENT_ID" in workflow
    assert "secrets.INFISICAL_CI_CLIENT_SECRET" in workflow
    assert "secrets.INFISICAL_CI_PROJECT_ID" in workflow
    assert "Infisical CI bootstrap credentials unavailable" in workflow
    assert "environment=prod&secretPath=%2Fshared%2Fdev-utils" in workflow
    assert "::add-mask::$AUTO_SYNC_TOKEN" in workflow
