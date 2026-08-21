#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from edu_agent.data_audit import audit_paths


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only EduAgent historical data audit")
    parser.add_argument("paths", nargs="+", help="SQLite, WAL/SHM, JSON/JSONL, log or Artifact paths")
    parser.add_argument("--output", help="write the redacted report without printing it")
    parser.add_argument("--fail-on-findings", action="store_true")
    args = parser.parse_args()
    report = audit_paths(args.paths)
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    return 1 if args.fail_on_findings and report["findings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
