"""Every declaration a live run is drawn from (filter_plugins/live_recipes.py) names what the tree has: its playbooks,
the plays and roles its stages and steps are matched by, its task files, and the task that says a stage is absent."""

import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("test_live_recipes_module", ROOT / "filter_plugins" / "live_recipes.py")
recipes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(recipes)


def plays_of(playbook):
    """Each play of a playbook, through its import_playbook entries: (file, play)."""
    out = []
    for play in yaml.safe_load((ROOT / playbook).read_text()) or []:
        imported = play.get("ansible.builtin.import_playbook") or play.get("import_playbook")
        if imported:
            out += plays_of(imported)
        else:
            out.append((playbook, play))
    return out


def task_names(items):
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if "name" in item:
            yield item["name"]
        for key in ("block", "rescue", "always", "pre_tasks", "tasks", "post_tasks"):
            yield from task_names(item.get(key))


def test_each_declaration_names_playbooks_plays_roles_files_and_tasks_the_tree_has():
    assert recipes.RECIPES, "no declaration"
    for recipe in recipes.RECIPES:
        for playbook in recipe.playbooks:
            assert (ROOT / playbook).is_file(), f"{recipe.name}: no playbook {playbook}"
            assert recipes.recipe_for(playbook) is recipe
        plays = [play for playbook in recipe.playbooks for play in plays_of(playbook)]
        named = {f"{file}:{play['name']}" for file, play in plays}
        for stage in recipe.stages:
            for given in stage.plays:
                file = given.split(":", 1)[0]
                assert file in recipe.playbooks, f"{stage.label}: {file} is not among {recipe.name}'s playbooks"
                assert ":" not in given or given in named, f"{stage.label}: no play {given}"
            stage_plays = [play for file, play in plays if file in {p.split(":", 1)[0] for p in stage.plays}]
            if stage.absent:
                found = {name for play in stage_plays for name in task_names([play])}
                assert stage.absent in found, f"{stage.label}: no task {stage.absent!r} in its plays"
            assert stage.steps, f"{stage.label} has no steps"
            for step in stage.steps:
                assert step.plays or step.roles or step.files, f"{stage.label} · {step.label} matches nothing"
                for given in step.plays:
                    assert given in named, f"{stage.label} · {step.label}: no play {given}"
                for role in step.roles:
                    assert (ROOT / "roles" / role / "tasks" / "main.yml").is_file(), f"no role {role}"
                for file in step.files:
                    assert (ROOT / "roles" / file).is_file(), f"{stage.label} · {step.label}: no file roles/{file}"


def test_every_stage_of_deploy_is_a_playbook_site_imports_in_its_order():
    deploy = recipes.recipe_for("site.yml")
    imported = [entry["ansible.builtin.import_playbook"] for entry in yaml.safe_load((ROOT / "site.yml").read_text())]
    assert [stage.plays[0] for stage in deploy.stages] == imported


def test_the_nas_steps_are_the_files_the_truenas_role_imports_in_its_order():
    deploy = recipes.recipe_for("truenas.yml")
    main = yaml.safe_load((ROOT / "roles" / "truenas" / "tasks" / "main.yml").read_text())

    def imports(items):
        for item in items or []:
            target = item.get("ansible.builtin.import_tasks")
            if target:
                yield f"truenas/tasks/{target}"
            yield from imports(item.get("block"))

    declared = [file for step in deploy.stages[0].steps for file in step.files]
    assert declared == list(imports(main))
