#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from edu_agent.data_audit import audit_paths


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only EduAgent historical data audit")
    parser.add_argument("paths", nargs="+", help="SQLite, WAL/SHM, JSON/JSONL, log or Artifact paths")
    args = parser.parse_args()
    print(json.dumps(audit_paths(args.paths), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
