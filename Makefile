# Entry points. Every Python tool comes from the project virtualenv, so the versions locked in
# requirements.txt are the ones that run. kubeconform is the exception: it comes from PATH, as does
# the openssl the idempotence tests use.
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
RUN      = @umask 077 && mkdir -p $(LOG_DIR) && ln -sf $(notdir $(RUN_LOG)) $(LOG_DIR)/latest.log \
           && echo "Logging to $(RUN_LOG) (Python environment: $(VENV))" && ANSIBLE_LOG_PATH=$(RUN_LOG) $(PLAYBOOK)
# Manifests are validated against the Kubernetes version Talos ships (v1.14.2 runs 1.37.1), with the
# JSON schemas pinned to one commit of yannh/kubernetes-json-schema instead of its moving master.
KUBERNETES_VERSION ?= 1.37.1
KUBE_SCHEMA_REF    ?= 8df8a883b68a24a104b4a9e43c1288090ae60b3b
KUBE_SCHEMAS := https://raw.githubusercontent.com/yannh/kubernetes-json-schema/$(KUBE_SCHEMA_REF)/{{.NormalizedKubernetesVersion}}-standalone{{.StrictSuffix}}/{{.ResourceKind}}{{.KindSuffix}}.json
KUBE_CACHE   ?= .cache/kubeconform
# Every role's custom modules, as paths.
MODULES := $(wildcard roles/*/library/*.py)
# The lock takes only releases at least 7 days old (pip's --uploaded-prior-to, passed through
# pip-compile), so a bad release has a week to be found out before it reaches this toolchain.
# pip-tools writes this command into the lock's header for people to re-run; under click 8.5 it adds
# a --no-index that was never passed, so the header is set here.
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
init: venv ## One-time setup: venv, plus inventory and local.yml from their examples
	@test -f inventory || cp inventory.example inventory
	@test -f group_vars/all/local.yml || cp group_vars/all/local.yml.example group_vars/all/local.yml
	@echo "Now fill in group_vars/all/local.yml (git-ignored) and make sure .vault_pass exists."

.PHONY: lint linters lint-yaml lint-ansible lint-module-docs test
lint: linters test ## Every linter, then the unit and policy tests

linters: lint-yaml lint-ansible lint-module-docs ## yamllint, ansible-lint, module docs (no tests)

lint-yaml: ## yamllint; warnings fail too
	$(BIN)/yamllint --strict .

# ansible-lint needs a vault password file for its syntax check. Without the real one (a fresh clone)
# it gets a placeholder, and it then skips the vault's contents; tests/policy checks that the vault
# stays encrypted.
lint-ansible: ## ansible-lint at the production profile
	@if [ -z "$$ANSIBLE_VAULT_PASSWORD_FILE" ] && [ ! -f .vault_pass ]; then \
	  mkdir -p .cache && echo placeholder > .cache/vault-placeholder; \
	  export ANSIBLE_VAULT_PASSWORD_FILE="$$PWD/.cache/vault-placeholder"; \
	fi; $(BIN)/ansible-lint

lint-module-docs: ## Every custom module's DOCUMENTATION parses (each failure is reported)
	@test -n "$(MODULES)" || { echo "No module found under roles/*/library: run make from the repository root"; exit 1; }
	@rc=0; for m in $(MODULES); do \
	  $(BIN)/ansible-doc -t module -M $$(dirname $$m) --json $$(basename $$m .py) </dev/null >/dev/null || rc=1; \
	done; exit $$rc

test: ## The unit tests, and the policy tests that check the repo's own rules (the lock matches requirements.in)
	$(BIN)/pytest -q tests/unit tests/policy

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

.PHONY: check-truenas
check-truenas: ## Dry-run the NAS configuration and show what would change
	$(RUN) truenas.yml --check --diff $(TAG_ARGS) $(ANSIBLE_ARGS)

.PHONY: truenas
truenas: ## Configure the NAS
	$(RUN) truenas.yml --diff $(TAG_ARGS) $(ANSIBLE_ARGS)

.PHONY: check-proxmox
check-proxmox: ## Dry-run the Proxmox storage configuration
	$(RUN) proxmox.yml --check --diff $(ANSIBLE_ARGS)

.PHONY: proxmox
proxmox: ## Add the NAS's shares to Proxmox as storage
	$(RUN) proxmox.yml --diff $(ANSIBLE_ARGS)

.PHONY: check
check: ## Dry-run everything
	$(RUN) site.yml --check --diff $(ANSIBLE_ARGS)

.PHONY: site
site: ## Configure everything, NAS first
	$(RUN) site.yml --diff $(ANSIBLE_ARGS)

.PHONY: vault-edit
vault-edit: ## Edit the encrypted vault (secrets only; addresses go in local.yml), then lint it
	$(BIN)/ansible-vault edit group_vars/all/vault.yml
	@$(MAKE) --no-print-directory lint-ansible
