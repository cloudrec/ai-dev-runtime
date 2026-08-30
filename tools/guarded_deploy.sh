#!/usr/bin/env bash
# Run a deploy step ONLY if the test gate passed.
#
# WHY THIS EXISTS. On 2026-08-30 a gate, a backup, a commit and a service restart were
# chained into one shell command. Eight tests failed and the deploy ran anyway, because a
# non-zero exit in the middle of an `&&`-less chain stops nothing. The deployed code
# happened to be fine — the failures were test-fixture sequencing — but that was luck, not
# process. Chaining a deploy behind a gate in one breath is the defect; this script is the
# fix, and it fails CLOSED: anything other than a clean gate exit refuses the deploy.
#
# Usage:
#   tools/guarded_deploy.sh --gate "<gate command>" --deploy "<deploy command>"
#   tools/guarded_deploy.sh --gate "..." --deploy "..." --dry-run
#
# Exit codes: 0 deploy ran and succeeded · 1 gate failed, deploy REFUSED · 2 bad usage
#             3 deploy itself failed (the gate had passed)
set -uo pipefail

GATE=""; DEPLOY=""; DRY=0
while [ $# -gt 0 ]; do
    case "$1" in
        --gate)   GATE="${2-}"; shift 2 ;;
        --deploy) DEPLOY="${2-}"; shift 2 ;;
        --dry-run) DRY=1; shift ;;
        *) echo "guarded_deploy: unknown argument: $1" >&2; exit 2 ;;
    esac
done
[ -n "$GATE" ] && [ -n "$DEPLOY" ] || { echo "usage: guarded_deploy.sh --gate CMD --deploy CMD [--dry-run]" >&2; exit 2; }

echo "== GATE =="
# SUBSHELL, and an `if` condition. Two separate hazards, both hit while writing this:
#   * an `if` condition is exempt from errexit, so a failing gate is CAPTURED rather than
#     killing this script before it can refuse anything;
#   * `eval` runs in the CURRENT shell, so a gate that itself calls `exit` (a wrapper
#     script, a `set -e` runner, or the literal `exit 1`) would terminate the guard and
#     return the gate's own status — which looks like a refusal but skips the refusal and
#     leaves no message. The subshell contains that.
if ( eval "$GATE" ); then rc=0; else rc=$?; fi
echo "== GATE EXIT: $rc =="

if [ "$rc" -ne 0 ]; then
    echo "REFUSED: the gate exited $rc. The deploy step was NOT run." >&2
    echo "         Fix the gate, then re-run. Never hand-run the deploy to 'save time'." >&2
    exit 1
fi

if [ "$DRY" -eq 1 ]; then
    echo "gate passed; --dry-run so the deploy step was not run:"
    echo "  $DEPLOY"
    exit 0
fi

echo "== DEPLOY (gate passed) =="
if ( eval "$DEPLOY" ); then drc=0; else drc=$?; fi
echo "== DEPLOY EXIT: $drc =="
[ "$drc" -eq 0 ] || exit 3
exit 0
