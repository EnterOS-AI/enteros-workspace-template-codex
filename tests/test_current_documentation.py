import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

RETIRED_GUIDANCE = {
    r"github://Molecule-AI": "the suspended GitHub install scheme",
    r"github\.com/Molecule-AI": "the suspended Molecule-AI GitHub organization",
    r"git clone https://github\.com/your-org": "the placeholder GitHub clone route",
    r"https://platform\.molecule\.ai": "the retired platform hostname",
    r"\bECR\b": "the retired AWS ECR deployment path",
    r"\bEC2\b": "the retired AWS workspace path",
    r"ghcr\.io/molecule-ai": "the retired GHCR image path",
    r"\bGHCR\b": "the retired GHCR image path",
    r"\bRailway\b": "the retired Railway deployment path",
    r"operator-host": "the retired operator-host access model",
    r"git push origin main": "direct pushes to the protected main branch",
}

RUNTIME_GUIDANCE_FILES = (
    ".gitea/workflows/ci.yml",
    ".gitea/workflows/publish-image.yml",
    ".gitea/workflows/sync-providers-yaml.yml",
    "Dockerfile",
    "adapter.py",
    "codex_minimax_config.sh",
    "executor.py",
    "provider_config.py",
    "requirements.txt",
)

RETIRED_RUNTIME_GUIDANCE = {
    r"\bAWS\b": "the retired AWS workspace topology",
    r"\bEBS\b": "the retired EBS restore topology",
    r"\bEC2\b": "the retired EC2 workspace topology",
    r"ec2\.go": "the retired EC2 provisioner name",
    r"\bECR\b": "the retired ECR registry",
    r"\bGHCR\b": "the retired GHCR registry",
    r"\bRailway\b": "the retired Railway deployment path",
    r"operator[- ]host": "the retired operator-host access model",
    r"Gitea 1\.22": "the retired Gitea server-version guidance",
    r"PyPI (publish|abuse)": "the retired public-PyPI release blocker",
    r"canonical provisioner-managed\s+install path used by the published": "the false published-image adapter path",
}


def _documentation_files() -> list[Path]:
    files = list(ROOT.glob("*.md"))
    files.extend(ROOT.glob("docs/**/*.md"))
    files.extend(ROOT.glob("runbooks/**/*.md"))
    return sorted(set(files))


def test_active_documentation_has_no_retired_operational_guidance():
    findings = []
    for path in _documentation_files():
        text = path.read_text(encoding="utf-8")
        for pattern, description in RETIRED_GUIDANCE.items():
            if re.search(pattern, text, re.IGNORECASE):
                findings.append(f"{path.relative_to(ROOT)}: {description}")

    assert not findings, "retired guidance remains:\n" + "\n".join(findings)


def test_runtime_comments_have_no_retired_operational_guidance():
    findings = []
    for relative_path in RUNTIME_GUIDANCE_FILES:
        text = (ROOT / relative_path).read_text(encoding="utf-8")
        for pattern, description in RETIRED_RUNTIME_GUIDANCE.items():
            if re.search(pattern, text, re.IGNORECASE):
                findings.append(f"{relative_path}: {description}")

    assert not findings, "retired runtime guidance remains:\n" + "\n".join(findings)


def test_retired_ecr_lifecycle_helper_is_absent():
    assert not (ROOT / "scripts" / "ensure-ecr-lifecycle.sh").exists()
