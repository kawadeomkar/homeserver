# Entry points. Every Python tool comes from the project virtualenv, so the versions locked in
# poetry.lock are the ones that run, here and in CI (.github/workflows/ci.yml). Poetry, kubeconform,
# kube-linter and gitleaks are the exceptions: they come from PATH (pipx or Homebrew locally; CI pins each
# version, and each binary's checksum), as does openssl, which the idempotence tests and vault-init use.
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
# Poetry installs the project virtualenv from poetry.lock. Install it yourself, outside that virtualenv
# (https://python-poetry.org/docs/#installation, e.g. `pipx install poetry`); pyproject.toml's
# requires-poetry says which versions work. Override with POETRY=<path>. Every Poetry command gets the
# project virtualenv as VIRTUAL_ENV, so it uses the one make does and never looks for a Python of its own.
POETRY  ?= poetry
POETRY_RUN   = VIRTUAL_ENV=$(abspath $(VENV)) $(POETRY) --no-interaction
POETRY_FOUND = @command -v $(POETRY) >/dev/null || { echo "Poetry is not on PATH: install it (pipx install poetry)"; exit 1; }
# Narrow a run to stages, e.g. make check-truenas TAGS=truenas_pools
TAGS    ?=
TAG_ARGS := $(if $(TAGS),--tags $(TAGS),)
# Anything else for ansible-playbook, such as a variable for one run:
#   make kubernetes ANSIBLE_ARGS='-e talos_allow_bootstrap=true'
# (make's own -e is not that: it is make's --environment-overrides, and passes nothing to Ansible).
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
# Custom resources (Flux's, MetalLB's, cert-manager's...) are validated against the CRD schemas of
# datreeio/CRDs-catalog, pinned to one commit like the Kubernetes schemas.
KUBE_CRD_SCHEMA_REF ?= 63669a570e231d4f1f8396d229a1de512bcf0a34
KUBE_CRD_SCHEMAS := https://raw.githubusercontent.com/datreeio/CRDs-catalog/$(KUBE_CRD_SCHEMA_REF)/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json
KUBE_CACHE   ?= .cache/kubeconform
KUBECONFORM   = kubeconform -strict -summary -cache $(KUBE_CACHE) -kubernetes-version $(KUBERNETES_VERSION) \
                -schema-location '$(KUBE_SCHEMAS)' -schema-location '$(KUBE_CRD_SCHEMAS)'
# What make lint-helm renders the charts into, and make lint-kube-linter each setup's manifests.
HELM_RENDERED ?= .cache/helm-rendered
K8S_RENDERED  ?= .cache/k8s-rendered
# Every role's custom modules, as paths.
MODULES := $(wildcard roles/*/library/*.py)

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

$(BIN)/activate:
	$(PYTHON) -m venv $(VENV)

.PHONY: venv
# Poetry's sync installs exactly the lock (the main and dev groups) and removes anything else, apart from pip.
venv: $(BIN)/activate ## Create the virtualenv and sync it to poetry.lock (Poetry from PATH)
	$(POETRY_FOUND)
	$(POETRY_RUN) sync

.PHONY: lock
# Re-resolves only what pyproject.toml changed, keeping every other locked version; UPDATE="<package> ..."
# moves those packages to their newest allowed versions too. Either way, only releases at least 7 days old
# (poetry.toml).
UPDATE ?=
lock: $(BIN)/activate ## Update poetry.lock after editing pyproject.toml (UPDATE=<packages>)
	$(POETRY_FOUND)
	$(POETRY_RUN) $(if $(UPDATE),update --lock $(UPDATE),lock)

.PHONY: init
init: venv ## One-time setup: venv, the git hooks, plus inventory and local.yml from their examples
	$(BIN)/pre-commit install
	git config blame.ignoreRevsFile .git-blame-ignore-revs
	@test -f inventory || cp inventory.example inventory
	@test -f group_vars/all/local.yml || cp group_vars/all/local.yml.example group_vars/all/local.yml
	@echo "Now fill in group_vars/all/local.yml (git-ignored) and make sure .vault_pass exists."

.PHONY: lint linters lint-lock lint-yaml lint-ansible lint-python lint-module-docs lint-github test
lint: linters test ## Every linter, then the unit and policy tests

linters: lint-lock lint-yaml lint-ansible lint-python lint-module-docs lint-github ## The lock, yamllint, ansible-lint, ruff, module docs, GitHub config schemas (no tests)

# Poetry checks that pyproject.toml is valid and that the lock was made from it (its content-hash), and
# --strict fails on warnings too. tests/policy checks what Poetry does not: the pins and the hashes.
lint-lock: ## pyproject.toml is valid, and poetry.lock is up to date with it
	$(POETRY_FOUND)
	$(POETRY_RUN) check --lock --strict

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

lint-github: ## dependabot.yml, renovate.json and the workflows against their published schemas (bundled, so offline)
	$(BIN)/check-jsonschema --builtin-schema vendor.dependabot .github/dependabot.yml
	$(BIN)/check-jsonschema --builtin-schema vendor.renovate renovate.json
	$(BIN)/check-jsonschema --builtin-schema vendor.github-workflows .github/workflows/*.yml

test: ## The unit tests, and the policy tests that check the repo's own rules, with a coverage report
	$(BIN)/pytest -q -m "not gitleaks" --cov --cov-report=term tests/unit tests/policy

.PHONY: test-gitleaks-rules
test-gitleaks-rules: ## .gitleaks.toml against generated fixtures (gitleaks from PATH)
	$(BIN)/pytest -q -m gitleaks tests/policy

# The leak checks the git hooks run (.pre-commit-config.yaml), with gitleaks from PATH.
GITLEAKS_FOUND = @command -v gitleaks >/dev/null || { echo "gitleaks is not on PATH (brew install gitleaks)"; exit 1; }

.PHONY: secrets-staged secrets-push
secrets-staged: ## gitleaks over the staged changes, and no unencrypted *.sops.yaml (the pre-commit hook)
	$(GITLEAKS_FOUND)
	gitleaks git --pre-commit --staged --config .gitleaks.toml --ignore-gitleaks-allow --redact --verbose --no-banner
	scripts/ci/check-sops-staged.sh

secrets-push: ## Secrets and identifiers in the commits being pushed (the pre-push hook)
	$(GITLEAKS_FOUND)
	BASE="$${PRE_COMMIT_FROM_REF:-}" HEAD="$${PRE_COMMIT_TO_REF:-HEAD}" scripts/ci/check-range.sh

.PHONY: test-idempotence
# CI runs the tests marked cluster (the VM, backup, Talos and Flux playbooks) in a job of their own, on one
# Python, and the rest on each: PYTEST_ARGS='-m cluster' or PYTEST_ARGS='-m "not cluster"'. Empty runs them all.
PYTEST_ARGS ?=
test-idempotence: ## Run each role repeatedly against local fakes (PYTEST_ARGS narrows it, e.g. -m cluster)
	$(BIN)/pytest -q $(PYTEST_ARGS) tests/integration

.PHONY: lint-kubeconform
# kubeconform retries each schema download itself (twice, with backoff), so an invalid manifest fails
# at once and nothing here retries.
# Every file as it is, before kustomize and Flux's substitution: kustomization.yaml files are kustomize's, not
# objects, and SOPS files carry their own top-level sops: key.
lint-kubeconform: ## kubeconform, strict, against Talos's Kubernetes version and the pinned CRD schemas (kubeconform from PATH)
	@mkdir -p $(KUBE_CACHE)
	$(KUBECONFORM) -ignore-filename-pattern 'kustomization\.yaml$$' -ignore-filename-pattern '\.sops\.yaml$$' k8s

# Each kustomize directory as Flux builds it, so a patch that no longer applies, or a resource listed and missing,
# fails here. The SOPS-encrypted Secrets are skipped: their sops: key is not a Secret's (test_k8s.py checks them).
.PHONY: lint-kustomize
lint-kustomize: ## kustomize build of every directory under k8s/ with a kustomization.yaml, then kubeconform (kustomize from PATH)
	@command -v kustomize >/dev/null || { echo "kustomize is not on PATH (brew install kustomize)"; exit 1; }
	@mkdir -p $(KUBE_CACHE)
	@rc=0; for k in $$(find k8s -name kustomization.yaml | sort); do d=$$(dirname "$$k"); \
	  echo "$$d"; kustomize build --load-restrictor=LoadRestrictionsNone "$$d" > $(KUBE_CACHE)/build.yaml \
	  && $(KUBECONFORM) -skip=Secret $(KUBE_CACHE)/build.yaml || rc=1; done; exit $$rc

# The charts the HelmReleases install, rendered at their pins with each setup's placeholder settings, then checked as
# the repo's own manifests are: kubeconform (a chart's own CRD definitions aside), kube-linter's request and image
# checks, and test_k8s.py's host paths and privilege rules. A chart bump that adds a hostPath or a privileged pod, or a misspelt values key a chart's
# schema refuses, fails before it merges.
.PHONY: lint-helm
lint-helm: ## Render every HelmRelease's chart and check what it renders (helm from PATH)
	@command -v helm >/dev/null || { echo "helm is not on PATH (brew install helm)"; exit 1; }
	$(BIN)/python scripts/ci/render_helm.py --out $(HELM_RENDERED) --kube-version $(KUBERNETES_VERSION)
	$(KUBECONFORM) -skip=CustomResourceDefinition $(HELM_RENDERED)
	kube-linter lint --config .kube-linter-charts.yaml --fail-if-no-objects-found $(HELM_RENDERED)
	HELM_RENDERED=$(HELM_RENDERED) $(BIN)/pytest -q -p no:cacheprovider tests/policy/test_k8s.py -k chart

.PHONY: lint-kube-linter lint-k8s
# On each setup's manifests as Flux applies them, built and with the settings substituted: kube-linter silently skips
# an object it cannot decode, such as a resource limit of "${GPU_I915}" (scripts/ci/render_manifests.py).
lint-kube-linter: ## kube-linter with the repo's checks, .kube-linter.yaml, on each setup's built manifests (kube-linter and kustomize from PATH)
	$(BIN)/python scripts/ci/render_manifests.py --out $(K8S_RENDERED)
	@rc=0; for d in $(K8S_RENDERED)/*/; do echo "$$d"; \
	  kube-linter lint --config .kube-linter.yaml --fail-if-no-objects-found "$$d" || rc=1; done; exit $$rc

# The Talos machine configurations of every setup, rendered by roles/talos's own render tasks and validated by
# talosctl, which catches what rendering lets through (a misspelt field). Needs no cluster and no vault. talosctl
# comes from PATH, pinned in CI with talos_version; without it the check is skipped locally, saying so, and fails
# in CI (CI set), as the gitleaks tests do.
.PHONY: lint-talos
lint-talos: ## Render every node's Talos configuration and validate it (talosctl from PATH)
	@command -v talosctl >/dev/null || { if [ -n "$$CI" ]; then echo "talosctl is not on PATH: the CI job must install it"; exit 1; fi; \
	  echo "talosctl is not on PATH: lint-talos skipped (CI runs it)"; exit 0; }; \
	  ANSIBLE_CONFIG=tests/integration/ansible.cfg ANSIBLE_NOCOLOR=1 $(PLAYBOOK) tests/talos_validate.yml </dev/null

lint-k8s: lint-kubeconform lint-kustomize lint-kube-linter lint-helm lint-talos ## Every manifest check, the charts, and the Talos configurations

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

.PHONY: check-kubernetes
check-kubernetes: ## Dry-run the Kubernetes cluster: its VMs, then Talos and Flux
	$(RUN) kubernetes.yml --check --diff $(TAG_ARGS) $(ANSIBLE_ARGS)

.PHONY: kubernetes
kubernetes: ## Build or correct the Kubernetes cluster: its VMs, then Talos and Flux (needs proxmox first; never upgrades)
	$(RUN) kubernetes.yml --diff $(TAG_ARGS) $(ANSIBLE_ARGS)

.PHONY: check-talos-upgrade
check-talos-upgrade: ## Show what talos-upgrade would move, with Kubernetes's own upgrade dry run
	$(RUN) talos-upgrade.yml --check --diff $(ANSIBLE_ARGS)

.PHONY: talos-upgrade
talos-upgrade: ## Move the running cluster to the pinned Talos and Kubernetes versions (etcd snapshot first)
	$(RUN) talos-upgrade.yml --diff $(ANSIBLE_ARGS)

.PHONY: etcd-snapshot
etcd-snapshot: ## Snapshot etcd into ~/.talos/homeserver/etcd/
	$(RUN) etcd-snapshot.yml $(ANSIBLE_ARGS)

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
	elif [ -f group_vars/all/vault.yml ] && ! { tracked=$$(git rev-parse -q --verify :group_vars/all/vault.yml 2>/dev/null) \
	    && [ -n "$$tracked" ] && [ "$$(git hash-object group_vars/all/vault.yml)" = "$$tracked" ]; }; then \
	  echo "group_vars/all/vault.yml does not open with .vault_pass and is not the tracked vault, so it may be yours," \
	    "with the cluster's secrets in it: restore the .vault_pass that opens it rather than replace it. To start" \
	    "afresh all the same, move the file out of the repository first."; \
	  exit 1; \
	else \
	  umask 077 && { [ -f .vault_pass ] || openssl rand -hex 32 > .vault_pass; } \
	  && printf -- '---\n{}\n' | $(BIN)/ansible-vault encrypt --output group_vars/all/vault.yml \
	  && if git ls-files --error-unmatch group_vars/all/vault.yml >/dev/null 2>&1; then \
	    git update-index --skip-worktree group_vars/all/vault.yml \
	    && echo "git now leaves group_vars/all/vault.yml out of commits; README.md says how to pull past it."; \
	  fi; \
	fi
