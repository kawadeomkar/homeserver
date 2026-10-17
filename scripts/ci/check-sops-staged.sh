#!/usr/bin/env bash
# Refuses a k8s/**/*.sops.yaml that is not SOPS-encrypted: a document without the sops: metadata SOPS adds, or a
# value under data or stringData that is not ENC[...]. gitleaks cannot catch this: its plaintext-Secret rule allows
# every *.sops.yaml, so an encryption that silently did not happen (a forgotten `sops --encrypt`, a path .sops.yaml
# does not match) would pass it. tests/policy/test_k8s.py would catch it too, but only in CI, after a push has
# published it. Needs only bash, git and awk.
#   no RANGE    the staged files (the pre-commit hook: make secrets-staged)
#   RANGE=a..b  every version of such a file in those commits (check-range.sh: the pre-push hook and CI)
# Written for bash 3.2 as well (macOS).
set -euo pipefail

# check <label>: the file on stdin.
check() {
  awk -v name="$1" '
    function finish() {
      if (keys && !sops) { printf "%s: no sops: metadata, so it was never encrypted\n", name; bad = 1 }
      keys = 0; sops = 0; data = 0
    }
    /^---/ { finish(); next }
    /^[ \t]*(#|$)/ { next }
    /^[^ \t]/ {
      keys = 1
      key = $0; sub(/:.*/, "", key)
      data = (key == "data" || key == "stringData")
      if (key == "sops") sops = 1
      inline = $0; sub(/^[^:]*:[ \t]*/, "", inline)
      if (data && inline != "" && inline != "{}") { printf "%s: %s is written inline, so it cannot be checked\n", name, key; bad = 1 }
      next
    }
    data {
      value = $0; sub(/^[ \t]*[^:]*:[ \t]*/, "", value)
      field = $0; sub(/^[ \t]*/, "", field); sub(/:.*/, "", field)
      if (value !~ /^ENC\[/) { printf "%s: %s is not encrypted\n", name, field; bad = 1 }
    }
    END { finish(); exit bad }
  '
}

pattern='^k8s/.*\.sops\.ya?ml$'
status=0
# Each git listing goes to a file first, so a git that fails fails the script (set -e does not reach a process
# substitution), and NUL-separated (-z), so a path git would quote, one with a non-ASCII name say, is read as is.
list=$(mktemp)
trap 'rm -f "$list"' EXIT
# check_listed <revision>: checks each *.sops.yaml the listing names, as it is in <revision> (":" for the index).
check_listed() {
  while IFS= read -r -d '' path; do
    [[ $path =~ $pattern ]] || continue
    if [ "$1" = ":" ]; then
      git show ":$path" | check "$path" || status=1
    else
      git show "$1:$path" | check "$path (in $1)" || status=1
    fi
  done < "$list"
}
if [ -z "${RANGE:-}" ]; then
  git diff --cached --name-only -z --diff-filter=ACMR > "$list"
  check_listed ":"
else
  commits=$(git rev-list "$RANGE")
  for commit in $commits; do
    # A merge's own changes against its first parent, as check-range.sh's gitleaks reads them: a file changed while
    # resolving a merge is in no other commit. --root lists a first commit's files too.
    git diff-tree --no-commit-id --name-only -r -z --root --diff-merges=first-parent --diff-filter=ACMR "$commit" > "$list"
    check_listed "$commit"
  done
fi
if [ "$status" -ne 0 ]; then
  echo "Encrypt it before it is committed: sops --encrypt --in-place <file> (k8s/README-secrets.md)." >&2
fi
exit "$status"
