import pytest

from tplink_vlan_as_code.state import SwitchState, Vlan


def test_json_roundtrip():
    s = SwitchState(True, {1: Vlan("Default"), 100: Vlan("mgmt", frozenset({1}), frozenset({2, 3}))}, (1, 100, 100))
    assert SwitchState.from_json(s.to_json()) == s


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d.update(dot1q_enabled="yes"), "dot1q_enabled"),
    (lambda d: d.update(pvids=[1, True]), "pvids"),
    (lambda d: d.update(pvids=[1, 5000]), "pvids"),
    (lambda d: d["vlans"].update({"abc": {"name": "x"}}), "invalid VLAN ID"),
    (lambda d: d["vlans"]["100"].update(tagged=[9]), "ports 1..3"),
    (lambda d: d["vlans"]["100"].update(tagged=[2]), "both tagged and untagged"),
    (lambda d: d["vlans"]["100"].update(name=7), "invalid entry"),
])
def test_from_json_rejects_malformed(mutate, message):
    d = SwitchState(True, {100: Vlan("mgmt", untagged=frozenset({2, 3}))}, (1, 100, 100)).to_json()
    mutate(d)
    with pytest.raises(ValueError, match=message):
        SwitchState.from_json(d)
