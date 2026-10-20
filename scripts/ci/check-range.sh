#!/usr/bin/env bash
# Checks the commits a change adds for secrets and network identifiers, with gitleaks and the rules as they
# are on the target, so a change cannot weaken its own scan, and for a *.sops.yaml that was never encrypted
# (check-sops-staged.sh).
# Used by CI (.github/workflows/ci.yml) and by the pre-push hook (.pre-commit-config.yaml), which gives the refs
# as pre-commit's PRE_COMMIT_FROM_REF and PRE_COMMIT_TO_REF.
#   BASE               the target: a pull request's base, a push's previous tip, the remote ref a pre-push
#                      hook updates (default PRE_COMMIT_FROM_REF); empty or all zeros means origin/main
#   HEAD               last commit checked (default PRE_COMMIT_TO_REF, else HEAD)
#   FIRST_PARENT=1     follow the first-parent line only (pushes to main)
#   REQUIRE_COMMITS=1  fail when there is nothing to check (pull requests and pushes)
# The commits checked are the ones after the merge-base of BASE and HEAD: a pull request's base is the
# target branch's tip, which stops being an ancestor of the head as soon as the target moves on.
# Written for bash 3.2 as well (macOS), so no arrays under set -u.
set -euo pipefail
command -v gitleaks >/dev/null || { echo "gitleaks is not on PATH (brew install gitleaks)"; exit 1; }
head=${HEAD:-${PRE_COMMIT_TO_REF:-HEAD}}
target=${BASE:-${PRE_COMMIT_FROM_REF:-}}
if [ -z "$target" ] || [ -z "${target//0/}" ]; then
  target=origin/main
fi
base=$(git merge-base "$target" "$head") || { echo "::error::$target and $head share no history"; exit 1; }

first_parent=""
if [ "${FIRST_PARENT:-0}" = 1 ]; then first_parent="--first-parent"; fi
# shellcheck disable=SC2086  # $first_parent is empty or a single word
expected=$(git rev-list --count $first_parent "$base..$head")
if [ "$expected" -eq 0 ]; then
  if [ "${REQUIRE_COMMITS:-0}" = 1 ]; then echo "::error::no commits between $base and $head"; exit 1; fi
  echo "Nothing new between $base and $head."; exit 0
fi
echo "Checking $expected commit(s): $base..$head"

cfg=$(mktemp); log=$(mktemp); trap 'rm -f "$cfg" "$log"' EXIT
# The target's rules; only the change that adds .gitleaks.toml is checked with its own.
git show "$target:.gitleaks.toml" > "$cfg" 2>/dev/null || git show "$head:.gitleaks.toml" > "$cfg"
if [ -n "${CI:-}" ]; then rm -f .gitleaksignore; fi

# --diff-merges=first-parent shows each merge's own change, so a line added while resolving a conflict
# is scanned; --text defeats -diff and binary attributes; --ignore-gitleaks-allow ignores inline allows.
# No empty words in --log-opts: gitleaks passes them to git, git fails, and gitleaks then reports
# "no leaks found" with exit 0 after scanning nothing. The checks below refuse such a run.
opts="${first_parent:+$first_parent }--diff-merges=first-parent --text $base..$head"
rc=0
gitleaks git --config "$cfg" --ignore-gitleaks-allow --redact --verbose --no-banner --log-opts="$opts" > "$log" 2>&1 || rc=$?
sed -E 's/\x1b\[[0-9;]*m//g' "$log"
if [ "$rc" -ne 0 ]; then exit "$rc"; fi
if sed -E 's/\x1b\[[0-9;]*m//g' "$log" | grep -q ' ERR '; then
  echo "::error::gitleaks reported an error, so its result cannot be trusted"; exit 1
fi
scanned=$(sed -E 's/\x1b\[[0-9;]*m//g' "$log" | sed -nE 's/.* ([0-9]+) commits scanned.*/\1/p' | tail -1)
if [ "${scanned:-0}" -ne "$expected" ]; then
  echo "::error::gitleaks scanned ${scanned:-0} commit(s), expected $expected"; exit 1
fi

# gitleaks allows every *.sops.yaml, so whether each version of one in these commits is encrypted is checked apart.
RANGE="$base..$head" "$(dirname "$0")/check-sops-staged.sh" || { echo "::error::an unencrypted *.sops.yaml"; exit 1; }
