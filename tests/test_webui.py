"""The HTTP client and TplinkDevice, end to end over real sockets against the switch emulator."""

from dataclasses import replace

import pytest

from tplink_vlan_as_code.device import DeviceError, TplinkDevice
from tplink_vlan_as_code.engine import build_plans, execute, link_report, preflight
from tplink_vlan_as_code.plan import DELETE_VLAN, ENABLE_DOT1Q, SET_PVID, SET_VLAN, Op
from tplink_vlan_as_code.state import Link, SwitchState, Vlan
from tplink_vlan_as_code.webui import WebUIError, extract_var

from .conftest import realize
from .emulator import SwitchEmulator

BASE = SwitchState(True, {1: Vlan("Default", untagged=frozenset(range(1, 9)))}, (1,) * 8)


@pytest.fixture
def emu():
    e = SwitchEmulator(BASE)
    yield e
    e.stop()


def connect(emu, password="hunter2"):
    return TplinkDevice("127.0.0.1", "admin", password, timeout=5, port=emu.port)


# -- parser

@pytest.mark.parametrize("text, expected", [
    ("var a = {x:1, 'y':\"two\", z:[0x0f, -3, 1.5, true, null]};", {"x": 1, "y": "two", "z": [15, -3, 1.5, True, None]}),
    ("var a = new Array(\n0,\n'b' // trailing comment\n);", [0, "b"]),
    ("var a = /* c */ {k:[1,2,],};", {"k": [1, 2]}),
    ("var a = 'it\\'s \\x41\\u0042';", "it's AB"),
    ("var b = 1; var a = 7;", 7),
])
def test_extract_var(text, expected):
    assert extract_var(text, "a") == expected


@pytest.mark.parametrize("text", [
    "var a = {x: alert(1)};",        # never evaluated, just refused
    "var a = {x: 1",                 # unterminated
    "var a = 'abc",
    "var a = [1 2];",
])
def test_extract_var_rejects(text):
    with pytest.raises(WebUIError):
        extract_var(text, "a")


def test_missing_var_is_none():
    assert extract_var("var b = 1;", "a") is None


# -- device over HTTP

def test_identity_and_state(emu):
    dev = connect(emu)
    assert dev.identity().mac == "00:00:5E:00:53:10" and dev.identity().hardware == "TL-SG108PE 3.0"
    assert dev.read_state() == BASE
    assert dev.links()[0] == Link(True, "1000M full") and dev.links()[1] == Link(True, None)
    assert len(dev.links()) == 8  # padding entries beyond the real ports are dropped


def test_every_write_reaches_the_switch(emu):
    dev = connect(emu)
    dev.read_state()
    dev.apply(Op(SET_VLAN, 100, "mgmt", (1, 4), (2, 3)))
    dev.apply(Op(SET_PVID, 100, port=2))
    dev.apply(Op(SET_VLAN, 99, "tmp"))
    dev.apply(Op(DELETE_VLAN, 99))
    s = dev.read_state()
    assert s.vlans[100] == Vlan("mgmt", frozenset({1, 4}), frozenset({2, 3})) and 99 not in s.vlans and s.pvid(2) == 100


def test_wrong_password_never_leaks(emu):
    with pytest.raises(DeviceError, match="login rejected") as e:
        connect(emu, password="wrong-secret")
    assert "wrong-secret" not in str(e.value)
    assert [m for m, p, _ in emu.requests if p == "/logon.cgi"] == ["POST"]  # tried once, never retried


def test_password_only_in_post_body(emu):
    dev = connect(emu)
    dev.read_state()
    dev.close()
    assert all("hunter2" not in path for _, path, _ in emu.requests)


def test_expired_session_logs_in_again(emu):
    dev = connect(emu)
    emu.session = "expired-elsewhere"
    assert dev.read_state() == BASE


def test_dropped_connection_after_write_is_not_an_error(emu):
    dev = connect(emu)
    emu.state = replace(emu.state, dot1q_enabled=False)
    emu.drop_next_write = True
    dev.apply(Op(ENABLE_DOT1Q))
    assert dev.read_state().dot1q_enabled


def test_redirects_are_refused(emu):
    dev = connect(emu)
    emu.redirect_everything = True
    with pytest.raises(DeviceError, match="refusing to follow"):
        dev.read_state()


def test_proxy_settings_are_ignored(emu, monkeypatch):
    monkeypatch.setenv("http_proxy", "http://198.51.100.9:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://198.51.100.9:3128")
    assert connect(emu).read_state() == BASE


# -- whole engine over HTTP

def test_full_apply_over_http(cfg, tmp_path):
    emus = {n: SwitchEmulator(realize(cfg.desired(n)), mac=cfg.switches[n].mac) for n in cfg.switches}
    try:
        ws = emus["workshop"]
        ws.state = replace(ws.state, pvids=ws.state.pvids[:5] + (1,) + ws.state.pvids[6:])

        def conn(spec):
            return TplinkDevice("127.0.0.1", "admin", "hunter2", timeout=5, port=emus[spec.name].port)

        plans = build_plans(cfg, conn)
        preflight(cfg, plans, allow_management_change=False, allow_untested_model=True)
        results = execute(cfg, plans, conn, tmp_path, lambda _: None)
        assert [(r["switch"], r.get("ops")) for r in results if r["status"] == "applied"] == [("workshop", 1)]
        assert all(not p.ops for p in build_plans(cfg, conn))
    finally:
        for e in emus.values():
            e.stop()


def test_link_report(cfg):
    desired = realize(cfg.desired("workshop"))
    # 1 cam north: no link; 2: disabled; 3 bench: fine; 4-6: unnamed with devices; 7: device but in no VLAN; 8: uplink
    links = [(1, 0), (0, 0), (1, 6), (1, 6), (1, 6), (1, 6), (1, 5), (1, 6)]
    emu = SwitchEmulator(desired, mac=cfg.switches["workshop"].mac, links=links)
    try:
        plans = build_plans(cfg, lambda spec: TplinkDevice("127.0.0.1", "admin", "hunter2", timeout=5, port=emu.port),
                            only=["workshop"])
        by_port = {r["port"]: r for r in link_report(cfg, plans)[0]["ports"]}
    finally:
        emu.stop()
    assert by_port[7]["severity"] == "warning" and "no VLAN" in by_port[7]["finding"] and by_port[7]["link"] == "100M full"
    assert by_port[6]["severity"] == "info" and "no name" in by_port[6]["finding"]
    assert by_port[2]["finding"] == "port is administratively disabled"
    assert by_port[1]["finding"] == "no link on port named 'cam north'"
    assert by_port[3]["finding"] is None
    assert by_port[8]["finding"] is None and by_port[8]["membership"] == "T100,200,400 pvid1"
