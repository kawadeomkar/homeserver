# .github/

CI, the ruleset that guards `main`, and Dependabot. Claude Code loads this file when it works on a file under
`.github/`. What a green check cannot vouch for, and the hooks that run before a commit, are in the root `CLAUDE.md`.

## CI

`.github/workflows/ci.yml` runs on every pull request and every push to `main`, splitting `just lint`'s and `just
test`'s types between its jobs (`tests/policy/test_github.py` checks that each type runs somewhere):

- `just lint` with every type but the documentation and the Kubernetes checks;
- `just lint docs` in a job of its own (`docs`: the role references and the modules' documentation), so a stale page
  is a failed check of its own within a minute;
- `just test unit policy` and `just test idempotence` on Python 3.13 and 3.14, on Arm runners (free for public
  repositories, and faster at the CPU-bound idempotence suite than x64);
- `just lint k8s` (kubeconform, kube-linter, the charts and the Talos configurations; talosctl downloaded by version
  and checksum);
- `leaks`, which runs `just test gitleaks` and `scripts/ci/check-range.sh` (gitleaks over the commits the change adds,
  with the target branch's `.gitleaks.toml`);
- actionlint and zizmor (pedantic persona, so a digest-less image or a missing permissions block fails) over the
  workflows;
- shellcheck over `scripts/ci/*.sh` alone, so a shell helper added elsewhere under `scripts/` means widening that glob;
- and, on pull requests only, `dependency-review`, which fails a change that adds a pip package with a known
  vulnerability, from GitHub's dependency graph (it cannot see the SHA-pinned actions, which zizmor's online
  `known-vulnerable-actions` audit covers instead).

`ci-success` aggregates them, allows `dependency-review` alone to be skipped (on pushes; `test_github.py` checks that
nothing else can be), and is the one check the "Protect main" ruleset is to require. `.github/rulesets/main.json`
holds that ruleset's reviewed body, but GitHub never reads the file: it takes effect only when applied (`gh api -X PUT
repos/kawadeomkar/homeserver/rulesets/24555579 --input .github/rulesets/main.json`), so read the live ruleset (`gh api
repos/kawadeomkar/homeserver/rulesets/24555579`) before relying on a red PR being unmergeable.

No job gets a secret (ansible-lint reads a placeholder vault password file) and none reaches the home network, so
`just check` stays local. Actions are pinned to full commit SHAs with a version comment, which the repository settings
require, and only an allow-list of actions may run. The allow-list also rejects `docker://` steps, so actionlint,
kubeconform, kube-linter and gitleaks are downloaded by version and checked against a sha256 (just through
`scripts/ci/install-just.sh`). `.github/actionlint.yaml` declares the `ubuntu-26.04` and `ubuntu-26.04-arm` runner
labels until actionlint knows them. CI makes a virtualenv of setup-python's Python in `$RUNNER_TEMP` and exports it as
`VIRTUAL_ENV` before `just install`, and installs `POETRY_VERSION` with pipx.

Outside the gate, `.github/workflows/scorecard.yml` runs OpenSSF Scorecard weekly and on every push to `main`,
publishes the result to the OpenSSF API and uploads it to code scanning; the API refuses results from a workflow that
breaks its rules, which the file's header lists, so read them before changing it. The full plan, including what later
PRs add, is `docs/CI-PLAN.md` (gitignored).

## Dependabot

Dependabot proposes weekly updates, at least 7 days after each release, for the SHA-pinned actions and every package
in the lock (`allow: dependency-type: all`; by default it would update only the packages `pyproject.toml` names). It
re-locks with its own Poetry, which must satisfy `requires-poetry` (keep that a range: outside it Dependabot opens no
PR), and it does not read `poetry.toml`. `commit-message.prefix` in `dependabot.yml` sets both its PR titles and its
commit subjects, so they read `[dependencies] Bump …`. Read each PR before merging it; never merge one only because CI
is green. The required check is strict, so a PR must be up to date with `main` before it merges, and Dependabot
rebases only on its schedule or a conflict: after merging one of its PRs, comment `@dependabot rebase` on the others
(or use "Update branch"). A Dependabot bump of docsible that changes the role references fails `just lint docs` until
`just fix docs` is run on its branch.

What neither Dependabot nor Renovate can see is bumped by hand each quarter: `scripts/runbooks/dependency-bumps.md`
lists every such pin with the integrity value that moves with it. Renovate, for what is under `k8s/`, is in
`k8s/CLAUDE.md`.
