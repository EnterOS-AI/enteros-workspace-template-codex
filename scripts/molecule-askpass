#!/bin/sh
# git-askpass helper. Reads HTTPS Basic-Auth credentials from env vars so
# the deployer can wire git authentication for any private remote without
# touching ~/.gitconfig or ~/.git-credentials inside the container.
#
# Wire-up: set GIT_ASKPASS=/usr/local/bin/molecule-askpass in the
# container env, then export GIT_HTTP_USERNAME / GIT_HTTP_PASSWORD (or the
# GITEA_USER / GITEA_TOKEN fallback pair). When git encounters an HTTPS
# auth challenge on a host that has no credential.helper configured for
# it, git invokes GIT_ASKPASS twice — once with a "Username for ..."
# prompt and once with a "Password for ..." prompt. We pattern-match on
# that prompt and emit the matching env var.
#
# No hardcoded hostnames or vendor names — the deployer decides which
# host these credentials apply to by virtue of setting GIT_ASKPASS only
# when the target remote is in scope. The helper itself is reusable for
# any HTTPS git remote.
#
# Failure mode: if the env vars are unset, we emit an empty string and
# let git surface "Authentication failed" — this is intentional, so a
# misconfigured deployment fails loudly at first push instead of silently
# falling through to an unrelated credential chain.

case "$1" in
    Username*)
        printf '%s\n' "${GIT_HTTP_USERNAME:-${GITEA_USER:-}}"
        ;;
    Password*)
        printf '%s\n' "${GIT_HTTP_PASSWORD:-${GITEA_TOKEN:-}}"
        ;;
    *)
        # Unknown prompt — emit empty and let git decide.
        printf '\n'
        ;;
esac
