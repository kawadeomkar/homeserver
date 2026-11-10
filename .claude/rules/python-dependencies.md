---
paths:
  - "pyproject.toml"
  - "poetry.lock"
  - "poetry.toml"
---
**Python dependencies** are managed with Poetry (2.x, non-package mode). Direct pins are exact (`==`) and live in
`pyproject.toml`: what the playbooks need in `[project] dependencies`, and the linters, tests and hooks in the `dev`
dependency group (PEP 735 `[dependency-groups]`, never `[tool.poetry.group.*.dependencies]`). `just lock` updates
`poetry.lock`, which resolves once for every Python in `requires-python`, with markers, and records a sha256 for every
file, which Poetry checks when it installs. It changes only what `pyproject.toml` changed (`just lock <packages>`
moves those on too), and `poetry.toml` makes it take only releases at least 7 days old (`solver.min-release-age`).
`poetry.toml` also installs wheels only, keeps the keyring closed, and lets Poetry run without `VIRTUAL_ENV` make an
environment of its own in its cache, never one in the checkout; it is committed so that it overrides each user's
global Poetry configuration. Poetry itself is not in the lock: each user installs it (above), `requires-poetry` says
which versions work, and CI installs `POETRY_VERSION` with pipx. `just install` runs `poetry sync`, which installs
exactly the main and `dev` groups and removes anything else but pip. CI makes a virtualenv of setup-python's Python in
`$RUNNER_TEMP` and exports it as `VIRTUAL_ENV` before `just install`. `just lint lock` (`poetry check --lock
--strict`) fails when the lock is stale, and `tests/policy/test_lock.py` when a pin is not exact or not the locked
version, or a package has no hashes. gitleaks's default rules skip `poetry.lock`, as they do every lock file;
`pyproject.toml` is scanned.
