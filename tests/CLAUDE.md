# tests/

How the suite is laid out and what it proves. Claude Code loads this file when it works on a file under `tests/`;
each role's README says which fakes and test playbooks cover it, and what the fakes were written from.

## The four types

`just test` runs four types, each on its own (`just test unit policy`):

- **unit** (`tests/unit/`): the modules, filters and scripts in process, with the modules' coverage (`.coveragerc`). A
  report, not a gate, since it sees only the in-process tests and not the modules the idempotence tests run inside
  ansible-playbook.
- **policy** (`tests/policy/`): the repo's own rules, below.
- **gitleaks**: `.gitleaks.toml` against generated fixtures (`tests/policy/test_gitleaks_rules.py`), gitleaks from
  `PATH`. Each rule is proved on what it must match and on what it must not (RFC 5737, loopback, image tags, version
  strings, QEMU's disk names, the certificate-pin placeholder). A leak the checks missed gets a fixture here.
- **idempotence** (`tests/integration/`): each role run repeatedly against local fakes, one pytest worker per CPU. More
  goes to its pytest with `--pytest-args` (`just test idempotence --pytest-args='-m cluster --shard 1/2'`); the
  cluster's roles are marked `cluster`.

## The idempotence harness

`tests/integration` checks every role against stateful fakes (`fake_truenas.py`, a TLS JSON-RPC server;
`fake_pve.py` behind `fake_pve_bin/`, with `apt-get` reading the scratch repository directory the test passes as
`proxmox_apt_sources_dir`; `fake_opnsense.py`, `fake_talos.py`, `fake_kubectl.py`, `fake_tailscale.py`), running each
role with this repo's configuration: a dry run, an apply, a second apply, a dry run, then drift corrected once. A new
API method or command needs a matching fake, or the test fails on purpose. The fakes always answer, so
`tests/unit/test_truenas_client.py` covers in process, with a scripted socket and a clock that never waits, what they
never do: TrueNAS refusing a login or a call, rate-limiting logins, dropping the connection, failing or never finishing
a job, and refusing an interface commit, which must discard the saved change, or never seeing the check-in, after which
TrueNAS rolls the address back by itself. The fakes cannot prove the real machines behave the same, so still run check
mode against the real machine first.

A test playbook under `tests/integration/` that stands in for a real one (`router.yml`, `homeassistant.yml`) runs the
real play's vars, pre-tasks and roles, and `tests/policy/test_roles.py` checks that they match. The idempotence tests
read NEXT STEPS' records from the default callback's custom stats (`next_steps` in `test_idempotence.py`), and the
tests that need a home directory or credentials run with `HOME` set to their own. gitleaks flags a MAC written out in
any tracked file, so the tests build theirs at run time. `openssl` comes from `PATH`, for the fake NAS's certificate.

`tests/conftest.py` sets the default stdout callback for every `ansible-playbook` a test starts, so a scratch tree
holding a copy of `ansible.cfg` alone runs (`tests/policy/helpers.py`'s `example_tree` builds one, `filter_plugins/`
included). It also writes each test's start and end as JSON lines to the file `HOMESERVER_LIVE_PYTEST` names, for
the live drawing of `just test` (`filter_plugins/CLAUDE.md`). `tests/vt.py` is a terminal that replays what a live run
wrote, for the tests that run one on a pseudo-terminal.

## What the policy tests hold

`tests/policy` checks the repo's own rules: the lock matches `pyproject.toml` and hashes every package
(`test_lock.py`), no vault is tracked, its path is ignored and its example lists every secret the group_vars read
(`test_vault.py`), the ruleset body requires the workflow's gate job and each lint and test type runs in some CI job
(`test_github.py`), and `local.yml`'s settings, its example and `just init`'s questions agree
(`test_make_init_settings.py`). The roles keep their conventions (`test_roles.py`):

- each role default matches its argument-spec entry;
- each module's documentation matches its argument spec and documents its check-mode, diff-mode and platform support
  (`attributes`), and check mode as its AnsibleModule declares it;
- the tracked configuration passes every role's argument specs and input checks, in the four setups the two switches
  make, with `local.yml.example` and with the `local.yml` of a setup without a NAS, while both playbooks refuse NAS
  settings that come without an address, and each refusal's message is checked;
- tasks pass only options the custom modules accept, and a task that calls a TrueNAS or OPNsense module carries
  `# noqa: args`;
- every option whose name looks like a secret (ansible-test's `no-log-needed` pattern) is `no_log`, bar a short list of
  named exceptions.

`test_claude_md.py` keeps the `CLAUDE.md` files and `.claude/rules/` small, scoped and free of imports, and gives each
role a rule. The manifests keep theirs where kube-linter cannot check them (`test_k8s.py`, described in its docstring and in
`k8s/CLAUDE.md`).
