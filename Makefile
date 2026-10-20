# Entry points: `make help` lists them. Each does the whole of its job by default; TYPE narrows one to part of it
# (make lint TYPE=yaml, make check TYPE=proxmox), and the other parameters below say how.
# Every Python tool comes from the project virtualenv, so the versions locked in
# poetry.lock are the ones that run, here and in CI (.github/workflows/ci.yml). Poetry, kubeconform,
# kube-linter and gitleaks are the exceptions: they come from PATH (pipx or Homebrew locally; CI pins each
# version, and each binary's checksum), as does openssl, which the idempotence tests and make vault TYPE=new use.
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

# TYPE narrows a target to some of what it does: one or more of its types, comma-separated (make lint TYPE=yaml,python),
# and exactly one for check and apply, which run one playbook (make check TYPE=proxmox). <target>_TYPES lists what each
# target takes, in the order they run; a target not in TYPED takes none. A type that is not a goal's, or a TYPE for a
# goal that takes none, stops make before anything runs.
TYPE    ?=
comma   := ,
empty   :=
space   := $(empty) $(empty)
TYPES   := $(strip $(subst $(comma), ,$(TYPE)))
TYPED   := init vault lint test fix check apply
init_TYPES  := full
vault_TYPES := new
lint_TYPES  := lock yaml ansible python docs github manifests charts talos
# make lint TYPE=k8s: the Kubernetes checks, the ones that need kubeconform, kustomize, helm, kube-linter or talosctl.
lint_ALIASES := k8s
k8s_TYPES   := manifests charts talos
test_TYPES  := unit policy gitleaks idempotence
fix_TYPES   := python docs
apply_TYPES := truenas proxmox homeassistant kubernetes
check_TYPES := $(apply_TYPES) talos-upgrade down up
list     = $(subst $(space),$(comma),$(strip $(1)))
define check_types
$(if $(filter $(1),$(TYPED)),,$(error make $(1) takes no TYPE))
$(if $(filter-out $($(1)_TYPES) $($(1)_ALIASES),$(TYPES)),$(error make $(1) takes TYPE=$(call list,$($(1)_TYPES) $($(1)_ALIASES)), not $(call list,$(filter-out $($(1)_TYPES) $($(1)_ALIASES),$(TYPES)))))
$(if $(and $(filter check apply,$(1)),$(word 2,$(TYPES))),$(error make $(1) runs one playbook: one TYPE at a time))
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
# Narrow make down and make up to some apps, groups or homeassistant, e.g. make down ONLY=jellyfin,nextcloud
ONLY    ?=
ONLY_ARGS := $(if $(ONLY),-e '{"only": "$(strip $(ONLY))"}',)
# Widen them to the cluster's VMs too, after everything else: make down ALL=1, make up ALL=1. Not with ONLY.
ALL     ?=
ALL_ARGS := $(if $(ALL),-e '{"cluster_vms": true}',)
# Anything else for ansible-playbook, such as a variable for one run:
#   make apply TYPE=kubernetes ANSIBLE_ARGS='-e talos_allow_bootstrap=true'
# (make's own -e is not that: it is make's --environment-overrides, and passes nothing to Ansible).
ANSIBLE_ARGS ?=
PLAYBOOK := $(BIN)/ansible-playbook
# Every playbook run is also written to its own log (git-ignored: it holds addresses), named after
# the target, TYPE and TAGS, e.g. logs/20261004-210501-apply-truenas-truenas_network.log. logs/latest.log
# points at the newest. The API key never appears: the modules mark it no_log.
LOG_DIR ?= logs
STAMP   := $(shell date +%Y%m%d-%H%M%S)
RUN_LOG  = $(LOG_DIR)/$(STAMP)-$@$(if $(TYPES),-$(TYPES))$(if $(TAGS),-$(subst $(comma),+,$(TAGS))).log
# A missing playbook binary means no virtualenv is active and there is no ./.venv; say so, rather than
# leave the shell's "No such file or directory" and a dangling latest.log.
RUN      = @test -x $(PLAYBOOK) || { echo "$(PLAYBOOK) not found: activate the project virtualenv, pass VENV=<its path>, or create ./.venv with make venv" >&2; exit 2; }; \
           umask 077 && mkdir -p $(LOG_DIR) && ln -sf $(notdir $(RUN_LOG)) $(LOG_DIR)/latest.log \
           && echo "Logging to $(RUN_LOG) (Python environment: $(VENV))" && ANSIBLE_LOG_PATH=$(RUN_LOG) $(PLAYBOOK)
# check and apply run the playbook TYPE names, or site.yml, every one in order, without it.
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
	@printf '\nTYPE narrows a target to part of what it does (several, comma-separated; check and apply take one):\n'
	@printf '  \033[36m%-14s\033[0m %s\n' \
	  init "$(init_TYPES): every optional setting too" \
	  vault "$(vault_TYPES): an empty vault of your own, for a setup without the maintainer's NAS" \
	  lint "$(lint_TYPES)" "" "k8s: $(k8s_TYPES)" \
	  test "$(test_TYPES)" \
	  fix "$(fix_TYPES)" \
	  check "$(check_TYPES)" \
	  apply "$(apply_TYPES)"
	@printf '\nOther parameters:\n'
	@printf '  \033[36m%-20s\033[0m %s\n' \
	  'PYTHON=<python>' 'venv: the Python 3.12+ it creates the virtualenv with' \
	  'UPDATE=<packages>' 'lock: move these to their newest allowed versions too' \
	  'PYTEST_ARGS=<args>' "test: more for pytest, e.g. PYTEST_ARGS='-m cluster'" \
	  'TAGS=<tags>' 'check, apply: those stages only, e.g. TYPE=truenas TAGS=truenas_pools' \
	  'ONLY=<names>' 'down, up, check TYPE=down|up: some apps, groups or homeassistant' \
	  'ALL=1' "down, up, check TYPE=down|up: the cluster's VMs too" \
	  'ANSIBLE_ARGS=<args>' "every playbook: more for ansible-playbook, e.g. '-e talos_allow_bootstrap=true'" \
	  'VENV=<path>' 'every target: the virtualenv (default: the active one, else ./.venv)'

##@ Setup
$(BIN)/activate:
	$(PYTHON) -m venv $(VENV)

.PHONY: venv
# Poetry's sync installs exactly the lock (the main and dev groups) and removes anything else, apart from pip.
venv: $(BIN)/activate ## Create the virtualenv and sync it to poetry.lock (Poetry from PATH)
	$(POETRY_FOUND)
	$(POETRY_RUN) sync

.PHONY: init
# The settings are asked for by scripts/init/init.py, which says at the end what is still required. Run it again
# whenever a setting changes: Enter keeps each one as it is. Without a terminal it only prints that summary.
init: venv ## The git hooks, then local.yml and the vault asked for one by one, and what is still missing
	$(BIN)/pre-commit install
	git config blame.ignoreRevsFile .git-blame-ignore-revs
	@test -f inventory || cp inventory.example inventory
	@$(BIN)/python scripts/init/init.py $(if $(TYPES),--full)

.PHONY: lock
# Re-resolves only what pyproject.toml changed, keeping every other locked version; UPDATE="<package> ..."
# moves those packages to their newest allowed versions too. Either way, only releases at least 7 days old
# (poetry.toml).
UPDATE ?=
lock: $(BIN)/activate ## Update poetry.lock after editing pyproject.toml
	$(POETRY_FOUND)
	$(POETRY_RUN) $(if $(UPDATE),update --lock $(UPDATE),lock)

.PHONY: vault vault/edit vault/new
vault: vault/$(if $(TYPES),$(TYPES),edit) ## Edit the vault's secrets (addresses go in local.yml), then lint it

vault/edit:
	$(BIN)/ansible-vault edit group_vars/all/vault.yml
	@$(MAKE) --no-print-directory lint TYPE=ansible

# Without a NAS the vault holds nothing this setup uses, but every run decrypts it, and only the maintainer
# has the password. This writes an empty vault over the tracked one, encrypted with .vault_pass, which it
# creates readable by its owner alone if there is none, and tells git to leave the file out of commits. A
# vault that already opens with .vault_pass, the maintainer's or one made here before, is left alone. Without
# ansible-vault it stops before anything else, since a vault it cannot open would look like someone else's.
# The empty vault reaches ansible-vault on stdin, so the file changes only once encryption has worked.
vault/new:
	@test -x $(BIN)/ansible-vault || { echo "ansible-vault is not in $(VENV): activate the project's virtualenv, or pass VENV=<path>"; exit 1; }
	@if [ -f .vault_pass ] && $(BIN)/ansible-vault view group_vars/all/vault.yml >/dev/null 2>&1; then \
	  echo "group_vars/all/vault.yml already opens with .vault_pass: nothing to do."; \
	elif [ -f group_vars/all/vault.yml ] && ! { tracked=$$(git rev-parse -q --verify :group_vars/all/vault.yml 2>/dev/null) \
	    && [ -n "$$tracked" ] && [ "$$(git hash-object group_vars/all/vault.yml)" = "$$tracked" ]; }; then \
	  echo "group_vars/all/vault.yml does not open with .vault_pass and is not the tracked vault, so it may be yours," \
	    "with your secrets in it: restore the .vault_pass that opens it rather than replace it. To start" \
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

# ansible-lint at the production profile, strict. It needs a vault password file for its syntax check. Without the
# real one (a fresh clone, or CI) it gets a placeholder, and it then skips the vault's contents; tests/policy checks
# that the vault stays encrypted.
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
# More for pytest, for every type, e.g. PYTEST_ARGS='-k plan' or, as CI splits the idempotence tests between its jobs,
# PYTEST_ARGS='-m cluster' (the VM, backup, Talos and Flux playbooks) and PYTEST_ARGS='-m "not cluster"'.
PYTEST_ARGS ?=
test: $(call targets,test) ## Every test: unit, policy, the gitleaks rules, then each role against local fakes

# One pytest worker per CPU, as for the idempotence tests. The unit tests report the modules' coverage (.coveragerc).
test/unit:
	$(BIN)/pytest -q -n auto --cov --cov-report=term $(PYTEST_ARGS) tests/unit

# The tests that check the repo's own rules, bar the gitleaks ones.
test/policy:
	$(BIN)/pytest -q -n auto -m "not gitleaks" $(PYTEST_ARGS) tests/policy

# .gitleaks.toml against generated fixtures, with gitleaks from PATH: skipped without it, and failing in CI.
test/gitleaks:
	$(BIN)/pytest -q -m gitleaks $(PYTEST_ARGS) tests/policy

# Each role run repeatedly against local fakes. One pytest worker per CPU: each test runs its own fakes and playbooks,
# so they run side by side, and in sequence the suite outgrew the CI job's timeout. Work stealing: the tests take from
# a second to a minute each, so a worker that runs out of tests takes some queued for another, rather than idling while
# the others finish theirs.
test/idempotence:
	$(BIN)/pytest -q -n auto --dist worksteal $(PYTEST_ARGS) tests/integration

.PHONY: fix $(addprefix fix/,$(fix_TYPES))
fix: $(call targets,fix) ## Write what lint can fix: the Python's formatting and safe fixes, every role's REFERENCE.md

# ruff's safe fixes, then its formatting; what it cannot fix is left for make lint TYPE=python to report.
fix/python:
	@rc=0; $(BIN)/ruff check --fix . || rc=1; $(BIN)/ruff format . || rc=1; exit $$rc

# Every role's REFERENCE.md, written with docsible from the role as it is: never edit one by hand.
fix/docs:
	@rc=0; $(call DOCSIBLE_RUN,.); \
	[ $$rc = 0 ] && printf 'Wrote %s\n' $(addsuffix /$(DOCSIBLE_PAGE),$(ROLES)); exit $$rc

##@ Machines
.PHONY: check
check: ## Dry run of apply, or of TYPE: what it would change, with the diff
	$(RUN) $(TYPE_PLAYBOOK) --check --diff $(TAG_ARGS) $(if $(filter down up,$(TYPES)),$(ONLY_ARGS) $(ALL_ARGS)) $(ANSIBLE_ARGS)

.PHONY: apply
apply: ## Configure every machine, the NAS first (its play ends by itself without one), or TYPE alone
	$(RUN) $(TYPE_PLAYBOOK) --diff $(TAG_ARGS) $(ANSIBLE_ARGS)

.PHONY: down
down: ## Spin the apps down (scaled to zero, Flux told to leave them) and Home Assistant's VM
	$(RUN) down.yml --diff $(ONLY_ARGS) $(ALL_ARGS) $(ANSIBLE_ARGS)

.PHONY: up
up: ## Bring back what make down stopped, the apps handed back to Flux
	$(RUN) up.yml --diff $(ONLY_ARGS) $(ALL_ARGS) $(ANSIBLE_ARGS)

.PHONY: talos-upgrade
talos-upgrade: ## Move the running cluster to the pinned Talos and Kubernetes versions (etcd snapshot first)
	$(RUN) talos-upgrade.yml --diff $(ANSIBLE_ARGS)

.PHONY: etcd-snapshot
etcd-snapshot: ## Snapshot etcd into ~/.talos/homeserver/etcd/
	$(RUN) etcd-snapshot.yml $(ANSIBLE_ARGS)
