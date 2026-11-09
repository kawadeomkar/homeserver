# roles/

The Ansible roles the playbooks at the repo root run. Claude Code loads this file when it works on a file under
`roles/`.

**Read a role's `README.md` before changing the role.** It holds the role's design: what it refuses and why, what it
never does, what was seen on the real machines, where it runs and in what order, the group_vars that feed it here,
and the tests and fakes that cover it. A change to a role updates its README in the same commit. `REFERENCE.md` is
docsible's page of the role as it is (below): never edit it by hand.

## The roles are generic; this setup is configuration

Role defaults change nothing (empty lists and dicts, null for "leave alone"), and task files hold no configuration
values, only variables, API namespaces and CLI verbs. Everything that describes these machines lives in tracked
group_vars (the table in the root `CLAUDE.md`), apart from addresses and serials, which live in the git-ignored
`group_vars/all/local.yml`. A new setting goes in one of those files, with a neutral default in the role and a line in
the role's `meta/argument_specs.yml` and `README.md`, never as a literal in a task; then `just fix docs` brings the
role's `REFERENCE.md` up to date. A setting `local.yml` gives also goes in `local.yml.example` and in the list `just
init` asks from (`scripts/init/settings.py`), and a secret in the vault under a `vault_` prefix, exposed by indirection
from a plain variable.

## Conventions

- Roles live in `./roles` beside the playbooks at the repo root, variables prefixed with the role name (ansible-lint
  enforces it). `ansible.cfg` deliberately sets no `roles_path`: roles beside a playbook are found by default, while
  `ansible-galaxy install` then defaults to `~/.ansible/roles`, which keeps third-party code out of this public repo.
  No role depends on a Galaxy collection.
- Task names in imported task files carry a `<file> | ` prefix (ansible-lint's `name[prefix]`).
- Every mutating step must be skipped in check mode and report what it would do, and a second run must report no
  changes. `tests/integration` proves both against stateful fakes (`tests/CLAUDE.md`), so a new API method or command
  needs a matching fake, or the test fails on purpose. A command goes through `command` rather than a module when a
  fake must stand in for it.
- What a role reads from a tool's output, and what it decides from it, is a filter plugin of its own
  (`roles/<role>/filter_plugins/`, pure Python), unit-tested in `tests/unit/`, so the tasks only read the machine and
  run what the filter returns.
- A custom module (`roles/<role>/library/`) returns only the fields it manages, makes every option whose name looks
  like a secret `no_log` (`tests/policy/test_roles.py` checks, bar a short list of named exceptions), and documents its
  check-mode, diff-mode and platform support (`attributes`). `just lint docs` runs `ansible-doc` on every custom
  module, since ansible-lint does not check `DOCUMENTATION`; the options every TrueNAS module shares are documented
  once, in the doc fragment `roles/truenas/doc_fragments/truenas_api.py`, which `ansible.cfg` points `ansible-doc` at.
- A task that calls a TrueNAS or OPNsense module carries `# noqa: args`: ansible-lint's args rule cannot import a
  role's `module_utils` (only a collection's, ansible-lint#3891), and without the comment it stopped at the first such
  task with a warning and checked no task after it in the file; `test_roles.py` checks those tasks' options and the
  comment, so ansible-lint prints no warning.
- **A report is plain text**, a string or a list of lines, its first line the gist, never a structure. **A report
  that leaves something to do records it** straight after itself with `set_stats`, under `homeserver_next`, as
  `{subject: {matter: {text, command}}}`. The record comes from the facts the message is built from, never from its
  text, and an empty one says nothing is left. Subjects and words are `just status`'s (`reboot`, `restart`,
  `tailscale`, `talos`, `etcd`, `backup`, `home_assistant`), and commands come from `status_commands`
  (`group_vars/all/status.yml`) where it has one, so no role names a recipe itself. NEXT STEPS prints them
  (`filter_plugins/CLAUDE.md`).
- Credentials go only to the machine they belong to, and not every role checks that itself. The NAS's and the
  router's clients compare the certificate pin before they send a key; `roles/talos` refuses a talosconfig or
  kubeconfig whose endpoint or CA is another cluster's; `roles/app_scale` compares the kubeconfig's server alone;
  `roles/status` leaves another server's kubeconfig unread. `roles/flux_bootstrap` has no check of its own: it is
  safe only after `roles/talos` in the same play, as in `kubernetes.yml`, and run anywhere else it would apply the
  age key to whatever cluster its kubeconfig names. A role that reaches a cluster or a machine checks its
  credentials first, so a test host's run never touches the home machines.

## Role references

`roles/<role>/REFERENCE.md` is docsible's page, written whole by `just fix docs` from the role as it is:
`meta/main.yml`, `meta/argument_specs.yml`, the defaults and vars as tables linked to their lines, and each tasks file
as a table and a Mermaid graph. Never edit one by hand, and never give docsible a template or its comment
annotations: a page says what the role says. A role's `README.md` stays its own, written by hand, and links to its
page; `.gitattributes` marks the pages generated, so a pull request's diff folds them.

A commit that changes a file under `roles/` runs `just fix docs` in the pre-commit hook `role-references`, which writes
the pages from the staged roles and, as pre-commit does with any hook that changes a file, fails the commit until the
pages it wrote are added too. `just lint docs` (CI's `docs` job) generates every page again from a copy of `roles/` in
`.cache/docsible` and fails on a difference, a role without a page, and any docsible output but its "Documentation
generated at" line: docsible reports a file it cannot read (in 0.8.0, one with an `!unsafe` tag), leaves it out and
exits 0. The justfile says why each flag is set. Links are relative, without `--repository-url`: `detect` would write
the current branch into every link, and for a role outside a collection docsible leaves `roles/<role>/` out of the
link (docsible#95).

docsible 0.8.0's limits, left as they are: a block's `always` tasks are in neither the table nor the graph, the table
shows no tags for a task inside a block (it reads them from top-level tasks only, so `roles/truenas`'s stage tags, the
ones `just deploy truenas --tags …` selects, read blank), table rows end in spaces, and `--comments` and `--task-line`
stay off (the first splits a table row at a multi-line comment, the second links a task whose name holds a quote to
`#L`).
