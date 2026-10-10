# Entry points. Every Python tool comes from the project virtualenv, so the versions locked in
# requirements.txt are the ones that run, here and in CI (.github/workflows/ci.yml). kubeconform,
# kube-linter and gitleaks are the exceptions: they come from PATH (Homebrew locally; CI pins each version
# and checksum), as does openssl, which the idempotence tests and vault-init use.
# The Python environment: the active virtualenv if there is one (VIRTUAL_ENV, set by
# `pyenv activate <env>` or `source <env>/bin/activate`), otherwise ./.venv. Override with VENV=<path>.
VENV    ?= $(if $(VIRTUAL_ENV),$(VIRTUAL_ENV),.venv)
BIN     := $(VENV)/bin
# Recipes find the virtualenv's tools first, as in an activated virtualenv (ansible-lint warns when its
# own directory is missing from PATH), and pip skips its check for a newer pip.
export PATH := $(abspath $(BIN)):$(PATH)
export PIP_DISABLE_PIP_VERSION_CHECK := 1
# Any Python 3.12+ works. In a shell where `python3` is broken, pass one explicitly:
#   make venv PYTHON=/path/to/python3.14
PYTHON  ?= python3
# Narrow a run to stages, e.g. make check-truenas TAGS=truenas_pools
TAGS    ?=
TAG_ARGS := $(if $(TAGS),--tags $(TAGS),)
ANSIBLE_ARGS ?=
PLAYBOOK := $(BIN)/ansible-playbook
# Every playbook run is also written to its own log (git-ignored: it holds addresses), named after
# the target and TAGS, e.g. logs/20261004-210501-truenas-truenas_network.log. logs/latest.log
# points at the newest. The API key never appears: the modules mark it no_log.
LOG_DIR ?= logs
STAMP   := $(shell date +%Y%m%d-%H%M%S)
comma   := ,
RUN_LOG  = $(LOG_DIR)/$(STAMP)-$@$(if $(TAGS),-$(subst $(comma),+,$(TAGS))).log
# A missing playbook binary means no virtualenv is active and there is no ./.venv; say so, rather than
# leave the shell's "No such file or directory" and a dangling latest.log.
RUN      = @test -x $(PLAYBOOK) || { echo "$(PLAYBOOK) not found: activate the project virtualenv, pass VENV=<its path>, or create ./.venv with make venv" >&2; exit 2; }; \
           umask 077 && mkdir -p $(LOG_DIR) && ln -sf $(notdir $(RUN_LOG)) $(LOG_DIR)/latest.log \
           && echo "Logging to $(RUN_LOG) (Python environment: $(VENV))" && ANSIBLE_LOG_PATH=$(RUN_LOG) $(PLAYBOOK)
# Manifests are validated against the Kubernetes version Talos ships (v1.14.2 runs 1.37.1), with the
# JSON schemas pinned to one commit of yannh/kubernetes-json-schema instead of its moving master.
KUBERNETES_VERSION ?= 1.37.1
KUBE_SCHEMA_REF    ?= 8df8a883b68a24a104b4a9e43c1288090ae60b3b
KUBE_SCHEMAS := https://raw.githubusercontent.com/yannh/kubernetes-json-schema/$(KUBE_SCHEMA_REF)/{{.NormalizedKubernetesVersion}}-standalone{{.StrictSuffix}}/{{.ResourceKind}}{{.KindSuffix}}.json
KUBE_CACHE   ?= .cache/kubeconform
# Every role's custom modules, as paths.
MODULES := $(wildcard roles/*/library/*.py)
# The lock takes only releases at least 7 days old, the cooldown Dependabot keeps too (pip's
# --uploaded-prior-to, passed through pip-compile). pip-tools writes this command into the lock's header
# for people to re-run; under click 8.5 it adds a --no-index that was never passed, so the header is set
# here. Dependabot ignores the header's command and infers the flags it needs from the file.
LOCK_COMMAND := pip-compile --allow-unsafe --generate-hashes --output-file=requirements.txt --strip-extras --pip-args='--uploaded-prior-to=P7D' requirements.in

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

$(BIN)/activate:
	$(PYTHON) -m venv $(VENV)

.PHONY: venv
# In an existing environment this installs and upgrades to the lock but removes nothing, and pip does not
# re-check the hashes of packages already at their pinned version. Recreate the environment to start clean.
venv: $(BIN)/activate ## Create the virtualenv and install the hash-locked tooling (pip included)
	$(BIN)/pip install --require-hashes -r requirements.txt

.PHONY: lock
lock: ## Re-resolve requirements.txt, with hashes, from requirements.in
	@test -x $(BIN)/pip-compile || { echo "pip-compile is not in $(VENV): run make venv first"; exit 1; }
	CUSTOM_COMPILE_COMMAND="$(LOCK_COMMAND)" $(BIN)/$(LOCK_COMMAND)

.PHONY: init
init: venv ## One-time setup: venv, the git hooks, plus inventory and local.yml from their examples
	$(BIN)/pre-commit install
	git config blame.ignoreRevsFile .git-blame-ignore-revs
	@test -f inventory || cp inventory.example inventory
	@test -f group_vars/all/local.yml || cp group_vars/all/local.yml.example group_vars/all/local.yml
	@echo "Now fill in group_vars/all/local.yml (git-ignored) and make sure .vault_pass exists."

.PHONY: lint linters lint-yaml lint-ansible lint-python lint-module-docs lint-github test
lint: linters test ## Every linter, then the unit and policy tests

linters: lint-yaml lint-ansible lint-python lint-module-docs lint-github ## yamllint, ansible-lint, ruff, module docs, GitHub config schemas (no tests)

lint-yaml: ## yamllint; warnings fail too
	$(BIN)/yamllint --strict .

# ansible-lint needs a vault password file for its syntax check. Without the real one (a fresh clone, or
# CI) it gets a placeholder, and it then skips the vault's contents; tests/policy checks that the vault
# stays encrypted.
lint-ansible: ## ansible-lint at the production profile, strict
	@if [ -z "$$ANSIBLE_VAULT_PASSWORD_FILE" ] && [ ! -f .vault_pass ]; then \
	  mkdir -p .cache && echo placeholder > .cache/vault-placeholder; \
	  export ANSIBLE_VAULT_PASSWORD_FILE="$$PWD/.cache/vault-placeholder"; \
	fi; $(BIN)/ansible-lint

lint-python: ## ruff: lint the Python and check its formatting (ruff format fixes that); both always run
	@rc=0; $(BIN)/ruff check . || rc=1; $(BIN)/ruff format --check . || rc=1; exit $$rc

lint-module-docs: ## Every custom module's DOCUMENTATION parses (each failure is reported)
	@test -n "$(MODULES)" || { echo "No module found under roles/*/library: run make from the repository root"; exit 1; }
	@rc=0; for m in $(MODULES); do \
	  $(BIN)/ansible-doc -t module -M $$(dirname $$m) --json $$(basename $$m .py) </dev/null >/dev/null || rc=1; \
	done; exit $$rc

lint-github: ## dependabot.yml and the workflows against their published schemas (bundled, so offline)
	$(BIN)/check-jsonschema --builtin-schema vendor.dependabot .github/dependabot.yml
	$(BIN)/check-jsonschema --builtin-schema vendor.github-workflows .github/workflows/*.yml

test: ## The unit tests, and the policy tests that check the repo's own rules, with a coverage report
	$(BIN)/pytest -q -m "not gitleaks" --cov --cov-report=term tests/unit tests/policy

.PHONY: test-gitleaks-rules
test-gitleaks-rules: ## .gitleaks.toml against generated fixtures (gitleaks from PATH)
	$(BIN)/pytest -q -m gitleaks tests/policy

# The leak checks the git hooks run (.pre-commit-config.yaml), with gitleaks from PATH.
GITLEAKS_FOUND = @command -v gitleaks >/dev/null || { echo "gitleaks is not on PATH (brew install gitleaks)"; exit 1; }

.PHONY: secrets-staged secrets-push
secrets-staged: ## gitleaks over the staged changes (the pre-commit hook)
	$(GITLEAKS_FOUND)
	gitleaks git --pre-commit --staged --config .gitleaks.toml --ignore-gitleaks-allow --redact --verbose --no-banner

secrets-push: ## Secrets and identifiers in the commits being pushed (the pre-push hook)
	$(GITLEAKS_FOUND)
	BASE="$${PRE_COMMIT_FROM_REF:-}" HEAD="$${PRE_COMMIT_TO_REF:-HEAD}" scripts/ci/check-range.sh

.PHONY: test-idempotence
test-idempotence: ## Run each role repeatedly against local fakes of TrueNAS and Proxmox
	$(BIN)/pytest -q tests/integration

.PHONY: lint-kubeconform
# kubeconform retries each schema download itself (twice, with backoff), so an invalid manifest fails
# at once and nothing here retries.
lint-kubeconform: ## kubeconform, strict, against Talos's Kubernetes version (kubeconform from PATH)
	@mkdir -p $(KUBE_CACHE)
	kubeconform -strict -summary -cache $(KUBE_CACHE) -kubernetes-version $(KUBERNETES_VERSION) \
	  -schema-location '$(KUBE_SCHEMAS)' k8s

.PHONY: lint-kube-linter lint-k8s
lint-kube-linter: ## kube-linter with the repo's checks, .kube-linter.yaml (kube-linter from PATH)
	kube-linter lint --config .kube-linter.yaml --fail-if-no-objects-found k8s

lint-k8s: lint-kubeconform lint-kube-linter ## Both manifest checks

.PHONY: ci
ci: lint test-idempotence lint-k8s ## Everything the CI gate runs, apart from linting the workflows

.PHONY: check-truenas
check-truenas: ## Dry-run the NAS configuration and show what would change
	$(RUN) truenas.yml --check --diff $(TAG_ARGS) $(ANSIBLE_ARGS)

.PHONY: truenas
truenas: ## Configure the NAS
	$(RUN) truenas.yml --diff $(TAG_ARGS) $(ANSIBLE_ARGS)

.PHONY: check-proxmox
check-proxmox: ## Dry-run the Proxmox host's repositories, upgrade and storage
	$(RUN) proxmox.yml --check --diff $(ANSIBLE_ARGS)

.PHONY: proxmox
proxmox: ## Configure the Proxmox host: repositories and upgrade, then VM storage (the NAS's shares, or a check of its own)
	$(RUN) proxmox.yml --diff $(ANSIBLE_ARGS)

.PHONY: check
check: ## Dry-run everything
	$(RUN) site.yml --check --diff $(ANSIBLE_ARGS)

.PHONY: site
site: ## Configure everything, NAS first (the NAS play ends by itself without one)
	$(RUN) site.yml --diff $(ANSIBLE_ARGS)

.PHONY: vault-edit
vault-edit: ## Edit the encrypted vault (secrets only; addresses go in local.yml), then lint it
	$(BIN)/ansible-vault edit group_vars/all/vault.yml
	@$(MAKE) --no-print-directory lint-ansible

# Without a NAS the vault holds nothing this setup uses, but every run decrypts it, and only the maintainer
# has the password. This writes an empty vault over the tracked one, encrypted with .vault_pass, which it
# creates readable by its owner alone if there is none, and tells git to leave the file out of commits. A
# vault that already opens with .vault_pass, the maintainer's or one made here before, is left alone. Without
# ansible-vault it stops before anything else, since a vault it cannot open would look like someone else's.
# The empty vault reaches ansible-vault on stdin, so the file changes only once encryption has worked.
.PHONY: vault-init
vault-init: ## Without a NAS: an empty vault of your own, with a new .vault_pass if there is none
	@test -x $(BIN)/ansible-vault || { echo "ansible-vault is not in $(VENV): activate the project's virtualenv, or pass VENV=<path>"; exit 1; }
	@if [ -f .vault_pass ] && $(BIN)/ansible-vault view group_vars/all/vault.yml >/dev/null 2>&1; then \
	  echo "group_vars/all/vault.yml already opens with .vault_pass: nothing to do."; \
	else \
	  umask 077 && { [ -f .vault_pass ] || openssl rand -hex 32 > .vault_pass; } \
	  && printf -- '---\n{}\n' | $(BIN)/ansible-vault encrypt --output group_vars/all/vault.yml \
	  && if git ls-files --error-unmatch group_vars/all/vault.yml >/dev/null 2>&1; then \
	    git update-index --skip-worktree group_vars/all/vault.yml \
	    && echo "git now leaves group_vars/all/vault.yml out of commits; README.md says how to pull past it."; \
	  fi; \
	fi
