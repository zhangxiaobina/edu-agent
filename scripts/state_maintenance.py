#!/usr/bin/env python3
"""Narrow, fail-closed SQLite backup, restore, integrity, and retention CLI."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from edu_agent.state import (
    RetentionPolicy,
    StateMaintenance,
    StateMaintenanceError,
    StateStorageError,
    StateStore,
    verify_backup_bundle,
)


def _path(value: str) -> Path:
    path = Path(value).expanduser()
    if not value.strip():
        raise argparse.ArgumentTypeError("path must be explicit")
    return path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Maintain one explicit EduAgent SQLite state database.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup = subparsers.add_parser("backup")
    backup.add_argument("--state", type=_path, required=True)
    backup.add_argument("--artifacts", type=_path, required=True)
    backup.add_argument("--target", type=_path, required=True)

    verify_backup = subparsers.add_parser("verify-backup")
    verify_backup.add_argument("--backup", type=_path, required=True)

    restore = subparsers.add_parser("restore")
    restore.add_argument("--backup", type=_path, required=True)
    restore.add_argument("--target-dir", type=_path, required=True)

    verify_state = subparsers.add_parser("verify-state")
    verify_state.add_argument("--state", type=_path, required=True)
    verify_state.add_argument("--artifacts", type=_path, required=True)

    gc = subparsers.add_parser("gc")
    gc.add_argument("--state", type=_path, required=True)
    gc.add_argument("--artifacts", type=_path, required=True)
    gc.add_argument("--operation-db", type=_path)
    gc.add_argument("--terminal-age-seconds", type=int, required=True)
    gc.add_argument("--artifact-age-seconds", type=int, required=True)
    gc.add_argument("--batch-size", type=int, default=100)
    gc.add_argument(
        "--apply",
        action="store_true",
        help="apply the exact bounded policy; omission is an audited dry-run",
    )
    return parser


def _execute(args: argparse.Namespace) -> dict:
    if args.command == "verify-backup":
        manifest, integrity = verify_backup_bundle(args.backup)
        return {
            "command": args.command,
            "manifest_sha256": manifest["manifest_sha256"],
            "integrity": integrity.to_dict(),
        }
    if args.command == "restore":
        return {
            "command": args.command,
            **StateMaintenance.restore(args.backup, args.target_dir).to_dict(),
        }

    if not args.state.expanduser().is_file():
        raise ValueError("state database must already exist")
    read_only = args.command == "verify-state"
    state = StateStore(args.state, read_only=read_only)
    maintenance = StateMaintenance(state, args.artifacts)
    if args.command == "backup":
        return {"command": args.command, **maintenance.backup(args.target).to_dict()}
    if args.command == "verify-state":
        return {"command": args.command, "integrity": maintenance.verify().to_dict()}
    if args.command == "gc":
        policy = RetentionPolicy(
            terminal_age_seconds=args.terminal_age_seconds,
            artifact_age_seconds=args.artifact_age_seconds,
            batch_size=args.batch_size,
        )
        report = maintenance.gc(
            policy,
            dry_run=not args.apply,
            operation_db_path=args.operation_db,
        )
        return {"command": args.command, **report.to_dict()}
    raise AssertionError(f"unsupported command: {args.command}")


def main() -> int:
    args = _parser().parse_args()
    try:
        result = _execute(args)
    except (StateMaintenanceError, StateStorageError, ValueError, OSError) as error:
        code = getattr(error, "error_code", "STATE_MAINTENANCE_FAILED")
        print(
            json.dumps(
                {"ok": False, "error": {"code": code, "message": str(error)}},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps({"ok": True, "result": result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
