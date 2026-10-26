"""Access to switches: the real one over HTTP, and an in-memory fake with the same interface.

The real switch is driven through its web UI (see webui.py). Writes do not report
success or failure, so callers must read state back after every write; the engine
does exactly that.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from . import snapshot
from .plan import DELETE_VLAN, ENABLE_DOT1Q, SET_PVID, SET_VLAN, Op, RuleError, apply_op
from .snapshot import Identity
from .state import Link, SwitchState, Vlan
from .webui import WebUI, WebUIError


class DeviceError(Exception):
    """Talking to a switch failed. Messages never contain the password."""


class Device(Protocol):
    def identity(self) -> Identity: ...
    def read_state(self) -> SwitchState: ...
    def links(self) -> tuple[Link, ...] | None: ...
    def apply(self, op: Op) -> None: ...
    def close(self) -> None: ...


SPEEDS = {2: "10M half", 3: "10M full", 4: "100M half", 5: "100M full", 6: "1000M full"}


class TplinkDevice:
    """A live TP-Link Easy Smart switch. Connecting logs in; `close()` logs out (one admin session at a time)."""

    def __init__(self, host: str, username: str, password: str, timeout: float = 15.0, port: int = 80):
        self.host = host
        self._password = password
        self._port_count: int | None = None
        with self._errors("login"):
            self._ui = WebUI(host, username, password, timeout, port)

    @contextmanager
    def _errors(self, what: str) -> Iterator[None]:
        try:
            yield
        except WebUIError as e:
            msg = str(e).replace(self._password, "***") if self._password else str(e)
            raise DeviceError(f"{self.host}: {what} failed: {msg}") from None

    def identity(self) -> Identity:
        with self._errors("reading system info"):
            info = self._ui.system_info()
            return Identity(info["hardwareStr"], info["firmwareStr"], info["macStr"].upper().replace("-", ":"))

    def read_state(self) -> SwitchState:
        with self._errors("reading 802.1Q state"):
            enabled, rows = self._ui.dot1q()
            pvids = tuple(self._ui.pvids())
        n = self._port_count = len(pvids)
        vlans = {vid: Vlan(name, _ports(tagged, n), _ports(untagged, n)) for vid, name, tagged, untagged in rows}
        return SwitchState(enabled, vlans, pvids)

    def links(self) -> tuple[Link, ...]:
        with self._errors("reading port status"):
            return tuple(Link(enabled, None if code == 0 else SPEEDS.get(code, f"speed code {code}"))
                         for enabled, code in self._ui.port_status())

    def apply(self, op: Op) -> None:
        with self._errors(op.describe()):
            if op.kind == ENABLE_DOT1Q:
                self._ui.enable_dot1q()
            elif op.kind == SET_VLAN:
                if self._port_count is None:
                    self._port_count = len(self._ui.pvids())
                self._ui.set_vlan(op.vid, op.name or "", list(op.tagged), list(op.untagged), self._port_count)
            elif op.kind == SET_PVID:
                self._ui.set_pvid(op.port, op.vid)
            elif op.kind == DELETE_VLAN:
                self._ui.delete_vlan(op.vid)
            else:
                raise ValueError(f"unknown op kind {op.kind!r}")

    def close(self) -> None:
        try:
            self._ui.logout()
        except WebUIError:
            pass  # best effort; the switch expires sessions on its own


class FakeDevice:
    """In-memory switch enforcing the DEVICE RULES in plan.py. Backs `--offline` and the tests.

    With `path`, the final state is written back to that snapshot file on close, so an
    offline apply followed by an offline plan behaves like the real thing.
    """

    def __init__(self, state: SwitchState, ident: Identity, path: Path | None = None, switch: str = "", host: str = "",
                 links: tuple[Link, ...] | None = None):
        self.state, self._identity, self._path, self._links = state, ident, path, links
        self._switch, self._host = switch, host
        self.applied: list[Op] = []

    @classmethod
    def from_snapshot(cls, path: Path) -> FakeDevice:
        snap = snapshot.read(path)
        return cls(snap.state, snap.identity, path, snap.switch, snap.host, snap.links)

    def identity(self) -> Identity:
        return self._identity

    def read_state(self) -> SwitchState:
        return self.state

    def links(self) -> tuple[Link, ...] | None:
        return self._links

    def apply(self, op: Op) -> None:
        try:
            self.state = apply_op(self.state, op)
        except RuleError as e:
            raise DeviceError(f"{op.describe()} rejected: {e}") from None
        self.applied.append(op)

    def close(self) -> None:
        if self._path and self.applied:
            snapshot.write(self._path, self._switch, self._host, self._identity, self.state, self._links)


def _ports(mask: int, port_count: int) -> frozenset[int]:
    """Port bitmask (bit 0 = port 1) to a set of 1-based port numbers."""
    return frozenset(i + 1 for i in range(port_count) if mask >> i & 1)
