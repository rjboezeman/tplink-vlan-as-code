"""TplinkDevice against a stub of the tplink-tool switch object: translation and error hygiene."""

from types import SimpleNamespace

import pytest

from tplink_vlan_as_code.device import DeviceError, TplinkDevice
from tplink_vlan_as_code.plan import SET_PVID, SET_VLAN, Op


class StubSwitch:
    def __init__(self):
        self.calls = []

    def get_system_info(self):
        return SimpleNamespace(hardware="TL-SG108PE 3.0 ", firmware="1.0.0", mac="00-00-5e-00-53-02")

    def get_dot1q_vlans(self):
        return 1, [SimpleNamespace(vid=1, name="Default", tagged_members=0, untagged_members=0b0110),
                   SimpleNamespace(vid=85, name="lan", tagged_members=0b1001, untagged_members=0)]

    def get_pvids(self):
        return [85, 1, 1, 85]

    def add_dot1q_vlan(self, *a):
        self.calls.append(("add", *a))

    def set_pvid(self, *a):
        raise RuntimeError("Login to 192.0.2.2 failed with password hunter2")

    def logout(self):
        pass


@pytest.fixture
def dev(monkeypatch):
    import tplink_tool

    stub = StubSwitch()
    monkeypatch.setattr(tplink_tool, "make_switch", lambda *a, **k: stub)
    d = TplinkDevice("192.0.2.2", "admin", "hunter2")
    d.stub = stub
    return d


def test_reads_and_translates(dev):
    assert dev.identity().mac == "00:00:5E:00:53:02" and dev.identity().hardware == "TL-SG108PE 3.0"
    s = dev.read_state()
    assert s.dot1q_enabled and s.pvids == (85, 1, 1, 85)
    assert s.vlans[1].untagged == {2, 3} and s.vlans[85].tagged == {1, 4}


def test_writes(dev):
    dev.apply(Op(SET_VLAN, 85, "lan", (1, 4), (2,)))
    assert dev.stub.calls == [("add", 85, "lan", [1, 4], [2])]


def test_errors_never_leak_the_password(dev):
    with pytest.raises(DeviceError) as e:
        dev.apply(Op(SET_PVID, 85, port=2))
    assert "hunter2" not in str(e.value) and "***" in str(e.value)
