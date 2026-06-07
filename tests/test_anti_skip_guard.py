"""Self-audit of the anti-skip guard defined in ``tests/conftest.py``.

The anti-skip guard is the defense-in-depth on the just-merged
``#88`` (fake codex binary fixture) / ``#46`` (adapter tests in
validate gate) keystone pair. Its job is to fail-closed if any of
the six ``test_setup_*`` credential preflight tests in
``test_modernization_pr1.py`` ever end up marked as ``skipped`` in a
CI run — a silent skip would mask a real credential-preflight
regression and wedge every production codex workspace on next boot.

This file is the self-audit. It runs in two modes:

  1. **WITH the fixture present (the live CI path).** A normal
     ``pytest`` run loads ``conftest.py``, the
     ``_fake_codex_on_path`` fixture injects a stub ``codex`` on
     PATH, the six target tests run (not skip), the
     ``_anti_skip_observed`` set stays empty, and the
     ``pytest_sessionfinish`` hook exits 0. The tests in this file
     PASS in this mode and document the live path.

  2. **WITHOUT the fixture (simulated regression).** The
     ``_check_anti_skip_violations`` helper is a pure function that
     takes an observed-set and returns the sorted violation list.
     The tests below inject synthetic skip entries into the
     observed-set and assert the function would surface them. This
     is the "self-audit of the guard itself" — the guard is correct
     iff an injected skip is detected, AND the live run with the
     fixture in place produces zero observed skips.

If any test in this file fails, the anti-skip guard is misconfigured
(either it is missing, the target set is wrong, or the violation
detection logic regressed). Treat that as a hard gate failure, not a
soft warning — the whole point of the guard is to fail closed, and
the guard cannot be allowed to silently stop working.
"""
from __future__ import annotations

import inspect
import re

import pytest

# Importing the conftest module loads the `_ANTI_SKIP_TARGETS` set,
# the `_anti_skip_observed` collector, the `_extract_short_name` /
# `_check_anti_skip_violations` helpers, and the two pytest hooks
# (`pytest_runtest_logreport`, `pytest_sessionfinish`) into this
# test module's namespace. `conftest` is the conventional pytest
# name and is on `sys.path` because pytest's rootdir handling puts
# it there.
import conftest as _conftest


# --- (1) Targets list integrity -------------------------------------------

def test_anti_skip_targets_set_is_nonempty() -> None:
    """The anti-skip target set MUST be non-empty. If it is empty,
    the guard does nothing — the exact failure mode this guard
    exists to prevent."""
    assert _conftest._ANTI_SKIP_TARGETS, (
        "_ANTI_SKIP_TARGETS is empty — the anti-skip guard does not "
        "guard anything. Restore the six credential-test nodeids."
    )


def test_anti_skip_targets_count_is_exactly_six() -> None:
    """The known set is six tests. If the count drifts, either a
    test was added without updating the guard, or one was removed
    and the guard was not updated. Both are regressions: the meta-
    test exists to make the count visible at review time."""
    n = len(_conftest._ANTI_SKIP_TARGETS)
    assert n == 6, (
        f"_ANTI_SKIP_TARGETS has {n} entries; expected exactly 6. "
        "If a target test was deleted intentionally, update this "
        "assertion AND the OPS_NOTES entry that documents the "
        "guard; if one was added, verify the new nodeid resolves "
        "to a real test (test_anti_skip_targets_resolve_to_real_"
        "tests_below)."
    )


def test_anti_skip_targets_all_live_in_modernization_pr1() -> None:
    """Every target must point at the modernization-pr1 file — the
    only place the six fail-closed credential preflight tests live.
    A target pointing elsewhere is a typo, not a real target."""
    for nodeid in _conftest._ANTI_SKIP_TARGETS:
        assert nodeid.startswith(
            "tests/test_modernization_pr1.py::"
        ), (
            f"anti-skip target {nodeid!r} is not in "
            "test_modernization_pr1.py. Either the test moved (fix "
            "the nodeid) or the target is wrong (remove it)."
        )


def test_anti_skip_targets_have_valid_nodeid_shape() -> None:
    """Each entry must be a full pytest nodeid
    (`path::test_name` or `path::test_name[param]`). The guard
    matches on these strings verbatim during the run."""
    pattern = re.compile(
        r"^tests/test_[A-Za-z0-9_]+\.py::[A-Za-z0-9_]+(\[[^]]+\])?$"
    )
    for nodeid in _conftest._ANTI_SKIP_TARGETS:
        assert pattern.match(nodeid), (
            f"anti-skip target {nodeid!r} is not a well-formed "
            "pytest nodeid (path::test_name[/param])."
        )


def test_anti_skip_targets_resolve_to_real_tests_below() -> None:
    """Every target's short name must correspond to a real,
    callable test in ``test_modernization_pr1.py``. This catches
    a target that references a deleted or renamed test (the guard
    would silently do nothing in that case)."""
    import importlib
    import test_modernization_pr1 as mod

    for nodeid in _conftest._ANTI_SKIP_TARGETS:
        short = nodeid.rsplit("::", 1)[-1]
        func = getattr(mod, short, None)
        assert func is not None, (
            f"anti-skip target {nodeid!r} does not resolve to a "
            f"callable in test_modernization_pr1.py (no attr {short!r})"
        )
        assert callable(func), (
            f"anti-skip target {nodeid!r} resolves to {func!r} "
            "which is not callable"
        )
        # Must look like a test (function with __wrapped__ or pyfunc
        # signature accepting self). A real pytest test always has a
        # `__pytest_marks__` attribute or a `__name__`; we accept
        # anything callable with `__name__` because pytest will mark
        # it as a test if it lives in a `test_*.py` file. The
        # stronger check is the assert below on the skip-pattern.
        assert hasattr(func, "__name__"), (
            f"anti-skip target {nodeid!r} resolves to something "
            "without a __name__ attribute — not a real test"
        )


def test_each_target_test_has_codex_on_path_skip_pattern() -> None:
    """For the anti-skip guard to be meaningful, each target test
    must contain the `shutil.which("codex"): pytest.skip(...)`
    pattern that would fire if the fake-codex fixture regressed.
    A target test that no longer has this pattern is a no-op
    target — the guard would never fire on it.

    The test is tolerant: it accepts the pattern in any of the
    common shapes (`if not shutil.which("codex")` or
    `if shutil.which("codex") is None`) so a future stylistic
    refactor of the skip check does not break this audit. What it
    WILL catch is the regression where someone removes the skip
    check entirely from a target test — the guard would silently
    become useless for that target.
    """
    import test_modernization_pr1 as mod

    for nodeid in _conftest._ANTI_SKIP_TARGETS:
        short = nodeid.rsplit("::", 1)[-1]
        src = inspect.getsource(getattr(mod, short))
        # Accept either of the two idiomatic skip-check shapes.
        has_which_check = (
            re.search(r'shutil\.which\(\s*["\']codex["\']\s*\)', src)
            is not None
        )
        has_skip_call = (
            re.search(r'pytest\.skip\(', src) is not None
        )
        assert has_which_check, (
            f"target {nodeid!r} no longer references "
            '`shutil.which("codex")` — the skip-check was removed. '
            "The guard is now a no-op for this target. Either "
            "restore the check or remove the target."
        )
        assert has_skip_call, (
            f"target {nodeid!r} no longer calls `pytest.skip(...)` "
            "— the guard is now a no-op for this target."
        )


# --- (2) Fixture integrity -------------------------------------------------

def test_fake_codex_fixture_is_autouse_and_session_scoped() -> None:
    """The `_fake_codex_on_path` fixture MUST be `autouse=True` and
    `scope='session'`. A future change that flips either flag would
    leave the credential tests unguarded — the guard would fire on
    every CI run, and the right fix is to restore the fixture, not
    to weaken the guard."""
    import tests.conftest as cf

    fixture_func = getattr(cf, "_fake_codex_on_path", None)
    assert fixture_func is not None, (
        "_fake_codex_on_path fixture was removed from conftest.py. "
        "Restore it (autouse=True, scope='session') or every "
        "credential preflight test will silently skip and the suite "
        "will report green with a real regression masked."
    )
    # pytest stores the fixture's scope/autouse metadata on a
    # `_fixture_function_marker` attribute. The exact name has
    # changed across pytest versions (older: `_pytestfixturefunction`;
    # newer: `_fixture_function_marker`), so probe a few candidates
    # before failing — a future pytest version will need the next
    # candidate added, but the test should still surface a clear
    # "marker is missing" failure rather than an AttributeError.
    fixture_marker = None
    marker_attr_used = None
    for attr in (
        "_fixture_function_marker",
        "_pytestfixturefunction",
        "pytestfixturefunction",
    ):
        candidate = getattr(fixture_func, attr, None)
        if candidate is not None:
            fixture_marker = candidate
            marker_attr_used = attr
            break
    assert fixture_marker is not None, (
        "_fake_codex_on_path is no longer decorated with @pytest."
        f"fixture (probed attrs: _fixture_function_marker, "
        f"_pytestfixturefunction, pytestfixturefunction — none "
        f"present). The autouse session fixture contract is "
        f"broken. If pytest renamed the attribute in a newer "
        f"version, add it to the probe list above."
    )
    assert getattr(fixture_marker, "scope", None) == "session", (
        f"_fake_codex_on_path scope is "
        f"{getattr(fixture_marker, 'scope', None)!r}; expected "
        "'session'. A per-function scope would re-create the stub "
        "per test (works but wasteful); a non-autouse scope would "
        "let the credential tests skip."
    )
    assert getattr(fixture_marker, "autouse", False) is True, (
        "_fake_codex_on_path is no longer autouse=True. The "
        "credential tests do not declare a fixture dependency, "
        "so a non-autouse fixture would not be requested and the "
        "tests would skip."
    )


# --- (3) Hooks are registered ----------------------------------------------

def test_logreport_hook_is_registered_on_conftest() -> None:
    """``pytest_runtest_logreport`` must be defined on the conftest
    module so the guard can collect skipped reports. If someone
    removes it, the collector set stays empty and the session-
    finish check vacuously passes — the exact silent-green
    regression the guard exists to prevent."""
    assert hasattr(_conftest, "pytest_runtest_logreport"), (
        "conftest.py no longer defines `pytest_runtest_logreport`. "
        "Restore the skip-report collector or the guard does not "
        "observe any skips."
    )


def test_sessionfinish_hook_is_registered_on_conftest() -> None:
    """``pytest_sessionfinish`` must be defined on the conftest
    module so the guard can fail-closed at the end of the run."""
    assert hasattr(_conftest, "pytest_sessionfinish"), (
        "conftest.py no longer defines `pytest_sessionfinish`. "
        "Restore the end-of-session violation check or the guard "
        "never fires."
    )


# --- (4) Self-audit of the violation-detection logic ----------------------

def test_violation_check_returns_empty_for_clean_observation() -> None:
    """The live CI path: the fake-codex fixture is in place, the
    six target tests run, the observed-set is empty, and the
    violation check returns []. This is the "with-fixture" self-
    audit."""
    assert _conftest._check_anti_skip_violations(set()) == []


def test_violation_check_detects_full_nodeid_skip() -> None:
    """A full-nodeid skip entry for a target is detected. The
    returned list contains the offending full nodeid."""
    target = next(iter(_conftest._ANTI_SKIP_TARGETS))
    observed = {target}
    violations = _conftest._check_anti_skip_violations(observed)
    assert target in violations, (
        f"violation check did not detect full-nodeid skip of {target}; "
        f"got {violations!r}"
    )


def test_violation_check_detects_short_name_skip() -> None:
    """A short-name skip entry (e.g. from a future nodeid format
    change) still matches. This keeps the guard useful if pytest
    ever changes how it formats nodeids."""
    full = "tests/test_modernization_pr1.py::test_setup_accepts_auth_json_only"
    short = _conftest._extract_short_name(full)
    assert short == "test_setup_accepts_auth_json_only"
    observed = {short}
    violations = _conftest._check_anti_skip_violations(observed)
    assert full in violations, (
        f"violation check did not detect short-name skip of {short}; "
        f"got {violations!r}"
    )


def test_violation_check_ignores_unrelated_skips() -> None:
    """Skips of non-target tests do not produce violations. This
    keeps the guard focused: it is the credential-preflight
    guard, not a no-skip-anywhere guard."""
    observed = {
        "tests/test_app_server.py::test_something_unrelated",
        "tests/test_executor.py::test_another_unrelated[param1]",
    }
    assert _conftest._check_anti_skip_violations(observed) == []


def test_violation_check_collects_all_violations() -> None:
    """If multiple targets are skipped, the function returns ALL
    of them (not just the first). This makes the failure message
    actionable: the operator can fix every broken test in one CI
    pass, not one at a time."""
    targets = sorted(_conftest._ANTI_SKIP_TARGETS)[:3]
    observed = set(targets)
    violations = _conftest._check_anti_skip_violations(observed)
    for t in targets:
        assert t in violations, (
            f"missing violation for {t!r} in {violations!r}"
        )
    assert len(violations) >= 3


def test_violation_check_returns_sorted_output() -> None:
    """The output is sorted, so the failure message is
    deterministic and easy to diff across runs."""
    observed = set(_conftest._ANTI_SKIP_TARGETS)
    violations = _conftest._check_anti_skip_violations(observed)
    assert violations == sorted(violations), (
        f"violation list is not sorted: {violations!r}"
    )


# --- (5) Extract-short-name helper ---------------------------------------

def test_extract_short_name_strips_parametrize_suffix() -> None:
    """Parametrized tests have nodeids like `::test_x[foo]`. The
    short-name extractor strips the parametrize suffix so the
    short-name fallback match works for parametrized targets."""
    assert _conftest._extract_short_name(
        "tests/test_modernization_pr1.py::test_x[foo]"
    ) == "test_x"


def test_extract_short_name_keeps_plain_names_intact() -> None:
    """Non-parametrized tests pass through unchanged."""
    assert _conftest._extract_short_name(
        "tests/test_modernization_pr1.py::test_setup_accepts_auth_json_only"
    ) == "test_setup_accepts_auth_json_only"


# --- (6) Live-with-fixture self-audit (the most important one) ------------

def test_live_run_with_fixture_records_zero_target_skips(
    request: pytest.FixtureRequest,
) -> None:
    """The end-to-end self-audit: under the live conftest (with
    the ``_fake_codex_on_path`` fixture active), the
    ``_anti_skip_observed`` set MUST contain zero of the six
    target nodeids. If it does, the live test session would have
    been failed by the session-finish hook, which means the guard
    is correctly armed AND the fixture is correctly providing
    the stub.

    This is the "with-fixture" half of the dispatch's
    "with/without-fixture self-audit" requirement. The
    "without-fixture" half is exercised by the violation-check
    tests above (which inject synthetic skips and verify the
    guard would fire).
    """
    observed = getattr(_conftest, "_anti_skip_observed", None)
    assert observed is not None, (
        "_anti_skip_observed collector is missing from conftest — "
        "the logreport hook was removed or the attribute renamed."
    )
    targets_seen = _conftest._ANTI_SKIP_TARGETS & observed
    assert not targets_seen, (
        f"the live test run observed these credential tests as "
        f"skipped: {sorted(targets_seen)}. Either the fake-codex "
        f"fixture regressed (most likely) or one of these tests "
        f"introduced a new skip branch. The session-finish hook "
        f"should have failed this run; if it did not, the hook "
        f"is broken. Investigate before merging."
    )
