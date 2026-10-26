import pytest

from tplink_vlan_as_code.config import ConfigError, load
from tplink_vlan_as_code.state import DEFAULT_VID

from .conftest import EXAMPLE


def load_variant(tmp_path, old: str, new: str):
    text = EXAMPLE.read_text()
    assert old in text, f"test setup: {old!r} not in example"
    p = tmp_path / "net.toml"
    p.write_text(text.replace(old, new, 1))
    return load(p)


def test_example_is_valid(cfg):
    assert cfg.warnings == ()
    assert cfg.apply_order() == ["attic", "office", "core"]  # furthest first
    assert cfg.protected_ports("core") == {1, 3, 4}
    assert cfg.protected_ports("office") == {1}


def test_desired_state(cfg):
    core = cfg.desired("core")
    assert core.vlans[85].tagged == {1, 3, 4} and core.vlans[85].untagged == {6}
    assert core.vlans[DEFAULT_VID].name is None and not core.vlans[DEFAULT_VID].tagged | core.vlans[DEFAULT_VID].untagged
    assert core.pvids == (4000, 10, 4000, 4000, 4000, 85, 90, 999)
    assert set(core.vlans) == {1, 10, 50, 85, 90, 999, 4000}  # every VLAN on every switch


@pytest.mark.parametrize("old, new, message", [
    ('name = "wifi"', 'name = "wifi"\ncolour = "#000000"', "unknown key 'colour'"),
    ('name = "wifi"', 'name = "<script>"', "invalid VLAN name"),
    ('name = "wifi"', 'name = "averyverylongname"', "invalid VLAN name"),
    ('host = "192.0.2.2"', 'host = "http://192.0.2.2/x"', "IPv4 address or hostname"),
    ('host = "192.0.2.2"', 'host = "2001:db8::2"', "IPv4 address or hostname"),
    ("5 = { untagged = 85 }\n", "", "every port exactly once"),
    ("6 = { untagged = 85 }", "6 = { untagged = 85 }\n10 = { untagged = 85 }\n", "every port exactly once"),
    ('3 = { name = "TV",      untagged = 90 }', '3 = { tagged = [90] }', "pvid"),
    ('7 = { name = "printer", untagged = 90 }', '7 = { untagged = 77 }', "VLAN 77 is not declared"),
    ('2 = { name = "AP",      untagged = 50, tagged = [10, 85] }', '2 = { untagged = 50, tagged = [50] }', "both untagged and tagged"),
    ('[switches.core]\nhost', '[switches.Core-Switch]\nhost', "switch name"),
    ('mac = "00:00:5E:00:53:02"', 'mac = "nope"', "invalid MAC"),
    ('# password_env = "TPLINK_PASSWORD"', 'password_env = "lower"', "environment variable name"),
    ("management_vlan = 85", "management_vlan = 86", "management_vlan"),
    ('upstream = { switch = "core", port = 4 }', 'upstream = { switch = "core", port = 3 }', "already used by office"),
    ('upstream = { switch = "core", port = 4 }', 'upstream = { switch = "core", port = 1 }', "own uplink"),
    ('upstream = { switch = "core", port = 4 }', 'upstream = { switch = "nas", port = 4 }', "unknown upstream"),
    ('uplink_port = 1                # towards the router', 'uplink_port = 1\nupstream = { switch = "attic", port = 2 }', "loops"),
    # office uplink without the management VLAN: unreachable, and the cable ends disagree
    ('1 = { name = "uplink",  tagged = [10, 50, 85, 90, 999], pvid = 4000 }',
     '1 = { name = "uplink",  tagged = [10, 50, 90, 999], pvid = 4000 }', "must be a member of management VLAN 85"),
])
def test_rejects(tmp_path, old, new, message):
    with pytest.raises(ConfigError) as e:
        load_variant(tmp_path, old, new)
    assert any(message in p for p in e.value.problems), e.value.problems


def test_non_management_mismatch_is_a_warning(tmp_path):
    cfg = load_variant(tmp_path, '1 = { name = "uplink",  tagged = [10, 50, 85, 90, 999], pvid = 4000 }',
                       '1 = { name = "uplink",  tagged = [10, 50, 85, 999], pvid = 4000 }')
    assert cfg.warnings == ("link core:3 <-> office:1: VLAN 90 is T on core, absent on office",)


def test_missing_mac_pin_warns(tmp_path):
    cfg = load_variant(tmp_path, 'mac = "00:00:5E:00:53:02"', "")
    assert any("no 'mac' pinned" in w for w in cfg.warnings)


def test_reports_all_problems_at_once(tmp_path):
    p = tmp_path / "net.toml"
    p.write_text('management_vlan = 5\nbogus = 1\n[vlans.0]\nname = "x"\n[switches]\n')
    with pytest.raises(ConfigError) as e:
        load(p)
    assert len(e.value.problems) >= 4
