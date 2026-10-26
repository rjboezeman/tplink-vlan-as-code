"""The 802.1Q state of one switch, as a plain value.

Desired state (built from the config file) and actual state (read from a switch
or a snapshot file) are the same type, so planning is a pure function of two
values and can be tested without hardware.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MIN_VID, MAX_VID = 1, 4094
DEFAULT_VID = 1  # the switch's built-in VLAN; it can be emptied but never deleted


@dataclass(frozen=True)
class Vlan:
    """One VLAN on one switch. `name=None` means "leave the name as it is on the switch"."""

    name: str | None
    tagged: frozenset[int] = frozenset()
    untagged: frozenset[int] = frozenset()

    def mode(self, port: int) -> str | None:
        """'T' (tagged member), 'U' (untagged member) or None (not a member)."""
        if port in self.tagged:
            return "T"
        if port in self.untagged:
            return "U"
        return None

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "tagged": sorted(self.tagged), "untagged": sorted(self.untagged)}


@dataclass(frozen=True)
class Link:
    """Live status of one port: administratively enabled, and the negotiated speed (None = no link)."""

    enabled: bool
    speed: str | None

    def to_json(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "speed": self.speed}

    @classmethod
    def from_json(cls, d: Any) -> Link:
        if not (isinstance(d, dict) and isinstance(d.get("enabled"), bool) and (d.get("speed") is None or isinstance(d.get("speed"), str))):
            raise ValueError("link must be {enabled: bool, speed: string or null}")
        return cls(d["enabled"], d["speed"])


@dataclass(frozen=True)
class SwitchState:
    """Complete 802.1Q picture of a switch. Ports are numbered from 1; `pvids[0]` is port 1."""

    dot1q_enabled: bool
    vlans: dict[int, Vlan]
    pvids: tuple[int, ...]

    @property
    def port_count(self) -> int:
        return len(self.pvids)

    def pvid(self, port: int) -> int:
        return self.pvids[port - 1]

    def to_json(self) -> dict[str, Any]:
        return {
            "dot1q_enabled": self.dot1q_enabled,
            "vlans": {str(vid): v.to_json() for vid, v in sorted(self.vlans.items())},
            "pvids": list(self.pvids),
        }

    @classmethod
    def from_json(cls, data: Any) -> SwitchState:
        """Parse untrusted JSON (snapshot files). Raises ValueError on anything malformed."""
        if not isinstance(data, dict):
            raise ValueError("state must be an object")
        enabled = data.get("dot1q_enabled")
        pvids = data.get("pvids")
        raw_vlans = data.get("vlans")
        if not isinstance(enabled, bool):
            raise ValueError("dot1q_enabled must be true or false")
        if not isinstance(pvids, list) or not pvids or not all(_is_vid(p) for p in pvids):
            raise ValueError("pvids must be a non-empty list of VLAN IDs")
        if not isinstance(raw_vlans, dict):
            raise ValueError("vlans must be an object keyed by VLAN ID")
        ports = range(1, len(pvids) + 1)
        vlans = {}
        for key, v in raw_vlans.items():
            if not (isinstance(key, str) and key.isdigit() and _is_vid(int(key))):
                raise ValueError(f"invalid VLAN ID {key!r}")
            if not isinstance(v, dict) or not (v.get("name") is None or isinstance(v.get("name"), str)):
                raise ValueError(f"VLAN {key}: invalid entry")
            members = {}
            for kind in ("tagged", "untagged"):
                lst = v.get(kind, [])
                if not isinstance(lst, list) or not all(isinstance(p, int) and p in ports for p in lst):
                    raise ValueError(f"VLAN {key}: {kind} must list ports 1..{len(pvids)}")
                members[kind] = frozenset(lst)
            if members["tagged"] & members["untagged"]:
                raise ValueError(f"VLAN {key}: a port cannot be both tagged and untagged")
            vlans[int(key)] = Vlan(v.get("name"), members["tagged"], members["untagged"])
        return cls(enabled, vlans, tuple(pvids))


def _is_vid(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool) and MIN_VID <= x <= MAX_VID
