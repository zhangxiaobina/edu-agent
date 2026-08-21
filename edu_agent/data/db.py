"""SQLite 连接助手：定位合成库、建库、取连接（行按 dict 返回）。"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

# 合成库默认放在包内 data/edu.db；可用环境变量覆盖。该文件已被 .gitignore 排除。
DEFAULT_DB_PATH = Path(__file__).resolve().parent / "edu.db"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def db_path() -> Path:
    """返回当前使用的合成库路径（环境变量 EDU_AGENT_DB 优先）。"""
    return Path(os.environ.get("EDU_AGENT_DB", str(DEFAULT_DB_PATH)))


def connect(path: str | os.PathLike | None = None) -> sqlite3.Connection:
    """打开连接；row_factory=Row 便于按列名取值，外键约束开启。"""
    target = Path(path) if path is not None else db_path()
    conn = sqlite3.connect(str(target), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """在给定连接上执行 schema.sql 建表（幂等：先 drop 同名表交给调用方控制）。"""
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


def rows_to_dicts(rows) -> list[dict]:
    """sqlite3.Row 列表 → 普通 dict 列表（工具返回值用）。"""
    return [dict(r) for r in rows]
