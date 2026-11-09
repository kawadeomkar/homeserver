"""Unit tests for roles/opnsense's filters: the backups ordered by their names, and the one a restore picks."""

import importlib.util
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[2] / "roles" / "opnsense" / "filter_plugins" / "opnsense.py"
spec = importlib.util.spec_from_file_location("opnsense_filters", PLUGIN)
filters = importlib.util.module_from_spec(spec)
spec.loader.exec_module(filters)

BACKUPS = [
    "/b/config-1762600000.5.xml",
    "/b/config-1762600000.xml",
    "/b/config-1762500000.9999.xml",
    "/b/config-1762600000.5_123.xml",
    "/b/config-1762600000.12.xml",
    "/b/.tmp-abc",
    "/b/notes.txt",
]


def test_backups_are_ordered_by_their_revisions_time_not_their_names():
    assert filters.backups(BACKUPS) == [
        "/b/config-1762500000.9999.xml",
        "/b/config-1762600000.xml",
        "/b/config-1762600000.12.xml",
        "/b/config-1762600000.5.xml",
        "/b/config-1762600000.5_123.xml",
    ]


@pytest.mark.parametrize(
    ("which", "picked"),
    [
        ("latest", "/b/config-1762600000.5_123.xml"),
        ("config-1762600000.12.xml", "/b/config-1762600000.12.xml"),
        ("config-1.xml", ""),
        ("notes.txt", ""),
    ],
)
def test_a_restore_picks_the_newest_or_the_one_named(which, picked):
    assert filters.backup_pick(BACKUPS, which) == picked


def test_latest_of_nothing_is_nothing():
    assert filters.backup_pick([], "latest") == ""


@pytest.mark.parametrize(
    ("name", "valid"),
    [("config-1762600000.12.xml", True), ("config-1762600000.xml", True), ("../config-1.xml", False), ("x", False)],
)
def test_a_backups_name(name, valid):
    assert filters.is_backup_name(name) is valid
