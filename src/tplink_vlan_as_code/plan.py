"""Turn (current state, desired state) into an ordered list of operations, and check it.

The planner is pure: no I/O. Every plan is simulated against `apply_op`, which
encodes the switch's rules as we understand them (see DEVICE RULES below). The
simulation yields the expected state after every single step; the executor
compares the real switch against those states and stops at the first mismatch.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Any

from .state import DEFAULT_VID, SwitchState, Vlan

ENABLE_DOT1Q = "enable_dot1q"
SET_VLAN = "set_vlan"        # create a VLAN, or replace its name and full membership
SET_PVID = "set_pvid"
DELETE_VLAN = "delete_vlan"

# DEVICE RULES, as observed on TP-Link Easy Smart web firmware (assumptions, enforced in simulation):
#   1. VLAN table changes require 802.1Q mode to be on.
#   2. VLAN 1 can be emptied but not deleted.
#   3. A VLAN that is a PVID of any port cannot be deleted.
#   4. A PVID must point at an existing VLAN.
#   5. At most MAX_VLANS 802.1Q VLANs exist at once.
#   6. Turning 802.1Q mode on resets the table to VLAN 1, all ports untagged, all PVIDs 1.
MAX_VLANS = 32


class RuleError(Exception):
    """An operation would violate a device rule."""


@dataclass(frozen=True)
class Op:
    """One write to one switch. `risk` is non-empty when the op may cut off management access."""

    kind: str
    vid: int | None = None
    name: str | None = None
    tagged: tuple[int, ...] = ()
    untagged: tuple[int, ...] = ()
    port: int | None = None
    risk: str = ""

    def describe(self) -> str:
        if self.kind == ENABLE_DOT1Q:
            return "enable 802.1Q VLAN mode"
        if self.kind == SET_VLAN:
            return f"set vlan {self.vid} {self.name or ''}: tagged {list(self.tagged)} untagged {list(self.untagged)}"
        if self.kind == SET_PVID:
            return f"set port {self.port} pvid {self.vid}"
        return f"delete vlan {self.vid}"

    def to_json(self) -> dict[str, Any]:
        d = {"kind": self.kind, "description": self.describe()}
        if self.risk:
            d["risk"] = self.risk
        return d


def apply_op(state: SwitchState, op: Op) -> SwitchState:
    """Return the state after `op`, or raise RuleError. Single source of the DEVICE RULES."""
    if op.kind == ENABLE_DOT1Q:
        ports = frozenset(range(1, state.port_count + 1))
        return SwitchState(True, {DEFAULT_VID: Vlan("Default", untagged=ports)}, (DEFAULT_VID,) * state.port_count)
    if not state.dot1q_enabled:
        raise RuleError("802.1Q mode is off")
    vlans = dict(state.vlans)
    if op.kind == SET_VLAN:
        if op.vid not in vlans and len(vlans) >= MAX_VLANS:
            raise RuleError(f"vlan {op.vid}: switch already has {MAX_VLANS} VLANs")
        name = op.name if op.name is not None else (vlans[op.vid].name if op.vid in vlans else "")
        vlans[op.vid] = Vlan(name, frozenset(op.tagged), frozenset(op.untagged))
        return replace(state, vlans=vlans)
    if op.kind == SET_PVID:
        if op.vid not in vlans:
            raise RuleError(f"port {op.port}: pvid {op.vid} does not exist")
        pvids = list(state.pvids)
        pvids[op.port - 1] = op.vid
        return replace(state, pvids=tuple(pvids))
    if op.kind == DELETE_VLAN:
        if op.vid == DEFAULT_VID:
            raise RuleError("vlan 1 cannot be deleted")
        if op.vid in state.pvids:
            raise RuleError(f"vlan {op.vid} is still a PVID")
        del vlans[op.vid]
        return replace(state, vlans=vlans)
    raise ValueError(f"unknown op kind {op.kind!r}")


def matches(actual: SwitchState, desired: SwitchState) -> bool:
    """True when `actual` satisfies `desired`. A desired VLAN name of None matches any name."""
    if actual.dot1q_enabled != desired.dot1q_enabled or actual.pvids != desired.pvids:
        return False
    if actual.vlans.keys() != desired.vlans.keys():
        return False
    return all(_vlan_matches(actual.vlans[vid], want) for vid, want in desired.vlans.items())


def _vlan_matches(have: Vlan | None, want: Vlan) -> bool:
    return (
        have is not None
        and have.tagged == want.tagged
        and have.untagged == want.untagged
        and (want.name is None or have.name == want.name)
    )


def make_plan(
    current: SwitchState, desired: SwitchState, mgmt_vid: int, protected_ports: Iterable[int]
) -> list[Op]:
    """Ops that take `current` to `desired`, ordered so no step breaks a device rule.

    Order: create/modify VLANs, then PVIDs, then deletes. That way a PVID never points at a
    missing VLAN and a VLAN is never deleted while still in use.

    `protected_ports` are the ports that carry this switch's management path (its uplink and
    the downlinks to switches behind it). Ops that could break that path get a `risk` reason.
    """
    if not current.dot1q_enabled:
        # Enabling resets the table, so anything planned past it would be guesswork. Re-plan after.
        return [Op(ENABLE_DOT1Q, risk="enabling 802.1Q replaces the VLAN table and restarts the web UI")]

    protected = set(protected_ports)
    mgmt_untagged = {
        p for p in protected
        for s in (current, desired)
        if mgmt_vid in s.vlans and s.vlans[mgmt_vid].mode(p) == "U"
    }
    ops = []
    for vid, want in sorted(desired.vlans.items()):
        have = current.vlans.get(vid)
        if _vlan_matches(have, want):
            continue
        risk = ""
        if vid == mgmt_vid:
            changed = sorted(p for p in protected if (have.mode(p) if have else None) != want.mode(p))
            if changed:
                risk = f"changes management VLAN {vid} membership on management-path port(s) {changed}"
        name = want.name if want.name is not None else (have.name if have else "")  # ops always carry what is sent
        ops.append(Op(SET_VLAN, vid, name, tuple(sorted(want.tagged)), tuple(sorted(want.untagged)), risk=risk))
    for port, (want, have) in enumerate(zip(desired.pvids, current.pvids, strict=True), start=1):
        if want != have:
            risk = f"changes PVID of management-path port {port}" if port in mgmt_untagged else ""
            ops.append(Op(SET_PVID, want, port=port, risk=risk))
    for vid in sorted(current.vlans.keys() - desired.vlans.keys()):
        risk = f"deletes management VLAN {vid}" if vid == mgmt_vid else ""
        ops.append(Op(DELETE_VLAN, vid, current.vlans[vid].name, risk=risk))
    return ops


def simulate(current: SwitchState, ops: list[Op]) -> list[SwitchState]:
    """Expected state after each op. Raises RuleError if the sequence breaks a device rule."""
    states, state = [], current
    for op in ops:
        state = apply_op(state, op)
        states.append(state)
    return states
