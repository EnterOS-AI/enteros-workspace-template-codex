"""Contract checks for exact runtime provenance in pull-request image builds."""

from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = ROOT / ".gitea" / "workflows" / "ci.yml"


def _docker_build_script(job_name: str) -> str:
    jobs = yaml.safe_load(CI_WORKFLOW.read_text())["jobs"]
    scripts = [
        step.get("run", "")
        for step in jobs[job_name]["steps"]
        if "docker build" in step.get("run", "")
    ]
    assert len(scripts) == 1, f"expected one docker build in {job_name}"
    return scripts[0]


@pytest.mark.parametrize("job_name", ("validate-runtime", "t4-conformance"))
def test_pr_image_build_pins_and_verifies_exact_runtime(job_name: str) -> None:
    script = _docker_build_script(job_name)

    assert ".runtime-version" in script
    assert '--build-arg RUNTIME_VERSION="$EXPECTED_RUNTIME_VERSION"' in script
    assert "importlib.metadata import version" in script
    assert 'version("molecules-workspace-runtime")' in script
    assert '"$ACTUAL_RUNTIME_VERSION" != "$EXPECTED_RUNTIME_VERSION"' in script

    if job_name == "validate-runtime":
        assert (
            'SMOKE_TAG="molecule-ai-workspace-codex-smoke-'
            '${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"' in script
        )
        assert ': "${GITHUB_RUN_ID:?GITHUB_RUN_ID is required}"' in script
        assert ': "${GITHUB_RUN_ATTEMPT:?GITHUB_RUN_ATTEMPT is required}"' in script
        assert '-t "$SMOKE_TAG"' in script
        assert 'docker run --rm --entrypoint python3 "$SMOKE_TAG"' in script
        assert 'docker image rm -f "$SMOKE_TAG"' in script
        assert "template-test" not in script
    else:
        assert (
            'T4_TAG="t4-conformance-test:'
            '${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}"' in script
        )
        assert '-t "$T4_TAG"' in script
        assert 'docker run --rm --entrypoint python3 "$T4_TAG"' in script
        assert "SMOKE_TAG" not in script
