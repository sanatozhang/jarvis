#!/usr/bin/env bash
#
# One-click deploy to both servers
#
# Usage:
#   ./deploy-all.sh              # deploy to both
#   ./deploy-all.sh 100          # deploy to 100 only
#   ./deploy-all.sh 102          # deploy to 102 only
#
# Prerequisites (one-time):
#   ssh-copy-id mac@10.0.52.100
#   ssh-copy-id mac@10.0.52.102
#
set -uo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
log()  { echo -e "${CYAN}[deploy]${NC} $*"; }
ok()   { echo -e "${GREEN}[  OK  ]${NC} $*"; }
fail() { echo -e "${RED}[FAILED]${NC} $*"; }

RESULT_100="" ; RESULT_102="" ; FAILURES=0

deploy() {
    local server="$1" script="$2" label="$3"
    log "——— ${label} (${server}) ———"

    local tmp; tmp=$(mktemp)
    local rc=0
    ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no "$server" \
        "export PATH=/opt/homebrew/bin:/usr/local/bin:\$PATH; cd ~/jarvis && bash ${script} update" \
        >"$tmp" 2>&1 || rc=$?

    if [ "$rc" -eq 0 ]; then
        ok "${label}"; tail -3 "$tmp" | sed 's/^/    /'
    else
        fail "${label} (exit ${rc})"; echo ""; cat "$tmp"; echo ""
    fi
    rm -f "$tmp"
    return "$rc"
}

TARGET="${1:-all}"

[ "$TARGET" = "100" ] || [ "$TARGET" = "all" ] && {
    deploy mac@10.0.52.100 ./deploy-bare.sh "100-bare" && RESULT_100=OK || { RESULT_100=FAIL; FAILURES=$((FAILURES+1)); }
    echo ""
}
[ "$TARGET" = "102" ] || [ "$TARGET" = "all" ] && {
    deploy mac@10.0.52.102 ./deploy.sh "102-docker" && RESULT_102=OK || { RESULT_102=FAIL; FAILURES=$((FAILURES+1)); }
    echo ""
}

log "========== Summary =========="
[ -n "$RESULT_100" ] && { [ "$RESULT_100" = OK ] && ok "100-bare" || fail "100-bare"; }
[ -n "$RESULT_102" ] && { [ "$RESULT_102" = OK ] && ok "102-docker" || fail "102-docker"; }
echo ""
[ "$FAILURES" -gt 0 ] && { fail "${FAILURES} failed"; exit 1; } || ok "All done"
