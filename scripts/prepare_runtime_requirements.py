#!/usr/bin/env python3
"""Extract the private runtime spec and remove it from the public solve."""
from __future__ import annotations

import argparse
import re
from pathlib import Path

try:
    from pip._vendor.packaging.requirements import InvalidRequirement, Requirement
    from pip._vendor.packaging.utils import canonicalize_name
except ImportError:  # pragma: no cover - local tooling may unvendor packaging
    from packaging.requirements import InvalidRequirement, Requirement
    from packaging.utils import canonicalize_name

RUNTIME_PROJECT = "molecules-workspace-runtime"
RETIRED_RUNTIME_PROJECT = "molecule-ai-workspace-runtime"


def _without_comment(raw: str) -> str:
    return re.split(r"(?<!\S)#", raw, maxsplit=1)[0].strip()


def prepare(source: Path, destination: Path) -> str:
    lines = source.read_text().splitlines()
    runtime_entries: list[tuple[int, Requirement]] = []

    for index, raw in enumerate(lines):
        candidate = _without_comment(raw)
        if not candidate:
            continue
        if candidate.startswith("-"):
            raise ValueError(f"pip requirement directive at line {index + 1}")
        if candidate.endswith("\\"):
            raise ValueError(f"requirement continuation at line {index + 1}")
        try:
            requirement = Requirement(candidate)
        except InvalidRequirement as exc:
            raise ValueError(f"unsupported requirement at line {index + 1}: {raw}") from exc
        name = canonicalize_name(requirement.name)
        if requirement.url is not None:
            raise ValueError(f"direct URL requirement at line {index + 1}")
        if name == canonicalize_name(RETIRED_RUNTIME_PROJECT):
            raise ValueError(f"retired runtime distribution at line {index + 1}")
        if name == canonicalize_name(RUNTIME_PROJECT):
            runtime_entries.append((index, requirement))

    if len(runtime_entries) != 1:
        raise ValueError(
            f"requirements must declare {RUNTIME_PROJECT} exactly once; "
            f"found {len(runtime_entries)}"
        )

    runtime_index, runtime = runtime_entries[0]
    if runtime.extras or runtime.marker is not None:
        raise ValueError("runtime extras and environment markers are forbidden")

    public_lines = [line for index, line in enumerate(lines) if index != runtime_index]
    destination.write_text("\n".join(public_lines) + ("\n" if public_lines else ""))
    return f"{RUNTIME_PROJECT}{runtime.specifier}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(prepare(args.source, args.destination))


if __name__ == "__main__":
    main()
