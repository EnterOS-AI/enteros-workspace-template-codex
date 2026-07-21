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
