# Entry points: `make help` lists them. Each does the whole of its job by default; TYPE narrows one to part of it
# (make lint TYPE=yaml, make check TYPE=proxmox), and the other parameters below say how.
# Every Python tool comes from the project's virtualenv, so the versions locked in
# poetry.lock are the ones that run, here and in CI (.github/workflows/ci.yml). Poetry, kubeconform,
# kube-linter and gitleaks are the exceptions: they come from PATH (pipx or Homebrew locally; CI pins each
# version, and each binary's checksum), as does openssl, which the idempotence tests and make vault TYPE=new use.
# The virtualenv is yours to make and keep, wherever you keep them (pyenv virtualenv, python3 -m venv): make never
# creates one, and make install fills it from the lock. make uses the active one (VIRTUAL_ENV, set by
# `pyenv activate <env>` or `source <env>/bin/activate`); otherwise the virtualenv python3 runs from, but only when
# this checkout chose it: `pyenv local <env>` wrote a .python-version at the checkout's top (a worktree also takes its
# main checkout's), or `pyenv shell <env>` chose it for the shell. A version pyenv takes from anywhere else (pyenv
# global, a .python-version above the checkout) is no one project's, and make install would remove every package
# poetry.lock does not list from it; a python3 that is not a virtualenv (the system's, Homebrew's) would run tools no
# lock pinned. Override with VENV=<path>. Finding none, make stops on any target but help, saying how to choose one.
CHOSEN_PYTHON = (f=$$(pyenv version-file 2>/dev/null) || exit 0; \
	d=$$(CDPATH='' cd -- "$$(dirname -- "$$f")" && pwd -P); \
	top=$$(git rev-parse --show-toplevel); \
	main=$$(dirname -- "$$(git rev-parse --path-format=absolute --git-common-dir)"); \
	if [ -n "$${PYENV_VERSION:-}" ] || [ "$$d" = "$$top" ] || [ "$$d" = "$$main" ]; then \
	  python3 -c 'import sys; print(sys.prefix if sys.prefix != sys.base_prefix else "")'; \
	fi) 2>/dev/null
ifeq ($(origin VENV),undefined)
VENV := $(or $(VIRTUAL_ENV),$(shell $(CHOSEN_PYTHON)))
endif
ifeq ($(strip $(VENV)),)
$(if $(filter-out help,$(MAKECMDGOALS)),$(error No Python environment for this checkout: activate the project's virtualenv, choose it here with pyenv local <env> (a .python-version at the checkout's top), or pass VENV=<its path>))
endif
BIN     := $(VENV)/bin
# Recipes find the virtualenv's tools first, as in an activated virtualenv (ansible-lint warns when its
# own directory is missing from PATH), and pip skips its check for a newer pip.
export PATH := $(abspath $(BIN)):$(PATH)
export PIP_DISABLE_PIP_VERSION_CHECK := 1
# Poetry installs the project virtualenv from poetry.lock. Install it yourself, outside that virtualenv
# (https://python-poetry.org/docs/#installation, e.g. `pipx install poetry`); pyproject.toml's
# requires-poetry says which versions work. Override with POETRY=<path>. Every Poetry command gets the
# project virtualenv as VIRTUAL_ENV, so it uses the one make does and never looks for a Python of its own.
POETRY  ?= poetry
POETRY_RUN   = VIRTUAL_ENV=$(abspath $(VENV)) $(POETRY) --no-interaction
POETRY_FOUND = @command -v $(POETRY) >/dev/null || { echo "Poetry is not on PATH: install it (pipx install poetry)"; exit 1; }
# make install and make lock refuse a Python that is not a virtualenv: poetry sync removes every package the lock does
# not list, so on a Python that is not the project's alone (a pyenv version, the system's) it would uninstall the tools
# installed there.
ENV_FOUND    = @$(BIN)/python -c 'import sys; sys.exit(sys.prefix == sys.base_prefix)' 2>/dev/null || { \
	  echo "$(VENV) is not a virtualenv, and poetry sync would remove every package there that poetry.lock does not list."; \
	  echo "Make one for this project and use it: pyenv virtualenv <python 3.12+> homeserver && pyenv local homeserver,"; \
	  echo "or python3 -m venv <dir> && source <dir>/bin/activate. Then make install."; exit 1; }

# TYPE narrows a target to some of what it does: one or more of its types, comma-separated (make lint TYPE=yaml,python),
# and exactly one for check and deploy, which run one playbook (make check TYPE=proxmox). <target>_TYPES lists what each
# target takes, in the order they run; a target not in TYPED takes none. A type that is not a goal's, or a TYPE for a
# goal that takes none, stops make before anything runs.
TYPE    ?=
comma   := ,
empty   :=
space   := $(empty) $(empty)
TYPES   := $(strip $(subst $(comma), ,$(TYPE)))
TYPED   := init vault lint test fix check deploy
init_TYPES  := full
vault_TYPES := new list switch
lint_TYPES  := lock yaml ansible python docs github manifests charts talos
# make lint TYPE=k8s: the Kubernetes checks, the ones that need kubeconform, kustomize, helm, kube-linter or talosctl.
lint_ALIASES := k8s
k8s_TYPES   := manifests charts talos
test_TYPES  := unit policy gitleaks idempotence
fix_TYPES   := python docs
deploy_TYPES := truenas proxmox homeassistant kubernetes
check_TYPES := $(deploy_TYPES) talos-upgrade suspend resume
list     = $(subst $(space),$(comma),$(strip $(1)))
define check_types
$(if $(filter $(1),$(TYPED)),,$(error make $(1) takes no TYPE))
$(if $(filter-out $($(1)_TYPES) $($(1)_ALIASES),$(TYPES)),$(error make $(1) takes TYPE=$(call list,$($(1)_TYPES) $($(1)_ALIASES)), not $(call list,$(filter-out $($(1)_TYPES) $($(1)_ALIASES),$(TYPES)))))
$(if $(and $(filter check deploy,$(1)),$(word 2,$(TYPES))),$(error make $(1) runs one playbook: one TYPE at a time))
$(if $(and $(filter vault,$(1)),$(word 2,$(TYPES))),$(error make vault does one thing: one TYPE at a time))
endef
ifneq ($(TYPES),)
$(foreach goal,$(MAKECMDGOALS),$(eval $(call check_types,$(goal))))
endif
# $(call pick,<target>): the types TYPE names when <target> is a goal, otherwise every one of them.
pick     = $(if $(and $(TYPES),$(filter $(1),$(MAKECMDGOALS))),$(TYPES),$($(1)_TYPES))
# Each type of lint, test and fix is a target of its own, <target>/<type>, so make -k runs every one and reports each
# failure, as CI does. Call them through TYPE: the targets are how it is built, not another way in.
targets  = $(addprefix $(1)/,$(patsubst k8s,$(k8s_TYPES),$(call pick,$(1))))

# Narrow a playbook run to stages, e.g. make check TYPE=truenas TAGS=truenas_pools
TAGS    ?=
TAG_ARGS := $(if $(TAGS),--tags $(TAGS),)
# Narrow make suspend and make resume to some apps, groups or homeassistant, e.g. make suspend ONLY=jellyfin,nextcloud
ONLY    ?=
ONLY_ARGS := $(if $(ONLY),-e '{"only": "$(strip $(ONLY))"}',)
# Widen them to the cluster's VMs too, after everything else: make suspend ALL=1, make resume ALL=1. Not with ONLY.
ALL     ?=
ALL_ARGS := $(if $(ALL),-e '{"cluster_vms": true}',)
# Anything else for ansible-playbook, such as a variable for one run:
#   make deploy TYPE=kubernetes ANSIBLE_ARGS='-e talos_allow_bootstrap=true'
# (make's own -e is not that: it is make's --environment-overrides, and passes nothing to Ansible).
ANSIBLE_ARGS ?=
PLAYBOOK := $(BIN)/ansible-playbook
# Setups (scripts/init/setups.py): each machine's local.yml, vault and .vault_pass, kept together in a store outside the
# checkout, SETUPS or $XDG_CONFIG_HOME/homeserver/setups (~/.config/homeserver/setups without it), and the one the
# checkout uses linked in at LOCAL, VAULT and .vault_pass. NAME names a setup for make vault, and FROM=current makes
# this checkout's own files one, with TYPE=new alone. Both come from make's command line alone, never the environment,
# where a shell may have put a NAME of its own (WSL sets it to the machine's name); and both are make vault's alone:
# any other target runs with the setup the checkout links to, so make deploy NAME=lab is refused rather than run with
# another setup. SETUP_NAME is the setup the checkout links to, for the log's name.
NAME   :=
FROM   :=
SETUPS ?=
ifneq ($(SETUPS),)
export SETUPS
endif
ifneq ($(NAME)$(FROM),)
NOT_VAULT := $(filter-out vault vault/% help,$(MAKECMDGOALS))
$(if $(NOT_VAULT),$(error NAME and FROM are make vault's: make $(firstword $(NOT_VAULT)) runs with the setup this checkout links to, so link the one you mean in first, make vault TYPE=switch NAME=<setup>))
$(if $(and $(FROM),$(filter-out new,$(or $(TYPES),edit))),$(error FROM=current is make vault TYPE=new's alone: make vault TYPE=new NAME=<name> FROM=current))
endif
LOCAL      := group_vars/all/local.yml
SETUPS_PY   = $(BIN)/python scripts/init/setups.py
SETUP_NAME := $(shell [ -L $(LOCAL) ] && basename "$$(dirname "$$(readlink $(LOCAL))")")
# Every playbook run is also written to its own log (git-ignored: it holds addresses), named after
# the setup, the target, TYPE and TAGS, e.g. logs/20261004-210501-home-deploy-truenas-truenas_network.log.
# logs/latest.log points at the newest. The API key never appears: the modules mark it no_log.
LOG_DIR ?= logs
STAMP   := $(shell date +%Y%m%d-%H%M%S)
RUN_LOG  = $(LOG_DIR)/$(STAMP)$(if $(SETUP_NAME),-$(SETUP_NAME))-$@$(if $(TYPES),-$(TYPES))$(if $(TAGS),-$(subst $(comma),+,$(TAGS))).log
# A missing playbook binary means make found a Python other than the project's virtualenv, or one not yet installed;
# say so, rather than leave the shell's "No such file or directory" and a dangling latest.log. Then the setup: a run
# names the one it uses, and stops when its links are not whole, which Ansible would pass over without a word.
RUN      = @test -x $(PLAYBOOK) || { echo "$(PLAYBOOK) not found: run make install in the project's virtualenv, or point make at it (pyenv local <env>, or VENV=<its path>)" >&2; exit 2; }; \
           $(SETUPS_PY) check && umask 077 && mkdir -p $(LOG_DIR) && ln -sf $(notdir $(RUN_LOG)) $(LOG_DIR)/latest.log \
           && echo "Logging to $(RUN_LOG) (Python environment: $(VENV))" && ANSIBLE_LOG_PATH=$(RUN_LOG) $(PLAYBOOK)
# check and deploy run the playbook TYPE names, or site.yml, every one in order, without it.
TYPE_PLAYBOOK = $(if $(TYPES),$(TYPES),site).yml

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
# What make lint TYPE=charts renders the charts into, and TYPE=manifests each setup's manifests.
HELM_RENDERED ?= .cache/helm-rendered
K8S_RENDERED  ?= .cache/k8s-rendered
# A tool the Kubernetes checks take from PATH, with how to get it.
FOUND = command -v $(1) >/dev/null || { echo "$(1) is not on PATH (brew install $(1))"; exit 1; }
# Every role's custom modules, as paths.
MODULES := $(wildcard roles/*/library/*.py)
# Each role's REFERENCE.md is docsible's page for it, generated whole from the role as it is: meta/main.yml,
# meta/argument_specs.yml, the defaults and vars, and every tasks file as a table and a Mermaid graph. It is never
# edited by hand; its README.md is, and stays the role's own. --no-docsible leaves out docsible's .docsible file,
# metadata meant to be filled in by hand, which would add only the date it was first made; --no-backup the copy of
# the previous page, which git keeps; the empty --playbook the example playbook docsible looks for in the role's
# tests/test.yml, which no role here has: without it docsible prints "playbook not found", which fails the run below.
ROLES         := $(patsubst %/,%,$(wildcard roles/*/))
DOCSIBLE_PAGE := REFERENCE.md
DOCSIBLE      := $(BIN)/docsible --no-backup --no-docsible --graph --playbook "" --output $(DOCSIBLE_PAGE)
# Runs docsible on every role, under the directory $(1), and sets rc=1 on a failure. docsible reports a file it cannot
# read, then leaves it out and exits 0, so any line besides its "Documentation generated at" fails the run.
DOCSIBLE_RUN   = for role in $(ROLES); do \
	  out=$$($(DOCSIBLE) --role $(1)/$$role 2>&1) || rc=1; \
	  if printf '%s\n' "$$out" | grep -v '^Documentation generated at: '; then rc=1; fi; \
	done

.DEFAULT_GOAL := help

.PHONY: help
# The targets with a ## description, under the ##@ section before them; then each target's types and the other
# parameters, the types read from the lists above so that help never names one that is gone.
help: ## Show this help
	@printf 'make <target> [TYPE=<type>[,<type>...]] [PARAMETER=<value>...]\n'
	@awk 'BEGIN {FS = ":.*## "} /^##@ / {printf "\n%s\n", substr($$0, 5); next} \
	  /^[a-zA-Z_-]+:.*## / {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@printf '\nTYPE narrows a target to part of what it does (several, comma-separated; check and deploy take one):\n'
	@printf '  \033[36m%-14s\033[0m %s\n' \
	  init "$(init_TYPES): every optional setting too" \
	  vault "$(vault_TYPES)" \
	  "" "new: with NAME, a setup of that name, empty, which the checkout then uses (FROM=current: of its own files);" \
	  "" "     without NAME, an empty vault in the checkout, and a .vault_pass if there is none" \
	  "" "list: every setup, the one this checkout uses marked; switch: this checkout to setup NAME" \
	  lint "$(lint_TYPES)" "" "k8s: $(k8s_TYPES)" \
	  test "$(test_TYPES)" \
	  fix "$(fix_TYPES)" \
	  check "$(check_TYPES)" \
	  deploy "$(deploy_TYPES)"
	@printf '\nOther parameters:\n'
	@printf '  \033[36m%-20s\033[0m %s\n' \
	  'UPDATE=<packages>' 'lock: move these to their newest allowed versions too' \
	  'PYTEST_ARGS=<args>' "test TYPE=idempotence: more for its pytest, e.g. PYTEST_ARGS='-m cluster'" \
	  'TAGS=<tags>' 'check, deploy: those stages only, e.g. TYPE=truenas TAGS=truenas_pools' \
	  'ONLY=<names>' 'suspend, resume, check TYPE=suspend|resume: some apps, groups or homeassistant' \
	  'ALL=1' "suspend, resume, check TYPE=suspend|resume: the cluster's VMs too" \
	  'ANSIBLE_ARGS=<args>' "every playbook: more for ansible-playbook, e.g. '-e talos_allow_bootstrap=true'" \
	  'NAME=<setup>' 'vault: the setup to make, to switch to, or (without TYPE) whose vault to edit' \
	  'FROM=current' "vault TYPE=new: this checkout's own local.yml, vault and .vault_pass become setup NAME" \
	  'SETUPS=<dir>' 'vault, init: where the setups are kept (default: ~/.config/homeserver/setups, or $$XDG_CONFIG_HOME/...)' \
	  'VENV=<path>' "every target: the project's virtualenv (default: the active one, else python3's here)"

##@ Setup
.PHONY: install
# Poetry's sync installs exactly the lock (the main and dev groups) into the project's virtualenv, and removes anything
# else there, apart from pip.
install: ## Install the locked tooling into the project's virtualenv (Poetry from PATH)
	$(POETRY_FOUND)
	$(ENV_FOUND)
	$(POETRY_RUN) sync

.PHONY: init
# The settings are asked for by scripts/init/init.py, which says at the end what is still required. Run it again
# whenever a setting changes: Enter keeps each one as it is. Without a terminal it only prints that summary. A
# virtualenv without the tooling gets make install first; one that has it is left as it is, so make init needs Poetry
# only to fill a new virtualenv (make install keeps a filled one in step with poetry.lock).
INIT_TOOLS := $(and $(wildcard $(BIN)/pre-commit),$(wildcard $(BIN)/ansible-playbook))
init: $(if $(INIT_TOOLS),,install) ## The git hooks, then local.yml and the vault asked for one by one, and what is still missing
	$(BIN)/pre-commit install
	git config blame.ignoreRevsFile .git-blame-ignore-revs
	@test -f inventory || cp inventory.example inventory
	@$(BIN)/python scripts/init/init.py $(if $(TYPES),--full)

.PHONY: lock
# Re-resolves only what pyproject.toml changed, keeping every other locked version; UPDATE="<package> ..."
# moves those packages to their newest allowed versions too. Either way, only releases at least 7 days old
# (poetry.toml).
UPDATE ?=
lock: ## Update poetry.lock after editing pyproject.toml
	$(POETRY_FOUND)
	$(ENV_FOUND)
	$(POETRY_RUN) $(if $(UPDATE),update --lock $(UPDATE),lock)

# The vault: git-ignored, at a path no commit ever held. Until 2026-10-20 it was committed at OLD_VAULT.
VAULT     := group_vars/all/local.vault.yml
OLD_VAULT := group_vars/all/vault.yml

.PHONY: vault vault/edit vault/new vault/list vault/switch
vault: vault/$(if $(TYPES),$(TYPES),edit) ## Edit the vault's secrets (addresses go in local.yml), then lint them; NAME= another setup's

# With NAME, another setup's vault, with its own password, and the checkout keeps the setup it uses; the lint reads
# the checkout's setup alone, so it is left out.
vault/edit:
ifneq ($(NAME),)
	@$(SETUPS_PY) edit '$(NAME)'
else
	@$(SETUPS_PY) check
	@test -f $(VAULT) || { echo "There is no $(VAULT): restore it from your password manager, or make vault TYPE=new"; exit 1; }
	$(BIN)/ansible-vault edit $(VAULT)
	@$(MAKE) --no-print-directory lint TYPE=ansible
endif

vault/list:
	@$(SETUPS_PY) list

vault/switch:
	@$(SETUPS_PY) switch '$(NAME)'

# With NAME, a setup of that name, which the checkout then uses: local.yml with every setting commented out, an empty
# vault and a random password (FROM=current: this checkout's own three files, moved into it).
# Without NAME, the vault in the checkout itself. Every run decrypts the vault, so it and .vault_pass must exist, and
# both are yours alone: neither is ever committed (.gitignore; README.md, Setups and the vault). This makes what is
# missing: a random .vault_pass readable by its owner alone, and an empty vault encrypted with it. A vault that already
# opens with .vault_pass is left alone, and one that does not is refused, since it may hold your secrets with their
# password lost; so is a checkout that still has the vault's old, committed place, whose vault moves rather than a new
# one being made beside it, and one that uses a setup, whose links this would write through. Without ansible-vault it
# stops before anything else. The empty vault reaches ansible-vault on stdin, so the file is written only once
# encryption has worked.
vault/new:
	@test -x $(BIN)/ansible-vault || { echo "ansible-vault is not in $(VENV): activate the project's virtualenv, or pass VENV=<path>"; exit 1; }
ifneq ($(NAME)$(FROM),)
	@$(SETUPS_PY) new '$(NAME)' --from='$(FROM)'
else
	@if [ -L .vault_pass ] || [ -L $(VAULT) ] || [ -L $(LOCAL) ]; then \
	  echo "This checkout uses a setup (make vault TYPE=list): make vault TYPE=new NAME=<name> makes another."; \
	  exit 1; \
	elif [ -f .vault_pass ] && [ -f $(VAULT) ] && $(BIN)/ansible-vault view $(VAULT) >/dev/null 2>&1; then \
	  echo "$(VAULT) already opens with .vault_pass: nothing to do."; \
	elif [ -f $(VAULT) ]; then \
	  echo "$(VAULT) does not open with .vault_pass, so it may hold your secrets: restore the .vault_pass" \
	    "that opens it rather than replace it. To start afresh all the same, move the file out of the repository first."; \
	  exit 1; \
	elif [ -f $(OLD_VAULT) ]; then \
	  echo "$(OLD_VAULT) is where the vault was committed until 2026-10-20. Move it rather than make a new one:" \
	    "README.md, Setups and the vault, says how."; \
	  exit 1; \
	else \
	  umask 077 && { [ -f .vault_pass ] || openssl rand -hex 32 > .vault_pass; } \
	  && printf -- '---\n{}\n' | $(BIN)/ansible-vault encrypt --output $(VAULT) \
	  && echo "Made $(VAULT), encrypted with .vault_pass. Keep a copy of both in your password manager."; \
	fi
endif

##@ Code
.PHONY: lint $(addprefix lint/,$(lint_TYPES))
lint: $(call targets,lint) ## Every linter; make -k lint reports every failure, not only the first

# Poetry checks that pyproject.toml is valid and that the lock was made from it (its content-hash), and
# --strict fails on warnings too. tests/policy checks what Poetry does not: the pins and the hashes.
lint/lock:
	$(POETRY_FOUND)
	$(POETRY_RUN) check --lock --strict

lint/yaml:
	$(BIN)/yamllint --strict .

# ansible-lint at the production profile, strict. Its syntax check reads the vault password file ansible.cfg names.
# Without .vault_pass (a fresh clone, or CI, which never has a vault: it is git-ignored) it gets a placeholder.
lint/ansible:
	@if [ -z "$$ANSIBLE_VAULT_PASSWORD_FILE" ] && [ ! -f .vault_pass ]; then \
	  mkdir -p .cache && echo placeholder > .cache/vault-placeholder; \
	  export ANSIBLE_VAULT_PASSWORD_FILE="$$PWD/.cache/vault-placeholder"; \
	fi; $(BIN)/ansible-lint

# ruff's lint and its formatting check, both always run; make fix TYPE=python fixes what it can.
lint/python:
	@rc=0; $(BIN)/ruff check . || rc=1; $(BIN)/ruff format --check . || rc=1; exit $$rc

# The documentation the roles carry: every custom module's DOCUMENTATION parses (ansible-lint does not read it), and
# every role's REFERENCE.md is what docsible generates from the role as it is, generated again from a copy of roles/
# so the tree is left as it is. Each failure is reported.
lint/docs:
	@test -n "$(MODULES)" || { echo "No module found under roles/*/library: run make from the repository root"; exit 1; }
	@rc=0; for m in $(MODULES); do \
	  $(BIN)/ansible-doc -t module -M $$(dirname $$m) --json $$(basename $$m .py) </dev/null >/dev/null || rc=1; \
	done; \
	rm -rf .cache/docsible && mkdir -p .cache/docsible && cp -R roles .cache/docsible/; \
	stale=0; $(call DOCSIBLE_RUN,.cache/docsible); \
	for role in $(ROLES); do diff -u $$role/$(DOCSIBLE_PAGE) .cache/docsible/$$role/$(DOCSIBLE_PAGE) || stale=1; done; \
	[ $$stale = 0 ] || { rc=1; echo "A role's $(DOCSIBLE_PAGE) is missing or stale: run make fix TYPE=docs and commit it" >&2; }; \
	exit $$rc

# dependabot.yml, renovate.json and the workflows against their published schemas, bundled with check-jsonschema, so
# offline.
lint/github:
	$(BIN)/check-jsonschema --builtin-schema vendor.dependabot .github/dependabot.yml
	$(BIN)/check-jsonschema --builtin-schema vendor.renovate renovate.json
	$(BIN)/check-jsonschema --builtin-schema vendor.github-workflows .github/workflows/*.yml

# The repo's own manifests, three ways, each run whatever the one before found:
# - kubeconform, strict, on every file as it is, against Talos's Kubernetes version and the pinned CRD schemas. Files
#   before kustomize and Flux's substitution: kustomization.yaml files are kustomize's, not objects, and SOPS files carry
#   their own top-level sops: key. kubeconform retries each schema download itself (twice, with backoff), so an invalid
#   manifest fails at once and nothing here retries.
# - each directory with a kustomization.yaml built as Flux builds it, then kubeconform, so a patch that no longer
#   applies, or a resource listed and missing, fails. The SOPS-encrypted Secrets are skipped: their sops: key is not a
#   Secret's (test_k8s.py checks them).
# - kube-linter with the repo's checks, .kube-linter.yaml, on each setup's manifests as Flux applies them, built and with
#   the settings substituted: kube-linter silently skips an object it cannot decode, such as a resource limit of
#   "${GPU_I915}" (scripts/ci/render_manifests.py).
lint/manifests:
	@$(call FOUND,kubeconform); $(call FOUND,kustomize); $(call FOUND,kube-linter)
	@mkdir -p $(KUBE_CACHE); rc=0; \
	echo "kubeconform: every file under k8s/"; \
	$(KUBECONFORM) -ignore-filename-pattern 'kustomization\.yaml$$' -ignore-filename-pattern '\.sops\.yaml$$' k8s || rc=1; \
	for k in $$(find k8s -name kustomization.yaml | sort); do d=$$(dirname "$$k"); \
	  echo "kustomize build $$d"; kustomize build --load-restrictor=LoadRestrictionsNone "$$d" > $(KUBE_CACHE)/build.yaml \
	  && $(KUBECONFORM) -skip=Secret $(KUBE_CACHE)/build.yaml || rc=1; done; \
	if $(BIN)/python scripts/ci/render_manifests.py --out $(K8S_RENDERED); then \
	  for d in $(K8S_RENDERED)/*/; do echo "kube-linter $$d"; \
	    kube-linter lint --config .kube-linter.yaml --fail-if-no-objects-found "$$d" || rc=1; done; \
	else rc=1; fi; \
	exit $$rc

# The charts the HelmReleases install, rendered at their pins with each setup's placeholder settings, then checked as
# the repo's own manifests are: kubeconform (a chart's own CRD definitions aside), kube-linter's request and image
# checks, and test_k8s.py's host paths and privilege rules. A chart bump that adds a hostPath or a privileged pod, or a
# misspelt values key a chart's schema refuses, fails before it merges.
lint/charts:
	@$(call FOUND,helm); $(call FOUND,kubeconform); $(call FOUND,kube-linter)
	$(BIN)/python scripts/ci/render_helm.py --out $(HELM_RENDERED) --kube-version $(KUBERNETES_VERSION)
	$(KUBECONFORM) -skip=CustomResourceDefinition $(HELM_RENDERED)
	kube-linter lint --config .kube-linter-charts.yaml --fail-if-no-objects-found $(HELM_RENDERED)
	HELM_RENDERED=$(HELM_RENDERED) $(BIN)/pytest -q -p no:cacheprovider tests/policy/test_k8s.py -k chart

# The Talos machine configurations of every setup, rendered by roles/talos's own render tasks and validated by
# talosctl, which catches what rendering lets through (a misspelt field). Needs no cluster and no vault. talosctl
# comes from PATH, pinned in CI with talos_version; without it the check is skipped locally, saying so, and fails
# in CI (CI set), as the gitleaks tests do.
lint/talos:
	@command -v talosctl >/dev/null || { if [ -n "$$CI" ]; then echo "talosctl is not on PATH: the CI job must install it"; exit 1; fi; \
	  echo "talosctl is not on PATH: make lint TYPE=talos skipped (CI runs it)"; exit 0; }; \
	  ANSIBLE_CONFIG=tests/integration/ansible.cfg ANSIBLE_NOCOLOR=1 $(PLAYBOOK) tests/talos_validate.yml </dev/null

.PHONY: test $(addprefix test/,$(test_TYPES))
# More for the idempotence tests' pytest, e.g. PYTEST_ARGS='-k storage' or, as CI splits them between its jobs,
# PYTEST_ARGS='-m cluster' (the VM, backup, Talos and Flux playbooks) and PYTEST_ARGS='-m "not cluster"'. The other types
# leave it out: a marker or a shard meant for these would select nothing there, or replace the marker that keeps the
# policy and gitleaks tests apart. Run pytest itself to narrow those.
PYTEST_ARGS ?=
test: $(call targets,test) ## Every test: unit, policy, the gitleaks rules, then each role against local fakes

# One pytest worker per CPU, as for the idempotence tests. The unit tests report the modules' coverage (.coveragerc).
test/unit:
	$(BIN)/pytest -q -n auto --cov --cov-report=term tests/unit

# The tests that check the repo's own rules, bar the gitleaks ones.
test/policy:
	$(BIN)/pytest -q -n auto -m "not gitleaks" tests/policy

# .gitleaks.toml against generated fixtures, with gitleaks from PATH: skipped without it, and failing in CI.
test/gitleaks:
	$(BIN)/pytest -q -m gitleaks tests/policy

# Each role run repeatedly against local fakes. One pytest worker per CPU: each test runs its own fakes and playbooks,
# so they run side by side, and in sequence the suite outgrew the CI job's timeout. Work stealing: the tests take from
# a second to a minute each, so a worker that runs out of tests takes some queued for another, rather than idling while
# the others finish theirs.
test/idempotence:
	$(BIN)/pytest -q -n auto --dist worksteal $(PYTEST_ARGS) tests/integration

.PHONY: fix $(addprefix fix/,$(fix_TYPES))
fix: $(call targets,fix) ## Write what lint can fix: the Python's formatting and safe fixes, every role's REFERENCE.md

# ruff's safe fixes, then its formatting, then a check that reports only what neither could fix (a long line the
# formatter wrapped is fixed, though the first pass saw it).
fix/python:
	$(BIN)/ruff check --fix --exit-zero --silent .
	$(BIN)/ruff format .
	$(BIN)/ruff check .

# Every role's REFERENCE.md, written with docsible from the role as it is: never edit one by hand.
fix/docs:
	@rc=0; $(call DOCSIBLE_RUN,.); \
	[ $$rc = 0 ] && printf 'Wrote %s\n' $(addsuffix /$(DOCSIBLE_PAGE),$(ROLES)); exit $$rc

##@ Machines
.PHONY: check
check: ## Dry run of deploy, or of TYPE: what it would change, with the diff
	$(RUN) $(TYPE_PLAYBOOK) --check --diff $(TAG_ARGS) $(if $(filter suspend resume,$(TYPES)),$(ONLY_ARGS) $(ALL_ARGS)) $(ANSIBLE_ARGS)

.PHONY: deploy
deploy: ## Build and configure everything, the NAS first (its play ends by itself without one), or TYPE alone
	$(RUN) $(TYPE_PLAYBOOK) --diff $(TAG_ARGS) $(ANSIBLE_ARGS)

.PHONY: suspend
suspend: ## Suspend the apps (scaled to zero, Flux told to leave them, data kept) and Home Assistant's VM
	$(RUN) suspend.yml --diff $(ONLY_ARGS) $(ALL_ARGS) $(ANSIBLE_ARGS)

.PHONY: resume
resume: ## Bring back what make suspend stopped, the apps handed back to Flux
	$(RUN) resume.yml --diff $(ONLY_ARGS) $(ALL_ARGS) $(ANSIBLE_ARGS)

.PHONY: talos-upgrade
talos-upgrade: ## Move the running cluster to the pinned Talos and Kubernetes versions (etcd snapshot first)
	$(RUN) talos-upgrade.yml --diff $(ANSIBLE_ARGS)

.PHONY: etcd-snapshot
etcd-snapshot: ## Snapshot etcd into ~/.talos/homeserver/etcd/
	$(RUN) etcd-snapshot.yml $(ANSIBLE_ARGS)
