#!/usr/bin/env bash
# The pre-commit hook (.pre-commit-config.yaml): gitleaks over the staged changes, with the rules as they are in the
# working tree, then no unencrypted *.sops.yaml among the staged files (check-sops-staged.sh). Both always run.
# Needs only bash, git, awk and gitleaks (brew install gitleaks).
# Written for bash 3.2 as well (macOS).
set -euo pipefail
command -v gitleaks >/dev/null || { echo "gitleaks is not on PATH (brew install gitleaks)"; exit 1; }
here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
cd "$(git rev-parse --show-toplevel)"
rc=0
gitleaks git --pre-commit --staged --config .gitleaks.toml --ignore-gitleaks-allow --redact --verbose --no-banner || rc=1
"$here/check-sops-staged.sh" || rc=1
exit "$rc"
