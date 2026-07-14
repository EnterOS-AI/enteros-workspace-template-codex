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


def test_retired_ecr_lifecycle_helper_is_absent():
    assert not (ROOT / "scripts" / "ensure-ecr-lifecycle.sh").exists()
