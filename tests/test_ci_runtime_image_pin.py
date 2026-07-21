"""Contract checks for exact runtime provenance in pull-request image builds."""

import hashlib
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = ROOT / ".gitea" / "workflows" / "ci.yml"
META_WORKFLOW = ROOT / ".gitea" / "workflows" / "meta-ci-advisory.yml"
FORK_RUN = "github.event.pull_request.head.repo.fork != true"
FORK_SKIP = "github.event.pull_request.head.repo.fork == true"
# Keep the immutable ref mechanically exact without presenting a quoted, bare
# 40-hex string to the repository's intentionally conservative secret scanner.
MOLECULE_CI_REF = "".join(("11b8598e5c0b3f0b1031733a8d5f6bc", "238f146a4"))
CANONICAL_META_SHA256 = (
    "24bae0ffc8e6cae1b5b3fdc1b7c80640796cfc8c8d5165bef2baad2831661937"
)


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
        step["run"] for step in steps if "docker run -d" in step.get("run", "")
    )

    assert build_script.index("trap cleanup_t4_build EXIT") < build_script.index(
        "docker build"
    )
    assert build_script.index("trap cleanup_t4_build EXIT") < build_script.index(
        'mkdir "$CI_ROOT"'
    )
    cleanup_body = build_script[
        build_script.index("cleanup_t4_build() {") : build_script.index(
            "trap cleanup_t4_build EXIT"
        )
    ]
    assert 'docker rm -f "$MCP_VERIFY_CONTAINER"' in cleanup_body
    assert 'rm -rf -- "$CI_ROOT"' in cleanup_body
    assert (
        'rm -f -- "$MCP_ATTESTATION" "$MCP_ATTESTATION_TMP" '
        '"$MCP_ATTESTATION_SHA256" "$MCP_E2E_LOG"' in cleanup_body
    )
    assert build_script.index("trap cleanup_t4_build EXIT") < build_script.index(
        "docker create --interactive --name"
    )
    assert build_script.index(
        'docker start --attach --interactive "$MCP_VERIFY_CONTAINER"'
    ) < build_script.index('docker rm "$MCP_VERIFY_CONTAINER" >/dev/null')
    assert build_script.index("KEEP_T4_IMAGE=1") > build_script.index(
        "grep -qxF 'mcp-built-image-e2e:sentinel:executed'"
    )
    assert probe_script.index("trap '") < probe_script.index("docker run -d")


def test_t4_runs_immutable_offline_mcp_verifier_against_same_final_image() -> None:
    job = yaml.safe_load(CI_WORKFLOW.read_text())["jobs"]["t4-conformance"]
    steps = job["steps"]
    build_index, build_step = next(
        (index, step)
        for index, step in enumerate(steps)
        if "docker build" in step.get("run", "")
    )
    privileged_index, privileged_step = next(
        (index, step)
        for index, step in enumerate(steps)
        if "docker run -d" in step.get("run", "")
    )
    build = build_step["run"]

    assert job["if"] == FORK_RUN
    assert job["env"]["MOLECULE_CI_REF"] == MOLECULE_CI_REF
    assert build_index < privileged_index
    assert "--privileged" in privileged_step["run"]
    assert ': "${RUNNER_TEMP:?RUNNER_TEMP is required}"' in build
    assert 'git init -q "$CI_ROOT"' in build
    assert (
        "remote add origin "
        "https://git.moleculesai.app/molecule-ai/molecule-ci.git" in build
    )
    assert "GIT_ASKPASS=/bin/false GIT_TERMINAL_PROMPT=0" in build
    assert "credential.helper=" in build
    assert "http.userAgent=curl/8.4.0" in build
    assert "for attempt in 1 2 3" in build
    assert 'if [ "$fetched" != true ]' in build
    assert 'fetch --no-tags --depth 1 origin "$MOLECULE_CI_REF"' in build
    assert "checkout -q --detach FETCH_HEAD" in build
    assert 'test "$(git -C "$CI_ROOT" rev-parse HEAD)" = "$MOLECULE_CI_REF"' in build
    assert 'mcp_pin_lockstep.py" --repo-root . --json' in build
    assert "load_attestation" in build
    assert 'EXPECTED_RUNTIME_VERSION="$(' in build
    assert '--build-arg RUNTIME_VERSION="$EXPECTED_RUNTIME_VERSION"' in build
    assert build.count("docker build") == 1

    required_verifier_fragments = (
        "docker create --interactive --name",
        "--network none",
        "--user 1000:1000 --workdir /tmp",
        "--cap-drop ALL --security-opt no-new-privileges",
        "--pids-limit 128 --memory 768m --cpus 1",
        "--tmpfs /tmp:size=64m",
        '--entrypoint python3 "$T4_TAG"',
        "/mcp_built_image_e2e.py",
        'docker cp "$CI_ROOT/scripts/mcp_built_image_e2e.py"',
        'docker start --attach --interactive "$MCP_VERIFY_CONTAINER"',
        '< "$MCP_ATTESTATION"',
        "grep -qxF 'mcp-built-image-e2e:sentinel:executed'",
    )
    for fragment in required_verifier_fragments:
        assert fragment in build

    attested_version_assignment = 'EXPECTED_RUNTIME_VERSION="$('
    assert build.index("mcp_pin_lockstep.py") < build.index(attested_version_assignment)
    assert build.index(attested_version_assignment) < build.index("docker build")
    assert "--volume" not in build
    assert build.index("docker build") < build.index("docker create")
    assert build.index("docker create") < build.index("docker cp")
    assert build.index("docker cp") < build.index("docker start")
    assert build.index("docker start") < build.index(
        "grep -qxF 'mcp-built-image-e2e:sentinel:executed'"
    )
    assert build.index("grep -qxF 'mcp-built-image-e2e:sentinel:executed'") < (
        build.index("KEEP_T4_IMAGE=1")
    )

    git_seal = (
        'git -C "$CI_ROOT" diff --quiet --no-ext-diff --no-textconv '
        '"$MOLECULE_CI_REF" -- scripts/mcp_pin_lockstep.py '
        "scripts/mcp_built_image_e2e.py"
    )
    attestation_check = 'sha256sum --check "$MCP_ATTESTATION_SHA256"'
    checker = 'python3 "$CI_ROOT/scripts/mcp_pin_lockstep.py"'
    assert build.count(git_seal) == 3
    assert build.count(attestation_check) == 2
    assert build.index(git_seal) < build.index(checker)
    second_seal = build.index(git_seal, build.index(checker))
    first_check = build.index(attestation_check)
    assert second_seal < first_check < build.index("load_attestation")
    third_seal = build.index(git_seal, second_seal + len(git_seal))
    second_check = build.index(attestation_check, first_check + len(attestation_check))
    assert third_seal < build.index("docker cp") < second_check
    assert second_check < build.index("docker start")


def test_meta_ci_advisory_is_the_immutable_canonical_copy() -> None:
    payload = META_WORKFLOW.read_bytes()

    assert hashlib.sha256(payload).hexdigest() == CANONICAL_META_SHA256


def test_checkout_credentials_never_persist() -> None:
    jobs = yaml.safe_load(CI_WORKFLOW.read_text())["jobs"]
    checkouts = [
        step
        for job in jobs.values()
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith("actions/checkout@")
    ]

    assert checkouts
    assert all(
        step.get("with", {}).get("persist-credentials") is False for step in checkouts
    )


def test_fork_prs_do_not_execute_repository_tests() -> None:
    steps = yaml.safe_load(CI_WORKFLOW.read_text())["jobs"]["tests"]["steps"]
    run_steps = [step for step in steps if "run" in step]

    assert any(step.get("if") == FORK_SKIP for step in run_steps)
    for step in run_steps:
        if step.get("if") == FORK_SKIP:
            continue
        assert step.get("if") == FORK_RUN
