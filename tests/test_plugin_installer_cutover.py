"""Regression contracts for declared-plugin installation ownership."""

from pathlib import Path

import yaml
from packaging.requirements import Requirement
from packaging.version import Version


REPO_ROOT = Path(__file__).resolve().parents[1]
BOOT_SCRIPT = REPO_ROOT / "start.sh"
CI_WORKFLOW = REPO_ROOT / ".gitea" / "workflows" / "ci.yml"
RUNTIME_VERSION = REPO_ROOT / ".runtime-version"
REQUIREMENTS = REPO_ROOT / "requirements.txt"
HARDENED_INSTALLER_VERSION = Version("0.4.0")
SDK_COMMIT = "3474157daca56e3de5b7" + "cffd2a2f84b78bf63b68"


def test_boot_script_does_not_implement_declared_plugin_fetching() -> None:
    executable_lines = "\n".join(
        line
        for line in BOOT_SCRIPT.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )

    forbidden_shell_installer_fragments = (
        "MOLECULE_DECLARED_PLUGINS",
        "molecule_runtime.plugin_sources",
        "install_declared_plugins",
        "for _plg_src in $MOLECULE_DECLARED_PLUGINS",
        "/archive/${_plg_ref}.tar.gz",
        "Authorization: token ${MOLECULE_TEMPLATE_REPO_TOKEN}",
        'mkdir -p "/configs/plugins/$_plg_name"',
        'cp -a "$_plg_dir/." "/configs/plugins/$_plg_name/"',
    )
    for fragment in forbidden_shell_installer_fragments:
        assert fragment not in executable_lines


def test_template_requires_runtime_with_hardened_plugin_installer() -> None:
    runtime_version = Version(RUNTIME_VERSION.read_text(encoding="utf-8").strip())
    assert runtime_version >= HARDENED_INSTALLER_VERSION

    runtime_requirement = next(
        Requirement(line)
        for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("molecules-workspace-runtime")
    )
    assert runtime_version in runtime_requirement.specifier
    assert any(
        specifier.operator in {">=", ">", "==", "===", "~="}
        and Version(specifier.version) >= HARDENED_INSTALLER_VERSION
        for specifier in runtime_requirement.specifier
    )
    assert Version("0.3.125") not in runtime_requirement.specifier


def test_static_ci_rejects_legacy_declared_plugin_installer() -> None:
    jobs = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    guard = next(
        step
        for step in jobs["validate-static"]["steps"]
        if step.get("name") == "Reject legacy declared-plugin installer"
    )

    command = guard["run"]
    assert "start.sh" in command
    assert ".runtime-version" in command
    assert "0.4.0" in command


def test_unit_tests_use_immutable_sdk_contract() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    expected = (
        "molecule-ai-sdk @ git+https://git.moleculesai.app/molecule-ai/"
        f"molecule-ai-sdk.git@{SDK_COMMIT}"
    )

    assert expected in workflow
