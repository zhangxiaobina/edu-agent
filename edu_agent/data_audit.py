"""Read-only historical data-boundary audit helpers."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

from .data_classification import DataClass, classify_key


AUDIT_SCHEMA_VERSION = "edu-agent.data-boundary-audit.v1"
_BINARY_PATTERNS: dict[DataClass, tuple[re.Pattern[bytes], ...]] = {
    DataClass.CREDENTIAL: (
        re.compile(rb"(?i)Bearer\s+[A-Za-z0-9._~+/=-]{8,}"),
        re.compile(rb"(?i)(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]"),
        re.compile(rb"(?i)canary[_-]?secret"),
        re.compile(rb"\b(?:sk|ghp|github_pat|xox[baprs]|pypi|npm|hf)[-_][A-Za-z0-9_.-]{8,}\b"),
    ),
    DataClass.STUDENT_PII: (
        re.compile(rb"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
        re.compile(rb"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)"),
    ),
}


@dataclass(frozen=True)
class AuditFinding:
    classification: str
    location: str
    count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification,
            "location": self.location,
            "count": self.count,
        }


def _scan_bytes(payload: bytes) -> Counter[DataClass]:
    counts: Counter[DataClass] = Counter()
    for category, patterns in _BINARY_PATTERNS.items():
        counts[category] += sum(len(pattern.findall(payload)) for pattern in patterns)
    return counts


def _walk_json(value: Any, counts: Counter[DataClass]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            category = classify_key(key)
            if category in {DataClass.CREDENTIAL, DataClass.STUDENT_PII}:
                counts[category] += 1
            _walk_json(child, counts)
    elif isinstance(value, list):
        for child in value:
            _walk_json(child, counts)
    elif isinstance(value, str):
        counts.update(_scan_bytes(value.encode("utf-8", errors="ignore")))


def _sqlite_findings(path: Path) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    uri = f"file:{quote(str(path.resolve()))}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return findings
    connection.row_factory = sqlite3.Row
    try:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        for table_row in tables:
            table = table_row["name"]
            if not str(table).replace("_", "").isalnum():
                continue
            columns = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            for column in columns:
                name = column["name"]
                category = classify_key(name)
                if category not in {DataClass.CREDENTIAL, DataClass.STUDENT_PII}:
                    continue
                row = connection.execute(
                    f'SELECT COUNT(*) AS count FROM "{table}" '
                    f'WHERE "{name}" IS NOT NULL AND CAST("{name}" AS TEXT) != \'\''
                ).fetchone()
                if row["count"]:
                    findings.append(AuditFinding(category.value, f"{path}:{table}.{name}", row["count"]))
            text_columns = [
                column["name"]
                for column in columns
                if any(part in str(column["type"] or "").upper() for part in ("TEXT", "CHAR", "CLOB"))
            ]
            for name in text_columns:
                counts: Counter[DataClass] = Counter()
                cursor = connection.execute(
                    f'SELECT "{name}" AS value FROM "{table}" WHERE "{name}" IS NOT NULL'
                )
                while rows := cursor.fetchmany(256):
                    for row in rows:
                        counts.update(_scan_bytes(str(row["value"]).encode("utf-8", errors="ignore")))
                for category, count in counts.items():
                    if count:
                        findings.append(
                            AuditFinding(category.value, f"{path}:{table}.{name}:content", count)
                        )
    except sqlite3.Error:
        # A malformed or non-state SQLite file still receives a raw byte scan.
        pass
    finally:
        connection.close()
    return findings


def _candidate_files(paths: Iterable[str | Path]) -> list[Path]:
    files: set[Path] = set()
    for supplied in paths:
        path = Path(supplied).expanduser()
        if path.is_dir():
            files.update(item for item in path.rglob("*") if item.is_file())
        elif path.is_file():
            files.add(path)
        for suffix in ("-wal", "-shm"):
            companion = Path(f"{path}{suffix}")
            if companion.is_file():
                files.add(companion)
    return sorted(files)


def audit_paths(paths: Iterable[str | Path]) -> dict[str, Any]:
    """Scan explicitly supplied paths without mutating or revealing matched values."""
    files = _candidate_files(paths)
    findings: list[AuditFinding] = []
    for path in files:
        if path.suffix in {".db", ".sqlite", ".sqlite3"}:
            findings.extend(_sqlite_findings(path))
        counts: Counter[DataClass] = Counter()
        try:
            payload = path.read_bytes()
        except OSError:
            continue
        counts.update(_scan_bytes(payload))
        if path.suffix.lower() in {".json", ".jsonl"}:
            values: list[Any] = []
            try:
                if path.suffix.lower() == ".jsonl":
                    values = [json.loads(line) for line in payload.decode("utf-8").splitlines() if line]
                else:
                    values = [json.loads(payload)]
            except (UnicodeDecodeError, json.JSONDecodeError):
                values = []
            for value in values:
                _walk_json(value, counts)
        for category, count in counts.items():
            if count:
                findings.append(AuditFinding(category.value, str(path), count))
    totals = Counter()
    for finding in findings:
        totals[finding.classification] += finding.count
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "mode": "read_only_dry_run",
        "files_scanned": len(files),
        "totals": dict(sorted(totals.items())),
        "findings": [finding.to_dict() for finding in findings],
    }


__all__ = ["AUDIT_SCHEMA_VERSION", "AuditFinding", "audit_paths"]
