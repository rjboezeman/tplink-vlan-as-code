import os
from dataclasses import replace

import pytest

from tplink_vlan_as_code.device import DeviceError, FakeDevice
from tplink_vlan_as_code.engine import (
    PartialApply,
    Refused,
    build_plans,
    execute,
    plan_document,
    preflight,
)
from tplink_vlan_as_code.plan import SET_PVID
from tplink_vlan_as_code.snapshot import Identity
from tplink_vlan_as_code.state import Vlan

from .conftest import connector, in_sync_devices


def drift(dev: FakeDevice, port: int = 4) -> None:
    """Port `port` moved from staff (untagged 200 / pvid 200) to guest (300); not a management-path port."""
    v = dict(dev.state.vlans)
    v[200] = replace(v[200], untagged=v[200].untagged - {port})
    v[300] = replace(v[300], untagged=v[300].untagged | {port})
    pvids = list(dev.state.pvids)
    pvids[port - 1] = 300
    dev.state = replace(dev.state, vlans=v, pvids=tuple(pvids))


def run(cfg, devices, tmp_path, **flags):
    connect = connector(devices)
    plans = build_plans(cfg, connect)
    preflight(cfg, plans, allow_management_change=flags.get("mgmt", False),
              allow_untested_model=flags.get("untested", True), approved=flags.get("approved"))
    return execute(cfg, plans, connect, tmp_path / "snaps", lambda _: None)


def test_apply_converges_and_snapshots(cfg, tmp_path):
    devices = in_sync_devices(cfg)
    drift(devices["workshop"])
    results = run(cfg, devices, tmp_path)
    assert [(r["switch"], r["status"]) for r in results] == [("studio", "in_sync"), ("workshop", "applied"), ("rack", "in_sync")]
    assert all(not p.ops for p in build_plans(cfg, connector(devices)))
    snap = tmp_path / "snaps" / os.path.basename(results[1]["snapshot"])
    assert snap.stat().st_mode & 0o777 == 0o600 and (tmp_path / "snaps").stat().st_mode & 0o777 == 0o700


def test_risky_change_needs_explicit_permission(cfg, tmp_path):
    devices = in_sync_devices(cfg)
    studio = devices["studio"]  # its uplink (port 8) lost tagged management VLAN 100
    studio.state = replace(studio.state, vlans={**studio.state.vlans, 100: Vlan("mgmt", untagged=studio.state.vlans[100].untagged)})
    with pytest.raises(Refused, match="management path"):
        run(cfg, devices, tmp_path)
    assert studio.applied == [] and not (tmp_path / "snaps").exists()
    run(cfg, devices, tmp_path, mgmt=True)
    assert studio.applied


def test_untested_hardware_needs_explicit_permission(cfg, tmp_path):
    devices = in_sync_devices(cfg)
    drift(devices["workshop"])
    with pytest.raises(Refused, match="not verified"):
        run(cfg, devices, tmp_path, untested=False)


def test_wrong_device_is_refused(cfg, tmp_path):
    devices = in_sync_devices(cfg)
    devices["studio"]._identity = Identity("TL-SG108PE 3.0", "x", "00:00:5E:00:53:99")
    with pytest.raises(Refused, match="config pins 00:00:5E:00:53:11"):
        build_plans(cfg, connector(devices))
    devices["studio"]._identity = Identity("TL-SG116E 1.0", "x", "00:00:5E:00:53:11")
    with pytest.raises(Refused, match="config says TL-SG108PE"):
        build_plans(cfg, connector(devices))


def test_state_changed_after_planning_is_refused(cfg, tmp_path):
    devices = in_sync_devices(cfg)
    drift(devices["workshop"])
    connect = connector(devices)
    plans = build_plans(cfg, connect)
    drift(devices["workshop"], port=5)  # someone else changed the switch in between
    with pytest.raises(Refused, match="changed since it was planned"):
        execute(cfg, plans, connect, tmp_path / "snaps", lambda _: None)
    assert devices["workshop"].applied == []


class IgnoresPvid(FakeDevice):
    """A switch that accepts a PVID write but does not apply it (writes report nothing)."""

    def apply(self, op):
        if op.kind != SET_PVID:
            super().apply(op)


def test_write_that_does_not_take_effect_stops_the_run(cfg, tmp_path):
    devices = in_sync_devices(cfg)
    devices["workshop"] = IgnoresPvid(devices["workshop"].state, devices["workshop"].identity())
    drift(devices["workshop"])
    drift(devices["rack"])
    with pytest.raises(PartialApply, match=r"step 3 \(set port 4 pvid 200\) did not take effect") as e:
        run(cfg, devices, tmp_path)
    assert e.value.switch == "workshop" and e.value.snapshot_path.exists()
    assert devices["rack"].applied == []  # stopped before the next switch


def test_lost_reachability_stops_the_run(cfg, tmp_path):
    devices = in_sync_devices(cfg)
    drift(devices["workshop"])
    drift(devices["rack"])

    def connect(spec):
        if spec.name == "studio" and devices["workshop"].applied:
            raise DeviceError("studio: timed out")
        return devices[spec.name]

    plans = build_plans(cfg, connector(devices))
    preflight(cfg, plans, allow_management_change=False, allow_untested_model=True)
    with pytest.raises(PartialApply, match="studio failed a fresh login"):
        execute(cfg, plans, connect, tmp_path / "snaps", lambda _: None)
    assert devices["rack"].applied == []


def test_saved_plan_must_still_match(cfg, tmp_path):
    devices = in_sync_devices(cfg)
    drift(devices["workshop"])
    approved = plan_document(cfg, build_plans(cfg, connector(devices)), None)
    drift(devices["workshop"], port=5)
    with pytest.raises(Refused, match="saved plan no longer matches"):
        run(cfg, devices, tmp_path, approved=approved)


def test_per_switch_credentials(cfg, monkeypatch):
    import tplink_vlan_as_code.engine as engine

    seen = {}
    monkeypatch.setattr(engine, "TplinkDevice", lambda host, username, password: seen.update({host: (username, password)}))
    connect = engine.live_connector({"TPLINK_PASSWORD": "shared", "TPLINK_PASSWORD_WORKSHOP": "workshop-only"})
    for spec in cfg.switches.values():
        connect(spec)
    assert seen == {"192.0.2.12": ("admin", "workshop-only"), "192.0.2.11": ("admin", "shared"), "192.0.2.10": ("admin", "shared")}
    with pytest.raises(DeviceError, match="set TPLINK_PASSWORD_RACK or TPLINK_PASSWORD"):
        engine.live_connector({})(cfg.switches["rack"])
