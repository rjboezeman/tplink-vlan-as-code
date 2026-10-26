from dataclasses import replace

import pytest

from tplink_vlan_as_code.plan import (
    DELETE_VLAN,
    ENABLE_DOT1Q,
    MAX_VLANS,
    SET_PVID,
    SET_VLAN,
    Op,
    RuleError,
    apply_op,
    make_plan,
    matches,
    simulate,
)
from tplink_vlan_as_code.state import SwitchState, Vlan

from .conftest import realize


def plan_for(cfg, name, current):
    return make_plan(current, cfg.desired(name), cfg.management_vlan, cfg.protected_ports(name))


def with_vlan(state, vid, vlan):
    return replace(state, vlans={**state.vlans, vid: vlan})


def test_in_sync_plans_nothing(cfg):
    for name in cfg.switches:
        assert plan_for(cfg, name, realize(cfg.desired(name))) == []


def test_order_is_vlans_then_pvids_then_deletes(cfg):
    cur = realize(cfg.desired("attic"))
    cur = with_vlan(cur, 7, Vlan("old", untagged=frozenset({5})))
    cur = replace(cur, pvids=cur.pvids[:4] + (7,) + cur.pvids[5:])            # port 5 uses the doomed VLAN
    cur = with_vlan(cur, 50, Vlan("wifi", tagged=frozenset({1})))              # port 2 lost untagged 50
    ops = plan_for(cfg, "attic", cur)
    assert [o.kind for o in ops] == [SET_VLAN, SET_PVID, DELETE_VLAN]
    assert ops[1] == Op(SET_PVID, 85, port=5)
    assert matches(simulate(cur, ops)[-1], cfg.desired("attic"))


def test_naive_order_would_break_a_rule(cfg):
    """Why the order matters: deleting a VLAN that is still a PVID is rejected by the switch."""
    cur = realize(cfg.desired("attic"))
    cur = replace(with_vlan(cur, 7, Vlan("old")), pvids=(7,) + cur.pvids[1:])
    with pytest.raises(RuleError, match="still a PVID"):
        simulate(cur, [Op(DELETE_VLAN, 7)])


def test_management_vlan_change_on_uplink_is_risky(cfg):
    cur = realize(cfg.desired("office"))
    cur = with_vlan(cur, 85, replace(cur.vlans[85], tagged=frozenset(), untagged=cur.vlans[85].untagged | {1}))
    [op] = plan_for(cfg, "office", cur)
    assert op.vid == 85 and "management-path port(s) [1]" in op.risk


def test_management_vlan_change_on_access_port_is_not_risky(cfg):
    cur = realize(cfg.desired("office"))
    cur = with_vlan(cur, 85, replace(cur.vlans[85], untagged=cur.vlans[85].untagged - {8}))
    [op] = plan_for(cfg, "office", cur)
    assert op.vid == 85 and op.risk == ""


def test_pvid_change_on_untagged_management_uplink_is_risky():
    cur = SwitchState(True, {1: Vlan("Default"), 85: Vlan("lan", untagged=frozenset({1, 2}))}, (1, 85))
    want = replace(cur, pvids=(85, 85))
    [op] = make_plan(cur, want, 85, {1})
    assert op.kind == SET_PVID and "management-path port 1" in op.risk


def test_vlan1_is_emptied_never_deleted(cfg):
    cur = realize(cfg.desired("attic"))
    cur = with_vlan(cur, 1, Vlan("Default", untagged=frozenset({6})))
    [op] = plan_for(cfg, "attic", cur)
    assert (op.kind, op.vid, op.name, op.untagged) == (SET_VLAN, 1, None, ())
    assert simulate(cur, [op])[-1].vlans[1].name == "Default"   # name None = keep the switch's name


def test_dot1q_off_plans_only_enable(cfg):
    cur = replace(realize(cfg.desired("attic")), dot1q_enabled=False)
    [op] = plan_for(cfg, "attic", cur)
    assert op.kind == ENABLE_DOT1Q and op.risk


@pytest.mark.parametrize("op, message", [
    (Op(DELETE_VLAN, 1), "cannot be deleted"),
    (Op(SET_PVID, 42, port=1), "does not exist"),
])
def test_device_rules(op, message):
    with pytest.raises(RuleError, match=message):
        apply_op(SwitchState(True, {1: Vlan("Default")}, (1, 1)), op)


def test_vlan_limit():
    full = SwitchState(True, {v: Vlan(str(v)) for v in range(1, MAX_VLANS + 1)}, (1,))
    with pytest.raises(RuleError, match="already has"):
        apply_op(full, Op(SET_VLAN, 999, "x"))
