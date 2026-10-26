from dataclasses import replace
from pathlib import Path

import pytest

from tplink_vlan_as_code import snapshot
from tplink_vlan_as_code.config import Config, load
from tplink_vlan_as_code.device import FakeDevice
from tplink_vlan_as_code.snapshot import Identity
from tplink_vlan_as_code.state import SwitchState

EXAMPLE = Path(__file__).parents[1] / "examples" / "example.toml"


@pytest.fixture
def cfg() -> Config:
    return load(EXAMPLE)


def ident(cfg: Config, name: str) -> Identity:
    spec = cfg.switches[name]
    return Identity(f"{spec.model} 3.0", "1.0.0 Build test", spec.mac)


def realize(state: SwitchState) -> SwitchState:
    """A desired state as a real switch would report it: every VLAN has a concrete name."""
    return replace(state, vlans={v: vl if vl.name is not None else replace(vl, name="Default")
                                 for v, vl in state.vlans.items()})


def in_sync_devices(cfg: Config) -> dict[str, FakeDevice]:
    return {n: FakeDevice(realize(cfg.desired(n)), ident(cfg, n)) for n in cfg.switches}


def connector(devices: dict[str, FakeDevice]):
    """Connector over a dict of fakes; close() is a no-op so tests can inspect them afterwards."""
    return lambda spec: devices[spec.name]


@pytest.fixture
def offline_dir(tmp_path, cfg) -> Path:
    """DIR/<switch>.json snapshots of an in-sync network, for --offline CLI runs."""
    d = tmp_path / "offline"
    for n, dev in in_sync_devices(cfg).items():
        snapshot.write(d / f"{n}.json", n, cfg.switches[n].host, dev.identity(), dev.state)
    return d
