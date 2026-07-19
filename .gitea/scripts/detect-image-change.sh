#!/usr/bin/env bash
# SSOT — byte-identical across every workspace-template repo (like
# .gitea/scripts/verify-runtime-pin.sh). Classifies whether the triggering
# event should PROMOTE the staging runtime_image_pins, decoupling "build +
# publish the image" from "move the staging pin" (task #261 build-off-box:
# split build from deploy).
#
# WHY: publish-image.yml runs on `push: [main]`, so BEFORE this guard EVERY
# merge to main — including a change that only touches the publish workflow
# itself (e.g. the ci-meta -> off-box runner-label edit) — re-ran the promote
# job and moved the STAGING runtime pin as a pure side-effect of landing a
# workflow edit. That coupling is what let a runner-port change silently
# repin staging. This classifier severs it: promote-pin + verify-pin gate on
# the image_changed output below, so landing a workflow/docs-only change still
# republishes the image (idempotent) but NEVER moves a pin. Promotion becomes
# its own explicit path: an image-content change (the real core->template
# .runtime-version cascade) OR a manual workflow_dispatch.
#
# CONTRACT — writes `image_changed=true|false` to $GITHUB_OUTPUT:
#   - workflow_dispatch (or any non-push event) -> true
#         An explicit human/automation run is an explicit promote intent.
#   - push -> true IFF the pushed commit range changed at least one file
#         OUTSIDE the decoupled surfaces (CI config / docs / tests). A push
#         confined to decoupled surfaces (e.g. only .gitea/workflows/**) -> false.
#   - undiffable / unknown push -> true (FAIL-SAFE toward the prior
#         always-promote behavior; we only ever SKIP a promote when we can
#         positively prove the push was decoupled-surface-only).
#
# DECOUPLED SURFACES (a push confined to these never moves a pin):
#   .gitea/**        CI workflows + CI scripts (this file, verify-runtime-pin.sh)
#   docs/**, runbooks/**, .cursor/**, *.md   documentation / editor config
#   tests/**, scripts/test-*.sh              test-only code (inert at runtime)
# EVERYTHING ELSE is image-relevant (Dockerfile, .runtime-version,
# requirements*.txt, adapter.py / *.py, config.yaml, start.sh, install.sh,
# scripts/<runtime>.sh, internal/**, ...) and DOES promote.
set -euo pipefail

OUT="${GITHUB_OUTPUT:-/dev/stdout}"
emit() {
  echo "image_changed=$1" >> "$OUT"
  echo "detect-image-change: image_changed=$1 ($2)"
}

EVENT_NAME="${EVENT_NAME:-${GITHUB_EVENT_NAME:-}}"

if [ "$EVENT_NAME" != "push" ]; then
  emit true "event=${EVENT_NAME:-<empty>} is not a push — explicit/other trigger promotes"
  exit 0
fi

ZERO='0000000000000000000000000000000000000000'
BEFORE="${BEFORE_SHA:-}"
AFTER="${AFTER_SHA:-}"
[ -n "$AFTER" ] || AFTER="$(git rev-parse HEAD 2>/dev/null || true)"

if [ -z "$BEFORE" ] || [ "$BEFORE" = "$ZERO" ] || ! git cat-file -e "${BEFORE}^{commit}" 2>/dev/null; then
  # New-branch push (all-zero BEFORE) or an unfetched base: diff the tip commit
  # against its first parent when one exists.
  if git rev-parse -q --verify "${AFTER}^" >/dev/null 2>&1; then
    RANGE="${AFTER}^..${AFTER}"
  else
    emit true "no diff base (root commit / undiffable) — fail-safe promote"
    exit 0
  fi
else
  RANGE="${BEFORE}..${AFTER}"
fi

FILES="$(git diff --name-only "$RANGE" 2>/dev/null || true)"
if [ -z "$FILES" ]; then
  emit true "empty/undiffable range ${RANGE} — fail-safe promote"
  exit 0
fi

is_decoupled() {
  case "$1" in
    .gitea/*)            return 0 ;;
    docs/*)              return 0 ;;
    runbooks/*)          return 0 ;;
    .cursor/*)           return 0 ;;
    tests/*)             return 0 ;;
    scripts/test-*.sh)   return 0 ;;
    *.md)                return 0 ;;
    *)                   return 1 ;;
  esac
}

CHANGED_IMAGE=false
while IFS= read -r f; do
  [ -z "$f" ] && continue
  if is_decoupled "$f"; then
    echo "  decoupled:      $f"
  else
    echo "  image-relevant: $f"
    CHANGED_IMAGE=true
  fi
done <<EOF
$FILES
EOF

emit "$CHANGED_IMAGE" "range=${RANGE}"
