"""Every playbook drawn live, declared: what each run is made of, and nothing about how it is drawn.

A declaration names the playbooks it covers, and says what their run is made of: its stages, each stage's steps,
and how a play or a task is matched to them. filter_plugins/run_view.py and filter_plugins/live_ansible.py draw any
run from a declaration alone, and a playbook no declaration names is drawn all the same: a stage per play, its roles
as steps (run_view.generic). A command extends the engine only through a declaration: what a declaration cannot say
becomes a new field here and new code in the engine, never code of one command's own.

Pure data: nothing here reads, writes or imports anything of the project's. Loaded by path as homeserver_live_recipes,
as every module here loads another; it has no filters of its own. tests/unit/test_live_recipes.py holds every
playbook, play, role, task file and task a declaration names to the tree.
"""

from __future__ import annotations


class Step:
    """A step of a stage, labelled as a person would name it (packages, not proxmox_apt), and what puts a task in it.

    plays: the plays every task of which is this step's, each "file:play name". tasks: the tasks, by name as drawn
    (without their file's "upgrade | "), that are this step's whatever their file, for a file whose tasks are in more
    than one step. roles: the roles every task of which is this step's. files: the task files (a path's end,
    truenas/tasks/pools.yml) every task of which is this step's, a file another imports staying its importer's, and an
    include's tasks its file's. A task that matches no step, such as a play's own refusals, is its neighbour's: the
    next task's step in its play, else the one before.

    each: the step's tasks come from an include with a loop, and each item is a step of its own, labelled with the
    item's label (loop_control's), or with what names gives for it; until the include runs, this step stands for
    them."""

    def __init__(self, label, plays=(), roles=(), files=(), each=False, names=None, tasks=()):
        self.label = label
        self.plays = tuple(plays)
        self.tasks = tuple(tasks)
        self.roles = tuple(roles)
        self.files = tuple(files)
        self.each = each
        self.names = dict(names or {})


class Stage:
    """A stage of a run: the playbooks (a file name) or plays ("file:play name") that are its, and its steps, in
    order. absent: the task, by name, that says why this setup has no such stage (the NAS's, without a NAS address):
    once it runs, the stage is drawn as absent, with what the task said, whatever the stage's other plays read first.
    reports: the tasks, by name, whose reports the summary shows whole, every line, rather than their first. when: one
    of the recipe's options, by its variable: without it, the stage is not drawn at all, and its plays' tasks are the
    log's alone."""

    def __init__(self, label, plays=(), steps=(), absent="", reports=(), when=""):
        self.label = label
        self.plays = tuple(plays)
        self.steps = tuple(steps)
        self.absent = absent
        self.reports = tuple(reports)
        self.when = when


class Recipe:
    """A command's run: its name (the title's first word: deploy, or check under --check, as check_name says; without
    one, the name followed by --check), the playbooks it covers, the first of which the command runs when it names
    none, its stages in order, and how many of the running step's last tasks are shown. options: the command's own
    options, as (variable, option): the variables just passes for them with -e, so the title says the command as it
    was typed (--only jellyfin for {"only": "jellyfin"}, --all for {"cluster_vms": true}).

    commands: a run of commands, not a playbook (scripts/live.py): one stage of the types it was asked for, each a step
    whose one task is its command, drawn without bars; unit and units name a step on the screen (check, checks)."""

    def __init__(
        self, name, playbooks=(), stages=(), check_name="", window=5, options=(), commands=False, unit="step", units=""
    ):
        self.name = name
        self.playbooks = tuple(playbooks)
        self.stages = tuple(stages)
        self.check_name = check_name or name
        self.window = window
        self.options = tuple(options)
        self.commands = commands
        self.unit = unit
        self.units = units or f"{unit}s"


# The NAS's stages, in the order roles/truenas/tasks/main.yml imports them, each named as just check's --tags name it.
NAS_STEPS = (
    ("network", "network"),
    ("pools", "pools"),
    ("system", "system"),
    ("datasets", "datasets"),
    ("snapshots", "snapshots"),
    ("NFS", "nfs"),
    ("alerts", "alerts"),
    ("services", "services"),
)

# The play every cluster playbook but kubernetes.yml begins with, on localhost.
REFUSE = "Refuse a cluster without the kubernetes inventory group"
# just suspend's and resume's options, as just's _scale passes them.
SCALE_OPTIONS = (("only", "--only"), ("cluster_vms", "--all"))
# The apps' waves (roles/app_scale's loop labels): every app, then the late ones, which are the databases here.
WAVES = {"late apps": "databases"}


def scale_apps(playbook, play):
    """The apps' stage of suspend and resume: the refusal of an inventory without the cluster's group and the role's
    reading and plan, then each wave of apps, as roles/app_scale's include loops over them; the report names each app
    and its replicas, whole."""
    return Stage(
        "apps",
        plays=(f"{playbook}:{REFUSE}", f"{playbook}:{play}"),
        absent="Say why the cluster is skipped",
        reports=("Say what was done",),
        steps=(
            Step(
                "plan",
                plays=(f"{playbook}:{REFUSE}",),
                files=("app_scale/tasks/main.yml",),
            ),
            Step("apps", files=("app_scale/tasks/wave.yml",), each=True, names=WAVES),
        ),
    )


RECIPES = (
    Recipe(
        name="deploy",
        check_name="check",
        playbooks=("site.yml", "truenas.yml", "proxmox.yml", "homeassistant.yml", "kubernetes.yml"),
        stages=(
            Stage(
                "NAS",
                plays=("truenas.yml",),
                absent="Say why the NAS is skipped",
                steps=(
                    Step("connect", files=("truenas/tasks/validate.yml", "truenas/tasks/connect.yml")),
                    *(Step(label, files=(f"truenas/tasks/{name}.yml",)) for label, name in NAS_STEPS),
                ),
            ),
            Stage(
                "Proxmox host",
                plays=("proxmox.yml",),
                steps=(
                    Step("packages", roles=("proxmox_apt",)),
                    Step("Tailscale", roles=("proxmox_tailscale",)),
                    Step("storage", roles=("proxmox_storage",)),
                    Step("backups", roles=("proxmox_backup",)),
                    Step("node exporter", roles=("proxmox_node_exporter",)),
                ),
            ),
            Stage(
                "Home Assistant",
                plays=("homeassistant.yml",),
                absent="Say why Home Assistant is skipped",
                steps=(Step("VM", roles=("proxmox_vm",)), Step("owner", roles=("haos_onboard",))),
            ),
            Stage(
                "cluster",
                plays=("kubernetes.yml",),
                absent="Say why the cluster is skipped",
                steps=(
                    Step("VMs", plays=("kubernetes.yml:Create the cluster's VMs",)),
                    Step("Talos", roles=("talos",)),
                    Step("Flux", roles=("flux_bootstrap",)),
                ),
            ),
        ),
    ),
    Recipe(
        name="suspend",
        playbooks=("suspend.yml",),
        options=SCALE_OPTIONS,
        stages=(
            scale_apps("suspend.yml", "Spin the apps down"),
            Stage(
                "Home Assistant",
                plays=("suspend.yml:Shut Home Assistant's VM down",),
                absent="Say why Home Assistant is skipped",
                steps=(Step("shut down", roles=("proxmox_vm",)),),
            ),
            Stage(
                "cluster VMs",
                plays=("suspend.yml:Shut the cluster's VMs down",),
                when="cluster_vms",
                absent="Say why the cluster's VMs are skipped",
                steps=(Step("shut down", roles=("proxmox_vm",)),),
            ),
        ),
    ),
    Recipe(
        name="resume",
        playbooks=("resume.yml",),
        options=SCALE_OPTIONS,
        stages=(
            Stage(
                "cluster VMs",
                plays=("resume.yml:Start the cluster's VMs",),
                when="cluster_vms",
                steps=(Step("start", roles=("proxmox_vm",)),),
            ),
            scale_apps("resume.yml", "Bring the apps back up"),
            Stage(
                "Home Assistant",
                plays=("resume.yml:Start Home Assistant's VM",),
                absent="Say why Home Assistant is skipped",
                steps=(Step("start", roles=("proxmox_vm",)),),
            ),
        ),
    ),
    # roles/talos's upgrade entry point, through one include_role: its plan, the snapshot, each node in turn, then
    # Kubernetes and the cluster's health. The plan and where the snapshot went whole in the summary.
    Recipe(
        name="talos-upgrade",
        playbooks=("talos-upgrade.yml",),
        stages=(
            Stage(
                "cluster",
                plays=("talos-upgrade.yml",),
                absent="Say why the cluster is skipped",
                reports=("Show the plan", "Say where the snapshot is"),
                steps=(
                    Step(
                        "plan",
                        plays=(f"talos-upgrade.yml:{REFUSE}",),
                        files=("talos/tasks/validate.yml", "talos/tasks/prepare.yml", "talos/tasks/upgrade.yml"),
                    ),
                    Step("etcd snapshot", files=("talos/tasks/snapshot.yml",)),
                    Step("nodes", files=("talos/tasks/upgrade-node.yml",), each=True),
                    Step("Kubernetes", tasks=("Upgrade Kubernetes", "Wait for the cluster to be healthy")),
                ),
            ),
        ),
    ),
    Recipe(
        name="etcd-snapshot",
        playbooks=("etcd-snapshot.yml",),
        stages=(
            Stage(
                "cluster",
                plays=("etcd-snapshot.yml",),
                absent="Say why the cluster is skipped",
                reports=("Say where the snapshot is",),
                steps=(
                    Step(
                        "prepare",
                        plays=(f"etcd-snapshot.yml:{REFUSE}",),
                        files=("talos/tasks/validate.yml", "talos/tasks/prepare.yml", "talos/tasks/etcd-snapshot.yml"),
                    ),
                    Step("snapshot", files=("talos/tasks/snapshot.yml",)),
                ),
            ),
        ),
    ),
)


# The recipes that run commands, a type at a time, each type a step: the stage is made of the types a run asks for.
COMMANDS = (
    Recipe(name="lint", commands=True, unit="check"),
    Recipe(name="fix", commands=True, unit="fix", units="fixes"),
)


def recipe_for(playbook):
    """The declaration that covers a playbook (a file name), or None."""
    return next((recipe for recipe in RECIPES if playbook in recipe.playbooks), None)


def commands_of(name, types):
    """The declaration of a run of commands, its stage the types asked for, in their order; None for no such recipe."""
    found = next((recipe for recipe in COMMANDS if recipe.name == name), None)
    if found is None:
        return None
    steps = tuple(Step(kind, tasks=(kind,)) for kind in types)
    stage = Stage(name, plays=(name,), steps=steps)
    return Recipe(name, stages=(stage,), window=1, commands=True, unit=found.unit, units=found.units)


class FilterModule:
    """No filters: loaded by path. Here so the filter loader takes the file as a plugin."""

    def filters(self):
        return {}
