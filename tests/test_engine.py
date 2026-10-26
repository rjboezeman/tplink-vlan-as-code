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


def drift(dev: FakeDevice, port: int = 6) -> None:
    """Port `port` moved from untagged 85 / pvid 85 to untagged 90 / pvid 90 (not a management-path port)."""
    v = dict(dev.state.vlans)
    v[85] = replace(v[85], untagged=v[85].untagged - {port})
    v[90] = replace(v[90], untagged=v[90].untagged | {port})
    pvids = list(dev.state.pvids)
    pvids[port - 1] = 90
    dev.state = replace(dev.state, vlans=v, pvids=tuple(pvids))


def run(cfg, devices, tmp_path, **flags):
    connect = connector(devices)
    plans = build_plans(cfg, connect)
    preflight(cfg, plans, allow_management_change=flags.get("mgmt", False),
              allow_untested_model=flags.get("untested", True), approved=flags.get("approved"))
    return execute(cfg, plans, connect, tmp_path / "snaps", lambda _: None)


def test_apply_converges_and_snapshots(cfg, tmp_path):
    devices = in_sync_devices(cfg)
    drift(devices["attic"])
    results = run(cfg, devices, tmp_path)
    assert [r["status"] for r in results] == ["applied", "in_sync", "in_sync"]
    assert all(not p.ops for p in build_plans(cfg, connector(devices)))
    snap = tmp_path / "snaps" / os.path.basename(results[0]["snapshot"])
    assert snap.stat().st_mode & 0o777 == 0o600 and (tmp_path / "snaps").stat().st_mode & 0o777 == 0o700


def test_risky_change_needs_explicit_permission(cfg, tmp_path):
    devices = in_sync_devices(cfg)
    office = devices["office"]
    office.state = replace(office.state, vlans={**office.state.vlans, 85: Vlan("lan", untagged=office.state.vlans[85].untagged)})
    with pytest.raises(Refused, match="management path"):
        run(cfg, devices, tmp_path)
    assert office.applied == [] and not (tmp_path / "snaps").exists()
    run(cfg, devices, tmp_path, mgmt=True)
    assert office.applied


def test_untested_hardware_needs_explicit_permission(cfg, tmp_path):
    devices = in_sync_devices(cfg)
    drift(devices["attic"])
    with pytest.raises(Refused, match="not verified"):
        run(cfg, devices, tmp_path, untested=False)


def test_wrong_device_is_refused(cfg, tmp_path):
    devices = in_sync_devices(cfg)
    devices["office"]._identity = Identity("TL-SG108PE 3.0", "x", "00:00:5E:00:53:99")
    with pytest.raises(Refused, match="config pins 00:00:5E:00:53:03"):
        build_plans(cfg, connector(devices))
    devices["office"]._identity = Identity("TL-SG116E 1.0", "x", "00:00:5E:00:53:03")
    with pytest.raises(Refused, match="config says TL-SG108PE"):
        build_plans(cfg, connector(devices))


def test_state_changed_after_planning_is_refused(cfg, tmp_path):
    devices = in_sync_devices(cfg)
    drift(devices["attic"])
    connect = connector(devices)
    plans = build_plans(cfg, connect)
    drift(devices["attic"], port=7)  # someone else changed the switch in between
    with pytest.raises(Refused, match="changed since it was planned"):
        execute(cfg, plans, connect, tmp_path / "snaps", lambda _: None)
    assert devices["attic"].applied == []


class IgnoresPvid(FakeDevice):
    """A switch that accepts a PVID write but does not apply it (writes report nothing)."""

    def apply(self, op):
        if op.kind != SET_PVID:
            super().apply(op)


def test_write_that_does_not_take_effect_stops_the_run(cfg, tmp_path):
    devices = in_sync_devices(cfg)
    devices["attic"] = IgnoresPvid(devices["attic"].state, devices["attic"].identity())
    drift(devices["attic"])
    drift(devices["office"])
    with pytest.raises(PartialApply, match=r"step 3 \(set port 6 pvid 85\) did not take effect") as e:
        run(cfg, devices, tmp_path)
    assert e.value.switch == "attic" and e.value.snapshot_path.exists()
    assert devices["office"].applied == []  # stopped before the next switch


def test_lost_reachability_stops_the_run(cfg, tmp_path):
    devices = in_sync_devices(cfg)
    drift(devices["attic"])
    drift(devices["core"])
    calls = []

    def connect(spec):
        calls.append(spec.name)
        if spec.name == "office" and devices["attic"].applied:
            raise DeviceError("office: timed out")
        return devices[spec.name]

    plans = build_plans(cfg, connector(devices))
    preflight(cfg, plans, allow_management_change=False, allow_untested_model=True)
    with pytest.raises(PartialApply, match="office failed a fresh login"):
        execute(cfg, plans, connect, tmp_path / "snaps", lambda _: None)
    assert devices["core"].applied == []


def test_saved_plan_must_still_match(cfg, tmp_path):
    devices = in_sync_devices(cfg)
    drift(devices["attic"])
    approved = plan_document(cfg, build_plans(cfg, connector(devices)), None)
    drift(devices["attic"], port=7)
    with pytest.raises(Refused, match="saved plan no longer matches"):
        run(cfg, devices, tmp_path, approved=approved)
