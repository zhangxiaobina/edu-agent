"""Builders for the complete Train/Dev/Test evaluation lineage corpus."""

from __future__ import annotations

import sqlite3
from typing import Iterable

from .lineage import SPLITS
from .tasks import EvalTask, build_tasks
from .tasks_test import build_test_tasks


def build_lineage_corpus(
    train_dev_conn: sqlite3.Connection,
    test_conn: sqlite3.Connection,
) -> list[EvalTask]:
    """Return all samples with families assigned before any split filtering."""
    return build_tasks(train_dev_conn, include_derived=True) + build_test_tasks(test_conn)


def tasks_for_split(tasks: Iterable[EvalTask], split: str) -> list[EvalTask]:
    if split not in SPLITS:
        raise ValueError(f"unsupported evaluation split: {split}")
    return [task for task in tasks if task.lineage is not None and task.lineage.split == split]


__all__ = ["build_lineage_corpus", "tasks_for_split"]
