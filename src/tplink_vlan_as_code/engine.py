"""Plan and apply across switches, with every safety check in one place.

Apply sequence (nothing is written until all checks pass for all switches):

  1. Plan: per switch, verify identity (model, pinned MAC) and port count, read
     state, compute ops, simulate them against the device rules.
  2. Preflight: refuse risky ops (management path) and untested hardware unless
     explicitly allowed; if a saved plan was given, refuse unless the fresh plan
     is identical to it.
  3. Execute, furthest switch first. Per switch: re-verify identity and that the
     state is unchanged since planning, write a snapshot (rollback point), then
     apply op by op, reading the state back after each write and stopping at the
     first mismatch. Finally reconnect with a fresh session and check every
     switch is still reachable before moving on.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import snapshot
from .config import Config, SwitchSpec
from .device import Device, DeviceError, FakeDevice, TplinkDevice
from .plan import ENABLE_DOT1Q, Op, RuleError, make_plan, matches, simulate
from .snapshot import Identity
from .state import DEFAULT_VID, Link, SwitchState

PLAN_FORMAT = "tplink-vlan-as-code/plan/v1"

# Hardware strings, exactly as the switch reports them, on which apply has been verified end-to-end.
# Anything else needs --allow-untested-model. Add a version here only after a verified real apply.
TESTED_HARDWARE: frozenset[str] = frozenset({
    "TL-SG108PE 5.0",  # firmware 1.0.0 Build 20230218 Rel.51358, three switches, Oct 2026
})

Connector = Callable[[SwitchSpec], Device]
Log = Callable[[str], None]


class Refused(Exception):
    """A safety check failed before anything was written."""


class PlanError(Exception):
    """No safe op sequence reaches the desired state (or the planner disagrees with itself)."""


class PartialApply(Exception):
    """Writes happened and then something went wrong. The switch may be between states."""

    def __init__(self, switch: str, detail: str, snapshot_path: Path | None):
        self.switch, self.detail, self.snapshot_path = switch, detail, snapshot_path
        hint = f"; restore with: tplink-vlan restore {snapshot_path} --switch {switch}" if snapshot_path else ""
        super().__init__(f"{switch}: {detail}{hint}")


@dataclass
class SwitchPlan:
    name: str
    host: str
    identity: Identity
    current: SwitchState
    ops: list[Op]
    expected: list[SwitchState]  # state after each op
    links: tuple[Link, ...] | None = None  # live port status; informational, not part of plan approval

    @property
    def untested(self) -> bool:
        return self.identity.hardware not in TESTED_HARDWARE

    def to_json(self) -> dict[str, Any]:
        return {
            "switch": self.name,
            "host": self.host,
            "identity": self.identity.to_json(),
            "in_sync": not self.ops,
            "current": self.current.to_json(),
            "ops": [op.to_json() for op in self.ops],
        }


def live_connector(env: Mapping[str, str]) -> Connector:
    def connect(spec: SwitchSpec) -> Device:
        password = next((env[v] for v in spec.password_envs if env.get(v)), None)
        if not password:
            raise DeviceError(f"{spec.name}: no password; set {' or '.join(spec.password_envs)}")
        return TplinkDevice(spec.host, spec.username, password)
    return connect


def offline_connector(directory: Path) -> Connector:
    """Use DIR/<switch>.json snapshot files instead of switches. Applies are written back to the files."""
    def connect(spec: SwitchSpec) -> Device:
        try:
            return FakeDevice.from_snapshot(directory / f"{spec.name}.json")
        except ValueError as e:
            raise DeviceError(str(e)) from None
    return connect


@contextmanager
def connected(connect: Connector, spec: SwitchSpec) -> Iterator[Device]:
    dev = connect(spec)
    try:
        yield dev
    finally:
        dev.close()


def check_identity(spec: SwitchSpec, ident: Identity) -> None:
    """Refuse to touch a device that is not the one the config describes."""
    if not ident.hardware.startswith(spec.model):
        raise Refused(f"{spec.name}: {spec.host} reports hardware {ident.hardware!r}, config says {spec.model}")
    if spec.mac and ident.mac != spec.mac:
        raise Refused(f"{spec.name}: {spec.host} reports MAC {ident.mac}, config pins {spec.mac}")


def plan_switch(cfg: Config, name: str, dev: Device, desired: SwitchState | None = None) -> SwitchPlan:
    spec = cfg.switches[name]
    ident = dev.identity()
    check_identity(spec, ident)
    current = dev.read_state()
    if current.port_count != len(spec.ports):
        raise Refused(f"{name}: switch has {current.port_count} ports, config declares {len(spec.ports)}")
    desired = desired or cfg.desired(name)
    if desired.port_count != current.port_count:
        raise Refused(f"{name}: target state has {desired.port_count} ports, switch has {current.port_count}")
    links = dev.links()
    ops = make_plan(current, desired, cfg.management_vlan, cfg.protected_ports(name))
    try:
        expected = simulate(current, ops)
    except RuleError as e:
        raise PlanError(f"{name}: no safe ordering found: {e}") from None
    if ops and ops[0].kind != ENABLE_DOT1Q and not matches(expected[-1], desired):
        raise PlanError(f"{name}: internal error, simulated result does not match the desired state")
    return SwitchPlan(name, spec.host, ident, current, ops, expected, links)


def build_plans(
    cfg: Config, connect: Connector, only: list[str] | None = None, targets: dict[str, SwitchState] | None = None
) -> list[SwitchPlan]:
    """Read every switch (read-only) and plan it. `targets` overrides the config's desired state (restore)."""
    plans = []
    for name in cfg.apply_order(only):
        with connected(connect, cfg.switches[name]) as dev:
            plans.append(plan_switch(cfg, name, dev, (targets or {}).get(name)))
    return plans


def plan_document(cfg: Config, plans: list[SwitchPlan], only: list[str] | None) -> dict[str, Any]:
    return {
        "format": PLAN_FORMAT,
        "config_sha256": cfg.sha256,
        "only": sorted(only or []),
        "switches": [p.to_json() for p in plans],
    }


def preflight(
    cfg: Config, plans: list[SwitchPlan], *, allow_management_change: bool, allow_untested_model: bool,
    approved: dict[str, Any] | None = None, only: list[str] | None = None,
) -> None:
    """Every check that can refuse an apply. Raises Refused; writes nothing."""
    if approved is not None and approved != plan_document(cfg, plans, only):
        raise Refused("the saved plan no longer matches the config or the switches; run plan again and re-approve")
    pending = [p for p in plans if p.ops]
    risky = [f"{p.name}: {op.describe()} -- {op.risk}" for p in pending for op in p.ops if op.risk]
    if risky and not allow_management_change:
        raise Refused("plan touches the management path (needs --allow-management-change):\n  " + "\n  ".join(risky))
    untested = sorted({p.identity.hardware for p in pending if p.untested})
    if untested and not allow_untested_model:
        raise Refused(f"apply is not verified on hardware {untested} (needs --allow-untested-model)")


def execute(cfg: Config, plans: list[SwitchPlan], connect: Connector, snapshot_dir: Path, log: Log) -> list[dict]:
    """Apply plans in order (already preflighted). Raises Refused before the first write, PartialApply after."""
    results, wrote = [], False
    for p in plans:
        if not p.ops:
            results.append({"switch": p.name, "status": "in_sync"})
            continue
        spec = cfg.switches[p.name]
        snap = None
        try:
            with connected(connect, spec) as dev:
                check_identity(spec, dev.identity())
                if dev.read_state() != p.current:
                    raise Refused(f"{p.name}: state changed since it was planned; run plan again")
                snap = snapshot.write(snapshot.timestamped(snapshot_dir, p.name), p.name, p.host, p.identity, p.current, p.links)
                log(f"{p.name}: snapshot {snap}")
                wrote = True
                for i, (op, want) in enumerate(zip(p.ops, p.expected, strict=True), start=1):
                    log(f"{p.name}: [{i}/{len(p.ops)}] {op.describe()}")
                    dev.apply(op)
                    got = dev.read_state()
                    if not _step_ok(op, got, want):
                        raise PartialApply(p.name, f"step {i} ({op.describe()}) did not take effect as expected: "
                                           + _diff(got, want), snap)
            _verify_reachable(cfg, connect, p, snap, log)
        except (DeviceError, Refused) as e:
            if not wrote:
                raise Refused(str(e)) from None
            raise PartialApply(p.name, str(e), snap) from None
        results.append({"switch": p.name, "status": "applied", "ops": len(p.ops), "snapshot": str(snap)})
    return results


def link_report(cfg: Config, plans: list[SwitchPlan]) -> list[dict[str, Any]]:
    """Compare live link state with the switch's VLAN config and the config file, port by port.

    warning: a device is connected (link up) but the port is in no VLAN on the switch other than
             an undeclared VLAN 1, where the switch parks such ports (device rule 8), so its traffic
             goes nowhere; or the port's live config differs from the config file.
    info:    a device on a port with no name in the config (undocumented), a named port without
             link (device missing), or a port that is administratively disabled.
    """
    out = []
    for p in plans:
        spec, desired = cfg.switches[p.name], cfg.desired(p.name)
        ports = []
        for port in spec.ports:
            n = port.number
            live = _membership(p.current, n)
            link = p.links[n - 1] if p.links else None
            finding, severity = None, None
            if link is None:
                pass  # reported once per switch via links_known
            elif link.speed and not any(n in vl.tagged | vl.untagged for v, vl in p.current.vlans.items()
                                        if v != DEFAULT_VID or DEFAULT_VID in cfg.vlans):
                finding, severity = "link up, but the port is in no VLAN (only built-in VLAN 1): its traffic goes nowhere", "warning"
            elif link.speed and live != _membership(desired, n):
                finding, severity = f"link up, and the switch has {live} where the config says {_membership(desired, n)}", "warning"
            elif not link.enabled:
                finding, severity = "port is administratively disabled", "info"
            elif link.speed and not port.name:
                finding, severity = "device connected, but the port has no name in the config", "info"
            elif not link.speed and port.name:
                finding, severity = f"no link on port named {port.name!r}", "info"
            ports.append({"port": n, "name": port.name, "enabled": link.enabled if link else None,
                          "link": link.speed if link else None, "membership": live,
                          "finding": finding, "severity": severity})
        out.append({"switch": p.name, "links_known": p.links is not None, "ports": ports})
    return out


def _membership(state: SwitchState, port: int) -> str:
    """Compact port summary, e.g. 'U85 T10,50 pvid85'; empty if the port is in no VLAN."""
    u = [v for v, vl in sorted(state.vlans.items()) if port in vl.untagged]
    t = [v for v, vl in sorted(state.vlans.items()) if port in vl.tagged]
    if not u and not t:
        return ""
    parts = [f"U{','.join(map(str, u))}"] if u else []
    parts += [f"T{','.join(map(str, t))}"] if t else []
    return " ".join(parts + [f"pvid{state.pvid(port)}"])


def _step_ok(op: Op, got: SwitchState, want: SwitchState) -> bool:
    # Enabling 802.1Q resets the table in firmware-specific ways; only the mode itself is checked, then re-plan.
    return got.dot1q_enabled if op.kind == ENABLE_DOT1Q else got == want


def _verify_reachable(cfg: Config, connect: Connector, p: SwitchPlan, snap: Path, log: Log) -> None:
    """A fresh login per switch proves management access survived; also re-check the final state."""
    for name in cfg.apply_order():
        spec = cfg.switches[name]
        try:
            with connected(connect, spec) as dev:
                check_identity(spec, dev.identity())
                if name == p.name:
                    final = dev.read_state()
                    if not _step_ok(p.ops[-1], final, p.expected[-1]):
                        raise PartialApply(p.name, "final state differs after reconnect: " + _diff(final, p.expected[-1]), snap)
        except (DeviceError, Refused) as e:
            raise PartialApply(p.name, f"after changing {p.name}, {name} failed a fresh login/check: {e}", snap) from None
    log(f"{p.name}: verified; all switches reachable")


def _diff(got: SwitchState, want: SwitchState) -> str:
    out = []
    if got.dot1q_enabled != want.dot1q_enabled:
        out.append(f"802.1Q {'on' if got.dot1q_enabled else 'off'}, expected {'on' if want.dot1q_enabled else 'off'}")
    for vid in sorted(got.vlans.keys() | want.vlans.keys()):
        g, w = got.vlans.get(vid), want.vlans.get(vid)
        if g != w:
            out.append(f"vlan {vid} is {g.to_json() if g else 'absent'}, expected {w.to_json() if w else 'absent'}")
    for port, (g, w) in enumerate(zip(got.pvids, want.pvids, strict=True), start=1):
        if g != w:
            out.append(f"port {port} pvid {g}, expected {w}")
    return "; ".join(out) or "no difference"
