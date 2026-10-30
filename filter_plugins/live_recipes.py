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

    plays: the plays every task of which is this step's, each "file:play name". roles: the roles every task of which
    is this step's. files: the task files (a path's end, truenas/tasks/pools.yml) every task of which is this step's,
    a file another imports staying its importer's. A task that matches no step, such as a play's own refusals, is its
    neighbour's: the next task's step in its play, else the one before."""

    def __init__(self, label, plays=(), roles=(), files=()):
        self.label = label
        self.plays = tuple(plays)
        self.roles = tuple(roles)
        self.files = tuple(files)


class Stage:
    """A stage of a run: the playbooks (a file name) or plays ("file:play name") that are its, and its steps, in
    order. absent: the task, by name, that says why this setup has no such stage (the NAS's, without a NAS address):
    once it runs, the stage is drawn as absent, with what the task said, whatever the stage's other plays read first."""

    def __init__(self, label, plays=(), steps=(), absent=""):
        self.label = label
        self.plays = tuple(plays)
        self.steps = tuple(steps)
        self.absent = absent


class Recipe:
    """A command's run: its name (the title's first word: deploy, or check under --check, as check_name says), the
    playbooks it covers, its stages in order, and how many of the running step's last tasks are shown."""

    def __init__(self, name, playbooks=(), stages=(), check_name="", window=5):
        self.name = name
        self.playbooks = tuple(playbooks)
        self.stages = tuple(stages)
        self.check_name = check_name or name
        self.window = window


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
)


def recipe_for(playbook):
    """The declaration that covers a playbook (a file name), or None."""
    return next((recipe for recipe in RECIPES if playbook in recipe.playbooks), None)


class FilterModule:
    """No filters: loaded by path. Here so the filter loader takes the file as a plugin."""

    def filters(self):
        return {}
