"""Shared pytest fixtures for the codex template test suite."""

from __future__ import annotations

import os
import shutil
import tempfile
import warnings
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def _fake_codex_on_path():
    """Ensure ``codex`` is on PATH so credential-preflight tests run.

    The CI runner image does not install the real ``@openai/codex`` npm
    package, so ``shutil.which("codex")`` returns ``None`` and several
    fail-closed credential tests in ``test_modernization_pr1.py`` skip
    themselves silently.  This fixture creates a minimal stub executable
    on a temporary directory that is prepended to ``PATH``, satisfying
    the ``which`` check without pulling in the full CLI (unit-test scope).

    If the real ``codex`` binary is already present, the fixture is a
    no-op.
    """
    if shutil.which("codex"):
        yield
        return

    with tempfile.TemporaryDirectory(prefix="fake-codex-") as tmpdir:
        codex_path = Path(tmpdir) / "codex"
        # A tiny Python stub that exits 0 and optionally answers
        # ``--version`` (defensive against future tests that might probe).
        codex_path.write_text(
            '#!/usr/bin/env python3\n'
            'import sys\n'
            'if "--version" in sys.argv:\n'
            '    print("codex 0.130.0 (fake stub for unit tests)")\n'
            'sys.exit(0)\n'
        )
        codex_path.chmod(0o755)

        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{tmpdir}{os.pathsep}{old_path}"
        warnings.warn(
            "No real 'codex' binary on PATH.  Using a fake stub so "
            "credential preflight tests run instead of skipping.  "
            "This is expected in CI; install @openai/codex globally "
            "if you want the real CLI.",
            stacklevel=2,
        )
        try:
            yield
        finally:
            os.environ["PATH"] = old_path
