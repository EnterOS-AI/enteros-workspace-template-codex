"""Shared pytest fixtures + anti-skip guard for the codex template test suite."""

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

    The anti-skip guard below (see ``_ANTI_SKIP_TARGETS`` +
    ``pytest_runtest_logreport`` + ``pytest_sessionfinish``) treats any
    skip of the 6 ``test_setup_*`` credential tests as a HARD FAIL.
    This is the defense-in-depth on the just-merged #88 / #46 gate:
    if this fixture regresses (or someone reintroduces a silent skip in
    one of the target tests), the suite reports RED with an actionable
    message rather than GREEN-with-skipped — which would mask a real
    credential-preflight regression.
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


# --- Anti-skip guard --------------------------------------------------------
# These six tests carry the fail-closed credential preflight invariants.
# Each one has a `pytest.skip("codex binary not on PATH ...")` branch that
# is ONLY meant to be reachable when the real `codex` CLI is absent AND
# the `_fake_codex_on_path` fixture above has not (yet) been added. The
# fixture removes that branch's reachability by putting a stub `codex`
# on PATH; if the fixture regresses, these tests would silently mark
# themselves as skipped and the suite would report GREEN-with-skipped,
# masking a real regression in the credential preflight.
#
# The guard below fails the session if any of these tests end up in the
# `skipped` set. The check is wired via two pytest hooks and a module-
# level collector (kept in a dedicated list, not on the global `pytest`
# singleton, so it survives if a downstream plugin shadows it).
#
# This guard must NEVER be weakened. If you are tempted to:
#   - allow one of these tests to skip (e.g. "just this once"),
#   - remove a target because the test was deleted,
#   - remove the session-finish check because it is "flaky",
# STOP. The right fix is to repair the underlying test or the fixture,
# not to weaken the guard. The whole point is fail-closed.

_ANTI_SKIP_TARGETS: frozenset = frozenset({
    "tests/test_modernization_pr1.py::test_setup_accepts_auth_json_only",
    "tests/test_modernization_pr1.py::test_setup_fails_closed_with_no_credential",
    "tests/test_modernization_pr1.py::test_setup_ignores_empty_auth_json",
    "tests/test_modernization_pr1.py::test_setup_fails_closed_when_model_is_provider_name",
    "tests/test_modernization_pr1.py::test_setup_passes_for_real_model_id",
    "tests/test_modernization_pr1.py::test_setup_ignores_legacy_model_provider_model_id",
})

# Collected at logreport time; read at session-finish time. Stored on the
# conftest module so it survives across the hook invocations without
# relying on a pytest singleton.
_anti_skip_observed: set = set()


def _extract_short_name(nodeid: str) -> str:
    """Pull the test function name out of a pytest nodeid.

    `tests/test_modernization_pr1.py::test_setup_accepts_auth_json_only`
    -> `test_setup_accepts_auth_json_only`. Tolerates parametrization
    suffixes (`::test_x[foo]`) and is robust against future nodeid
    formatting changes by falling back to the last `::` segment.
    """
    return nodeid.rsplit("::", 1)[-1].split("[", 1)[0]


def _check_anti_skip_violations(observed: set) -> list:
    """Return the sorted list of anti-skip targets that were skipped.

    Pure function (no pytest hook machinery) so the meta-test in
    `tests/test_anti_skip_guard.py` can exercise it directly without
    having to spin up a full pytest session. The two output invariants
    the meta-test asserts:

      1. With the fixture present, `observed` does NOT intersect
         `_ANTI_SKIP_TARGETS` (the live test run will exercise this
         path and the suite passes — the session-finish hook then sees
         an empty violation list and exits 0).
      2. With the fixture absent (simulated by injecting a synthetic
         skip into `observed`), the intersection is non-empty and the
         function returns a non-empty list — the session-finish hook
         then emits the failure message and exits non-zero.
    """
    # Match on the full nodeid if observed in full, else fall back to
    # the short name. This keeps the guard useful even if a future
    # refactor renames the test file (the short-name match still
    # catches a regression in the underlying test).
    short_to_targets: dict = {}
    for full in _ANTI_SKIP_TARGETS:
        short_to_targets.setdefault(_extract_short_name(full), set()).add(full)

    violations: set = set()
    for entry in observed:
        # Direct full-nodeid match.
        if entry in _ANTI_SKIP_TARGETS:
            violations.add(entry)
            continue
        # Short-name fallback.
        short = _extract_short_name(entry)
        if short in short_to_targets:
            violations.update(short_to_targets[short])
    return sorted(violations)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_logreport(report):
    """Record every test that reports outcome='skipped'.

    We capture ALL skipped reports (any `when`) — a skip at `setup`
    time (e.g. from a fixture) is just as much a guard violation as a
    skip at `call` time. We deliberately do NOT distinguish; the whole
    point is to fail closed on any silent skip of a target.
    """
    if getattr(report, "outcome", None) == "skipped":
        nodeid = getattr(report, "nodeid", None)
        if nodeid:
            _anti_skip_observed.add(nodeid)


@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    """Fail-closed if any anti-skip target was observed as skipped.

    We use `pytest.exit(returncode=2)` so the failure surfaces in CI
    as a non-zero process exit, independent of pytest's normal
    exit-status arithmetic (which can collapse to 0 if every other
    test passed). The message is intentionally verbose: it names the
    failing tests, points at the fixture, and refuses to suggest
    "weakening" the guard as a remedy.
    """
    violations = _check_anti_skip_violations(_anti_skip_observed)
    if not violations:
        return

    lines = [
        "ANTI-SKIP GUARD FAILED — credential preflight tests silently "
        "skipped when they MUST run.",
        "",
        "Skipped target(s):",
        *(f"  - {v}" for v in violations),
        "",
        "Why this is a hard failure (not a soft warning):",
        "  These six tests are the fail-closed credential-preflight "
        "gate. They are the only line of defense that catches a "
        "regression in the `codex` CLI 0.130 setup() path (mode C "
        "auth.json, no-credential fail-closed, model-vs-provider-name "
        "wedge guard, etc.). If any of them skip silently, the suite "
        "reports GREEN while the credential preflight is broken — and "
        "every production codex workspace wedges on next boot.",
        "",
        "Likely root causes (investigate these, do NOT weaken the "
        "guard):",
        "  1. The `_fake_codex_on_path` fixture in tests/conftest.py "
        "was removed, broken, or had its `autouse=True` flag flipped "
        "off. With no stub on PATH, `shutil.which('codex')` returns "
        "None and the test's `pytest.skip(...)` branch fires.",
        "  2. A new test was added to `_ANTI_SKIP_TARGETS` whose "
        "nodeid does not match any real test in the suite (typo, "
        "rename). Fix the nodeid.",
        "  3. A target test was deleted or renamed without updating "
        "`_ANTI_SKIP_TARGETS`. Either restore the test or, if the "
        "removal is intentional, also remove the entry from "
        "`_ANTI_SKIP_TARGETS` AND update the meta-test count "
        "assertion in tests/test_anti_skip_guard.py.",
        "  4. A future change reintroduced a NEW `pytest.skip(...)` "
        "branch into one of these tests that is reachable even with "
        "the fixture in place. Remove that branch — the fixture's "
        "contract is 'these tests always run'.",
        "",
        "If you genuinely need to allow one of these tests to skip "
        "(e.g. it is being moved to a slow/integration suite), open "
        "a CTO escalation: weakening this guard is a security-"
        "relevant change to the credential preflight surface, not a "
        "local refactor.",
    ]
    # `pytest.exit` raises SystemExit; pytest catches it and uses the
    # returncode as the process exit code. Using 2 (misuse) rather
    # than 1 (test failure) so CI surfaces this as a guard violation
    # distinct from an ordinary test failure.
    pytest.exit("\n".join(lines), returncode=2)
