# Entry points. Every target uses the project virtualenv, so the versions pinned in
# requirements.txt are the ones that run.
# The Python environment: the active virtualenv if there is one (VIRTUAL_ENV, set by
# `pyenv activate <env>` or `source <env>/bin/activate`), otherwise ./.venv. Override with VENV=<path>.
VENV    ?= $(if $(VIRTUAL_ENV),$(VIRTUAL_ENV),.venv)
BIN     := $(VENV)/bin
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

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

$(BIN)/activate:
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip

.PHONY: venv
venv: $(BIN)/activate ## Create the virtualenv and install the pinned tooling
	$(BIN)/pip install -r requirements.txt

.PHONY: init
init: venv ## One-time setup: venv, plus inventory and local.yml from their examples
	@test -f inventory || cp inventory.example inventory
	@test -f group_vars/all/local.yml || cp group_vars/all/local.yml.example group_vars/all/local.yml
	@echo "Now fill in group_vars/all/local.yml (git-ignored) and make sure .vault_pass exists."

.PHONY: lint
lint: ## yamllint, ansible-lint and the module unit tests
	$(BIN)/yamllint .
	$(BIN)/ansible-lint
	$(BIN)/pytest -q tests/unit

.PHONY: test-idempotence
test-idempotence: ## Run each role repeatedly against local fakes of TrueNAS and Proxmox
	$(BIN)/pytest -q tests/integration

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
vault-edit: ## Edit the encrypted vault (secrets only; addresses go in local.yml)
	$(BIN)/ansible-vault edit group_vars/all/vault.yml
