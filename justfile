# Entry points: `just` lists them, by group, and `just --usage <recipe>` shows one's arguments and options. Each does
# the whole of its job by default, and its arguments narrow it to part of it (just lint yaml python, just check proxmox).
# Every Python tool comes from the project's virtualenv, so the versions locked in poetry.lock are the ones that run,
# here and in CI (.github/workflows/ci.yml). just, Poetry, kubeconform, kube-linter, kustomize, helm, talosctl and
# gitleaks are the exceptions: they come from PATH (Homebrew or pipx locally; CI pins each version, and each binary's
# checksum), as does openssl, which the idempotence tests use. just is one because it runs just install, which fills
# the virtualenv: it cannot come from inside it.
# The virtualenv is yours to make and keep, wherever you keep them (pyenv virtualenv, python3 -m venv): just never
# creates one, and just install fills it from the lock. just uses the active one (VIRTUAL_ENV, set by `pyenv activate
# <env>` or `source <env>/bin/activate`); otherwise the virtualenv python3 runs from, but only when this checkout chose
# it: `pyenv local <env>` wrote a .python-version at the checkout's top (a worktree also takes its main checkout's), or
# `pyenv shell <env>` chose it for the shell. A version pyenv takes from anywhere else (pyenv global, a .python-version
# above the checkout) is no one project's, and just install would remove every package poetry.lock does not list from
# it; a python3 that is not a virtualenv (the system's, Homebrew's) would run tools no lock pinned. `pyenv shell <env>`
# counts by its PYENV_VERSION, but only when that differs from what the version files say: pyenv's shims export
# PYENV_VERSION too, set from those files, to whatever they run, and just runs through one when pip installed it into a
# pyenv version. Override with VENV=<path> in the environment. Finding none, a recipe that needs it stops before any of
# its lines runs, saying how to choose one; `just` and `just --list` never need it.

# A just from 1.55 on that is older than this says so by name. Older ones (Debian 13's 1.40, Ubuntu 26.04's 1.45) do not
# know the setting and stop at this line with "Unknown setting": README says where to get a newer one. CI pins its own
# version (JUST_VERSION in .github/workflows/ci.yml).
set minimum-version := '1.55.0'
# Only what a recipe uses is worked out, so `just --list` asks nothing of pyenv or git. `just` alone runs help, which
# works out VENV first, as every recipe does: an exported variable is always worked out.
set lazy
# A recipe's arguments reach its lines as "$1", "$@", so an argument holding spaces or quotes stays one argument.
set positional-arguments
set shell := ["bash", "-euo", "pipefail", "-c"]
set script-interpreter := ["bash", "-euo", "pipefail"]

chosen_python := ```
    (f=$(pyenv version-file 2>/dev/null) || exit 0
    d=$(CDPATH='' cd -- "$(dirname -- "$f")" && pwd -P)
    top=$(git rev-parse --show-toplevel 2>/dev/null) || top=
    main=$(dirname -- "$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)")
    shell=; [ -z "${PYENV_VERSION:-}" ] || [ "$PYENV_VERSION" = "$(PYENV_VERSION='' pyenv version-name)" ] || shell=1
    if [ -n "$shell" ] || [ "$d" = "$top" ] || [ "$d" = "$main" ]; then
      python3 -c 'import sys; print(sys.prefix if sys.prefix != sys.base_prefix else "")'
    fi) 2>/dev/null || true
```
# One variable, exported, so that VENV in the environment, `just VENV=<path> <recipe>` and the just a recipe runs again
# (lint, test and fix, once per type) all mean the same virtualenv. A relative VENV in the environment is the caller's;
# on just's command line it replaces this whole expression, so give it absolute there.
export VENV := if env('VENV', '') != '' { clean(join(invocation_directory(), env('VENV'))) } else if env('VIRTUAL_ENV', '') != '' { env('VIRTUAL_ENV') } else { chosen_python }
bin := if VENV == '' { error("No Python environment for this checkout: activate the project's virtualenv, choose it here with pyenv local <env> (a .python-version at the checkout's top), or set VENV=<its path>") } else { VENV / 'bin' }
# Recipes find the virtualenv's tools first, as in an activated virtualenv (ansible-lint warns when its own directory is
# missing from PATH), and pip skips its check for a newer pip.
export PATH := if VENV == '' { env('PATH') } else { VENV / 'bin' + ':' + env('PATH') }
export PIP_DISABLE_PIP_VERSION_CHECK := '1'
self := quote(just_executable()) + ' --justfile ' + quote(justfile())

# Poetry installs the project virtualenv from poetry.lock. Install it yourself, outside that virtualenv
# (https://python-poetry.org/docs/#installation, e.g. `pipx install poetry`); pyproject.toml's requires-poetry says
# which versions work. Override with POETRY=<path>. Every Poetry command gets the project virtualenv as VIRTUAL_ENV, so
# it uses the one just does and never looks for a Python of its own.
poetry := env('POETRY', 'poetry')
poetry_run := 'VIRTUAL_ENV=' + quote(VENV) + ' ' + quote(poetry) + ' --no-interaction'
# Poetry missing stops a recipe that needs it, with how to get it.
poetry_found := 'command -v ' + quote(poetry) + ' >/dev/null || { echo "Poetry is not on PATH: install it (pipx install poetry)" >&2; exit 1; }'
# just install and just lock refuse a Python that is not a virtualenv: poetry sync removes every package the lock does
# not list, so on a Python that is not the project's alone (a pyenv version, the system's) it would uninstall the tools
# installed there. These checks are lines of the recipes themselves, never dependencies, which --no-deps would skip.
virtualenv_found := quote(bin / 'python') + " -c 'import sys; sys.exit(sys.prefix == sys.base_prefix)' 2>/dev/null || { echo " + quote(VENV + ' is not a virtualenv, and poetry sync would remove every package there that poetry.lock does not list. Make one for this project and use it (pyenv virtualenv <python 3.12+> homeserver && pyenv local homeserver, or python3 -m venv <dir> && source <dir>/bin/activate), then just install.') + ' >&2; exit 1; }'

# The types lint, test and fix run without arguments, in this order, each a private recipe <recipe>-<type>; written as
# the pattern their arguments must match, so a type that is not one stops just before anything runs.
# lint k8s: the Kubernetes checks, the ones that need kubeconform, kustomize, helm, kube-linter or talosctl.
lint_types := 'lock|yaml|ansible|python|docs|github|manifests|charts|talos'
k8s_types := 'manifests|charts|talos'
test_types := 'unit|policy|gitleaks|idempotence'
fix_types := 'python|docs'

# The playbooks check and deploy take, and suspend's and resume's options, each written once for every recipe that
# takes them. --only is checked loosely, as the playbooks read it (blanks and empty names dropped); they refuse a name
# that is no app, group or homeassistant.
playbooks := 'site|truenas|proxmox|homeassistant|kubernetes'
playbook_help := 'site (the default: every one, the NAS first), truenas, proxmox, homeassistant or kubernetes'
only_pattern := '[a-z0-9, -]*'
only_help := 'some apps, groups or homeassistant, comma-separated'
check_help := 'what it would do, changing nothing'
# ansible-playbook's arguments: after the playbook for check and deploy, whose first argument is the playbook; after --
# for the recipes with options of their own, since just reads every word that begins with - before it as one of them.
playbook_args_help := "ansible-playbook's, after the playbook: --tags <stage>, -e <var>=<value>, -v"
# deploy refuses uncommitted changes to what Ansible reads (_playbook), unless given this after the playbook.
allow_dirty_help := "; --allow-dirty deploys uncommitted changes too (every playbook: just deploy site --allow-dirty)"
option_args_help := "ansible-playbook's, after --: before it, just reads a word that begins with - as one of this recipe's options"
# status's sections, in the order the report shows them, and the file each run writes the report to, which the recipe
# reads its exit code from.
status_sections := 'apps|admin|tailscale|vms|storage|backups|cluster|nas'
status_report := '.cache/status/report.json'

# The vault: git-ignored, at a path no commit ever held. Until 2026-10-20 it was committed as group_vars/all/vault.yml.
vault := 'group_vars/all/local.vault.yml'
# Setups (scripts/init/setups.py): each machine's local.yml, vault and .vault_pass, kept together in a store outside the
# checkout, SETUPS or $XDG_CONFIG_HOME/homeserver/setups (~/.config/homeserver/setups without it), and the one the
# checkout uses linked in at group_vars/all/local.yml, the vault and .vault_pass. just vault names a setup as an
# argument, never from the environment, so a NAME a shell exports (WSL sets one) reaches nothing; every other recipe
# runs with the setup the checkout links to.
setups := quote(bin / 'python') + ' scripts/init/setups.py'
ansible_playbook := bin / 'ansible-playbook'
log_dir := 'logs'

# Manifests are validated against the Kubernetes version Talos ships (v1.14.2 runs 1.37.1), with the JSON schemas
# pinned to one commit of yannh/kubernetes-json-schema instead of its moving master.
kubernetes_version := '1.37.1'
kube_schema_ref := '8df8a883b68a24a104b4a9e43c1288090ae60b3b'
kube_schemas := 'https://raw.githubusercontent.com/yannh/kubernetes-json-schema/' + kube_schema_ref + '/{{.NormalizedKubernetesVersion}}-standalone{{.StrictSuffix}}/{{.ResourceKind}}{{.KindSuffix}}.json'
# Custom resources (Flux's, MetalLB's, cert-manager's...) are validated against the CRD schemas of
# datreeio/CRDs-catalog, pinned to one commit like the Kubernetes schemas.
kube_crd_schema_ref := '63669a570e231d4f1f8396d229a1de512bcf0a34'
kube_crd_schemas := 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/' + kube_crd_schema_ref + '/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'
kube_cache := '.cache/kubeconform'
kubeconform := 'kubeconform -strict -summary -cache ' + kube_cache + ' -kubernetes-version ' + kubernetes_version + " -schema-location '" + kube_schemas + "' -schema-location '" + kube_crd_schemas + "'"
# What lint charts renders the charts into, and lint manifests each setup's manifests.
helm_rendered := '.cache/helm-rendered'
k8s_rendered := '.cache/k8s-rendered'
# Each role's REFERENCE.md is docsible's page for it, generated whole from the role as it is: meta/main.yml,
# meta/argument_specs.yml, the defaults and vars, and every tasks file as a table and a Mermaid graph. It is never
# edited by hand; its README.md is, and stays the role's own. --no-docsible leaves out docsible's .docsible file,
# metadata meant to be filled in by hand, which would add only the date it was first made; --no-backup the copy of the
# previous page, which git keeps; the empty --playbook the example playbook docsible looks for in the role's
# tests/test.yml, which no role here has: without it docsible prints "playbook not found", which fails the run below.
docsible := quote(bin / 'docsible') + ' --no-backup --no-docsible --graph --playbook "" --output REFERENCE.md'

# What `just` prints above the recipes: just writes each one's arguments in its own notation, where playbook='site' reads
# as something to type, and has no setting to write them as --usage does ([playbook]). just --list prints without it.
list_heading := """
    Available recipes. An argument written name='x' can be left out, and is then x; give only its value
    (just deploy proxmox). *name takes any number of values. [OPTIONS] are --flags, which just help <recipe>
    lists; a recipe that has them takes ansible-playbook's arguments after -- (just suspend --check -- -v).
""" + "\n"

# help with recipes' names shows how to call each; without one, every recipe. It takes the names as its arguments, so
# `just help deploy` never goes on to run deploy. --usage takes one recipe at a time.
[default]
[doc("Show the recipes; `just help <recipe>` for one's arguments and options")]
[no-exit-message]
[script]
help *recipe:
    [ $# -gt 0 ] || exec {{ self }} --list --unsorted --list-heading {{ quote(list_heading) }}
    sep=
    for r; do printf '%s' "$sep"; {{ self }} --usage "$r"; sep=$'\n'; done

[doc("Install the locked tooling into the project's virtualenv (Poetry from PATH)")]
[group('setup')]
install:
    @{{ poetry_found }}
    @{{ virtualenv_found }}
    {{ poetry_run }} sync

# The settings are asked for by scripts/init/init.py, which says at the end what is still required. Run it again
# whenever a setting changes: Enter keeps each one as it is. Without a terminal it only prints that summary. A
# virtualenv without the tooling gets just install first; one that has it is left as it is, so just init needs Poetry
# only to fill a new virtualenv (just install keeps a filled one in step with poetry.lock).
[arg('full', long='full', value='--full', help='every optional setting too')]
[doc('The git hooks, then local.yml and the vault asked for one by one, and what is still missing')]
[group('setup')]
init full='':
    @[ -x {{ bin }}/pre-commit ] && [ -x {{ bin }}/ansible-playbook ] || {{ self }} install
    {{ bin }}/pre-commit install
    git config blame.ignoreRevsFile .git-blame-ignore-revs
    @test -f inventory || cp inventory.example inventory
    @{{ bin }}/python scripts/init/init.py {{ full }}

# Re-resolves only what pyproject.toml changed, keeping every other locked version. Either way, only releases at least
# 7 days old (poetry.toml).
[doc('Update poetry.lock after editing pyproject.toml; packages named move to their newest allowed versions too')]
[group('setup')]
lock *packages:
    @{{ poetry_found }}
    @{{ virtualenv_found }}
    {{ poetry_run }} {{ if packages == '' { 'lock' } else { 'update --lock' } }} "$@"

# edit <setup>: another setup's vault, with its own password, and the checkout keeps the setup it uses; the lint reads
# the checkout's setup alone, so it is left out. new <setup>: a setup of that name, which the checkout then uses:
# local.yml with every setting commented out, a vault with every key empty and a random password (--from current: this
# checkout's own three files, moved into it). new alone: the vault in the checkout itself, and a .vault_pass if there is
# none, both encrypted before either is written; a vault that already opens is left alone, and one that does not is
# refused, since it may hold your secrets with their password lost. A new vault is local.vault.yml.example as it is:
# every key the playbooks read, each empty (unset) under a comment saying what it is for (scripts/init/setups.py says
# the rest).
[arg('action', pattern='edit|new|list|switch', help='edit (the default), new, list or switch')]
[arg('setup', help="switch and edit: the setup; new: its name, or none for the checkout's own vault")]
[arg('source', long='from', pattern='(current)?', help="new: this checkout's own local.yml, vault and .vault_pass become the setup")]
[doc("Edit the vault's secrets, then lint them; or the setups: list, switch <setup>, new [<setup>] [--from current], edit <setup>")]
[group('setup')]
[script]
vault action='edit' setup='' source='':
    action=$1 setup=$2 source=$3
    if [ -n "$source" ] && [ "$action" != new ]; then
      echo "--from current is just vault new's alone: just vault new <name> --from current" >&2; exit 1
    fi
    case $action in
      list)
        [ -z "$setup" ] || { echo "just vault list takes no setup" >&2; exit 1; }
        exec {{ setups }} list ;;
      switch)
        exec {{ setups }} switch "$setup" ;;
      new)
        test -x {{ bin }}/ansible-vault || { echo "ansible-vault is not in {{ VENV }}: activate the project's virtualenv, or set VENV=<path>" >&2; exit 1; }
        exec {{ setups }} new "$setup" --from="$source" ;;
      edit)
        [ -z "$setup" ] || exec {{ setups }} edit "$setup"
        {{ setups }} check
        test -f {{ vault }} || { echo "There is no {{ vault }}: restore it from your password manager, or just vault new" >&2; exit 1; }
        {{ bin }}/ansible-vault edit {{ vault }}
        exec {{ self }} lint ansible ;;
    esac

[arg('types', pattern=lint_types + '|k8s')]
[doc('Every linter, the failures named at the end; types: lock yaml ansible python docs github manifests charts talos, and k8s for the last three')]
[group('code')]
[no-exit-message]
lint *types: _venv
    @{{ self }} _each lint {{ quote(lint_types) }} '' "$@"

# --pytest-args: more for the idempotence tests' pytest, e.g. --pytest-args='-k storage' or, as CI splits them between
# its jobs, --pytest-args='-m cluster' (the VM, backup, Talos and Flux playbooks) and --pytest-args='-m "not cluster"'.
# The other types leave it out: a marker or a shard meant for these would select nothing there, or replace the marker
# that keeps the policy and gitleaks tests apart. Run pytest itself to narrow those.
[arg('types', pattern=test_types)]
[arg('pytest_args', long='pytest-args', help="idempotence: more for its pytest, e.g. --pytest-args='-m cluster'")]
[doc('Every test, the failures named at the end; types: unit policy gitleaks idempotence')]
[group('code')]
[no-exit-message]
test pytest_args='' *types: _venv
    @{{ self }} _each test {{ quote(test_types) }} "$1" "${@:2}"

[arg('types', pattern=fix_types)]
[doc("Write what lint can fix: the Python's formatting and safe fixes, every role's REFERENCE.md; types: python docs")]
[group('code')]
[no-exit-message]
fix *types: _venv
    @{{ self }} _each fix {{ quote(fix_types) }} '' "$@"

# Runs <recipe>-<type> for each type named, or for each of <recipe>'s types in their order, then fails naming every type
# that failed: one run reports every failure, as CI's jobs do. Ctrl-C stops the whole run, as it stopped make: the trap
# fires once the type running ends, whether its tool died of the signal or caught it and exited (pytest exits 2). extra
# is test's --pytest-args, which only test-idempotence takes.
[no-exit-message]
[private]
[script]
_each recipe all extra *types:
    trap 'exit 130' INT
    recipe=$1 all=$2 extra=$3; shift 3
    [ $# -gt 0 ] || set -- ${all//|/ }
    picked=
    for t in "$@"; do
      [ "$t" != k8s ] || t='{{ k8s_types }}' t=${t//|/ }
      for u in $t; do case " $picked " in *" $u "*) ;; *) picked="$picked $u" ;; esac; done
    done
    failed=
    for t in $picked; do
      if [ "$recipe-$t" = test-idempotence ] && [ -n "$extra" ]; then set -- "$recipe-$t" "$extra"; else set -- "$recipe-$t"; fi
      {{ self }} "$@" || { rc=$?; [ "$rc" -lt 128 ] || exit "$rc"; failed="$failed $t"; }
    done
    [ -z "$failed" ] || { echo "just $recipe failed:$failed" >&2; exit 1; }

# Poetry checks that pyproject.toml is valid and that the lock was made from it (its content-hash), and --strict fails
# on warnings too. tests/policy checks what Poetry does not: the pins and the hashes.
[private]
lint-lock:
    @{{ poetry_found }}
    {{ poetry_run }} check --lock --strict

[private]
lint-yaml:
    {{ bin }}/yamllint --strict .

# ansible-lint at the production profile, strict. Its syntax check reads the vault password file ansible.cfg names.
# Without .vault_pass (a fresh clone, or CI, which never has a vault: it is git-ignored) it gets a placeholder.
[private]
[script]
lint-ansible:
    if [ -z "${ANSIBLE_VAULT_PASSWORD_FILE:-}" ] && [ ! -f .vault_pass ]; then
      mkdir -p .cache && echo placeholder > .cache/vault-placeholder
      export ANSIBLE_VAULT_PASSWORD_FILE="$PWD/.cache/vault-placeholder"
    fi
    exec {{ bin }}/ansible-lint

# ruff's lint and its formatting check, both always run; just fix python fixes what it can.
[private]
lint-python:
    @rc=0; {{ bin }}/ruff check . || rc=1; {{ bin }}/ruff format --check . || rc=1; exit $rc

# The documentation the roles carry: every custom module's DOCUMENTATION parses (ansible-lint does not read it), and
# every role's REFERENCE.md is what docsible generates from the role as it is, generated again from a copy of roles/
# so the tree is left as it is. Each failure is reported.
[private]
[script]
lint-docs:
    modules=(roles/*/library/*.py)
    [ -e "${modules[0]}" ] || { echo "No module found under roles/*/library" >&2; exit 1; }
    rc=0
    for m in "${modules[@]}"; do
      {{ bin }}/ansible-doc -t module -M "$(dirname "$m")" --json "$(basename "$m" .py)" </dev/null >/dev/null || rc=1
    done
    rm -rf .cache/docsible && mkdir -p .cache/docsible && cp -R roles .cache/docsible/
    {{ self }} _docsible .cache/docsible || rc=1
    stale=0
    for role in roles/*/; do
      role=${role%/}; diff -u "$role/REFERENCE.md" ".cache/docsible/$role/REFERENCE.md" || stale=1
    done
    [ $stale = 0 ] || { rc=1; echo "A role's REFERENCE.md is missing or stale: run just fix docs and commit it" >&2; }
    exit $rc

# dependabot.yml, renovate.json and the workflows against their published schemas, bundled with check-jsonschema, so
# offline.
[private]
lint-github:
    {{ bin }}/check-jsonschema --builtin-schema vendor.dependabot .github/dependabot.yml
    {{ bin }}/check-jsonschema --builtin-schema vendor.renovate renovate.json
    {{ bin }}/check-jsonschema --builtin-schema vendor.github-workflows .github/workflows/*.yml

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
# The run fails when any of the three found something.
[private]
[script]
lint-manifests: (_found 'kubeconform') (_found 'kustomize') (_found 'kube-linter')
    mkdir -p {{ kube_cache }}; rc=0
    echo "kubeconform: every file under k8s/"
    {{ kubeconform }} -ignore-filename-pattern 'kustomization\.yaml$' -ignore-filename-pattern '\.sops\.yaml$' k8s || rc=1
    for k in $(find k8s -name kustomization.yaml | sort); do
      d=$(dirname "$k"); echo "kustomize build $d"
      kustomize build --load-restrictor=LoadRestrictionsNone "$d" > {{ kube_cache }}/build.yaml \
        && {{ kubeconform }} -skip=Secret {{ kube_cache }}/build.yaml || rc=1
    done
    if {{ bin }}/python scripts/ci/render_manifests.py --out {{ k8s_rendered }}; then
      for d in {{ k8s_rendered }}/*/; do
        echo "kube-linter $d"; kube-linter lint --config .kube-linter.yaml --fail-if-no-objects-found "$d" || rc=1
      done
    else rc=1; fi
    exit $rc

# The charts the HelmReleases install, rendered at their pins with each setup's placeholder settings, then checked as
# the repo's own manifests are: kubeconform (a chart's own CRD definitions aside), kube-linter's request and image
# checks, and test_k8s.py's host paths and privilege rules. A chart bump that adds a hostPath or a privileged pod, or a
# misspelt values key a chart's schema refuses, fails before it merges. kubeconform's cache directory is made here too,
# so lint charts runs alone on a fresh clone.
[private]
lint-charts: (_found 'helm') (_found 'kubeconform') (_found 'kube-linter')
    @mkdir -p {{ kube_cache }}
    {{ bin }}/python scripts/ci/render_helm.py --out {{ helm_rendered }} --kube-version {{ kubernetes_version }}
    {{ kubeconform }} -skip=CustomResourceDefinition {{ helm_rendered }}
    kube-linter lint --config .kube-linter-charts.yaml --fail-if-no-objects-found {{ helm_rendered }}
    HELM_RENDERED={{ helm_rendered }} {{ bin }}/pytest -q -p no:cacheprovider tests/policy/test_k8s.py -k chart

# The Talos machine configurations of every setup, rendered by roles/talos's own render tasks and validated by
# talosctl, which catches what rendering lets through (a misspelt field). Needs no cluster and no vault. talosctl
# comes from PATH, pinned in CI with talos_version; without it the check is skipped locally, saying so, and fails
# in CI (CI set), as the gitleaks tests do.
[private]
[script]
lint-talos:
    if ! command -v talosctl >/dev/null; then
      if [ -n "${CI:-}" ]; then echo "talosctl is not on PATH: the CI job must install it" >&2; exit 1; fi
      echo "talosctl is not on PATH: just lint talos skipped (CI runs it)"; exit 0
    fi
    ANSIBLE_CONFIG=tests/integration/ansible.cfg ANSIBLE_NOCOLOR=1 exec {{ ansible_playbook }} tests/talos_validate.yml </dev/null

# One pytest worker per CPU, as for the idempotence tests. The unit tests report the modules' coverage (.coveragerc).
[private]
test-unit:
    {{ bin }}/pytest -q -n auto --cov --cov-report=term tests/unit

# The tests that check the repo's own rules, bar the gitleaks ones.
[private]
test-policy:
    {{ bin }}/pytest -q -n auto -m "not gitleaks" tests/policy

# .gitleaks.toml against generated fixtures, with gitleaks from PATH: skipped without it, and failing in CI.
[private]
test-gitleaks:
    {{ bin }}/pytest -q -m gitleaks tests/policy

# Each role run repeatedly against local fakes. One pytest worker per CPU: each test runs its own fakes and playbooks,
# so they run side by side, and in sequence the suite outgrew the CI job's timeout. Work stealing: the tests take from
# a second to a minute each, so a worker that runs out of tests takes some queued for another, rather than idling while
# the others finish theirs. pytest_args is test's --pytest-args, a string the shell reads, quotes and all.
[private]
test-idempotence pytest_args='':
    {{ bin }}/pytest -q -n auto --dist worksteal {{ pytest_args }} tests/integration

# ruff's safe fixes, then its formatting, then a check that reports only what neither could fix (a long line the
# formatter wrapped is fixed, though the first pass saw it).
[private]
fix-python:
    {{ bin }}/ruff check --fix --exit-zero --silent .
    {{ bin }}/ruff format .
    {{ bin }}/ruff check .

# Every role's REFERENCE.md, written with docsible from the role as it is: never edit one by hand.
[private]
fix-docs:
    @{{ self }} _docsible . && printf 'Wrote %s\n' roles/*/REFERENCE.md

# docsible on every role under <dir>. docsible reports a file it cannot read, then leaves it out and exits 0, so any
# line besides its "Documentation generated at" fails the run.
[private]
[script]
_docsible dir:
    rc=0
    for role in roles/*/; do
      role=${role%/}
      out=$({{ docsible }} --role "$1/$role" 2>&1) || rc=1
      if printf '%s\n' "$out" | grep -v '^Documentation generated at: '; then rc=1; fi
    done
    exit $rc

# check <playbook> is deploy <playbook> --check; the other playbook recipes take --check themselves.
[arg('playbook', pattern=playbooks, help=playbook_help)]
[arg('ansible_args', help=playbook_args_help)]
[doc("Dry run of deploy, or of one playbook: what it would change, with the diff; then ansible-playbook's arguments: just check site --tags truenas_nfs")]
[group('machines')]
[no-exit-message]
check playbook='site' *ansible_args:
    @{{ self }} _playbook check "$1" --check --diff "${@:2}"

# Narrow a run to stages with --tags (just check truenas --tags truenas_pools), or pass a variable for one run:
# just deploy kubernetes -e talos_allow_bootstrap=true.
[arg('playbook', pattern=playbooks, help=playbook_help)]
[arg('ansible_args', help=playbook_args_help + allow_dirty_help)]
[doc("Build and configure everything, the NAS first, or one playbook; then ansible-playbook's arguments: just deploy truenas --tags truenas_pools")]
[group('machines')]
[no-exit-message]
deploy playbook='site' *ansible_args:
    @{{ self }} _playbook deploy "$1" --diff "${@:2}"

[arg('all', long='all', value='true', help="the cluster's VMs too, after everything else; not with --only")]
[arg('ansible_args', help=option_args_help)]
[arg('check', long='check', value='--check', help=check_help)]
[arg('only', long='only', pattern=only_pattern, help=only_help)]
[doc("Suspend the apps (scaled to zero, Flux told to leave them, data kept) and Home Assistant's VM")]
[group('machines')]
[no-exit-message]
suspend only='' all='' check='' *ansible_args:
    @{{ self }} _scale suspend "$@"

[arg('all', long='all', value='true', help="the cluster's VMs first; not with --only")]
[arg('ansible_args', help=option_args_help)]
[arg('check', long='check', value='--check', help=check_help)]
[arg('only', long='only', pattern=only_pattern, help=only_help)]
[doc('Bring back what just suspend stopped, the apps handed back to Flux')]
[group('machines')]
[no-exit-message]
resume only='' all='' check='' *ansible_args:
    @{{ self }} _scale resume "$@"

# suspend's and resume's options as the playbooks' variables: only as JSON, so spaces survive, and cluster_vms a boolean.
[no-exit-message]
[private]
[script]
_scale recipe only all check *ansible_args:
    recipe=$1 only=$2 all=$3 check=$4; shift 4
    case ${1-} in "" | -*) ;; *)
      echo "just $recipe takes apps with --only: just $recipe --only $1; ansible-playbook's arguments follow --" >&2; exit 2 ;;
    esac
    args=(--diff)
    [ -z "$check" ] || args+=(--check)
    [ -z "$only" ] || args+=(-e "{\"only\": \"$only\"}")
    [ -z "$all" ] || args+=(-e '{"cluster_vms": true}')
    exec {{ self }} _playbook "$recipe" "$recipe" "${args[@]}" "$@"

[arg('ansible_args', help="ansible-playbook's: --check shows the plan, changing nothing")]
[doc('Move the running cluster to the pinned Talos and Kubernetes versions (etcd snapshot first); --check shows the plan')]
[group('machines')]
[no-exit-message]
talos-upgrade *ansible_args:
    @{{ self }} _playbook talos-upgrade talos-upgrade --diff "$@"

[arg('ansible_args', help="ansible-playbook's: --check says what it would do")]
[doc("Snapshot etcd into ~/.talos/homeserver/etcd/, or the etcd/ under local.yml's kubernetes_talos_config_dir")]
[group('machines')]
[no-exit-message]
etcd-snapshot *ansible_args:
    @{{ self }} _playbook etcd-snapshot etcd-snapshot "$@"

# status reads the machines and changes nothing (status.yml), as a dry run with the repo's stdout callback, and keeps
# no log. It exits with the report's code, read from the file the run writes, which just passes on as its own: 0 when
# nothing is wrong, 1 for a problem, 2 when a machine could not be read, 3 when the run failed or was refused. The
# sections to show come first (just status vms storage), checked here before anything runs and again by the role, and
# ansible-playbook's arguments follow -- (just status vms -- -e status_probe_apps=false): before it, just reads a word
# that begins with - as one of status's own options, and refuses one it does not have. The recipe hands on everything
# from the first word that begins with -.
# Colour only on a terminal, and never with NO_COLOR set; on a terminal, without --json, the line on what is being read
# is drawn at once and kept up to date (STATUS_LIVE), except on one that says it can do nothing but print (TERM=dumb).
# The setup's links are checked by vars_plugins/setup_links.py, which keeps stdout to the report alone.
[arg('json', long='json', value='true', help='the report as JSON, alone on stdout')]
[arg('sections', help="some of apps admin tailscale vms storage backups cluster nas; then, after --, ansible-playbook's arguments")]
[doc('What runs, where to reach it and what needs doing, read without changing anything; sections: apps admin tailscale vms storage backups cluster nas')]
[group('machines')]
[no-exit-message]
[script]
status json='' *sections:
    json=$1; shift
    test -x {{ ansible_playbook }} || { echo "{{ ansible_playbook }} not found: run just install in the project's virtualenv, or point just at it (pyenv local <env>, or VENV=<its path>)" >&2; exit 3; }
    sections=()
    while [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; do
      case $1 in TYPE=* | JSON=* | ANSIBLE_ARGS=* | VENV=*)
        echo "$1: just takes no make-style parameter; just help status says what it takes" >&2; exit 3 ;;
      esac
      [[ $1 =~ ^({{ status_sections }})$ ]] || { echo "just status has no section $1: it takes {{ replace(status_sections, '|', ' ') }}" >&2; exit 3; }
      sections+=("$1"); shift
    done
    extra=()
    [ ${#sections[@]} -eq 0 ] || extra=(-e "{\"types\": \"$(IFS=,; echo "${sections[*]}")\"}")
    color=; if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then color=1; fi
    live=; if [ -t 1 ] && [ -z "$json" ] && [ "${TERM:-dumb}" != dumb ]; then live=1; fi
    # This checkout's version, which the report compares each machine's stamp with.
    version=$({{ self }} _deploy_version) || exit 3
    read -r commit describe date <<< "$version"
    rm -f {{ status_report }}
    rc=0
    ANSIBLE_STDOUT_CALLBACK=homeserver_status STATUS_COLOR=$color STATUS_LIVE=$live STATUS_QUIET=$json \
      HOMESERVER_COMMIT=$commit HOMESERVER_DESCRIBE=$describe HOMESERVER_COMMIT_DATE=$date \
      {{ ansible_playbook }} status.yml --check ${extra[@]+"${extra[@]}"} ${@+"$@"} </dev/null || rc=$?
    if [ "$rc" -ne 0 ] || [ ! -s {{ status_report }} ]; then echo "just status: the run failed (ansible-playbook exited $rc)" >&2; exit 3; fi
    if [ -n "$json" ]; then cat {{ status_report }}; fi
    exit "$({{ bin }}/python -c 'import json, sys; print(json.load(open(sys.argv[1]))["exit"])' {{ status_report }})"

# Every playbook run. A missing playbook binary means just found a Python other than the project's virtualenv, or one
# not yet installed: say so, rather than leave the shell's "No such file or directory" and a dangling latest.log. Then
# the setup: a run names the one it uses, and stops when its links are not whole, which Ansible would pass over without
# a word (vars_plugins/setup_links.py stops ansible-playbook started any other way). Every run is also written to its
# own log (git-ignored: it holds addresses), named after the setup, the recipe, the playbook and --tags, e.g.
# logs/20261004-210501-home-deploy-truenas-truenas_network.log; logs/latest.log points at the newest. The API key
# never appears: the modules mark it no_log. A -- the caller put before ansible-playbook's arguments is dropped, and a
# make-style TAGS=… or ANSIBLE_ARGS=… is refused rather than handed to ansible-playbook as a playbook's name. A run with
# --check logs as check-<recipe>, told by the arguments as ansible-playbook reads them: -C in a cluster of short options
# (-CD), --check or a prefix of it, never inside -e's value. A second run started in the same second logs to a file of
# its own (-2.log, -3.log), never into the first's. Last, the version (_deploy_version): the log's first line, said on
# the terminal, and given to the plays as HOMESERVER_COMMIT, HOMESERVER_DESCRIBE and HOMESERVER_COMMIT_DATE, always
# set, so a value left in the caller's shell never reaches a run; the deploy plays stamp it in the machines' Proxmox
# Notes. A real deploy refuses a tree that is no git checkout, whose stamp could name no commit, and uncommitted
# changes to what Ansible reads unless given --allow-dirty, which is taken out of the arguments here and refused for
# any other recipe, which refuses nothing; worked out after the checks above, so they still refuse before anything
# runs. A run that only lists or checks syntax (--list-tasks, --list-hosts, --list-tags, --syntax-check) deploys
# nothing and is never refused. HOMESERVER_PARTIAL names --start-at-task or --step, which leave tasks out as --limit
# and --skip-tags do, though no variable tells a play so: such a run stamps nothing.
[no-exit-message]
[private]
[script]
_playbook recipe playbook *ansible_args:
    recipe=$1 book=$2; shift 2
    test -x {{ ansible_playbook }} || { echo "{{ ansible_playbook }} not found: run just install in the project's virtualenv, or point just at it (pyenv local <env>, or VENV=<its path>)" >&2; exit 2; }
    args=() tags= prev= allow_dirty= partial= check= listing=
    for a in "$@"; do
      [ "$a" != -- ] || continue
      case $prev in -e | --extra-vars) ;; *) case $a in TYPE=* | TAGS=* | ONLY=* | ALL=* | ANSIBLE_ARGS=* | NAME=* | FROM=* | VENV=*)
        echo "$a: just takes no make-style parameter; just help $recipe says what it takes" >&2; exit 2 ;;
      --allow-dirty) allow_dirty=1; continue ;;
      --start-at-task | --start-at-task=* | --step) partial=${a%%=*} ;;
      # argparse takes any unambiguous prefix of a long option; --c could be --connection too.
      --ch | --che | --chec | --check) check=1 ;;
      --list-tasks | --list-hosts | --list-tags | --syntax-check) listing=1 ;;
      --*) ;;
      # Short options, one after another, until one that takes a value, which is the rest of the word.
      -?*) cluster=${a#-}
        while [ -n "$cluster" ]; do
          case ${cluster:0:1} in C) check=1 ;; [MefilTtcu]) break ;; esac
          cluster=${cluster:1}
        done ;;
      esac ;; esac
      case $prev in --tags|-t) tags=$a ;; esac
      case $a in --tags=*) tags=${a#--tags=} ;; esac
      args+=("$a") prev=$a
    done
    if [ -n "$allow_dirty" ] && [ "$recipe" != deploy ]; then
      echo "--allow-dirty is just deploy's alone: just $recipe refuses no uncommitted changes" >&2; exit 2
    fi
    {{ setups }} check
    setup=$({{ setups }} name 2>/dev/null || true)
    name=$recipe; [ "$book" = "$recipe" ] || [ "$book" = site ] || name="$name-$book"
    [ -z "$check" ] || [ "$recipe" = check ] || name="check-$name"
    version=$({{ self }} _deploy_version)
    read -r commit describe date <<< "$version"
    if [ -z "$commit" ]; then said="not a git checkout, so no version"; else said="$describe (committed $date)"; fi
    if [ "$recipe" = deploy ] && [ -z "$check" ] && [ -z "$listing" ]; then
      if [ -z "$commit" ]; then
        echo "just deploy runs from a git checkout, so that what reaches the machines is a commit you can find again, and their stamps name it: run it from a clone" >&2
        exit 2
      elif [ -z "$allow_dirty" ] && [ "${describe%-dirty}" != "$describe" ]; then
        echo "just deploy refuses uncommitted changes to what Ansible reads ($describe): commit them, so that what reaches the machines is a commit you can find again, and their stamps name it, or deploy them as they are with --allow-dirty after the playbook (just deploy $book --allow-dirty)" >&2
        exit 2
      fi
    fi
    base={{ log_dir }}/$(date +%Y%m%d-%H%M%S)${setup:+-$setup}-$name${tags:+-${tags//,/+}}
    umask 077; mkdir -p {{ log_dir }}
    # Created without clobbering (set -C), so a run started in the same second never writes into another's log.
    log=$base.log n=1
    until ( set -C; echo "homeserver $said" > "$log" ) 2>/dev/null; do
      n=$((n + 1)); [ "$n" -le 9 ] || { echo "just $recipe cannot create its log, $log" >&2; exit 2; }
      log=$base-$n.log
    done
    ln -sf "${log##*/}" {{ log_dir }}/latest.log
    echo "Logging to $log (Python environment: {{ VENV }})"
    echo "Version: homeserver $said"
    HOMESERVER_COMMIT=$commit HOMESERVER_DESCRIBE=$describe HOMESERVER_COMMIT_DATE=$date HOMESERVER_PARTIAL=$partial \
      ANSIBLE_LOG_PATH=$log exec {{ ansible_playbook }} "$book.yml" ${args[@]+"${args[@]}"}

# The version a run records (scripts/runbooks/tags.md): the newest commit on the first-parent chain that changed a
# file Ansible may read, named by git describe after the annotated CalVer tag before it (2026.11.0-3-g<commit>), or
# by the commit alone before the first. A commit that touches only k8s/ (Flux's), tests/, .github/, scripts/ci/, the
# linters', hooks' and tests' configuration or Markdown leaves it alone, so a merge no playbook can see moves no
# machine's stamp; the list names what to leave out, so a new file counts until it is left out too. -dirty when those
# files have uncommitted or untracked changes. Only a tag of CalVer's own shape counts, 20YY.M.PATCH (2026.11.0): any
# other (2026.11.0-rc1, v1.0, 2026.11, 2026.11.0.1) is passed over, never picked, so a stray tag never names a
# version. Printed on one line, the commit, its describe string and its date, each checked against the shape it must
# have, so nothing else ever reaches Ansible: the plays read them from the environment, which Ansible never templates,
# where an -e would be. Outside a git checkout, an empty line; a checkout git refuses to read, and a shallow clone,
# whose history may stop before the commit, stop the run.
[private]
[script]
_deploy_version:
    # Each would change how git matches the paths below, and none is this repo's to set.
    unset GIT_LITERAL_PATHSPECS GIT_GLOB_PATHSPECS GIT_NOGLOB_PATHSPECS GIT_ICASE_PATHSPECS
    spec=(. ':(exclude)k8s' ':(exclude)tests' ':(exclude).github' ':(exclude)scripts/ci' ':(exclude,glob)**/*.md')
    for f in .ansible-lint .coveragerc .git-blame-ignore-revs .gitattributes .gitleaks.toml '.kube-linter*.yaml' \
      .pre-commit-config.yaml .yamlfmt .yamllint pytest.ini renovate.json ruff.toml; do spec+=(":(exclude)$f"); done
    # Only "not a git repository" means no checkout: git missing, or refusing a checkout it does not trust, is said.
    if ! err=$(LC_ALL=C git rev-parse --is-inside-work-tree 2>&1 >/dev/null); then
      case $err in *"not a git repository"*) echo; exit 0 ;; esac
      echo "git cannot read this checkout, so the run has no version: ${err:-git failed}" >&2; exit 2
    fi
    if [ "$(git rev-parse --is-shallow-repository)" = true ]; then
      echo "this clone is shallow, so the commit that last changed Ansible's files may lie outside it: git fetch --unshallow, then run it again" >&2; exit 2
    fi
    # GitHub signs main's merges, and log.showSignature would print gpg's lines before the commit.
    log=(git -c log.showSignature=false log -1)
    # Empty in a repository with no commit yet, which has no version either.
    commit=$("${log[@]}" --first-parent --format=%H -- "${spec[@]}" 2>/dev/null) || commit=
    [ -n "$commit" ] || { echo; exit 0; }
    # git describe matches tags by glob, which cannot say 20YY.M.PATCH: each tag of that shape is named to it instead,
    # and with none it gives the commit alone.
    calver='20[0-9]{2}\.([1-9]|1[0-2])\.(0|[1-9][0-9]*)'
    match=()
    while IFS= read -r tag; do match+=(--match "$tag"); done < <(git tag --list | grep -E "^$calver\$" || true)
    [ ${#match[@]} -gt 0 ] || match=(--exclude '*')
    describe=$(git describe --first-parent --always --abbrev=12 "${match[@]}" "$commit")
    date=$("${log[@]}" --format=%cs "$commit")
    # Untracked files count whatever status.showUntrackedFiles says: a new role file changes what a run does. Its own
    # assignment, so a git status that fails stops the run rather than reading as a clean tree; without the index lock,
    # which a refresh of its stat data would take, since nothing here writes.
    changes=$(git --no-optional-locks status --porcelain --untracked-files=normal -- "${spec[@]}")
    [ -z "$changes" ] || describe=$describe-dirty
    # A net for git's own output. --abbrev=12 is a minimum, which git lengthens when 12 digits are not unique.
    hex='[0-9a-f]{12,40}'
    if ! [[ $commit =~ ^[0-9a-f]{40}$ && $date =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ &&
            $describe =~ ^($calver(-[0-9]+-g$hex)?|$hex)(-dirty)?$ ]]; then
      echo "git gave an unexpected version: commit '$commit', describe '$describe', date '$date'" >&2; exit 2
    fi
    echo "$commit $describe $date"

# lint, test and fix run just once per type: finding no virtualenv, this says so once, before any of them.
[private]
_venv:
    @test -x {{ bin }}/python || { echo "{{ VENV }} has no bin/python: point just at the project's virtualenv" >&2; exit 1; }

# A tool the Kubernetes checks take from PATH, with how to get it.
[private]
_found tool:
    @command -v "$1" >/dev/null || { echo "$1 is not on PATH (brew install $1)" >&2; exit 1; }
