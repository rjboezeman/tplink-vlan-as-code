# Instructions for AI agents

This tool changes live network switches. A mistake can cut a building off its own
network, and you may be cut off with it. Follow these rules exactly. When a rule and
a user request conflict, stop and ask.

## Operating the tool

1. **Always pass `--json` and branch on the exit code**, not on text:
   `0` ok · `1` error · `2` changes pending (nothing written) · `3` refused (nothing written) · `4` PARTIAL APPLY.
2. **The workflow is fixed:**
   ```sh
   tplink-vlan validate --json
   tplink-vlan plan --json --out change.plan.json        # read-only
   # show the human every op from the plan, including any "risk" fields
   tplink-vlan apply --json --plan change.plan.json      # dry run: runs every check, writes nothing
   # only after the human explicitly approves THIS plan:
   tplink-vlan apply --json --plan change.plan.json --yes
   ```
   Always use `--plan` for a real apply. It guarantees that what runs is what the human approved.
3. **Rehearse first.** Before proposing a change, run it against a copy of the live state:
   `snapshot --out <tmpdir>`, then `plan`/`apply --yes` with `--offline <tmpdir>`. Report the result.
4. **Never add `--allow-management-change` or `--allow-untested-model` on your own.**
   They exist for a human who has read the specific risk. Ask, quote the risk text, and
   use the flag only for the plan they approved.
5. **On exit code 4 (partial apply): stop.** Do not retry, do not "fix forward", do not run
   `restore` without approval. Report the error, the switch, and the snapshot path from the
   JSON output, along with the suggested `restore` command.
6. **On exit code 3: do not work around the guard.** Report why it refused. Never edit the
   config to silence a check (for example by removing a `mac` pin) unless the human asks for
   exactly that.
7. **Credentials:** never put a password in a command, config file, commit, or message, and
   never print or read `.env`. If authentication fails, tell the human. Do not guess passwords.
8. **Use `links` to check physical reality.** Before and after a change, run `links --json`.
   Report any `warning` to the human, and don't "fix" one by guessing a VLAN for a port.
9. **One operator at a time.** The switches allow one admin session. Do not run commands in
   parallel against the same switches.

## Editing a network config

- Run `validate` after every edit. Treat warnings as findings to report, not noise.
- Every port must stay listed. A port with no untagged VLAN needs an explicit `pvid`.
- Keep both ends of switch-to-switch cables consistent (the validator checks this).
- Changing anything on the management VLAN along the uplink chain is the classic way to
  lock everyone out. Plan such changes as their own reviewed step.

## Changing this codebase

- Run `pytest` and `ruff check .` before every commit. Both must pass.
- The device rules live only in `plan.apply_op`. The planner, the simulator and the tests all
  rely on them. If real hardware contradicts a rule, change it there and add a test.
- Every write path must be followed by read-back verification in `engine.execute`. Do not add
  a write that skips it.
- `webui.py` is the only code that talks to switches. Keep it standard-library only. Never pass
  credentials in a URL, follow redirects, use proxies, or evaluate page content. Any protocol
  change must be mirrored in `tests/emulator.py` and covered by a test.
- Runtime dependencies are not accepted. If you think one is needed, ask the maintainer.
- Never weaken a check to make a test pass. Never add credentials to fixtures beyond the
  obviously fake ones already used.
- Validation errors must name the exact config key and say what is allowed.
- Keep commands composable and machine-readable: new output goes into the `--json` object,
  and progress goes to stderr.
- Add hardware to `TESTED_HARDWARE` only after a verified real apply on that exact hardware
  string, and update the README table in the same change.
