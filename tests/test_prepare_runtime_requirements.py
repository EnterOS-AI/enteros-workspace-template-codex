"""Tests for filtering the private runtime out of the public pip solve."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "prepare_runtime_requirements.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("prepare_runtime_requirements", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_exact_runtime_pin_is_returned_and_removed(tmp_path):
    module = _load_module()
    source = tmp_path / "requirements.txt"
    destination = tmp_path / "public-requirements.txt"
    source.write_text(
        "molecules-workspace-runtime==0.3.125\n"
        "a2a-sdk==1.0.3\n"
    )

    requirement = module.prepare(source, destination)

    assert requirement == "molecules-workspace-runtime==0.3.125"
    assert destination.read_text() == "a2a-sdk==1.0.3\n"


def test_runtime_direct_reference_is_rejected(tmp_path):
    module = _load_module()
    source = tmp_path / "requirements.txt"
    source.write_text(
        "molecules-workspace-runtime @ "
        "https://example.invalid/molecules_workspace_runtime-9-py3-none-any.whl\n"
    )

    with pytest.raises(ValueError, match="direct URL"):
        module.prepare(source, tmp_path / "public-requirements.txt")


def test_runtime_marker_and_duplicate_declarations_are_rejected(tmp_path):
    module = _load_module()
    source = tmp_path / "requirements.txt"
    source.write_text(
        "molecules-workspace-runtime>=0.3; python_version > '3'\n"
        "molecules_workspace_runtime<0.4\n"
    )

    with pytest.raises(ValueError):
        module.prepare(source, tmp_path / "public-requirements.txt")
