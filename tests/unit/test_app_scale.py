"""roles/app_scale's filters: which Deployments are the apps, and what just suspend and just resume do to them.

The idempotence tests run the role against a fake cluster; these cover the reading and the plan on their own,
including forms the fake never produces (kubectl's JSON as text, an unset replica count, a malformed record).
"""

import importlib.util
import json
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[2] / "roles" / "app_scale" / "filter_plugins" / "app_scale.py"
_spec = importlib.util.spec_from_file_location("app_scale_filters", PLUGIN)
fs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fs)

ANNOTATION = "example.org/replicas"
LATE = "app.kubernetes.io/component=database"


def kustomization(name, path, namespace="flux-system"):
    return {"metadata": {"name": name, "namespace": namespace}, "spec": {"path": path}}


def deployment(namespace, name, owner, replicas=1, labels=None, annotations=None, owner_namespace="flux-system"):
    spec = {"selector": {"matchLabels": {"app.kubernetes.io/name": name, "tier": "app"}}}
    if replicas is not None:
        spec["replicas"] = replicas
    return {
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {
                "kustomize.toolkit.fluxcd.io/name": owner,
                "kustomize.toolkit.fluxcd.io/namespace": owner_namespace,
                **(labels or {}),
            },
            "annotations": annotations or {},
        },
        "spec": spec,
    }


KUSTOMIZATIONS = {
    "items": [
        kustomization("apps-media", "./k8s/apps/media"),
        kustomization("apps-nextcloud", "k8s/apps/nextcloud/"),
        kustomization("infra-controllers", "./k8s/infrastructure/controllers/nas"),
        kustomization("apps-elsewhere", "./k8s/apps-old/x"),
    ]
}
HELD = {fs.RECONCILE: "disabled", ANNOTATION: "2"}


def found(*deployments):
    return fs.apps(json.dumps({"items": list(deployments)}), KUSTOMIZATIONS, "k8s/apps", LATE, ANNOTATION)


def test_apps_are_what_kustomizations_under_the_path_apply():
    apps = found(
        deployment("media", "sonarr", "apps-media"),
        deployment("nextcloud", "nextcloud-db", "apps-nextcloud", labels={"app.kubernetes.io/component": "database"}),
        deployment("media", "jellyfin", "apps-media", replicas=None, annotations=HELD),
        # Not apps: the platform's, one under a path that only starts alike, one another namespace's Kustomization
        # of the same name applied, and one no Flux Kustomization applied.
        deployment("local-path-storage", "local-path-provisioner", "infra-controllers"),
        deployment("old", "x", "apps-elsewhere"),
        deployment("media", "radarr", "apps-media", owner_namespace="other"),
        {"metadata": {"name": "traefik", "namespace": "traefik", "labels": {}}, "spec": {"replicas": 1}},
    )
    assert apps == [
        {
            "name": "jellyfin",
            "namespace": "media",
            "replicas": 1,
            "recorded": 2,
            "held": True,
            "late": False,
            "selector": "app.kubernetes.io/name=jellyfin,tier=app",
        },
        {
            "name": "sonarr",
            "namespace": "media",
            "replicas": 1,
            "recorded": None,
            "held": False,
            "late": False,
            "selector": "app.kubernetes.io/name=sonarr,tier=app",
        },
        {
            "name": "nextcloud-db",
            "namespace": "nextcloud",
            "replicas": 1,
            "recorded": None,
            "held": False,
            "late": True,
            "selector": "app.kubernetes.io/name=nextcloud-db,tier=app",
        },
    ]


@pytest.mark.parametrize(("recorded", "expected"), [("3", 3), ("", None), ("two", None), ("-1", None)])
def test_a_record_is_a_count_or_nothing(recorded, expected):
    (app,) = found(deployment("media", "sonarr", "apps-media", annotations={ANNOTATION: recorded}))
    assert app["recorded"] == expected


def test_no_late_label_makes_no_app_late():
    deployments = {"items": [deployment("n", "db", "apps-media", labels={"app.kubernetes.io/component": "database"})]}
    (app,) = fs.apps(deployments, KUSTOMIZATIONS, "k8s/apps", "", ANNOTATION)
    assert app["late"] is False


APPS = found(
    deployment("media", "sonarr", "apps-media"),
    deployment("media", "homepage", "apps-media", replicas=2),
    deployment("nextcloud", "nextcloud", "apps-nextcloud"),
    deployment("nextcloud", "nextcloud-db", "apps-nextcloud", labels={"app.kubernetes.io/component": "database"}),
)


def names(waves):
    return [[f"{s['namespace']}/{s['name']}" for s in wave] for wave in waves]


def test_down_stops_the_apps_then_their_databases_and_holds_each():
    result = fs.plan(APPS, "down", [], [], ANNOTATION)
    assert names(result["waves"]) == [
        ["media/homepage", "media/sonarr", "nextcloud/nextcloud"],
        ["nextcloud/nextcloud-db"],
    ]
    assert result["order"] == ["media/homepage, media/sonarr, nextcloud/nextcloud", "nextcloud/nextcloud-db"]
    homepage = result["waves"][0][0]
    assert homepage["replicas"] == 0
    assert json.loads(homepage["patch"]) == {
        "metadata": {"annotations": {fs.RECONCILE: "disabled", ANNOTATION: "2"}},
        "spec": {"replicas": 0},
    }
    assert result["unknown"] == [] and result["unchanged"] == []
    assert result["apps"] == ["homepage", "nextcloud", "nextcloud-db", "sonarr"]
    assert result["groups"] == ["media", "nextcloud"]


def held(apps, **replicas):
    """The apps after a down: each at zero (or at the given count), held, its replicas recorded."""
    out = []
    for app in apps:
        out.append(app | {"replicas": replicas.get(app["name"], 0), "held": True, "recorded": max(app["replicas"], 1)})
    return out


def test_down_leaves_what_is_down_and_stops_again_what_was_started_by_hand():
    result = fs.plan(held(APPS, sonarr=1), "down", [], [], ANNOTATION)
    assert names(result["waves"]) == [["media/sonarr"]]
    assert result["unchanged"] == ["media/homepage", "nextcloud/nextcloud", "nextcloud/nextcloud-db"]
    # The count recorded first is kept, not the one found.
    assert json.loads(result["waves"][0][0]["patch"])["metadata"]["annotations"][ANNOTATION] == "1"
    started = held(APPS, homepage=1)
    homepage = fs.plan(started, "down", ["homepage"], [], ANNOTATION)["waves"][0][0]
    assert json.loads(homepage["patch"])["metadata"]["annotations"][ANNOTATION] == "2"


def test_down_records_at_least_one_replica():
    (app,) = [a for a in APPS if a["name"] == "sonarr"]
    step = fs.plan([app | {"replicas": 0}], "down", [], [], ANNOTATION)["waves"][0][0]
    assert json.loads(step["patch"])["metadata"]["annotations"][ANNOTATION] == "1"


def test_up_starts_the_databases_first_and_only_what_down_stopped():
    # Sonarr was never stopped.
    stopped = [app if app["name"] == "sonarr" else held([app])[0] for app in APPS]
    result = fs.plan(stopped, "up", [], [], ANNOTATION)
    assert names(result["waves"]) == [["nextcloud/nextcloud-db"], ["media/homepage", "nextcloud/nextcloud"]]
    homepage = result["waves"][1][0]
    assert homepage["replicas"] == 2
    assert json.loads(homepage["patch"]) == {
        "metadata": {"annotations": {fs.RECONCILE: None, ANNOTATION: None}},
        "spec": {"replicas": 2},
    }
    assert result["unchanged"] == ["media/sonarr"]
    assert fs.plan(APPS, "up", [], [], ANNOTATION)["waves"] == []


@pytest.mark.parametrize(
    ("only", "expected"),
    [
        (["sonarr"], [["media/sonarr"]]),
        # A group, and a name that is a group and an app: the group.
        (["media"], [["media/homepage", "media/sonarr"]]),
        (["nextcloud"], [["nextcloud/nextcloud"], ["nextcloud/nextcloud-db"]]),
        (["nextcloud-db", "sonarr"], [["media/sonarr"], ["nextcloud/nextcloud-db"]]),
        (["homeassistant"], []),
    ],
)
def test_only_narrows_to_apps_and_groups(only, expected):
    result = fs.plan(APPS, "down", only, ["homeassistant"], ANNOTATION)
    assert names(result["waves"]) == expected
    assert result["unknown"] == []


def test_unknown_names_are_reported():
    result = fs.plan(APPS, "down", ["plex", "sonarr", "homeassistant", "traefik"], [], ANNOTATION)
    assert result["unknown"] == ["homeassistant", "plex", "traefik"]
