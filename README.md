# tplink-vlan-as-code

Declarative 802.1Q VLAN and PVID management for TP-Link **Easy Smart** switches
(TL-SG105E, TL-SG108E, TL-SG108PE, TL-SG116E and relatives).

These switches have no API, no SSH and no serial console, only a web UI. This
tool lets you describe the whole L2 network in one TOML file and then:

```text
validate  ->  plan (read-only diff)  ->  apply (dry run)  ->  apply --yes (verified)
```

It is built to be safe to run unattended, including by AI agents: nothing is
written without an explicit `--yes`, every write is verified by reading the
switch back, and changes that could lock you out of your own switches are
refused unless you say otherwise. It also compares each port's live link state
with its configuration, so a device plugged into an unconfigured port gets reported.

It has **no third-party runtime dependencies**. Every byte sent to a switch comes from
this repository's own small client ([`webui.py`](src/tplink_vlan_as_code/webui.py)),
built on the Python standard library.

> **Status: alpha.** The tool has a full test suite against a simulated switch,
> but apply has **not yet been verified end-to-end on real hardware**.
> Read-only commands (`validate`, `plan`, `snapshot`, `diagram`) are safe to try.
> See [Supported hardware](#supported-hardware).

Not affiliated with TP-Link.

## Safety model

| Guard | What it prevents |
|---|---|
| Dry run by default | `apply` and `restore` only write when you pass `--yes`. |
| Strict config validation | Typos (unknown keys are errors), undeclared VLANs, ports in two untagged VLANs, unreachable switches. VLAN names are limited to a safe charset because they end up in the switch's web UI. |
| Identity check | Before reading or writing, the model must match and, if pinned, the MAC address must match. A different device at the same IP is refused. |
| Port-count check | The live switch must have exactly the ports the config declares. |
| Simulated plan | Every plan is replayed against a model of the switch's rules (for example, you cannot delete a VLAN that is still a PVID), so the ordering is known to be valid before anything is sent. |
| Management-path guard | Any change to the management VLAN on a switch's uplink, or on a downlink to another switch, is flagged and refused without `--allow-management-change`. |
| Untested-hardware guard | Writes to hardware not yet verified with this tool are refused without `--allow-untested-model`. |
| Approved plans | `plan --out p.json` then `apply --plan p.json` applies only if a fresh plan is *identical*: same config, same switch state, same ops. |
| Pre-change snapshot | The full state of each switch is saved before its first write, to restore with `tplink-vlan restore`. |
| Read-back after every write | The switch's web UI does not report whether a write worked, so after each operation the state is read back and compared with the simulated state. The run stops at the first mismatch. |
| Furthest switch first | Switches are changed leaves-first, so a mistake never cuts the path to a switch that has not been done yet. After each switch, every switch must accept a fresh login before the run continues. |
| Concurrent change detection | If a switch changed between plan and apply, the apply is refused. |

## Install

Requires Python 3.11+.

```sh
pip install git+https://github.com/rjboezeman/tplink-vlan-as-code
tplink-vlan --version
```

## Quick start

1. **Describe your network.** Copy [`examples/example.toml`](examples/example.toml), a
   fictional three-switch office, to `network.toml` (the default) or any name you like,
   for example `mysetup.toml` with `-c mysetup.toml`. Give each switch its own
   `[switches.<name>]` section. The format rules are at the top of the example, and
   every port of every switch must be listed. Top-level `*.toml` files other than
   `pyproject.toml` are git-ignored, so your real topology stays out of any commit.
2. **Provide the passwords** without putting them in the config or on the command line.
   Each switch has its own credentials: `username` in its config section (default `admin`),
   and a password from `TPLINK_PASSWORD_<NAME>`, falling back to a shared `TPLINK_PASSWORD`:
   ```sh
   umask 077; cat > .env <<'END'        # next to network.toml; must be mode 0600
   TPLINK_PASSWORD_CORE=...
   TPLINK_PASSWORD_OFFICE=...
   TPLINK_PASSWORD_ATTIC=...
   END
   ```
   Already-set environment variables take precedence. Set `password_env = "VAR"` in a
   switch's section to use exactly that variable, with no fallback.
3. **Validate** (no network access):
   ```sh
   tplink-vlan validate
   ```
4. **Take a snapshot** of what is on the switches now (read-only). Keep it, because it is
   your way back:
   ```sh
   tplink-vlan snapshot --out snapshots/baseline
   ```
5. **Plan** (read-only). This shows every change, and `!!` marks the risky ones:
   ```sh
   tplink-vlan plan --out reviewed.plan.json
   ```
6. **Apply** after reviewing. Without `--yes` it is still a dry run that runs all checks:
   ```sh
   tplink-vlan apply --plan reviewed.plan.json          # dry run, all checks
   tplink-vlan apply --plan reviewed.plan.json --yes    # write, verify, snapshot
   ```

Options go **after** the command: `tplink-vlan plan -c other.toml --json`.

## Commands

| Command | Network | Writes | Purpose |
|---|---|---|---|
| `validate` | no | no | Check the config; print warnings and the apply order. |
| `plan [--out FILE]` | read | no | Diff the switches against the config. |
| `apply [--plan FILE] [--yes]` | read/write | with `--yes` | Make the switches match the config. |
| `restore SNAPSHOT --switch NAME [--yes]` | read/write | with `--yes` | Make one switch match a snapshot. Same guards as apply. |
| `links [--all]` | read | no | Compare live port link state with the VLAN config. Exit 2 on warnings. |
| `snapshot --out DIR` | read | local files | Save every switch's state, plus link state, to `DIR/<switch>.json`. |
| `diagram [--out FILE]` | no | local file | Render the config as a draw.io diagram. |

Common options: `-c/--config FILE` (default `network.toml`), `--json`, `--only SWITCH`
(repeatable), `--offline DIR`, `--env-file FILE`.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | OK: valid, in sync, or applied and verified |
| 1 | Error: bad config or arguments, cannot connect, no safe plan |
| 2 | Attention needed: plan found differences, apply ran without `--yes`, or `links` found warnings. Nothing was written. |
| 3 | Refused by a safety check. Nothing was written. |
| 4 | **Partial apply**: some writes happened and then something failed. The output names the snapshot to restore. |

With `--json`, stdout carries exactly one JSON object (`ok`, `exit_code`, plus
`plan`, `results` or `error`). Progress goes to stderr.

## Testing changes offline

Every command accepts `--offline DIR`. It uses `DIR/<switch>.json` snapshot files
in place of the switches, and an offline apply writes the result back to those files.
That gives you a full rehearsal with the real starting state:

```sh
tplink-vlan snapshot --out /tmp/rehearsal                       # copy of the live state
$EDITOR network.toml
tplink-vlan plan  --offline /tmp/rehearsal
tplink-vlan apply --offline /tmp/rehearsal --yes --allow-untested-model
tplink-vlan plan  --offline /tmp/rehearsal                      # expect: in sync
```

The simulated switch enforces the device rules listed below. A rehearsal therefore catches
ordering problems, but not firmware quirks the rules do not describe. On real hardware,
read-back verification catches those.

## Device rules (assumptions)

The planner and the offline simulator assume the following switch behaviour, defined in
one place: `apply_op` in [`plan.py`](src/tplink_vlan_as_code/plan.py).

1. VLAN table changes require 802.1Q mode to be on.
2. VLAN 1 can be emptied but not deleted.
3. A VLAN that is the PVID of any port cannot be deleted.
4. A PVID must point at an existing VLAN.
5. At most 32 VLANs.
6. Turning 802.1Q mode on resets the table. If it is off, the plan contains only that step.
   Run plan again afterwards.

If your firmware behaves differently, the read-back check stops the run and points you at the
snapshot. Please open an issue with the details.

## Link check

`tplink-vlan links` reads each port's link state (the same information the switch's
Port Setting page shows) and compares it with the configuration:

| Severity | Finding |
|---|---|
| warning | Link is up, but the port is in **no VLAN** on the switch. A device is connected and its traffic is dropped. |
| warning | Link is up, and the port's live VLAN config differs from the config file. Run `plan`. |
| info | Link is up on a port that has no `name` in the config: an undocumented device. |
| info | No link on a port that has a `name`: the documented device is missing or off. |
| info | The port is administratively disabled. |

```text
== workshop:
   -- port  1 cam north        link down       U400 pvid400               no link on port named 'cam north'
   !! port  7                  link 100M full  (no VLAN)                  link up, but the port is in no VLAN on the switch: its traffic is dropped
```

The result is a point-in-time reading. A device that is asleep or powered off looks
like an empty port.

## Diagram

`tplink-vlan diagram` writes a draw.io file. It has one VLAN-by-port matrix per switch, with
cables drawn from the `upstream` links. Cells where the two ends of a cable disagree are
outlined in red. Open it with [draw.io](https://app.diagrams.net) or the desktop app.

## Supported hardware

Switch access uses the Easy Smart web UI over HTTP. The pages and endpoints used are listed
at the top of [`webui.py`](src/tplink_vlan_as_code/webui.py). Models with the same web UI
(the "E" series) should work; the older DE series uses different pages and does not.
Verified with this tool:

| Hardware (as reported by the switch) | Firmware | read | apply |
|---|---|---|---|
| *none yet* | | | |

Hardware not in this list needs `--allow-untested-model` for writes. After a successful
real apply on new hardware, add the exact hardware string to `TESTED_HARDWARE` in
[`engine.py`](src/tplink_vlan_as_code/engine.py) and to this table.

## Security

See [SECURITY.md](SECURITY.md) for the threat model. In short:

- **These switches only speak HTTP.** The admin password crosses the network in clear text
  on every login. Run this tool only from a host on the management VLAN, and do not reuse
  that password anywhere else.
- Passwords come only from environment variables or a `.env` file that must be mode `0600`.
  They are never accepted as arguments, never written to snapshots or plans, and are scrubbed
  from error messages.
- Pin each switch's `mac` in the config.
- Snapshots and saved plans are written `0600` in `0700` directories. They contain topology
  information, not secrets. They are in `.gitignore` by default.

## Development

```sh
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest && .venv/bin/ruff check .
```

The code is small and layered. Each module's docstring explains its role:

| Module | Role | I/O |
|---|---|---|
| `state.py` | `SwitchState` value type | none |
| `config.py` | TOML loading and validation, giving the desired `SwitchState` per switch | reads the config |
| `plan.py` | diff, ordering, risk flags, device rules, simulation | none |
| `webui.py` | HTTP client for the switch web UI, and a JavaScript-literal parser that cannot execute code | network |
| `device.py` | `TplinkDevice` (real switch, via `webui.py`) and `FakeDevice` (simulator) | network |
| `snapshot.py` | snapshot files | files |
| `engine.py` | orchestration and every safety check | via devices |
| `cli.py` | argument parsing, output and exit codes | |
| `diagram.py` | draw.io rendering | none |

`tests/emulator.py` is a local HTTP server that speaks the switch's protocol. The client,
the engine and a full apply are tested end to end against it over real sockets.

AI agents: read [AGENTS.md](AGENTS.md) first.

## License

Apache-2.0, see [LICENSE](LICENSE).

The web UI endpoint and parameter names were first documented by
[tplink-tool](https://github.com/jfrancis42/tplink-tool). This project contains none of
its code.
