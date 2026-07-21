"""Contract checks for exact runtime provenance in pull-request image builds."""

from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = ROOT / ".gitea" / "workflows" / "ci.yml"
FORK_RUN = "github.event.pull_request.head.repo.fork != true"
FORK_SKIP = "github.event.pull_request.head.repo.fork == true"


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


def test_t4_image_cleanup_covers_build_and_probe_failures() -> None:
    steps = yaml.safe_load(CI_WORKFLOW.read_text())["jobs"]["t4-conformance"]["steps"]
    build_script = next(
        step["run"] for step in steps if "docker build" in step.get("run", "")
    )
    probe_script = next(
        step["run"]
        for step in steps
        if "docker run -d" in step.get("run", "")
    )

    assert build_script.index("trap cleanup_t4_build EXIT") < build_script.index(
        "docker build"
    )
    assert build_script.index("KEEP_T4_IMAGE=1") > build_script.index(
        '"$ACTUAL_RUNTIME_VERSION" != "$EXPECTED_RUNTIME_VERSION"'
    )
    assert probe_script.index("trap '") < probe_script.index("docker run -d")


def test_checkout_credentials_never_persist() -> None:
    jobs = yaml.safe_load(CI_WORKFLOW.read_text())["jobs"]
    checkouts = [
        step
        for job in jobs.values()
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith("actions/checkout@")
    ]

    assert checkouts
    assert all(step.get("with", {}).get("persist-credentials") is False for step in checkouts)


def test_fork_prs_do_not_execute_repository_tests() -> None:
    steps = yaml.safe_load(CI_WORKFLOW.read_text())["jobs"]["tests"]["steps"]
    run_steps = [step for step in steps if "run" in step]

    assert any(step.get("if") == FORK_SKIP for step in run_steps)
    for step in run_steps:
        if step.get("if") == FORK_SKIP:
            continue
        assert step.get("if") == FORK_RUN
