"""Command line interface. Run `tplink-vlan --help`; see README.md and AGENTS.md for workflows.

Output: human-readable by default; with --json, exactly one JSON object on stdout
(progress goes to stderr). The exit code always carries the outcome (see EXIT CODES).
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

from . import __version__, snapshot
from .config import ConfigError, load
from .device import DeviceError
from .engine import (
    PartialApply,
    PlanError,
    Refused,
    build_plans,
    execute,
    live_connector,
    offline_connector,
    plan_document,
    preflight,
)

# EXIT CODES
OK = 0               # valid / in sync / applied and verified
ERROR = 1            # bad config, bad arguments, cannot connect, unplannable
CHANGES_PENDING = 2  # plan found differences, or apply ran without --yes (nothing written)
REFUSED = 3          # a safety check refused the operation (nothing written)
PARTIAL = 4          # apply wrote some changes and then failed; see the snapshot to restore


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    out = _Output(args.json)
    try:
        return args.func(args, out)
    except ConfigError as e:
        return out.fail(ERROR, str(e), problems=e.problems)
    except (DeviceError, PlanError, ValueError, OSError) as e:
        return out.fail(ERROR, str(e))
    except Refused as e:
        return out.fail(REFUSED, f"refused, nothing was written: {e}")
    except PartialApply as e:
        return out.fail(PARTIAL, f"PARTIAL APPLY: {e}", switch=e.switch,
                        snapshot=str(e.snapshot_path) if e.snapshot_path else None)


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", default="network.toml", type=Path, help="desired-state file (default: %(default)s)")
    common.add_argument("--json", action="store_true", help="print one JSON object on stdout")
    common.add_argument("--only", action="append", metavar="SWITCH", help="limit to this switch (repeatable)")
    common.add_argument("--offline", type=Path, metavar="DIR",
                        help="use DIR/<switch>.json snapshots instead of the switches (apply writes back to them)")
    common.add_argument("--env-file", type=Path,
                        help="file with KEY=VALUE credentials (default: .env next to the config, if present); must not be group/world readable")

    p = argparse.ArgumentParser(prog="tplink-vlan", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(required=True, metavar="COMMAND")

    c = sub.add_parser("validate", parents=[common], help="check the config file; no network access")
    c.set_defaults(func=cmd_validate)

    c = sub.add_parser("plan", parents=[common], help="read the switches and show what apply would change (read-only)")
    c.add_argument("--out", type=Path, help="save the plan as JSON, for `apply --plan` after review")
    c.set_defaults(func=cmd_plan)

    for name, func, helptext in (
        ("apply", cmd_apply, "make the switches match the config; a dry run unless --yes"),
        ("restore", cmd_restore, "make one switch match a snapshot file; a dry run unless --yes"),
    ):
        c = sub.add_parser(name, parents=[common], help=helptext)
        if name == "restore":
            c.add_argument("snapshot", type=Path, help="snapshot file to restore")
            c.add_argument("--switch", required=True, help="switch (name in the config) to restore")
        else:
            c.add_argument("--plan", type=Path, help="only apply if a fresh plan is identical to this saved one")
        c.add_argument("--yes", action="store_true", help="actually write to the switches")
        c.add_argument("--allow-management-change", action="store_true",
                       help="permit ops that touch the management path (can lock you out)")
        c.add_argument("--allow-untested-model", action="store_true",
                       help="permit writes to hardware not yet verified with this tool")
        c.add_argument("--snapshot-dir", type=Path, default=Path("snapshots"),
                       help="where pre-change snapshots go (default: %(default)s)")
        c.set_defaults(func=func)

    c = sub.add_parser("snapshot", parents=[common], help="save every switch's current state as JSON (read-only)")
    c.add_argument("--out", type=Path, required=True, metavar="DIR")
    c.set_defaults(func=cmd_snapshot)

    c = sub.add_parser("diagram", parents=[common], help="render the config as a draw.io diagram; no network access")
    c.add_argument("--out", type=Path, default=Path("network.drawio"))
    c.set_defaults(func=cmd_diagram)
    return p


# --- commands -----------------------------------------------------------------------------------

def cmd_validate(args, out: _Output) -> int:
    cfg = load(args.config)
    out.line(f"{args.config}: valid, {len(cfg.vlans)} VLANs, {len(cfg.switches)} switches "
             f"(apply order: {', '.join(cfg.apply_order())})")
    out.warn_all(cfg.warnings)
    return out.done(OK, config_sha256=cfg.sha256, apply_order=cfg.apply_order(), warnings=list(cfg.warnings))


def cmd_plan(args, out: _Output) -> int:
    cfg, plans = _plan(args, out)
    doc = plan_document(cfg, plans, args.only)
    if args.out:
        _write_private(args.out, json.dumps(doc, indent=2) + "\n")
        out.line(f"plan saved to {args.out}")
    pending = any(p.ops for p in plans)
    return out.done(CHANGES_PENDING if pending else OK, plan=doc, warnings=list(cfg.warnings))


def cmd_apply(args, out: _Output) -> int:
    approved = json.loads(args.plan.read_text()) if args.plan else None
    cfg, plans = _plan(args, out)
    return _apply(args, out, cfg, plans, approved)


def cmd_restore(args, out: _Output) -> int:
    snap = snapshot.read(args.snapshot)
    args.only = [args.switch]
    cfg = load(args.config)
    if args.switch not in cfg.switches:
        raise ValueError(f"unknown switch {args.switch!r}")
    cfg, plans = _plan(args, out, cfg, targets={args.switch: snap.state})
    if plans[0].identity.mac != snap.identity.mac:
        raise Refused(f"snapshot is of MAC {snap.identity.mac}, {args.switch} is {plans[0].identity.mac}")
    return _apply(args, out, cfg, plans, None)


def cmd_snapshot(args, out: _Output) -> int:
    cfg, plans = _plan(args, out, quiet=True)
    files = []
    for p in plans:
        files.append(str(snapshot.write(args.out / f"{p.name}.json", p.name, p.host, p.identity, p.current)))
        out.line(f"{p.name}: saved {files[-1]}")
    return out.done(OK, files=files)


def cmd_diagram(args, out: _Output) -> int:
    from .diagram import render

    cfg = load(args.config)
    args.out.write_text(render(cfg))
    out.line(f"wrote {args.out}")
    return out.done(OK, file=str(args.out))


# --- helpers ------------------------------------------------------------------------------------

def _plan(args, out: _Output, cfg=None, targets=None, quiet=False):
    cfg = cfg or load(args.config)
    for name in args.only or []:
        if name not in cfg.switches:
            raise ValueError(f"--only {name!r}: no such switch in {args.config}")
    out.warn_all(cfg.warnings)
    connect = offline_connector(args.offline) if args.offline else live_connector(_environment(args))
    plans = build_plans(cfg, connect, args.only, targets)
    if not quiet:
        for p in plans:
            note = "" if not p.untested else "  [hardware not verified with this tool]"
            out.line(f"== {p.name} ({p.host}, {p.identity.hardware}, fw {p.identity.firmware}){note}: "
                     + ("in sync" if not p.ops else f"{len(p.ops)} change(s)"))
            for op in p.ops:
                out.line(f"   {op.describe()}" + (f"   !! {op.risk}" if op.risk else ""))
    return cfg, plans


def _apply(args, out: _Output, cfg, plans, approved) -> int:
    if not any(p.ops for p in plans):
        return out.done(OK, results=[{"switch": p.name, "status": "in_sync"} for p in plans])
    preflight(cfg, plans, allow_management_change=args.allow_management_change,
              allow_untested_model=args.allow_untested_model, approved=approved, only=args.only)
    if not args.yes:
        out.line("dry run: nothing written. Re-run with --yes to apply.")
        return out.done(CHANGES_PENDING, plan=plan_document(cfg, plans, args.only))
    results = execute(cfg, plans, offline_connector(args.offline) if args.offline else live_connector(_environment(args)),
                      args.snapshot_dir, out.progress)
    return out.done(OK, results=results)


def _environment(args) -> dict[str, str]:
    """os.environ, plus KEY=VALUE lines from the env file for keys not already set."""
    env = dict(os.environ)
    path = args.env_file or args.config.parent / ".env"
    if not path.exists():
        if args.env_file:
            raise ValueError(f"--env-file {path} does not exist")
        return env
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ValueError(f"{path} is accessible by group/others (mode {stat.filemode(mode)}); run: chmod 600 {path}")
    for line in path.read_text().splitlines():
        key, sep, value = line.strip().partition("=")
        key = key.removeprefix("export ").strip()
        if sep and key and not key.startswith("#"):
            env.setdefault(key, value.strip().strip("\"'"))
    return env


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


class _Output:
    """Human text on stdout, or a single JSON object on stdout with progress on stderr."""

    def __init__(self, as_json: bool):
        self.json = as_json

    def line(self, text: str) -> None:
        print(text, file=sys.stderr if self.json else sys.stdout)

    def progress(self, text: str) -> None:
        print(text, file=sys.stderr)

    def warn_all(self, warnings) -> None:
        for w in warnings:
            print(f"warning: {w}", file=sys.stderr)

    def done(self, code: int, **data: Any) -> int:
        if self.json:
            print(json.dumps({"ok": code in (OK, CHANGES_PENDING), "exit_code": code, **data}, indent=2))
        return code

    def fail(self, code: int, message: str, **data: Any) -> int:
        if self.json:
            print(json.dumps({"ok": False, "exit_code": code, "error": message, **data}, indent=2))
        else:
            print(f"error: {message}", file=sys.stderr)
        return code


if __name__ == "__main__":
    sys.exit(main())
