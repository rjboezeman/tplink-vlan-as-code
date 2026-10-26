"""Snapshot files: one switch's identity and complete 802.1Q state, as JSON.

Written automatically before every apply (the rollback point), by the `snapshot`
command, and used as the switch stand-in for `--offline` runs.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .state import Link, SwitchState

FORMAT = "tplink-vlan-as-code/snapshot/v1"


@dataclass(frozen=True)
class Identity:
    hardware: str  # e.g. "TL-SG108PE 3.0"
    firmware: str
    mac: str       # upper-case, colon separated

    def to_json(self) -> dict[str, str]:
        return {"hardware": self.hardware, "firmware": self.firmware, "mac": self.mac}


@dataclass(frozen=True)
class Snapshot:
    switch: str
    host: str
    taken_at: str
    identity: Identity
    state: SwitchState
    links: tuple[Link, ...] | None = None  # informational; absent in older or hand-made files


def write(path: Path, switch: str, host: str, identity: Identity, state: SwitchState,
          links: tuple[Link, ...] | None = None) -> Path:
    """Write atomically with mode 0600 (directory 0700): a half-written rollback point is worse than none."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    doc = {
        "format": FORMAT,
        "switch": switch,
        "host": host,
        "taken_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "identity": identity.to_json(),
        "state": state.to_json(),
    }
    if links is not None:
        doc["links"] = [link.to_json() for link in links]
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(doc, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
    return path


def read(path: Path) -> Snapshot:
    """Parse a snapshot file. Raises ValueError with the file name on anything malformed."""
    try:
        doc = json.loads(path.read_text())
        if not isinstance(doc, dict) or doc.get("format") != FORMAT:
            raise ValueError(f"not a {FORMAT} file")
        ident = doc.get("identity")
        if not isinstance(ident, dict) or not all(isinstance(ident.get(k), str) for k in ("hardware", "firmware", "mac")):
            raise ValueError("identity must have string hardware, firmware and mac")
        snap = Snapshot(
            switch=str(doc.get("switch", "")),
            host=str(doc.get("host", "")),
            taken_at=str(doc.get("taken_at", "")),
            identity=Identity(ident["hardware"], ident["firmware"], ident["mac"].upper()),
            state=SwitchState.from_json(doc.get("state")),
            links=_links(doc.get("links")),
        )
        if snap.links is not None and len(snap.links) != snap.state.port_count:
            raise ValueError("links must have one entry per port")
        return snap
    except (OSError, ValueError) as e:
        raise ValueError(f"{path}: {e}") from None


def timestamped(directory: Path, switch: str) -> Path:
    return directory / f"{switch}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"


def _links(raw) -> tuple[Link, ...] | None:
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ValueError("links must be a list")
    return tuple(Link.from_json(x) for x in raw)
