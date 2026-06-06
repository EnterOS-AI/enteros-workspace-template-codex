#!/usr/bin/env bash
#
# verify-runtime-pin.sh — fail-loud assertion that runtime_image_pins actually
# moved to the digest we just pushed.
#
# This is the keystone "comprehensive CI/CD catches the mistake" guard. The
# 2026-06-04 codex incident: publish-image.yml's promote was a best-effort
# `continue-on-error: true` commit-status POST that 403'd silently, so the
# build went GREEN while runtime_image_pins[codex] stayed STALE and reprovisions
# kept pulling the old image. A green build that did not move the pin is the
# exact failure this script makes RED.
#
# It reads back the pin via the control-plane List endpoint
# (GET /cp/admin/runtime-image), finds the (TEMPLATE_NAME, region=global) row,
# and asserts image_digest == EXPECTED_DIGEST. Any divergence — missing row,
# wrong digest, region skew, List 4xx/5xx — exits non-zero (build RED).
#
# This script is intentionally TEMPLATE-AGNOSTIC and is committed verbatim into
# every runtime-template repo (codex, claude-code, hermes, …) so a future missed
# promote on ANY template is caught, not just codex. Keep the copies byte-identical.
#
# Required env:
#   CP_HOST            control-plane host, e.g. api.moleculesai.app
#   CP_ADMIN_API_TOKEN bearer token for /cp/admin/* (per-environment)
#   TEMPLATE_NAME      runtime_image_pins.template_name, e.g. codex
#   EXPECTED_DIGEST    sha256:<64hex> the publish job just pushed
# Optional env:
#   ENV_NAME           label for log lines (prod|staging); default "?"
#   TOKEN_SECRET_NAME  name of the secret holding CP_ADMIN_API_TOKEN (for a
#                      clearer error when it is unset); default CP_ADMIN_API_TOKEN
#   REGION             pin region to assert; default "global" (cp#336: only
#                      global is written)

set -euo pipefail

ENV_NAME="${ENV_NAME:-?}"
REGION="${REGION:-global}"
TOKEN_SECRET_NAME="${TOKEN_SECRET_NAME:-CP_ADMIN_API_TOKEN}"

fail() { echo "::error::[verify-runtime-pin ${ENV_NAME}] $*"; exit 1; }

[ -n "${CP_HOST:-}" ]            || fail "CP_HOST is empty"
[ -n "${TEMPLATE_NAME:-}" ]      || fail "TEMPLATE_NAME is empty"
[ -n "${EXPECTED_DIGEST:-}" ]    || fail "EXPECTED_DIGEST is empty — publish job did not expose a digest"
[ -n "${CP_ADMIN_API_TOKEN:-}" ] || fail "${TOKEN_SECRET_NAME} secret not configured on this repo — cannot verify ${ENV_NAME} pin"

case "${EXPECTED_DIGEST}" in
  sha256:*) : ;;
  *) fail "EXPECTED_DIGEST does not look like sha256:<digest> (got ${EXPECTED_DIGEST})" ;;
esac

resp="$(mktemp)"
code="$(curl -sS -o "${resp}" -w '%{http_code}' \
  -H "Authorization: Bearer ${CP_ADMIN_API_TOKEN}" \
  -H "Accept: application/json" \
  "https://${CP_HOST}/cp/admin/runtime-image")"

if [ "${code}" != "200" ]; then
  echo "----- response body -----"; cat "${resp}"; echo; echo "-------------------------"
  fail "GET /cp/admin/runtime-image returned HTTP ${code} (expected 200) — cannot confirm the pin moved"
fi

# Pull the actual digest for (TEMPLATE_NAME, REGION) out of {"pins":[...]}.
actual="$(
  TEMPLATE_NAME="${TEMPLATE_NAME}" REGION="${REGION}" python3 - "${resp}" <<'PY'
import json, os, sys
template = os.environ["TEMPLATE_NAME"]
region = os.environ["REGION"]
with open(sys.argv[1]) as fh:
    data = json.load(fh)
pins = data.get("pins") or []
for p in pins:
    if p.get("template_name") == template and p.get("region") == region:
        print(p.get("image_digest", ""))
        break
PY
)"

if [ -z "${actual}" ]; then
  echo "----- runtime_image_pins -----"; cat "${resp}"; echo; echo "------------------------------"
  fail "no runtime_image_pins row for (template=${TEMPLATE_NAME}, region=${REGION}) on ${ENV_NAME} — promote did not land"
fi

if [ "${actual}" != "${EXPECTED_DIGEST}" ]; then
  fail "PIN MISMATCH on ${ENV_NAME}: runtime_image_pins[${TEMPLATE_NAME},${REGION}].image_digest=${actual} but the build pushed ${EXPECTED_DIGEST}. The promote silently skipped — reprovisions would pull the STALE image. Failing the build."
fi

echo "::notice::[verify-runtime-pin ${ENV_NAME}] OK — runtime_image_pins[${TEMPLATE_NAME},${REGION}] == ${EXPECTED_DIGEST}"
