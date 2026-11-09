"""The router's backups by name, for roles/opnsense. Pure: they run on the controller, read no file, and are
unit-tested in tests/unit/test_opnsense_filters.py."""

import re

# A backup is named after its revision's history id on the router: config-<microtime>.xml, with _<nanoseconds> when
# two saves shared a microsecond (OPNsense/Core/Config.php's backup()). library/opnsense_backup.py orders them the same.
BACKUP_ID = re.compile(r"^config-(\d+)(?:\.(\d+))?(?:_(\d+))?\.xml$")


def _order(name):
    match = BACKUP_ID.match(name)
    if not match:
        return None
    seconds, fraction, nanoseconds = match.groups()
    return (float(f"{seconds}.{fraction or 0}"), int(nanoseconds or -1))


def backups(paths):
    """The paths of the backups among `paths`, oldest first; anything else left out."""
    found = [str(p) for p in paths or [] if _order(str(p).rsplit("/", 1)[-1]) is not None]
    return sorted(found, key=lambda p: _order(p.rsplit("/", 1)[-1]))


def backup_pick(paths, which):
    """The path of the backup `which` names: latest, the newest, or a backup's own name. Empty when there is none."""
    ordered = backups(paths)
    if which == "latest":
        return ordered[-1] if ordered else ""
    return next((p for p in ordered if p.rsplit("/", 1)[-1] == which), "")


def is_backup_name(name):
    """Whether `name` is how a backup is named."""
    return _order(str(name or "")) is not None


class FilterModule:
    def filters(self):
        return {
            "opnsense_backups": backups,
            "opnsense_backup_pick": backup_pick,
            "opnsense_is_backup_name": is_backup_name,
        }
