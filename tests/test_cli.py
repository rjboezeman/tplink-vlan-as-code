"""End-to-end through the CLI in --offline mode: the workflow an operator (human or AI) follows."""

import json
import xml.etree.ElementTree as ET
from dataclasses import replace

from tplink_vlan_as_code import snapshot
from tplink_vlan_as_code.cli import CHANGES_PENDING, ERROR, OK, REFUSED, main

from .conftest import EXAMPLE


def cli(capsys, *args):
    code = main([args[0], "--json", "-c", str(EXAMPLE), *args[1:]])
    return code, json.loads(capsys.readouterr().out)


def make_drift(offline_dir):
    path = offline_dir / "workshop.json"
    s = snapshot.read(path)
    v, d = s.state.vlans, s.state.vlans[1]  # port 5 drops out of VLAN 200; the switch parks it in VLAN 1 (rule 8)
    v = {**v, 200: replace(v[200], untagged=v[200].untagged - {5}), 1: replace(d, untagged=d.untagged | {5})}
    snapshot.write(path, s.switch, s.host, s.identity, replace(s.state, vlans=v))


def test_validate(capsys):
    code, out = cli(capsys, "validate")
    assert code == OK and out["apply_order"] == ["studio", "workshop", "rack"]


def test_plan_dry_run_apply_verify(capsys, offline_dir, tmp_path):
    off = ["--offline", str(offline_dir)]
    code, out = cli(capsys, "plan", *off)
    assert code == OK and all(s["in_sync"] for s in out["plan"]["switches"])

    make_drift(offline_dir)
    code, out = cli(capsys, "plan", *off, "--out", str(tmp_path / "p.json"))
    assert code == CHANGES_PENDING
    assert [o["description"] for o in out["plan"]["switches"][1]["ops"]] == [
        "set vlan 200 staff: tagged [8] untagged [3, 4, 5]", "set vlan 1 Default: tagged [] untagged [7]"]
    assert (tmp_path / "p.json").stat().st_mode & 0o777 == 0o600

    code, out = cli(capsys, "apply", *off, "--allow-untested-model")              # no --yes: dry run
    assert code == CHANGES_PENDING and snapshot.read(offline_dir / "workshop.json").state.vlans[200].untagged == {3, 4}

    code, out = cli(capsys, "apply", *off, "--yes")                                # untested hardware
    assert code == REFUSED and "nothing was written" in out["error"]

    code, out = cli(capsys, "apply", *off, "--yes", "--allow-untested-model", "--plan", str(tmp_path / "p.json"),
                    "--snapshot-dir", str(tmp_path / "snaps"))
    assert code == OK and out["results"][1] == {**out["results"][1], "switch": "workshop", "status": "applied"}
    assert cli(capsys, "plan", *off)[0] == OK

    # roll back to the pre-change snapshot
    snap = out["results"][1]["snapshot"]
    code, out = cli(capsys, "restore", snap, "--switch", "workshop", *off, "--yes", "--allow-untested-model",
                    "--snapshot-dir", str(tmp_path / "snaps"))
    assert code == OK and cli(capsys, "plan", *off)[0] == CHANGES_PENDING


def test_restore_refuses_snapshot_of_other_switch(capsys, offline_dir):
    code, out = cli(capsys, "restore", str(offline_dir / "studio.json"), "--switch", "workshop", "--offline", str(offline_dir))
    assert code == REFUSED and "snapshot is of MAC" in out["error"]


def test_env_file_must_be_private(capsys, tmp_path):
    env = tmp_path / ".env"
    env.write_text("TPLINK_PASSWORD=secret\n")
    env.chmod(0o644)
    code, out = cli(capsys, "plan", "--env-file", str(env))
    assert code == ERROR and "chmod 600" in out["error"] and "secret" not in json.dumps(out)


def test_unknown_only_switch(capsys):
    code, out = cli(capsys, "plan", "--only", "nas")
    assert code == ERROR and "no such switch" in out["error"]


def test_diagram_escapes_names(capsys, tmp_path):
    cfg = tmp_path / "net.toml"
    cfg.write_text(EXAMPLE.read_text().replace('name = "printer"', 'name = "<img src=x onerror=alert(1)>"'))
    out = tmp_path / "net.drawio"
    assert main(["diagram", "-c", str(cfg), "--out", str(out)]) == OK
    values = [c.get("value", "") for c in ET.parse(out).iter("mxCell")]
    assert any("&lt;img" in v for v in values) and not any("<img" in v for v in values)


def test_links_offline_without_link_data(capsys, offline_dir):
    code, out = cli(capsys, "links", "--offline", str(offline_dir))
    assert code == OK and not any(s["links_known"] for s in out["links"])
