"""Access to switches: the real one over HTTP, and an in-memory fake with the same interface.

The real switch is driven through tplink-tool, which scrapes the Easy Smart web UI.
Its writes do not report success or failure, so callers must read state back after
every write; the engine does exactly that.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from . import snapshot
from .plan import DELETE_VLAN, ENABLE_DOT1Q, SET_PVID, SET_VLAN, Op, RuleError, apply_op
from .snapshot import Identity
from .state import SwitchState, Vlan


class DeviceError(Exception):
    """Talking to a switch failed. Messages never contain the password."""


class Device(Protocol):
    def identity(self) -> Identity: ...
    def read_state(self) -> SwitchState: ...
    def apply(self, op: Op) -> None: ...
    def close(self) -> None: ...


class TplinkDevice:
    """A live TP-Link Easy Smart switch. Connecting logs in; `close()` logs out (one admin session at a time)."""

    def __init__(self, host: str, username: str, password: str, timeout: float = 15.0):
        from tplink_tool import make_switch  # imported here so offline use and tests do not need it

        self.host = host
        self._password = password
        with self._errors("login"):
            self._sw = make_switch(host, username=username, password=password, timeout=timeout)

    @contextmanager
    def _errors(self, what: str) -> Iterator[None]:
        try:
            yield
        except Exception as e:  # the library raises RuntimeError, requests errors, parse errors...
            msg = str(e).replace(self._password, "***") if self._password else str(e)
            raise DeviceError(f"{self.host}: {what} failed: {type(e).__name__}: {msg}") from None

    def identity(self) -> Identity:
        with self._errors("reading system info"):
            info = self._sw.get_system_info()
            return Identity(info.hardware.strip(), info.firmware.strip(), info.mac.strip().upper().replace("-", ":"))

    def read_state(self) -> SwitchState:
        with self._errors("reading 802.1Q state"):
            enabled, entries = self._sw.get_dot1q_vlans()
            pvids = tuple(int(p) for p in self._sw.get_pvids())
            n = len(pvids)
            vlans = {
                e.vid: Vlan(e.name, _ports(e.tagged_members, n), _ports(e.untagged_members, n))
                for e in entries
            }
            return SwitchState(bool(enabled), vlans, pvids)

    def apply(self, op: Op) -> None:
        with self._errors(op.describe()):
            if op.kind == ENABLE_DOT1Q:
                self._sw.set_dot1q_enabled(True)
            elif op.kind == SET_VLAN:
                self._sw.add_dot1q_vlan(op.vid, op.name or "", list(op.tagged), list(op.untagged))
            elif op.kind == SET_PVID:
                self._sw.set_pvid([op.port], op.vid)  # one port per call: multi-port bitmasks are unverified upstream
            elif op.kind == DELETE_VLAN:
                self._sw.delete_dot1q_vlan(op.vid)
            else:
                raise ValueError(f"unknown op kind {op.kind!r}")

    def close(self) -> None:
        try:
            self._sw.logout()
        except Exception:  # noqa: S110 - best effort; the switch expires sessions on its own
            pass


class FakeDevice:
    """In-memory switch enforcing the DEVICE RULES in plan.py. Backs `--offline` and the tests.

    With `path`, the final state is written back to that snapshot file on close, so an
    offline apply followed by an offline plan behaves like the real thing.
    """

    def __init__(self, state: SwitchState, ident: Identity, path: Path | None = None, switch: str = "", host: str = ""):
        self.state, self._identity, self._path = state, ident, path
        self._switch, self._host = switch, host
        self.applied: list[Op] = []

    @classmethod
    def from_snapshot(cls, path: Path) -> FakeDevice:
        snap = snapshot.read(path)
        return cls(snap.state, snap.identity, path, snap.switch, snap.host)

    def identity(self) -> Identity:
        return self._identity

    def read_state(self) -> SwitchState:
        return self.state

    def apply(self, op: Op) -> None:
        try:
            self.state = apply_op(self.state, op)
        except RuleError as e:
            raise DeviceError(f"{op.describe()} rejected: {e}") from None
        self.applied.append(op)

    def close(self) -> None:
        if self._path and self.applied:
            snapshot.write(self._path, self._switch, self._host, self._identity, self.state)


def _ports(mask: int, port_count: int) -> frozenset[int]:
    """Port bitmask (bit 0 = port 1) to a set of 1-based port numbers."""
    return frozenset(i + 1 for i in range(port_count) if mask >> i & 1)
