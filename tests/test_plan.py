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
    cur = realize(cfg.desired("workshop"))
    cur = with_vlan(cur, 7, Vlan("old"))
    cur = replace(cur, pvids=cur.pvids[:4] + (7,) + cur.pvids[5:])            # port 5 uses the doomed VLAN
    cur = with_vlan(cur, 400, Vlan("cameras", tagged=frozenset({8})))         # ports 1-2 lost untagged 400
    ops = plan_for(cfg, "workshop", cur)
    assert [o.kind for o in ops] == [SET_VLAN, SET_PVID, DELETE_VLAN]
    assert ops[1] == Op(SET_PVID, 200, port=5)
    assert matches(simulate(cur, ops)[-1], cfg.desired("workshop"))


def test_naive_order_would_break_a_rule(cfg):
    """Why the order matters: deleting a VLAN that is still a PVID is rejected by the switch."""
    cur = realize(cfg.desired("workshop"))
    cur = replace(with_vlan(cur, 7, Vlan("old")), pvids=(7,) + cur.pvids[1:])
    with pytest.raises(RuleError, match="still a PVID"):
        simulate(cur, [Op(DELETE_VLAN, 7)])


def test_management_vlan_change_on_uplink_is_risky(cfg):
    cur = realize(cfg.desired("studio"))  # uplink port 8 carries mgmt untagged instead of tagged
    vl = cur.vlans[100]
    cur = with_vlan(cur, 100, replace(vl, tagged=vl.tagged - {8}, untagged=vl.untagged | {8}))
    [op] = plan_for(cfg, "studio", cur)
    assert op.vid == 100 and "management-path port(s) [8]" in op.risk


def test_management_vlan_change_on_access_port_is_not_risky(cfg):
    cur = realize(cfg.desired("rack"))  # admin desk (port 7) lost the management VLAN
    cur = with_vlan(cur, 100, replace(cur.vlans[100], untagged=cur.vlans[100].untagged - {7}))
    [op] = plan_for(cfg, "rack", cur)
    assert op.vid == 100 and op.risk == ""


def test_pvid_change_on_untagged_management_uplink_is_risky():
    cur = SwitchState(True, {1: Vlan("Default"), 100: Vlan("mgmt", untagged=frozenset({1, 2}))}, (1, 100))
    want = replace(cur, pvids=(100, 100))
    [op] = make_plan(cur, want, 100, {1})
    assert op.kind == SET_PVID and "management-path port 1" in op.risk


def test_vlan1_is_emptied_never_deleted(cfg):
    cur = realize(cfg.desired("workshop"))
    cur = with_vlan(cur, 1, Vlan("Default", untagged=frozenset({6})))
    [op] = plan_for(cfg, "workshop", cur)
    assert (op.kind, op.vid, op.name, op.untagged) == (SET_VLAN, 1, "Default", ())  # undeclared name: keep the switch's


def test_dot1q_off_plans_only_enable(cfg):
    cur = replace(realize(cfg.desired("workshop")), dot1q_enabled=False)
    [op] = plan_for(cfg, "workshop", cur)
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
        apply_op(full, Op(SET_VLAN, 4001, "x", untagged=(1,)))


def test_empty_vlan_cannot_be_created():
    """Rule 7, found on real hardware: the switch silently ignores creating a VLAN with no ports."""
    with pytest.raises(RuleError, match="at least one member port"):
        apply_op(SwitchState(True, {1: Vlan("Default")}, (1, 1)), Op(SET_VLAN, 90, "domotica"))
    apply_op(SwitchState(True, {1: Vlan("Default", untagged=frozenset({1}))}, (1, 1)), Op(SET_VLAN, 1, "Default"))  # VLAN 1 may be empty


def test_vlan_left_without_ports_is_deleted(cfg):
    cur = realize(cfg.desired("workshop"))
    cur = with_vlan(cur, 300, Vlan("guest", tagged=frozenset({8})))  # present on the switch, no port in config
    [op] = plan_for(cfg, "workshop", cur)
    assert (op.kind, op.vid) == (DELETE_VLAN, 300)
