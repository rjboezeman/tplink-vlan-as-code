import pytest

from tplink_vlan_as_code.config import ConfigError, load
from tplink_vlan_as_code.state import DEFAULT_VID

from .conftest import EXAMPLE

STUDIO_UPLINK = '8 = { name = "uplink",   tagged = [100, 200, 300, 400], pvid = 1 }'


def load_variant(tmp_path, old: str, new: str):
    text = EXAMPLE.read_text()
    assert old in text, f"test setup: {old!r} not in example"
    p = tmp_path / "net.toml"
    p.write_text(text.replace(old, new, 1))
    return load(p)


def test_example_is_valid(cfg):
    assert cfg.warnings == ()
    assert cfg.apply_order() == ["studio", "workshop", "rack"]  # furthest first
    assert cfg.protected_ports("rack") == {1, 2, 8}
    assert cfg.protected_ports("studio") == {8}


def test_desired_state(cfg):
    rack = cfg.desired("rack")
    assert rack.vlans[100].tagged == {1, 2, 8} and rack.vlans[100].untagged == {7}
    assert rack.vlans[DEFAULT_VID].name is None and not rack.vlans[DEFAULT_VID].tagged | rack.vlans[DEFAULT_VID].untagged
    assert rack.pvids == (1, 1, 200, 200, 400, 1, 100, 1)
    assert set(rack.vlans) == {1, 100, 200, 300, 400}
    assert 300 not in cfg.desired("workshop").vlans  # no port in VLAN 300 there: the switch cannot hold it


@pytest.mark.parametrize("old, new, message", [
    ('name = "staff"', 'name = "staff"\ncolour = "#000000"', "unknown key 'colour'"),
    ('name = "staff"', 'name = "<script>"', "invalid VLAN name"),
    ('name = "staff"', 'name = "averyverylongname"', "invalid VLAN name"),
    ('host = "192.0.2.10"', 'host = "http://192.0.2.10/x"', "IPv4 address or hostname"),
    ('host = "192.0.2.10"', 'host = "2001:db8::10"', "IPv4 address or hostname"),
    ("4 = { untagged = 200 }\n", "", "every port exactly once"),
    ("6 = { untagged = 300 }", "6 = { untagged = 300 }\n10 = { untagged = 300 }\n", "every port exactly once"),
    ('3 = { name = "bench",     untagged = 200 }', "3 = { tagged = [200] }", "pvid"),
    ('4 = { name = "printer",     untagged = 200 }', "4 = { untagged = 77 }", "VLAN 77 is not declared"),
    ('3 = { name = "wifi",     untagged = 300, tagged = [100, 200] }', "3 = { untagged = 300, tagged = [300] }",
     "both untagged and tagged"),
    ("[switches.rack]\nhost", "[switches.Rack-Switch]\nhost", "switch name"),
    ('mac = "00:00:5E:00:53:10"', 'mac = "nope"', "invalid MAC"),
    ('# password_env = "RACK_SW_PW"', 'password_env = "lower"', "environment variable name"),
    ("management_vlan = 100", "management_vlan = 101", "management_vlan"),
    ('upstream = { switch = "rack", port = 2 }', 'upstream = { switch = "rack", port = 1 }', "already used by studio"),
    ('upstream = { switch = "rack", port = 2 }', 'upstream = { switch = "rack", port = 8 }', "own uplink"),
    ('upstream = { switch = "rack", port = 2 }', 'upstream = { switch = "nas", port = 2 }', "unknown upstream"),
    ("uplink_port = 8                # towards the firewall",
     'uplink_port = 8\nupstream = { switch = "workshop", port = 7 }', "loops"),
    # studio uplink without the management VLAN: unreachable, and the cable ends disagree
    (STUDIO_UPLINK, STUDIO_UPLINK.replace("100, ", ""), "must be a member of management VLAN 100"),
])
def test_rejects(tmp_path, old, new, message):
    with pytest.raises(ConfigError) as e:
        load_variant(tmp_path, old, new)
    assert any(message in p for p in e.value.problems), e.value.problems


def test_non_management_mismatch_is_a_warning(tmp_path):
    cfg = load_variant(tmp_path, STUDIO_UPLINK, STUDIO_UPLINK.replace("300, ", ""))
    assert cfg.warnings == ("link rack:1 <-> studio:8: VLAN 300 is T on rack, absent on studio",)


def test_missing_mac_pin_warns(tmp_path):
    cfg = load_variant(tmp_path, 'mac = "00:00:5E:00:53:10"', "")
    assert any("no 'mac' pinned" in w for w in cfg.warnings)


def test_reports_all_problems_at_once(tmp_path):
    p = tmp_path / "net.toml"
    p.write_text('management_vlan = 5\nbogus = 1\n[vlans.0]\nname = "x"\n[switches]\n')
    with pytest.raises(ConfigError) as e:
        load(p)
    assert len(e.value.problems) >= 4


def test_pvid_must_name_a_vlan_present_on_the_switch(tmp_path):
    with pytest.raises(ConfigError) as e:  # workshop has no port in VLAN 300
        load_variant(tmp_path, "7 = { pvid = 1 }", "7 = { pvid = 300 }")
    assert any("switches.workshop.ports.7.pvid" in p and "cannot hold an empty VLAN" in p for p in e.value.problems)


def test_all_vlans_on_trunks(tmp_path):
    cfg = load_variant(tmp_path, "# all_vlans_on_trunks = true", "all_vlans_on_trunks = true")
    ws, rack = cfg.desired("workshop"), cfg.desired("rack")
    assert ws.vlans[300].tagged == {8} and not ws.vlans[300].untagged       # uplink now carries guest
    assert rack.vlans[300].tagged == {1, 2, 8}                              # and so does the downlink
    assert rack.vlans[200].untagged == {3, 4} and 1 not in rack.vlans[200].untagged
    assert cfg.warnings == ()  # both cable ends expanded the same way
    # the router uplink (rack:8) is not a switch-to-switch trunk and is left as written
    assert cfg.switches["rack"].ports[7].tagged == (100, 200, 300, 400)


def test_all_vlans_on_trunks_must_be_bool(tmp_path):
    with pytest.raises(ConfigError, match="all_vlans_on_trunks"):
        load_variant(tmp_path, "# all_vlans_on_trunks = true", 'all_vlans_on_trunks = "yes"')
