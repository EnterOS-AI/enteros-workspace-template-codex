# Historical operations note — Codex template

> Historical only. This file is not a release, deployment, rerun, or pin-change
> runbook. Current delivery behavior is defined by
> `.gitea/workflows/publish-image.yml`.

In May 2026, an image publication for an early Codex auth update failed at a
retired external-registry login step even though repository CI passed. The
incident motivated a fresh publication and pin verification, but its original
commands and environment assumptions no longer apply.

Current images are built by Gitea Actions from `main`, published to the Gitea
OCI registry, and checked by the workflow's promotion/readback jobs. Use the
live action run and job logs for present incidents. Git history preserves the
old forensic record if the exact legacy failure text is needed.
