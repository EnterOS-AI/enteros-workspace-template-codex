from __future__ import annotations

import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = ROOT / ".gitea" / "workflows"
REVIEWED_MOLECULE_CI_REF = "".join(("11b8598e5c0b3f0b1031", "733a8d5f6bc238f146a4"))
GIT_CLONE = re.compile(r"\bgit\s+clone\b")


def _workflow_files() -> list[Path]:
    return sorted((*WORKFLOW_DIR.glob("*.yml"), *WORKFLOW_DIR.glob("*.yaml")))


def _scoped_fetch_workflows() -> list[Path]:
    workflows = [WORKFLOW_DIR / "ci.yml"]
    secret_scan = WORKFLOW_DIR / "secret-scan.yml"
    if secret_scan.exists():
        scanner_workflow = secret_scan.read_text()
        if (
            "molecule-ci-ssot" in scanner_workflow
            or "scan_diff_secrets.py" in scanner_workflow
        ):
            workflows.append(secret_scan)
    return workflows


def test_no_workflow_uses_a_mutable_molecule_ci_clone() -> None:
    offenders = []
    for path in _workflow_files():
        workflow = yaml.safe_load(path.read_text())
        for job_name, job in workflow.get("jobs", {}).items():
            for step in job.get("steps", []):
                script = str(step.get("run", ""))
                if "molecule-ci.git" in script and GIT_CLONE.search(script):
                    offenders.append(f"{path.relative_to(ROOT).as_posix()}:{job_name}")
    assert offenders == [], f"mutable molecule-ci clone(s): {offenders}"


def test_validation_and_secret_scan_fetches_are_exact_and_credential_free() -> None:
    observed_fetches = 0

    for workflow_path in _scoped_fetch_workflows():
        workflow = yaml.safe_load(workflow_path.read_text())
        workflow_env = workflow.get("env", {})
        workflow_fetches = 0

        for job_name, job in workflow["jobs"].items():
            steps = job.get("steps", [])
            fetch_steps = [
                step for step in steps if "molecule-ci.git" in str(step.get("run", ""))
            ]
            if not fetch_steps:
                continue

            checkouts = [
                step
                for step in steps
                if str(step.get("uses", "")).startswith("actions/checkout@")
            ]
            assert checkouts, (
                f"{workflow_path.name}:{job_name} has no repository checkout"
            )
            assert all(
                step.get("with", {}).get("persist-credentials") is False
                for step in checkouts
            ), f"{workflow_path.name}:{job_name} persists checkout credentials"

            for step in fetch_steps:
                observed_fetches += 1
                workflow_fetches += 1
                effective_env = {
                    **workflow_env,
                    **job.get("env", {}),
                    **step.get("env", {}),
                }
                assert (
                    effective_env.get("MOLECULE_CI_REF") == REVIEWED_MOLECULE_CI_REF
                ), f"{workflow_path.name}:{job_name} does not use the reviewed ref"

                script = " ".join(step["run"].replace("\\\n", " ").split())
                for required in (
                    "GIT_ASKPASS=/bin/false",
                    "GIT_TERMINAL_PROMPT=0",
                    "GIT_TERMINAL_PROMPT=0 git -c",
                    "git -c credential.helper= -c http.userAgent=curl/8.4.0",
                    "remote add origin https://git.moleculesai.app/molecule-ai/molecule-ci.git",
                    '--no-tags --depth 1 origin "$MOLECULE_CI_REF"',
                    "for attempt in 1 2 3; do",
                    'if [ "$fetched" != true ]; then',
                    "checkout -q --detach FETCH_HEAD",
                    "rev-parse HEAD",
                    "exit 1",
                ):
                    assert required in script, (
                        f"{workflow_path.name}:{job_name} molecule-ci fetch "
                        f"missing {required!r}"
                    )
                assert re.search(
                    r"(?:ACTUAL_CI_REF|test ).*rev-parse HEAD.*MOLECULE_CI_REF",
                    script,
                ), f"{workflow_path.name}:{job_name} does not verify fetched HEAD"

        assert workflow_fetches > 0, f"{workflow_path.name} has no molecule-ci fetch"

    assert observed_fetches > 0, "no molecule-ci validation/scanner fetch was checked"
