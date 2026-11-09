# filter_plugins/ and callback_plugins/

The engine that draws every run, report and live output. Claude Code loads this file when it works on a file in
either directory (`callback_plugins/CLAUDE.md` imports it).

## The palette and the region

**Every report and live output draws through one palette and one region.** `filter_plugins/screen.py` builds lines as
`(text, style)` segments in one palette (`Line`, `cut`, `render`, `fill`, `bar`, `spark`, `and_list`, and
`attention_items`, the list of what needs attention that `just status` and NEXT STEPS both print).
`filter_plugins/live_region.py` puts a frame at the bottom of a terminal that has a size (never pre-commit's hooks'
pseudo-terminal, which reads 0 by 0), as follows:
- one write per redraw, in synchronized output, the cursor moved up exactly the lines drawn, so no line may wrap;
- Display's `display` wrapped, so everything else prints above;
- the region steps aside for a prompt or a timed `pause`;
- the echo is off while it draws, when stdin is a terminal and the run is in its foreground (from the background,
  changing it would stop the run);
- a resize redraws from the top;
- Ctrl-C is held off between a write and its count of the lines drawn.
- a frame that cannot be drawn ends the region, never the run: it is erased and its owner told.

`filter_plugins/text.py` makes what a machine printed safe to print, for NEXT STEPS' records and every live run's
words: no escape sequence, no control character, and one line when asked. The three modules are ported from
claude-on-proxmox's, which proved them, and the projects stay independent, so a fix there is compared by hand. A
module loads another by path, once, as `homeserver_<stem>` (`_sibling`): a filter plugin is no module another can
import. Each has a `FilterModule` with no filters, so the playbooks' filter loader takes it. A scratch tree copies
`filter_plugins/` (`tests/policy/helpers.py`'s `example_tree`). `tests/unit/test_screen.py` and
`tests/unit/test_live_region.py` cover them, the second against a stand-in Display held to ansible-core's.

## What a run prints, and NEXT STEPS

`ansible.cfg` names the stdout callback `callback_plugins/homeserver.py`, with `callback_plugins = callback_plugins` so
that a playbook in any directory finds it, since Ansible stops a run whose stdout callback it cannot load.

**At verbosity 0 every run is drawn**, whoever starts it (`filter_plugins/live_ansible.py`, the engine): live at a
terminal that can draw (the region, above), a line per step otherwise; a run of several playbooks (`ansible-playbook
a.yml b.yml`, a recap after each) is the stream, NEXT STEPS after each recap. The default callback still renders every
task, sent to the run's log alone, so the log keeps every line it held; the run's own words (reports, failures,
warnings, a dry run's diffs; a real run's diffs go to the log alone) go to the screen in colour and the log plain, and
a summary replaces PLAY RECAP on the screen, NEXT STEPS in it. Any `-v`, or `ANSIBLE_STDOUT_CALLBACK=default`, is the
stream, with NEXT STEPS after the recap.

**Each hook calls the default's own first, then the engine inside a try**: on any error the engine lets go (the region
erased, one line on stderr) and the rest prints classically, since Ansible only warns when a callback raises and the
event would be lost. It carries the default's documentation fragments: without them every hook fails, Ansible only
warns, and every result disappears from the screen and the log in a run that exits 0. `tests/unit/test_callback.py`
compares the two callbacks' output and log at `-v` line by line.

**What a run is made of is its declaration** (`filter_plugins/live_recipes.py`, pure data): deploy's stages are its
playbooks, each with steps named as a person names them (packages, not `proxmox_apt`), matched by role, task file or
play, a play's own tasks (its refusals) going to their neighbour's step, and each stage's `absent` task ("Say why … is
skipped") marking a stage this setup lacks; a playbook no declaration names is drawn as a stage per play, its roles as
steps. suspend's and resume's stages are their plays (the apps, Home Assistant, the cluster's VMs, which a stage's
`when` draws only with `--all`), the apps' waves each a step of its own (a step's `each`: a looped include's items as
steps, by their loop labels, renamed by `names`), their report whole in the summary (a stage's `reports`), and the
recipe's `options` put `--only` and `--all` in the title. talos-upgrade's one stage, through one `include_role`, is
its plan, the etcd snapshot, a step per node (`each`) and Kubernetes (a step's `tasks`, by name: they share the plan's
file); etcd-snapshot's says where its snapshot went. An include whose file is literal counts each of its tasks in the
step it will be in, at the start. A play that ends its host (`meta: end_host`) before its work leaves its stage "not
in this run", or says why with the report its play printed last; a stage whose play comes later waits for it, its time
its own tasks'; the label column widens to the longest label (up to 24).

**`just lint`, `just fix` and `just test` are drawn too**, at a terminal and never in CI or a git hook (the justfile's
`_each`; pre-commit runs a hook on a terminal that reads 0 by 0, where nothing is drawn): `scripts/live.py` runs each
type's `just <recipe>-<type>` in turn, a step each with no bar and its last line dim after it
(`filter_plugins/live_commands.py`, a declaration in `live_recipes.py`'s `COMMANDS`), every line to a log in `logs/`,
a failed type's last 40 lines above, then a CHECKS summary and _each's exit codes. A test type's step counts its tests
instead, several running at once under xdist, each failure said above by its test's name and the summary naming them:
`tests/conftest.py` writes each test's start and end as JSON lines to the file `HOMESERVER_LIVE_PYTEST` names, from
pytest's controller alone, and takes the variable out of the environment, so a pytest a test starts never writes among
them; a command runs in a session of its own (a prompt at /dev/tty fails), Ctrl-C is sent on to it, and a second, or
SIGTERM or SIGHUP, ends its whole tree (`filter_plugins/processes.py`, ported from claude-on-proxmox's).

The model (`filter_plugins/run_view.py`) counts every task the plays compile to at the start (an include whose file is
literal too), so each bar knows its end and never moves back; a refusal (a failed assert) prints its message alone,
never the assertion; the exit line follows ansible-playbook's code, 4 for a host not reached before 2 for a failure. A
drawn run needs a log: `scripts/run_log.py` names every log, `_playbook`'s and, 0600 in a 0700 `logs/`, a hand run's.
The engine names no command; what a declaration cannot say becomes a field there and code in the engine.
`tests/unit/test_run_view.py` draws every moment from events at 100 and 80 columns, `test_live_recipes.py` holds the
declarations to the tree, and `tests/integration/test_deploy_live.py` runs the fake Proxmox host's play on a
pseudo-terminal and reads it back through `tests/vt.py`.

**A report is plain text**, a string or a list of lines, its first line the gist, never a structure. **A report that
leaves something to do records it** straight after itself with `set_stats`, under `homeserver_next`, as `{subject:
{matter: {text, command}}}`. The record comes from the facts the message is built from, never from its text, and an
empty one says nothing is left. Subjects and words are `just status`'s (`reboot`, `restart`, `tailscale`, `talos`,
`etcd`, `backup`, `home_assistant`), and commands come from `status_commands` where it has one. The idempotence tests
read the records from the default callback's custom stats (`next_steps` in `test_idempotence.py`). `tests/conftest.py`
sets the default stdout callback for every `ansible-playbook` a test starts, so a scratch tree holding a copy of
`ansible.cfg` alone runs, and `just init`'s settings check pins it too.
