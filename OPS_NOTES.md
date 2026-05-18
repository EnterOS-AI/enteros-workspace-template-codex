# OPS_NOTES — codex workspace template

Operational/release notes for this template. Build-inert (this file is
not COPYed into the image and does not affect the Docker build).

## 2026-05-18 — republish of `283f371` (PR #6) after a flaked ECR-login

**Why this commit exists:** PR #6 (`283f371`, "bump CLI to 0.130.0 +
consume CODEX_AUTH_JSON subscription OAuth") merged to `main` at
2026-05-18T04:32:57Z. Its `on: push: branches:[main]` `publish-image`
run (Gitea action_run **78342**, job "Build & push
workspace-template-codex image", task 125567) **failed at the "Log in
to ECR" step** with:

```
Run set -euo pipefail
aws ecr get-login-password --region us-east-2 | docker login --username AWS --password-stdin "${ECR_REGISTRY}"
Failed to initialize: protocol not available
##[error]Process completed with exit code 1.
```

This is a **transient act_runner/AWS-CLI environment flake at the
ECR-login step**, NOT a code or workflow defect:

* `ci.yml` on the same commit (`283f371`, run 78341) **passed**.
* `publish-image.yml` is **byte-identical** between this commit and the
  immediately-prior `858b093` (`git diff 858b093 283f371 --
  .gitea/workflows/publish-image.yml` is empty), and `858b093`'s
  publish-image (run 78300) **succeeded** and pushed `sha-858b093`.
* The fix diff `858b093..283f371` only touches
  Dockerfile/adapter.py/start.sh/requirements/tests.

Net effect of the flake: **no `sha-283f371` image was ever pushed to
ECR** (`molecule-ai/workspace-template-codex` only has `sha-741b29b`,
`sha-a051e18`, `sha-858b093`, `latest=858b093`). The deployed prod
codex CP pin is still `git_sha 741b29b` (codex CLI `~0.57`, no
`CODEX_AUTH_JSON` -> `~/.codex/auth.json` Mode C, dead default model
`gpt-5`), which is why the codex-runtime prod workspaces
(`prod-Reviewer`, `prod-Researcher`) cannot authenticate codex, never
start the app-server, and never drain their A2A inbox.

Gitea 1.22.6 has no REST workflow rerun / `workflow_dispatch.inputs`
endpoint (404), so the canonical rerun mechanism is a fresh commit to
`main`. This PR carries no functional change — it exists solely to
re-trip `publish-image` and produce the `sha-<merge>` image of the
already-merged, already-reviewed `283f371` fix.

**After this lands and `publish-image` succeeds:** the new image
digest must be promoted onto the codex CP runtime-image pin (there is
no auto-promoter — manual `psql`/CP pin bump, same class as
MEMORY.md "codex pin auto-promote gap"), then the two codex prod
workspaces restarted/re-provisioned to pull it.
