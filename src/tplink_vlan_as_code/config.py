"""Load and validate the desired-state config file (TOML).

The config is the trust boundary for everything the tool sends to a switch, so
validation is strict: unknown keys are errors (catches typos), every value is
checked against a narrow pattern, and all problems are reported at once.
See examples/example.toml for the format.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .plan import MAX_VLANS
from .state import DEFAULT_VID, MAX_VID, MIN_VID, SwitchState, Vlan

# Names are sent to the switch and shown in its web UI: keep them to a charset that cannot inject markup.
VLAN_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,10}$")
SWITCH_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")  # also used in snapshot file names
HOSTNAME = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
MODEL = re.compile(r"^TL-[A-Z0-9-]{2,20}$")
MAC = re.compile(r"^[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}$")
USERNAME = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
ENV_VAR = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
MAX_PORTS = 52


class ConfigError(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("invalid config:\n  " + "\n  ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class VlanSpec:
    vid: int
    name: str
    color: str | None = None
    description: str = ""


@dataclass(frozen=True)
class PortSpec:
    number: int
    name: str
    untagged: int | None
    tagged: tuple[int, ...]
    pvid: int


@dataclass(frozen=True)
class SwitchSpec:
    name: str
    host: str
    model: str
    mac: str | None  # normalised to upper-case with colons
    username: str
    password_envs: tuple[str, ...]  # environment variables to try, in order; the first one set wins
    uplink_port: int
    upstream: tuple[str, int] | None  # (parent switch, parent port)
    ports: tuple[PortSpec, ...]


@dataclass(frozen=True)
class Config:
    path: Path
    sha256: str
    title: str
    management_vlan: int
    vlans: dict[int, VlanSpec]
    switches: dict[str, SwitchSpec]
    warnings: tuple[str, ...]

    def desired(self, switch: str) -> SwitchState:
        """A VLAN exists on a switch only where it has at least one port (the firmware cannot hold an
        empty VLAN); VLAN 1 always exists. Membership comes from the port table. A port in no VLAN
        is untagged in VLAN 1, because the switch puts it there (device rule 8)."""
        spec = self.switches[switch]
        vids = {v for p in spec.ports for v in (*p.tagged, p.untagged) if v is not None} | {DEFAULT_VID}
        tagged = {v: set() for v in vids}
        untagged = {v: set() for v in vids}
        for p in spec.ports:
            for v in p.tagged:
                tagged[v].add(p.number)
            if p.untagged is not None:
                untagged[p.untagged].add(p.number)
            if p.untagged is None and not p.tagged:
                untagged[DEFAULT_VID].add(p.number)
        vlans = {
            v: Vlan(self.vlans[v].name if v in self.vlans else None, frozenset(tagged[v]), frozenset(untagged[v]))
            for v in vids
        }
        return SwitchState(True, vlans, tuple(p.pvid for p in spec.ports))

    def children(self, switch: str) -> list[SwitchSpec]:
        return [s for s in self.switches.values() if s.upstream and s.upstream[0] == switch]

    def protected_ports(self, switch: str) -> set[int]:
        """Ports on `switch` that carry management traffic: its uplink plus downlinks to other switches."""
        return {self.switches[switch].uplink_port} | {c.upstream[1] for c in self.children(switch)}

    def depth(self, switch: str) -> int:
        up = self.switches[switch].upstream
        return 0 if up is None else 1 + self.depth(up[0])

    def apply_order(self, only: list[str] | None = None) -> list[str]:
        """Furthest switches first, so a mistake never cuts the path to a switch not yet done."""
        names = [n for n in self.switches if not only or n in only]
        return sorted(names, key=lambda n: (-self.depth(n), n))


def load(path: str | Path) -> Config:
    path = Path(path)
    raw = path.read_bytes()
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise ConfigError([f"{path}: {e}"]) from None
    p = _Parser()
    cfg = p.parse(data, path, hashlib.sha256(raw).hexdigest())
    if p.problems:
        raise ConfigError(p.problems)
    return cfg


class _Parser:
    def __init__(self):
        self.problems: list[str] = []
        self.warnings: list[str] = []

    def err(self, where: str, msg: str) -> None:
        self.problems.append(f"{where}: {msg}")

    def keys(self, where: str, table: Any, allowed: set[str], required: set[str] = frozenset()) -> bool:
        if not isinstance(table, dict):
            self.err(where, "must be a table")
            return False
        for k in sorted(set(table) - allowed):
            self.err(where, f"unknown key {k!r} (allowed: {', '.join(sorted(allowed))})")
        for k in sorted(required - set(table)):
            self.err(where, f"missing required key {k!r}")
        return True

    def string(self, where: str, value: Any, pattern: re.Pattern, what: str) -> str | None:
        if isinstance(value, str) and pattern.match(value):
            return value
        self.err(where, f"invalid {what}: {value!r}")
        return None

    def parse(self, data: dict, path: Path, sha256: str) -> Config:
        self.keys("config", data, {"title", "management_vlan", "all_vlans_on_trunks", "vlans", "switches"},
                  {"management_vlan", "vlans", "switches"})
        all_on_trunks = data.get("all_vlans_on_trunks", False)
        if not isinstance(all_on_trunks, bool):
            self.err("all_vlans_on_trunks", "must be true or false")
            all_on_trunks = False
        title = data.get("title", "")
        if not isinstance(title, str) or len(title) > 120:
            self.err("title", "must be a string of at most 120 characters")
            title = ""
        vlans = self.parse_vlans(data.get("vlans", {}))
        mgmt = data.get("management_vlan")
        if mgmt not in vlans:
            self.err("management_vlan", f"{mgmt!r} must be one of the declared VLANs")
        switches = {}
        raw_switches = data.get("switches", {})
        if not isinstance(raw_switches, dict) or not raw_switches:
            self.err("switches", "declare at least one switch")
            raw_switches = {}
        for name, raw in raw_switches.items():
            spec = self.parse_switch(name, raw, vlans)
            if spec:
                switches[name] = spec
        if not self.problems:
            if all_on_trunks:
                switches = self.expand_trunks(switches, vlans)
            self.check_topology(switches, mgmt)
            self.check_pvids(switches)
        return Config(path, sha256, title, mgmt, vlans, switches, tuple(self.warnings))

    def parse_vlans(self, raw: Any) -> dict[int, VlanSpec]:
        vlans: dict[int, VlanSpec] = {}
        if not isinstance(raw, dict):
            self.err("vlans", "must be a table keyed by VLAN ID")
            return vlans
        for key, v in raw.items():
            where = f"vlans.{key}"
            if not (key.isdigit() and MIN_VID <= int(key) <= MAX_VID):
                self.err(where, f"VLAN ID must be {MIN_VID}..{MAX_VID}")
                continue
            if not self.keys(where, v, {"name", "color", "description"}, {"name"}):
                continue
            name = self.string(f"{where}.name", v.get("name"), VLAN_NAME, "VLAN name (1-10 of A-Z a-z 0-9 _ . -)")
            color = v.get("color")
            if color is not None:
                color = self.string(f"{where}.color", color, COLOR, "colour (#RRGGBB)")
            desc = v.get("description", "")
            if not isinstance(desc, str):
                self.err(f"{where}.description", "must be a string")
                desc = ""
            if name:
                vlans[int(key)] = VlanSpec(int(key), name, color, desc)
        if len(set(vlans) | {DEFAULT_VID}) > MAX_VLANS:
            self.err("vlans", f"at most {MAX_VLANS} VLANs including VLAN 1")
        return vlans

    def parse_switch(self, name: str, raw: Any, vlans: dict[int, VlanSpec]) -> SwitchSpec | None:
        where = f"switches.{name}"
        n_before = len(self.problems)
        if not SWITCH_NAME.match(name):
            self.err(where, "switch name must be 1-32 of a-z 0-9 _ - (starting with a letter or digit)")
        allowed = {"host", "model", "mac", "username", "password_env", "uplink_port", "upstream", "ports"}
        if not self.keys(where, raw, allowed, {"host", "model", "uplink_port", "ports"}):
            return None
        host = raw.get("host")
        if not _valid_host(host):
            self.err(f"{where}.host", f"must be an IPv4 address or hostname without scheme, port or path: {host!r}")
        model = self.string(f"{where}.model", raw.get("model"), MODEL, "model (e.g. TL-SG108PE)")
        mac = raw.get("mac")
        if mac is not None:
            mac = self.string(f"{where}.mac", mac, MAC, "MAC address")
            mac = mac and mac.upper().replace("-", ":")
        else:
            self.warnings.append(f"{where}: no 'mac' pinned; the tool cannot tell this switch from an impostor at {host}")
        username = self.string(f"{where}.username", raw.get("username", "admin"), USERNAME, "username")
        if "password_env" in raw:
            password_envs = (self.string(f"{where}.password_env", raw["password_env"], ENV_VAR, "environment variable name"),)
        else:  # per-switch password if set, else the shared one
            password_envs = ("TPLINK_PASSWORD_" + name.upper().replace("-", "_"), "TPLINK_PASSWORD")
        ports = self.parse_ports(where, raw.get("ports"), vlans)
        uplink = raw.get("uplink_port")
        if not (isinstance(uplink, int) and 1 <= uplink <= len(ports)):
            self.err(f"{where}.uplink_port", f"must be a port number 1..{len(ports)}")
        upstream = raw.get("upstream")
        if upstream is not None:
            if self.keys(f"{where}.upstream", upstream, {"switch", "port"}, {"switch", "port"}):
                upstream = (upstream.get("switch"), upstream.get("port"))
                if not isinstance(upstream[0], str):
                    self.err(f"{where}.upstream.switch", "must be a switch name")
            else:
                upstream = None
        if len(self.problems) > n_before:
            return None
        return SwitchSpec(name, host, model, mac, username, password_envs, uplink, upstream, ports)

    def parse_ports(self, where: str, raw: Any, vlans: dict[int, VlanSpec]) -> tuple[PortSpec, ...]:
        where = f"{where}.ports"
        if not isinstance(raw, dict) or not raw:
            self.err(where, "must be a table with one entry per port")
            return ()
        numbers = sorted(int(k) for k in raw if k.isdigit())
        if numbers != list(range(1, len(raw) + 1)) or len(raw) > MAX_PORTS:
            self.err(where, "must declare every port exactly once, numbered 1..N (unused ports too)")
            return ()
        declared = set(vlans) | {DEFAULT_VID}
        ports = []
        for n in numbers:
            pw, p = f"{where}.{n}", raw[str(n)]
            if not self.keys(pw, p, {"name", "untagged", "tagged", "pvid"}):
                continue
            name = p.get("name", "")
            if not isinstance(name, str) or len(name) > 40:
                self.err(f"{pw}.name", "must be a string of at most 40 characters")
                name = ""
            untagged = p.get("untagged")
            if untagged is not None and untagged not in declared:
                self.err(f"{pw}.untagged", f"VLAN {untagged!r} is not declared")
            tagged = p.get("tagged", [])
            if not isinstance(tagged, list) or any(t not in declared for t in tagged) or len(set(tagged)) != len(tagged):
                self.err(f"{pw}.tagged", "must be a list of distinct declared VLANs")
                tagged = []
            if untagged in tagged:
                self.err(pw, f"VLAN {untagged} cannot be both untagged and tagged")
            pvid = p.get("pvid", untagged)
            if pvid is None:
                self.err(f"{pw}.pvid", "required when the port has no untagged VLAN")
            elif pvid not in declared:
                self.err(f"{pw}.pvid", f"VLAN {pvid!r} is not declared")
            ports.append(PortSpec(n, name, untagged, tuple(tagged), pvid))
        return tuple(ports)

    def expand_trunks(self, switches: dict[str, SwitchSpec], vlans: dict[int, VlanSpec]) -> dict[str, SwitchSpec]:
        """all_vlans_on_trunks: both ends of every switch-to-switch cable carry every declared VLAN, tagged
        (unless it is untagged there), so every VLAN exists on every switch. Router uplinks are left alone."""
        out = dict(switches)
        for s in switches.values():
            if not s.upstream:
                continue
            parent, pport = s.upstream
            if parent not in out or not (isinstance(pport, int) and 1 <= pport <= len(out[parent].ports)):
                continue  # check_topology reports it
            for name, number in ((s.name, s.uplink_port), (parent, pport)):
                sw = out[name]
                p = sw.ports[number - 1]
                tagged = tuple(sorted(set(p.tagged) | {v for v in vlans if v != p.untagged}))
                ports = list(sw.ports)
                ports[number - 1] = replace(p, tagged=tagged)
                out[name] = replace(sw, ports=tuple(ports))
        return out

    def check_pvids(self, switches: dict[str, SwitchSpec]) -> None:
        """A PVID must name a VLAN that exists on that switch, i.e. has at least one port there (or VLAN 1)."""
        for s in switches.values():
            present = {v for p in s.ports for v in (*p.tagged, p.untagged) if v is not None} | {DEFAULT_VID}
            for p in s.ports:
                if p.pvid not in present:
                    self.err(f"switches.{s.name}.ports.{p.number}.pvid",
                             f"VLAN {p.pvid} has no port on {s.name}, and the switch cannot hold an empty VLAN; "
                             "give it a port, or use pvid = 1 (VLAN 1 always exists; route nothing to it)")

    def check_topology(self, switches: dict[str, SwitchSpec], mgmt: int) -> None:
        used_parent_ports = {}
        for s in switches.values():
            uplink = s.ports[s.uplink_port - 1]
            if mgmt not in uplink.tagged and uplink.untagged != mgmt:
                self.err(f"switches.{s.name}.uplink_port",
                         f"port {s.uplink_port} must be a member of management VLAN {mgmt}, or the switch is unreachable")
            if s.upstream is None:
                continue
            parent_name, parent_port = s.upstream
            parent = switches.get(parent_name)
            where = f"switches.{s.name}.upstream"
            if parent is None or parent_name == s.name:
                self.err(where, f"unknown upstream switch {parent_name!r}")
                continue
            if not (isinstance(parent_port, int) and 1 <= parent_port <= len(parent.ports)):
                self.err(where, f"port must be 1..{len(parent.ports)} on {parent_name}")
                continue
            if parent_port == parent.uplink_port:
                self.err(where, f"port {parent_port} is {parent_name}'s own uplink")
            if (parent_name, parent_port) in used_parent_ports:
                self.err(where, f"{parent_name} port {parent_port} is already used by {used_parent_ports[(parent_name, parent_port)]}")
            used_parent_ports[(parent_name, parent_port)] = s.name
            self.check_link(s, uplink, parent, parent.ports[parent_port - 1], mgmt)
        for name in switches:  # cycle check: walk upstream at most len(switches) steps
            seen, cur = set(), name
            while cur is not None and cur in switches and cur not in seen:
                seen.add(cur)
                up = switches[cur].upstream
                cur = up[0] if up else None
            if cur in seen:
                self.err(f"switches.{name}.upstream", "upstream chain loops back on itself")
                return

    def check_link(self, child: SwitchSpec, cport: PortSpec, parent: SwitchSpec, pport: PortSpec, mgmt: int) -> None:
        """Both ends of a switch-to-switch cable should carry the same VLANs the same way."""
        def mode(p: PortSpec, v: int) -> str | None:
            return "U" if p.untagged == v else "T" if v in p.tagged else None
        link = f"{parent.name}:{pport.number} <-> {child.name}:{cport.number}"
        vids = {v for p in (cport, pport) for v in (*p.tagged, p.untagged) if v is not None}
        for v in sorted(vids):
            a, b = mode(pport, v), mode(cport, v)
            if a == b:
                continue
            msg = f"link {link}: VLAN {v} is {a or 'absent'} on {parent.name}, {b or 'absent'} on {child.name}"
            if v == mgmt:
                self.err(f"switches.{child.name}", msg + " (management VLAN must match on both ends)")
            else:
                self.warnings.append(msg)


def _valid_host(host: Any) -> bool:
    if not isinstance(host, str):
        return False
    try:
        return ipaddress.ip_address(host).version == 4  # the switch client builds http://{host}/ unbracketed
    except ValueError:
        return bool(HOSTNAME.match(host))
