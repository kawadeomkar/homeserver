---
paths:
  - ".yamlfmt"
  - ".yamllint"
  - ".ansible-lint"
  - "ruff.toml"
  - ".git-blame-ignore-revs"
---
YAML style comes from `.yamlfmt` (basic, `---` document start, line breaks retained). `.yamllint` (160-column lines,
run with `--strict`) and `.ansible-lint` (production profile, with `strict`, so its warnings fail too) both skip
`k8s/`, which `just lint k8s` covers with kubeconform and kube-linter.

**Python** is formatted and linted by ruff, configured in `ruff.toml`: 120 columns, Python 3.12, and modules and
callback plugins exempt from `E402`, since their imports follow their documentation strings. `ruff format` formats;
`just lint python` checks both. The commit that first formatted everything is listed in `.git-blame-ignore-revs`,
which GitHub reads and `just init` points `git blame` at; list any later reformatting commit there too. `just test
unit` also reports the modules' coverage (`.coveragerc`): a report, not a gate, since it sees only the in-process
tests and not the modules the idempotence tests run inside ansible-playbook.
