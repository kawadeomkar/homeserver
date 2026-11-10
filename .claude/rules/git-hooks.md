---
paths:
  - ".pre-commit-config.yaml"
  - ".gitleaks.toml"
  - "scripts/ci/**"
---
**Git hooks** (`.pre-commit-config.yaml`, run by pre-commit from the lock; `just init` installs them): gitleaks over
the staged changes, then `scripts/ci/check-sops-staged.sh` (both in `scripts/ci/check-staged.sh`), and before a push,
`scripts/ci/check-range.sh` over the commits being pushed (pre-commit's refs as its `BASE` and `HEAD`), the check CI
runs. Each leak hook runs its script directly, not through just, and needs only bash and gitleaks from Homebrew (`brew
install gitleaks`), so it works in any shell, with or without the virtualenv active. The one more, `role-references`,
runs `just fix docs` when a staged file is under `roles/`, so it needs just on `PATH` and the project's virtualenv,
found as just always finds it (a commit from a tool that runs git without pyenv on `PATH` fails there, saying how to
choose one); it fails a commit whose pages it changed, and the commit goes through once they are added. The linters
stay in `just lint` and CI. A hook can be skipped with `--no-verify`, which is why CI's `leaks` and `docs` jobs repeat
the checks.
