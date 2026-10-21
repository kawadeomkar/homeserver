"""Runs roles/proxmox_tailscale repeatedly against the fakes in fake_tailscale_bin/ and checks that it is idempotent.

From a fresh Proxmox host: a dry run changes nothing and says what it would do; the first run installs Tailscale,
loads the guard, turns on forwarding and logs the node in, in that order; a second run and a dry run after it change
nothing; drift is corrected by one run and left alone by the next. Also: a node stopped by hand is brought back up
without a key, a stopped tailscaled is started again after the guard, a logged-out node without a key is refused,
the key never reaches an argument list or Ansible's output, an upgrade is installed only when asked for, the guard
protects the NAS's address with a NAS and nothing without one, is reloaded and never restarted, and refuses Debian's
nftables service. The role runs with this repo's own configuration (group_vars/proxmox/tailscale.yml) and RFC 5737
addresses.

Run: make test TYPE=idempotence
"""

import json
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fake_tailscale  # noqa: E402
from test_idempotence import changed_tasks, expect, read, run, write  # noqa: E402

PLAYBOOK = "proxmox_tailscale.yml"
ROUTE = "192.0.2.0/24"
NAS = "192.0.2.10"
# Built from pieces so this file holds nothing gitleaks would take for a key.
AUTH_KEY = "-".join(["tskey", "auth", "kTEST" + "CNTRL", "notarealkeynotarealkey"])


def tailscale_vars(tmp_path, nas=True, **more):
    """Variables for the fake host: its state file, scratch paths for everything the role writes, and a file://
    repository serving a fake signing key. With a NAS, the NAS's static address, which the guard then protects."""
    repository = tmp_path / "repository"
    for name in ["repository", "keyrings", "sources", "sysctl.d", "nftables.d", "systemd"]:
        (tmp_path / name).mkdir()
    (repository / "trixie.noarmor.gpg").write_bytes(b"not a real key\n")
    state = tmp_path / "tailscale.json"
    fresh = fake_tailscale.fresh_state()
    fresh["auth_key"] = AUTH_KEY
    write(state, fresh)
    extra = {
        "fake_tailscale_state": str(state),
        "tailscale_advertise_routes": [ROUTE],
        "vault_tailscale_auth_key": AUTH_KEY,
        "proxmox_tailscale_suite": "trixie",
        "proxmox_tailscale_repository": repository.as_uri(),
        "proxmox_tailscale_keyring": str(tmp_path / "keyrings" / "tailscale-archive-keyring.gpg"),
        "proxmox_tailscale_sources_dir": str(tmp_path / "sources"),
        "proxmox_tailscale_sysctl_file": str(tmp_path / "sysctl.d" / "99-tailscale.conf"),
        "proxmox_tailscale_guard_file": str(tmp_path / "nftables.d" / "tailnet-guard.nft"),
        "proxmox_tailscale_systemd_dir": str(tmp_path / "systemd"),
    }
    if nas:
        extra["truenas_static_address"] = NAS + "/24"
    return extra | more


def settled(tmp_path, nas=True, **more):
    """A host this repo's configuration has already been applied to. test_tailscale_role_is_idempotent proves that
    one run settles a host with a NAS and the default variables; any other setup gets a second run here that proves
    the same for it."""
    extra = tailscale_vars(tmp_path, nas, **more)
    expect(run(PLAYBOOK, extra), "homeserver", changed=True, note="first run")
    if not nas or more:
        expect(run(PLAYBOOK, extra), "homeserver", changed=0, note="second run")
    return extra, Path(extra["fake_tailscale_state"])


def change(path, edit):
    """Change the fake's state behind the role's back."""
    state = read(path)
    edit(state)
    write(path, state)


def position(calls, prefix):
    """Where the first call starting with `prefix` is."""
    return next(i for i, call in enumerate(calls) if call[: len(prefix)] == prefix)


def test_tailscale_role_is_idempotent(tmp_path):
    """A fresh host: the dry run changes nothing and says what it would do; the first run does it, the guard loaded
    before forwarding and both before the login; the runs after it change nothing."""
    extra = tailscale_vars(tmp_path)
    path = Path(extra["fake_tailscale_state"])
    outputs = []

    result = run(PLAYBOOK, extra, check=True)
    outputs.append(result[2])
    expect(result, "homeserver", changed=True, note="dry run on a fresh host")
    assert read(path)["mutations"] == 0, "the dry run changed the fake host"
    for text in [
        "Tailscale is not in the package lists yet",
        "Would enable the guard at boot and load the guard",
        "IPv4 forwarding is off on the node",
        "Would log the node in with the auth key",
    ]:
        assert text in result[2], f"{text!r} missing\n{result[2][-3000:]}"

    result = run(PLAYBOOK, extra)
    outputs.append(result[2])
    expect(result, "homeserver", changed=True, note="first run")
    s = read(path)
    assert {
        name: s["packages"]["installed"].get(name) for name in fake_tailscale.REPOSITORY
    } == fake_tailscale.REPOSITORY
    assert s["node"]["backend"] == "Running" and s["node"]["tags"] == ["tag:homeserver"]
    prefs = s["node"]["prefs"]
    assert prefs["advertise-routes"] == ROUTE
    assert (prefs["accept-routes"], prefs["accept-dns"], prefs["ssh"], prefs["auto-update"]) == (
        False,
        False,
        False,
        False,
    )
    assert s["sysctl"]["net.ipv4.ip_forward"] == "1"
    assert s["units"]["tailnet-guard.service"] == {"enabled": True, "active": True}
    assert s["loads"] == ["start tailnet-guard.service"] and s["restarts"] == 0
    assert NAS + " . tcp . 443" in s["table"] and "ip daddr @guarded jump tailnet_to_guarded" in s["table"]
    dropin = Path(extra["proxmox_tailscale_systemd_dir"]) / "tailscaled.service.d" / "10-tailnet-guard.conf"
    assert (
        "Requires=tailnet-guard.service" in dropin.read_text() and "After=tailnet-guard.service" in dropin.read_text()
    )
    sources = (Path(extra["proxmox_tailscale_sources_dir"]) / "tailscale.sources").read_text()
    assert "Suites: trixie\n" in sources and f"Signed-By: {extra['proxmox_tailscale_keyring']}\n" in sources

    calls = s["calls"]
    guard = position(calls, ["systemctl", "reload-or-restart", "tailnet-guard.service"])
    forwarding = position(calls, ["sysctl", "-p"])
    login = position(calls, ["tailscale", "up"])
    assert guard < forwarding < login, "the guard must load before forwarding, and both before the login"
    assert position(calls, ["nft", "-c", "-f"]) < guard, "the guard's file was not checked before it was installed"
    key_file = next(arg for arg in calls[login] if arg.startswith("--auth-key=file:")).split(":", 1)[1]
    assert not Path(key_file).exists(), "the auth key's file was left on the host"
    assert "Nothing for the admin console to fix." in result[2], result[2][-3000:]

    for check in (False, True):
        result = run(PLAYBOOK, extra, check=check)
        outputs.append(result[2])
        expect(
            result,
            "homeserver",
            changed=0,
            note=f"{'dry run' if check else 'run'} once applied ({changed_tasks(result[2])})",
        )

    s = read(path)
    assert all(AUTH_KEY not in " ".join(call) for call in s["calls"]), "the auth key reached an argument list"
    for out in outputs:
        assert AUTH_KEY not in out, "the auth key reached Ansible's output"


def test_tailscale_role_corrects_drift(tmp_path):
    """Preferences, routes, forwarding and the guard's table changed by hand: one run puts them back, reloading the
    guard, and the next changes nothing."""
    extra, path = settled(tmp_path)

    def drift(state):
        state["node"]["prefs"].update({"accept-dns": True, "advertise-routes": "", "auto-update": True})
        state["sysctl"]["net.ipv4.ip_forward"] = "0"
        state["table"] = None

    change(path, drift)
    before = read(path)["mutations"]
    result = run(PLAYBOOK, extra, check=True)
    expect(result, "homeserver", changed=True, note="dry run after drift")
    assert read(path)["mutations"] == before, "the dry run changed the fake host"
    assert "Would set advertise-routes, accept-dns, auto-update with tailscale set." in result[2], result[2][-3000:]
    assert "its table is not loaded" in result[2], result[2][-3000:]

    expect(run(PLAYBOOK, extra), "homeserver", changed=True, note="run after drift")
    s = read(path)
    assert s["node"]["prefs"]["accept-dns"] is False and s["node"]["prefs"]["advertise-routes"] == ROUTE
    assert s["node"]["prefs"]["auto-update"] is False
    assert s["sysctl"]["net.ipv4.ip_forward"] == "1" and s["table"]
    assert s["loads"][-1] == "reload tailnet-guard.service"
    expect(run(PLAYBOOK, extra), "homeserver", changed=0, note="run after the correction")


def test_tailscale_role_brings_a_stopped_node_up(tmp_path):
    """`tailscale down` by hand: the next run brings the node back up, naming every managed flag, without a key."""
    extra, path = settled(tmp_path)
    change(path, lambda state: state["node"].update(backend="Stopped"))
    expect(run(PLAYBOOK, extra), "homeserver", changed=True, note="run on a stopped node")
    s = read(path)
    assert s["node"]["backend"] == "Running"
    up = s["calls"][[i for i, call in enumerate(s["calls"]) if call[:2] == ["tailscale", "up"]][-1]]
    assert not any(arg.startswith("--auth-key") for arg in up), up
    expect(run(PLAYBOOK, extra), "homeserver", changed=0, note="run after bringing it up")


def test_tailscale_role_starts_tailscaled_again(tmp_path):
    """`systemctl stop tailnet-guard` by hand, which stops tailscaled through its drop-in, and tailscaled disabled too:
    the dry run says it would load the guard and start and enable tailscaled, without asking for a key; the run does
    it, the guard first, and the node comes back logged in, without a login; the next run changes nothing."""
    extra, path = settled(tmp_path)

    def stopped(state):
        state["units"]["tailnet-guard.service"]["active"] = False
        state["units"]["tailscaled.service"] = {"enabled": False, "active": False}

    change(path, stopped)
    before = read(path)["mutations"]
    result = run(PLAYBOOK, extra | {"vault_tailscale_auth_key": ""}, check=True)
    expect(result, "homeserver", changed=True, note="dry run with tailscaled stopped")
    assert read(path)["mutations"] == before, "the dry run changed the fake host"
    for text in ["load the guard (it is not running)", "Would enable tailscaled at boot and start tailscaled"]:
        assert text in result[2], f"{text!r} missing\n{result[2][-3000:]}"
    assert "Would log the node in" not in result[2], result[2][-3000:]

    calls = len(read(path)["calls"])
    expect(run(PLAYBOOK, extra), "homeserver", changed=True, note="run with tailscaled stopped")
    s = read(path)
    assert s["units"]["tailscaled.service"] == {"enabled": True, "active": True}
    assert s["units"]["tailnet-guard.service"]["active"] and s["node"]["backend"] == "Running"
    run_calls = s["calls"][calls:]
    guard = position(run_calls, ["systemctl", "reload-or-restart", "tailnet-guard.service"])
    assert guard < position(run_calls, ["systemctl", "start", "tailscaled.service"])
    assert not any(call[:2] == ["tailscale", "up"] for call in run_calls)
    expect(run(PLAYBOOK, extra), "homeserver", changed=0, note="run after starting tailscaled")


def test_tailscale_role_needs_an_auth_key(tmp_path):
    """A host that is not logged in, and no key in the vault: the dry run and the run both stop, naming the vault's
    variable, before any login is tried."""
    extra = tailscale_vars(tmp_path, vault_tailscale_auth_key="")
    for check in (True, False):
        rc, _, out = run(PLAYBOOK, extra, check=check)
        assert rc != 0 and "vault_tailscale_auth_key" in out, out[-3000:]
    assert not any(call[:2] == ["tailscale", "up"] for call in read(Path(extra["fake_tailscale_state"]))["calls"])


def test_tailscale_role_without_routes_does_nothing(tmp_path):
    """Without tailscale_advertise_routes in local.yml, the role runs no command, dry or real."""
    extra = tailscale_vars(tmp_path, tailscale_advertise_routes=[])
    for check in (True, False):
        expect(run(PLAYBOOK, extra, check=check), "homeserver", changed=0, note="run without routes")
    assert read(Path(extra["fake_tailscale_state"]))["calls"] == []
    assert list((tmp_path / "sources").iterdir()) == []


def test_tailscale_role_reports_what_the_console_must_fix(tmp_path):
    """A route the policy does not approve, a Serve configuration and Tailscale's own warnings are reported, and the
    run still passes."""
    extra = tailscale_vars(tmp_path)

    def console(state):
        state.update(auto_approve=False, serve={"TCP": {"443": {"HTTPS": True}}})
        state["health"] = ["UDP GRO forwarding is suboptimally configured on vmbr0"]

    change(Path(extra["fake_tailscale_state"]), console)
    result = run(PLAYBOOK, extra)
    expect(result, "homeserver", changed=True, note="run with an unapproved route")
    for text in [
        f"Advertised but not approved, or served by another subnet router: {ROUTE}",
        "Tailscale Serve or Funnel is configured on the node",
        "Tailscale reports: UDP GRO forwarding is suboptimally configured on vmbr0",
    ]:
        assert text in result[2], f"{text!r} missing\n{result[2][-3000:]}"


def test_tailscale_role_upgrades_tailscale(tmp_path):
    """A newer release in the repository: a dry run on lists that know it reports it, the run installs it and reports
    the versions (the package restarting tailscaled), and with upgrades off nothing is upgraded."""
    extra, path = settled(tmp_path)
    change(path, lambda state: state["packages"]["repository"].update(tailscale="1.104.2"))
    no_upgrade = extra | {"proxmox_tailscale_upgrade": False}
    expect(run(PLAYBOOK, no_upgrade), "homeserver", changed=0, note="run with upgrades off")
    assert read(path)["packages"]["installed"]["tailscale"] == "1.104.1"

    result = run(PLAYBOOK, extra, check=True)
    expect(result, "homeserver", changed=True, note="dry run with a newer release in the lists")
    assert "Would install or upgrade: tailscale 1.104.1 → 1.104.2" in result[2], result[2][-3000:]
    result = run(PLAYBOOK, extra)
    expect(result, "homeserver", changed=True, note="run that upgrades")
    assert "Installed or upgraded: tailscale 1.104.1 → 1.104.2" in result[2], result[2][-3000:]
    s = read(path)
    assert s["packages"]["installed"]["tailscale"] == "1.104.2" and s["restarts"] == 1
    expect(run(PLAYBOOK, extra), "homeserver", changed=0, note="run after the upgrade")


def test_tailscale_guard_follows_the_setup(tmp_path):
    """Without a NAS the guard has no guarded hosts and keeps its other rules. Changing the allowed ports reloads the
    guard, never restarts it, and leaves tailscaled alone."""
    extra, path = settled(tmp_path, nas=False)
    table = read(path)["table"]
    assert "guarded" not in table and "ip daddr != @routes" in table and "ct state != { established, related }" in table

    more = extra | {"truenas_static_address": NAS + "/24"}
    expect(run(PLAYBOOK, more), "homeserver", changed=True, note="run with a NAS")
    ports = more | {"proxmox_tailscale_guard_allow": ["tcp/443", "tcp/80"]}
    expect(run(PLAYBOOK, ports), "homeserver", changed=True, note="run with another allowed port")
    s = read(path)
    assert NAS + " . tcp . 80" in s["table"]
    assert s["loads"] == ["start tailnet-guard.service"] + ["reload tailnet-guard.service"] * 2
    assert s["restarts"] == 0 and not any(call[:2] == ["systemctl", "restart"] for call in s["calls"])
    expect(run(PLAYBOOK, ports), "homeserver", changed=0, note="run after the reload")


def test_tailscale_guard_refuses_debians_nftables_service(tmp_path):
    """An enabled nftables.service would flush the guard at boot: the run stops before writing the guard."""
    extra = tailscale_vars(tmp_path)
    change(
        Path(extra["fake_tailscale_state"]),
        lambda state: state["units"].update({"nftables.service": {"enabled": True, "active": True}}),
    )
    rc, _, out = run(PLAYBOOK, extra)
    assert rc != 0 and "flush ruleset" in out, out[-3000:]
    assert not Path(extra["proxmox_tailscale_guard_file"]).exists()


def test_tailscale_settings_take_flag_names():
    """The repo's settings are tailscale set flags, which the fake knows, so a typo fails the tests."""
    settings = yaml.safe_load((HERE.parents[1] / "group_vars" / "proxmox" / "tailscale.yml").read_text())
    unknown = set(settings["proxmox_tailscale_settings"]) - fake_tailscale.SET_FLAGS
    assert unknown == set(), json.dumps(sorted(unknown))
