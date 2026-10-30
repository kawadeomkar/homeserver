"""just lint's, just fix's and just test's types drawn live: each type's command a step of one run, at a terminal.

    scripts/live.py --just <just> --justfile <justfile> [--title <words>] [--extra <arguments>] <recipe> <type>...

The justfile's _each runs it, in place of its own loop, when a run of lint, fix or test can be drawn: stdout a terminal
that is not dumb, and no CI, whose logs keep every line. It runs `just <recipe>-<type>` for each type, in order, and
exits as _each does: 0, 1 when a type failed, 130 after Ctrl-C. filter_plugins/live_commands.py draws them, each type
a step of the recipe's declaration (filter_plugins/live_recipes.py, COMMANDS), a test run's tests counted in its step
from what tests/conftest.py writes, and logs every line they print to logs/, named as scripts/run_log.py names every
run's log, since the region keeps them off the screen. extra is test's --pytest-args, which only test-idempotence
takes.

Should it not draw after all (no terminal, or what it draws with will not load), it runs the types as _each does, each
printing as it runs.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _arguments(argv):
    parser = argparse.ArgumentParser(prog="scripts/live.py", description=__doc__.splitlines()[0])
    parser.add_argument("--just", required=True, help="the just that runs the recipes")
    parser.add_argument("--justfile", required=True)
    parser.add_argument("--title", default="", help="the run as it was asked for: lint python docs")
    parser.add_argument("--extra", default="", help="test's --pytest-args, for test-idempotence alone")
    parser.add_argument("recipe")
    parser.add_argument("types", nargs="+")
    return parser.parse_args(argv)


def commands(args):
    """Each type and the command that runs it, as _each runs it."""
    out = []
    for kind in args.types:
        argv = [args.just, "--justfile", args.justfile, f"{args.recipe}-{kind}"]
        if f"{args.recipe}-{kind}" == "test-idempotence" and args.extra:
            argv.append(args.extra)
        out.append((kind, argv))
    return out


def passed_through(args, reason=""):
    """The types as _each runs them, each printing as it runs: the failures named at the end."""
    if reason:
        print(f"[homeserver] the run is not drawn ({reason})", file=sys.stderr)
    failed = []
    for kind, argv in commands(args):
        try:
            rc = subprocess.call(argv)
        except KeyboardInterrupt:
            return 130
        if rc >= 128:
            return rc
        if rc:
            failed.append(kind)
    if failed:
        print(f"just {args.recipe} failed: {' '.join(failed)}", file=sys.stderr)
        return 1
    return 0


def _module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    sys.modules[name] = module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv=None):
    args = _arguments(sys.argv[1:] if argv is None else argv)
    if not sys.stdout.isatty() or os.environ.get("TERM", "dumb") == "dumb":
        return passed_through(args)
    try:
        engine = _module("homeserver_live_commands", ROOT / "filter_plugins" / "live_commands.py")
        if not engine.live.can_draw():
            return passed_through(args)
        recipe = engine.view.recipes.commands_of(args.recipe, args.types)
        if recipe is None:
            return passed_through(args, f"no declaration for {args.recipe}")
        run_log = _module("homeserver_run_log", ROOT / "scripts" / "run_log.py")
        log = run_log.create(ROOT, args.recipe, args.recipe)
        engine.engine.start_log(log)
    except Exception as error:  # noqa: BLE001 - a run that cannot be drawn runs as it did before
        return passed_through(args, f"{type(error).__name__}: {error}")
    shown = os.path.relpath(log, Path.cwd())
    return engine.run(args.title or args.recipe, recipe, commands(args), shown if not shown.startswith("..") else log)


if __name__ == "__main__":
    sys.exit(main())
