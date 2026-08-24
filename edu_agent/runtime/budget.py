from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, ROUND_CEILING
from typing import Any, Mapping


BUDGET_LEDGER_SCHEMA_VERSION = 1
BUDGET_LEDGER_MIGRATION = "014_run_budget_ledger"
DEFAULT_PRICING_VERSION = "unpriced@2026-08-24.r4.4.v1"

_DIMENSIONS = (
    "model_calls",
    "tool_calls",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cost_microusd",
    "wall_time_ms",
)
_LIMIT_FIELDS = {
    "model_calls": "max_model_calls",
    "tool_calls": "max_tool_calls",
    "input_tokens": "max_input_tokens",
    "output_tokens": "max_output_tokens",
    "total_tokens": "max_total_tokens",
    "cost_microusd": "max_cost_microusd",
    "wall_time_ms": "max_wall_time_ms",
}
_EXHAUSTION_ORDER = (
    "model_calls",
    "tool_calls",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cost_microusd",
    "wall_time_ms",
)
_SAFE_METADATA_KEYS = frozenset(
    {
        "attempt_id",
        "attempt_sequence",
        "component",
        "estimated",
        "failure_kind",
        "model",
        "provider",
        "route_role",
        "status",
    }
)


class BudgetLedgerError(RuntimeError):
    pass


class BudgetIdentityError(BudgetLedgerError):
    pass


class BudgetOperationConflict(BudgetLedgerError):
    pass


class BudgetExceeded(BudgetLedgerError):
    def __init__(self, dimension: str, snapshot: Mapping[str, Any]):
        self.dimension = dimension
        self.stop_reason = f"budget_exhausted:{dimension}"
        self.snapshot = dict(snapshot)
        used = self.snapshot.get(dimension, 0)
        limit = self.snapshot.get(_LIMIT_FIELDS[dimension])
        super().__init__(f"预算已耗尽：{dimension}（{used}/{limit}）")


@dataclass(frozen=True)
class BudgetAmounts:
    model_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_microusd: int = 0
    wall_time_ms: int = 0

    def __post_init__(self) -> None:
        for name in _DIMENSIONS:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"budget amount {name} must be a non-negative integer")
        # Usage commits normally set total=input+output.  Reservations are a
        # vector of independent maxima, however: a child with a 4k aggregate
        # token cap must be able to reserve 4k input, 4k output and 4k total
        # without pretending that its aggregate cap is 8k.

    def to_dict(self) -> dict[str, int]:
        return asdict(self)

    @classmethod
    def from_value(cls, value: Mapping[str, Any] | BudgetAmounts | None) -> BudgetAmounts:
        if isinstance(value, cls):
            return value
        source = dict(value or {})
        input_tokens = _non_negative_int(source.get("input_tokens", 0), "input_tokens")
        output_tokens = _non_negative_int(source.get("output_tokens", 0), "output_tokens")
        total_tokens = source.get("total_tokens", input_tokens + output_tokens)
        return cls(
            model_calls=_non_negative_int(source.get("model_calls", 0), "model_calls"),
            tool_calls=_non_negative_int(source.get("tool_calls", 0), "tool_calls"),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=_non_negative_int(total_tokens, "total_tokens"),
            cost_microusd=_non_negative_int(
                source.get("cost_microusd", 0), "cost_microusd"
            ),
            wall_time_ms=_non_negative_int(
                source.get("wall_time_ms", 0), "wall_time_ms"
            ),
        )


@dataclass(frozen=True)
class BudgetLimits:
    max_model_calls: int
    max_tool_calls: int
    max_input_tokens: int
    max_output_tokens: int
    max_total_tokens: int
    max_cost_microusd: int | None
    max_wall_time_ms: int

    def __post_init__(self) -> None:
        for name in (
            "max_model_calls",
            "max_tool_calls",
            "max_input_tokens",
            "max_output_tokens",
            "max_total_tokens",
            "max_wall_time_ms",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"budget limit {name} must be a positive integer")
        if self.max_total_tokens > self.max_input_tokens + self.max_output_tokens:
            raise ValueError(
                "max_total_tokens cannot exceed max_input_tokens + max_output_tokens"
            )
        if self.max_cost_microusd is not None and (
            isinstance(self.max_cost_microusd, bool)
            or not isinstance(self.max_cost_microusd, int)
            or self.max_cost_microusd < 0
        ):
            raise ValueError("max_cost_microusd must be null or a non-negative integer")

    def to_dict(self) -> dict[str, int | None]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> BudgetLimits:
        return cls(**dict(value))


@dataclass(frozen=True)
class ModelPrice:
    input_per_million_usd: Decimal
    output_per_million_usd: Decimal


class ModelPriceCatalog:
    """Versioned, explicit token prices. Missing routes remain unknown."""

    def __init__(
        self,
        *,
        version: str = DEFAULT_PRICING_VERSION,
        prices: Mapping[str, Mapping[str, Any]] | None = None,
    ):
        if not isinstance(version, str) or not version.strip():
            raise ValueError("pricing version must be a non-empty string")
        self.version = version.strip()
        self._prices: dict[str, ModelPrice] = {}
        for key, value in dict(prices or {}).items():
            if not isinstance(key, str) or not key.strip() or not isinstance(value, Mapping):
                raise ValueError("budget prices must map stable route/model names to objects")
            unknown = set(value) - {
                "input_per_million_usd",
                "output_per_million_usd",
            }
            if unknown:
                raise ValueError(f"unknown price fields for {key}: {sorted(unknown)}")
            if set(value) != {
                "input_per_million_usd",
                "output_per_million_usd",
            }:
                raise ValueError(f"both input/output prices are required for {key}")
            input_price = _finite_decimal(
                value["input_per_million_usd"],
                f"{key}.input_per_million_usd",
            )
            output_price = _finite_decimal(
                value["output_per_million_usd"],
                f"{key}.output_per_million_usd",
            )
            self._prices[key.strip().lower()] = ModelPrice(input_price, output_price)

    def resolve(self, *, provider: str | None, model: str | None) -> ModelPrice | None:
        provider_key = (provider or "").strip().lower()
        model_key = (model or "").strip().lower()
        for key in (f"{provider_key}:{model_key}", model_key):
            if key and key in self._prices:
                return self._prices[key]
        return None

    def to_dict(self) -> dict[str, dict[str, str]]:
        return {
            key: {
                "input_per_million_usd": _decimal_text(price.input_per_million_usd),
                "output_per_million_usd": _decimal_text(price.output_per_million_usd),
            }
            for key, price in sorted(self._prices.items())
        }

    def quote_microusd(
        self,
        *,
        provider: str | None,
        model: str | None,
        input_tokens: int,
        output_tokens: int,
    ) -> int | None:
        price = self.resolve(provider=provider, model=model)
        if price is None:
            return None
        # One token at USD/million has the same numeric value in micro-USD.
        value = (
            Decimal(input_tokens) * price.input_per_million_usd
            + Decimal(output_tokens) * price.output_per_million_usd
        )
        return int(value.to_integral_value(rounding=ROUND_CEILING))


def initialize_run_budget_schema(connection: sqlite3.Connection, *, now: str) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS run_budget_ledgers (
            root_run_id TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            session_id TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            pricing_version TEXT NOT NULL,
            pricing_json TEXT NOT NULL,
            limits_json TEXT NOT NULL,
            used_json TEXT NOT NULL,
            reserved_json TEXT NOT NULL,
            estimated_operations INTEGER NOT NULL DEFAULT 0,
            unknown_cost_operations INTEGER NOT NULL DEFAULT 0,
            stop_reason TEXT,
            finalized_operation_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            finalized_at TEXT
        );

        CREATE TABLE IF NOT EXISTS run_budget_operations (
            root_run_id TEXT NOT NULL
                REFERENCES run_budget_ledgers(root_run_id) ON DELETE CASCADE,
            operation_id TEXT NOT NULL,
            owner_run_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('reserved', 'committed', 'released')),
            request_fingerprint TEXT NOT NULL,
            reserved_json TEXT NOT NULL,
            actual_json TEXT,
            usage_source TEXT,
            cost_status TEXT NOT NULL CHECK(cost_status IN ('known', 'unknown')),
            parent_operation_id TEXT,
            metadata_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(root_run_id, operation_id)
        );

        """
    )
    columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(run_budget_operations)"
        ).fetchall()
    }
    ledger_columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(run_budget_ledgers)"
        ).fetchall()
    }
    if "pricing_json" not in ledger_columns:
        connection.execute(
            "ALTER TABLE run_budget_ledgers "
            "ADD COLUMN pricing_json TEXT NOT NULL DEFAULT '{}'"
        )
    if "parent_operation_id" not in columns:
        connection.execute(
            "ALTER TABLE run_budget_operations ADD COLUMN parent_operation_id TEXT"
        )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_run_budget_operations_owner
            ON run_budget_operations(root_run_id, owner_run_id, status, created_at)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_run_budget_operations_parent
            ON run_budget_operations(root_run_id, parent_operation_id, status)
        """
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO state_schema_migrations(version, applied_at)
        VALUES (?, ?)
        """,
        (BUDGET_LEDGER_MIGRATION, now),
    )


class RunBudgetLedger:
    """Persistent budget truth for one complete parent/descendant run tree.

    Calls are integer attempts, tokens are Provider token units, cost is stored
    as integer micro-USD, and wall time is root elapsed milliseconds. Planner
    and model-based compaction are model attempts; deterministic compression is
    wall time only. Tool planning/validation is free, while every dispatched
    tool call is charged once. Retry and fallback calls are separate attempts.
    """

    def __init__(
        self,
        state_store,
        *,
        root_run_id: str,
        session_id: str,
        actor_id: str,
        tenant_id: str,
        limits: BudgetLimits | None = None,
        pricing: ModelPriceCatalog | None = None,
    ):
        for name, value in (
            ("root_run_id", root_run_id),
            ("session_id", session_id),
            ("actor_id", actor_id),
            ("tenant_id", tenant_id),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"budget {name} must be a non-empty string")
        self.state_store = state_store
        self.root_run_id = root_run_id
        self.session_id = session_id
        self.actor_id = actor_id
        self.tenant_id = tenant_id
        self.pricing = pricing or ModelPriceCatalog()
        self._ensure(limits)

    @classmethod
    def open(
        cls,
        state_store,
        *,
        root_run_id: str,
        pricing: ModelPriceCatalog | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> RunBudgetLedger:
        """Open persisted identity/limits without refreshing any allowance."""

        if connection is None:
            with state_store.connect() as active:
                row = active.execute(
                    "SELECT * FROM run_budget_ledgers WHERE root_run_id=?",
                    (root_run_id,),
                ).fetchone()
        else:
            row = connection.execute(
                "SELECT * FROM run_budget_ledgers WHERE root_run_id=?",
                (root_run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"run budget ledger does not exist: {root_run_id}")
        if int(row["schema_version"]) != BUDGET_LEDGER_SCHEMA_VERSION:
            raise BudgetIdentityError("unsupported run budget ledger schema")
        persisted_prices = json.loads(row["pricing_json"])
        persisted_pricing = pricing or ModelPriceCatalog(
            version=row["pricing_version"],
            prices=persisted_prices,
        )
        if persisted_pricing.version != row["pricing_version"]:
            raise BudgetIdentityError("run budget pricing version cannot change after creation")
        if persisted_pricing.to_dict() != persisted_prices:
            raise BudgetIdentityError("run budget prices cannot change after creation")
        ledger = cls.__new__(cls)
        ledger.state_store = state_store
        ledger.root_run_id = root_run_id
        ledger.session_id = row["session_id"]
        ledger.actor_id = row["actor_id"]
        ledger.tenant_id = row["tenant_id"]
        ledger.pricing = persisted_pricing
        return ledger

    @contextmanager
    def _write_connection(self, connection: sqlite3.Connection | None):
        if connection is not None:
            yield connection
            return
        with self.state_store.connect() as owned:
            owned.execute("BEGIN IMMEDIATE")
            try:
                yield owned
            except BudgetExceeded:
                # Exhaustion is itself durable state.  Other exceptions retain
                # normal sqlite context-manager rollback semantics.
                owned.commit()
                raise

    def _ensure(self, limits: BudgetLimits | None) -> None:
        now = self.state_store.now_iso()
        with self.state_store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM run_budget_ledgers WHERE root_run_id=?",
                (self.root_run_id,),
            ).fetchone()
            if row is None:
                if limits is None:
                    raise KeyError(f"run budget ledger does not exist: {self.root_run_id}")
                zero = _json(BudgetAmounts().to_dict())
                connection.execute(
                    """
                    INSERT INTO run_budget_ledgers(
                        root_run_id, schema_version, session_id, actor_id, tenant_id,
                        pricing_version, pricing_json, limits_json, used_json, reserved_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self.root_run_id,
                        BUDGET_LEDGER_SCHEMA_VERSION,
                        self.session_id,
                        self.actor_id,
                        self.tenant_id,
                        self.pricing.version,
                        _json(self.pricing.to_dict()),
                        _json(limits.to_dict()),
                        zero,
                        zero,
                        now,
                        now,
                    ),
                )
                return
            self._check_identity(row)
            if int(row["schema_version"]) != BUDGET_LEDGER_SCHEMA_VERSION:
                raise BudgetIdentityError("unsupported run budget ledger schema")
            if limits is not None and json.loads(row["limits_json"]) != limits.to_dict():
                raise BudgetIdentityError("run budget limits cannot change after creation")
            if row["pricing_version"] != self.pricing.version:
                raise BudgetIdentityError("run budget pricing version cannot change after creation")
            if json.loads(row["pricing_json"]) != self.pricing.to_dict():
                raise BudgetIdentityError("run budget prices cannot change after creation")

    def _check_identity(self, row: Mapping[str, Any]) -> None:
        actual = (
            row["root_run_id"],
            row["session_id"],
            row["actor_id"],
            row["tenant_id"],
        )
        expected = (
            self.root_run_id,
            self.session_id,
            self.actor_id,
            self.tenant_id,
        )
        if actual != expected:
            raise BudgetIdentityError("run budget root scope does not match")

    def reserve(
        self,
        operation_id: str,
        *,
        owner_run_id: str,
        kind: str,
        amount: BudgetAmounts | Mapping[str, Any],
        cost_known: bool = True,
        metadata: Mapping[str, Any] | None = None,
        parent_operation_id: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        operation_id = _stable_id(operation_id, "operation_id")
        owner_run_id = _stable_id(owner_run_id, "owner_run_id")
        kind = _stable_id(kind, "kind")
        requested = BudgetAmounts.from_value(amount)
        if parent_operation_id is not None:
            parent_operation_id = _stable_id(
                parent_operation_id,
                "parent_operation_id",
            )
        safe_metadata = _safe_metadata(metadata)
        identity_metadata = {
            key: safe_metadata[key]
            for key in (
                "attempt_id",
                "attempt_sequence",
                "component",
                "provider",
                "model",
                "route_role",
            )
            if key in safe_metadata
        }
        fingerprint = _fingerprint(
            {
                "owner_run_id": owner_run_id,
                "kind": kind,
                "amount": requested.to_dict(),
                "cost_known": bool(cost_known),
                "parent_operation_id": parent_operation_id,
                "identity_metadata": identity_metadata,
            }
        )
        now = self.state_store.now_iso()
        with self._write_connection(connection) as active:
            row = self._load(active)
            existing = active.execute(
                """
                SELECT * FROM run_budget_operations
                WHERE root_run_id=? AND operation_id=?
                """,
                (self.root_run_id, operation_id),
            ).fetchone()
            if existing is not None:
                if existing["request_fingerprint"] != fingerprint:
                    raise BudgetOperationConflict(
                        f"budget operation {operation_id} is bound to another request"
                    )
                return self._snapshot_from_row(row, now=now)
            if row["stop_reason"] is not None:
                stop_reason = str(row["stop_reason"])
                dimension = stop_reason.removeprefix("budget_exhausted:")
                if dimension in _LIMIT_FIELDS:
                    raise BudgetExceeded(
                        dimension,
                        self._snapshot_from_row(row, now=now),
                    )
            if row["finalized_at"] is not None:
                raise BudgetOperationConflict("run budget ledger is already finalized")
            used = BudgetAmounts.from_value(json.loads(row["used_json"]))
            reserved = BudgetAmounts.from_value(json.loads(row["reserved_json"]))
            limits = BudgetLimits.from_dict(json.loads(row["limits_json"]))
            elapsed = self._elapsed_ms(row, now)
            parent = None
            if parent_operation_id is not None:
                parent = self._operation(active, parent_operation_id)
                if parent["status"] != "reserved":
                    raise BudgetOperationConflict(
                        f"parent budget operation {parent_operation_id} is not reserved"
                    )
                parent_remaining = BudgetAmounts.from_value(
                    json.loads(parent["reserved_json"])
                )
                _subtract(parent_remaining, requested)
                exceeded = self._exceeded(
                    limits,
                    _add(used, reserved),
                    elapsed_ms=elapsed,
                )
            else:
                exceeded = self._exceeded(
                    limits,
                    _add(used, reserved, requested),
                    elapsed_ms=elapsed,
                )
            if exceeded is not None:
                stop_reason = row["stop_reason"] or f"budget_exhausted:{exceeded}"
                active.execute(
                    """
                    UPDATE run_budget_ledgers
                    SET stop_reason=?, updated_at=? WHERE root_run_id=?
                    """,
                    (stop_reason, now, self.root_run_id),
                )
                updated = self._load(active)
                snapshot = self._snapshot_from_row(updated, now=now)
                self._insert_trace(
                    active,
                    event="budget.exhausted",
                    operation_id=operation_id,
                    details={"dimension": exceeded, "ledger": snapshot},
                    now=now,
                )
                raise BudgetExceeded(exceeded, snapshot)
            next_reserved = (
                reserved
                if parent is not None
                else _add(reserved, requested)
            )
            if parent is not None:
                parent_remaining = BudgetAmounts.from_value(
                    json.loads(parent["reserved_json"])
                )
                active.execute(
                    """
                    UPDATE run_budget_operations SET reserved_json=?, updated_at=?
                    WHERE root_run_id=? AND operation_id=? AND status='reserved'
                    """,
                    (
                        _json(_subtract(parent_remaining, requested).to_dict()),
                        now,
                        self.root_run_id,
                        parent_operation_id,
                    ),
                )
            active.execute(
                """
                INSERT INTO run_budget_operations(
                    root_run_id, operation_id, owner_run_id, kind, status,
                    request_fingerprint, reserved_json, actual_json, usage_source,
                    cost_status, parent_operation_id, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'reserved', ?, ?, NULL, NULL, ?, ?, ?, ?, ?)
                """,
                (
                    self.root_run_id,
                    operation_id,
                    owner_run_id,
                    kind,
                    fingerprint,
                    _json(requested.to_dict()),
                    "known" if cost_known else "unknown",
                    parent_operation_id,
                    _json(safe_metadata),
                    now,
                    now,
                ),
            )
            active.execute(
                """
                UPDATE run_budget_ledgers SET reserved_json=?, updated_at=?
                WHERE root_run_id=?
                """,
                (_json(next_reserved.to_dict()), now, self.root_run_id),
            )
            updated = self._load(active)
            snapshot = self._snapshot_from_row(updated, now=now)
            self._insert_trace(
                active,
                event="budget.reserved",
                operation_id=operation_id,
                details={"kind": kind, "reserved": requested.to_dict(), "ledger": snapshot},
                now=now,
            )
            return snapshot

    def commit(
        self,
        operation_id: str,
        *,
        actual: BudgetAmounts | Mapping[str, Any],
        usage_source: str,
        cost_known: bool,
        metadata: Mapping[str, Any] | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        operation_id = _stable_id(operation_id, "operation_id")
        if usage_source not in {"provider_actual", "estimated", "reported", "none"}:
            raise ValueError("unsupported budget usage source")
        charged = BudgetAmounts.from_value(actual)
        safe_metadata = _safe_metadata(metadata)
        actual_payload = charged.to_dict()
        if not cost_known:
            actual_payload["cost_microusd"] = None
        now = self.state_store.now_iso()
        with self._write_connection(connection) as active:
            row = self._load(active)
            operation = self._operation(active, operation_id)
            if operation["status"] == "committed":
                if (
                    json.loads(operation["actual_json"]) != actual_payload
                    or operation["usage_source"] != usage_source
                    or operation["cost_status"] != ("known" if cost_known else "unknown")
                ):
                    raise BudgetOperationConflict(
                        f"budget operation {operation_id} was committed differently"
                    )
                return self._snapshot_from_row(row, now=now)
            if operation["status"] == "released":
                raise BudgetOperationConflict(
                    f"released budget operation {operation_id} cannot be committed"
                )
            reserved_for_operation = BudgetAmounts.from_value(
                json.loads(operation["reserved_json"])
            )
            used = BudgetAmounts.from_value(json.loads(row["used_json"]))
            reserved = BudgetAmounts.from_value(json.loads(row["reserved_json"]))
            next_reserved = _subtract(reserved, reserved_for_operation)
            next_used = _add(used, charged)
            limits = BudgetLimits.from_dict(json.loads(row["limits_json"]))
            elapsed = self._elapsed_ms(row, now)
            exceeded = self._exceeded(limits, _add(next_used, next_reserved), elapsed_ms=elapsed)
            stop_reason = row["stop_reason"]
            if exceeded is not None and stop_reason is None:
                stop_reason = f"budget_exhausted:{exceeded}"
            merged_metadata = {**json.loads(operation["metadata_json"]), **safe_metadata}
            active.execute(
                """
                UPDATE run_budget_operations
                SET status='committed', actual_json=?, usage_source=?, cost_status=?,
                    metadata_json=?, updated_at=?
                WHERE root_run_id=? AND operation_id=? AND status='reserved'
                """,
                (
                    _json(actual_payload),
                    usage_source,
                    "known" if cost_known else "unknown",
                    _json(merged_metadata),
                    now,
                    self.root_run_id,
                    operation_id,
                ),
            )
            active.execute(
                """
                UPDATE run_budget_ledgers
                SET used_json=?, reserved_json=?,
                    estimated_operations=estimated_operations+?,
                    unknown_cost_operations=unknown_cost_operations+?,
                    stop_reason=?, updated_at=?
                WHERE root_run_id=?
                """,
                (
                    _json(next_used.to_dict()),
                    _json(next_reserved.to_dict()),
                    int(usage_source == "estimated"),
                    int(not cost_known),
                    stop_reason,
                    now,
                    self.root_run_id,
                ),
            )
            updated = self._load(active)
            snapshot = self._snapshot_from_row(updated, now=now)
            self._insert_trace(
                active,
                event="budget.committed",
                operation_id=operation_id,
                details={
                    "kind": operation["kind"],
                    "usage": actual_payload,
                    "usage_source": usage_source,
                    "cost_status": "known" if cost_known else "unknown",
                    "ledger": snapshot,
                },
                now=now,
            )
            return snapshot

    def release(
        self,
        operation_id: str,
        *,
        reason: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        operation_id = _stable_id(operation_id, "operation_id")
        now = self.state_store.now_iso()
        with self._write_connection(connection) as active:
            row = self._load(active)
            operation = self._operation(active, operation_id)
            if operation["status"] in {"released", "committed"}:
                return self._snapshot_from_row(row, now=now)
            amount = BudgetAmounts.from_value(json.loads(operation["reserved_json"]))
            reserved = BudgetAmounts.from_value(json.loads(row["reserved_json"]))
            next_reserved = _subtract(reserved, amount)
            metadata = json.loads(operation["metadata_json"])
            if reason:
                metadata["status"] = str(reason)[:96]
            active.execute(
                """
                UPDATE run_budget_operations
                SET status='released', metadata_json=?, updated_at=?
                WHERE root_run_id=? AND operation_id=? AND status='reserved'
                """,
                (_json(metadata), now, self.root_run_id, operation_id),
            )
            active.execute(
                """
                UPDATE run_budget_ledgers SET reserved_json=?, updated_at=?
                WHERE root_run_id=?
                """,
                (_json(next_reserved.to_dict()), now, self.root_run_id),
            )
            updated = self._load(active)
            snapshot = self._snapshot_from_row(updated, now=now)
            self._insert_trace(
                active,
                event="budget.released",
                operation_id=operation_id,
                details={"kind": operation["kind"], "released": amount.to_dict(), "ledger": snapshot},
                now=now,
            )
            return snapshot

    def release_owner_reservations(
        self,
        owner_run_id: str,
        *,
        reason: str,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        """Release every remaining allocation for one child exactly once."""

        owner_run_id = _stable_id(owner_run_id, "owner_run_id")
        now = self.state_store.now_iso()
        with self._write_connection(connection) as active:
            row = self._load(active)
            operations = active.execute(
                """
                SELECT * FROM run_budget_operations
                WHERE root_run_id=? AND owner_run_id=? AND status='reserved'
                ORDER BY CASE WHEN parent_operation_id IS NULL THEN 1 ELSE 0 END,
                         operation_id
                """,
                (self.root_run_id, owner_run_id),
            ).fetchall()
            if not operations:
                return self._snapshot_from_row(row, now=now)
            released = BudgetAmounts()
            for operation in operations:
                amount = BudgetAmounts.from_value(json.loads(operation["reserved_json"]))
                released = _add(released, amount)
                metadata = json.loads(operation["metadata_json"])
                metadata["status"] = str(reason)[:96]
                active.execute(
                    """
                    UPDATE run_budget_operations
                    SET status='released', metadata_json=?, updated_at=?
                    WHERE root_run_id=? AND operation_id=? AND status='reserved'
                    """,
                    (
                        _json(metadata),
                        now,
                        self.root_run_id,
                        operation["operation_id"],
                    ),
                )
            reserved = BudgetAmounts.from_value(json.loads(row["reserved_json"]))
            active.execute(
                """
                UPDATE run_budget_ledgers SET reserved_json=?, updated_at=?
                WHERE root_run_id=?
                """,
                (
                    _json(_subtract(reserved, released).to_dict()),
                    now,
                    self.root_run_id,
                ),
            )
            updated = self._load(active)
            snapshot = self._snapshot_from_row(updated, now=now)
            self._insert_trace(
                active,
                event="budget.owner_released",
                operation_id=f"{owner_run_id}:release",
                details={
                    "released_operations": len(operations),
                    "released": released.to_dict(),
                    "ledger": snapshot,
                },
                now=now,
            )
            return snapshot

    def owner_usage(
        self,
        owner_run_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> BudgetAmounts:
        owner_run_id = _stable_id(owner_run_id, "owner_run_id")
        if connection is None:
            with self.state_store.connect() as active:
                return self.owner_usage(owner_run_id, connection=active)
        rows = connection.execute(
            """
            SELECT actual_json FROM run_budget_operations
            WHERE root_run_id=? AND owner_run_id=? AND status='committed'
            """,
            (self.root_run_id, owner_run_id),
        ).fetchall()
        total = BudgetAmounts()
        for row in rows:
            payload = json.loads(row["actual_json"] or "{}")
            payload["cost_microusd"] = payload.get("cost_microusd") or 0
            total = _add(total, BudgetAmounts.from_value(payload))
        return total

    def owner_snapshot(self, owner_run_id: str) -> dict[str, Any]:
        owner_run_id = _stable_id(owner_run_id, "owner_run_id")
        with self.state_store.connect() as connection:
            rows = connection.execute(
                """
                SELECT actual_json, usage_source, cost_status
                FROM run_budget_operations
                WHERE root_run_id=? AND owner_run_id=? AND status='committed'
                """,
                (self.root_run_id, owner_run_id),
            ).fetchall()
        total = BudgetAmounts()
        unknown_cost = False
        estimated = False
        for row in rows:
            payload = json.loads(row["actual_json"] or "{}")
            payload["cost_microusd"] = payload.get("cost_microusd") or 0
            total = _add(total, BudgetAmounts.from_value(payload))
            unknown_cost = unknown_cost or row["cost_status"] == "unknown"
            estimated = estimated or row["usage_source"] == "estimated"
        result: dict[str, Any] = {
            **total.to_dict(),
            "estimated": estimated,
            "cost_status": "unknown" if unknown_cost else "known",
            "cost_usd": (
                None if unknown_cost else _microusd_to_usd(total.cost_microusd)
            ),
            "known_cost_usd": _microusd_to_usd(total.cost_microusd),
        }
        return result

    def record_free_operation(
        self,
        operation_id: str,
        *,
        owner_run_id: str,
        kind: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.reserve(
            operation_id,
            owner_run_id=owner_run_id,
            kind=kind,
            amount=BudgetAmounts(),
            metadata=metadata,
        )
        return self.commit(
            operation_id,
            actual=BudgetAmounts(),
            usage_source="none",
            cost_known=True,
            metadata=metadata,
        )

    def finalize(
        self,
        operation_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        operation_id = _stable_id(operation_id, "operation_id")
        now = self.state_store.now_iso()
        with self._write_connection(connection) as active:
            row = self._load(active)
            if row["finalized_operation_id"] is not None:
                if row["finalized_operation_id"] != operation_id:
                    raise BudgetOperationConflict("run budget already has another finalizer")
                return self._snapshot_from_row(row, now=row["finalized_at"] or now)
            outstanding = active.execute(
                """
                SELECT operation_id FROM run_budget_operations
                WHERE root_run_id=? AND status='reserved'
                ORDER BY operation_id
                """,
                (self.root_run_id,),
            ).fetchall()
            active.execute(
                """
                UPDATE run_budget_operations SET status='released', updated_at=?
                WHERE root_run_id=? AND status='reserved'
                """,
                (now, self.root_run_id),
            )
            zero = BudgetAmounts().to_dict()
            active.execute(
                """
                INSERT INTO run_budget_operations(
                    root_run_id, operation_id, owner_run_id, kind, status,
                    request_fingerprint, reserved_json, actual_json, usage_source,
                    cost_status, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'finalizer', 'committed', ?, ?, ?, 'none',
                          'known', ?, ?, ?)
                """,
                (
                    self.root_run_id,
                    operation_id,
                    self.root_run_id,
                    _fingerprint({"kind": "finalizer", "operation_id": operation_id}),
                    _json(zero),
                    _json(zero),
                    _json({"status": "finalized"}),
                    now,
                    now,
                ),
            )
            active.execute(
                """
                UPDATE run_budget_ledgers
                SET reserved_json=?, finalized_operation_id=?, finalized_at=?, updated_at=?
                WHERE root_run_id=?
                """,
                (_json(zero), operation_id, now, now, self.root_run_id),
            )
            updated = self._load(active)
            snapshot = self._snapshot_from_row(updated, now=now)
            self._insert_trace(
                active,
                event="budget.finalized",
                operation_id=operation_id,
                details={
                    "released_operations": len(outstanding),
                    "ledger": snapshot,
                },
                now=now,
            )
            return snapshot

    def check_limits(self, *, persist: bool = True) -> dict[str, Any]:
        """Check elapsed wall time and persisted exhaustion without charging."""

        now = self.state_store.now_iso()
        with self.state_store.connect() as connection:
            if persist:
                connection.execute("BEGIN IMMEDIATE")
            row = self._load(connection)
            used = BudgetAmounts.from_value(json.loads(row["used_json"]))
            reserved = BudgetAmounts.from_value(json.loads(row["reserved_json"]))
            limits = BudgetLimits.from_dict(json.loads(row["limits_json"]))
            exceeded = self._exceeded(
                limits,
                _add(used, reserved),
                elapsed_ms=self._elapsed_ms(row, now),
            )
            stop_reason = row["stop_reason"]
            if exceeded is not None and stop_reason is None and persist:
                stop_reason = f"budget_exhausted:{exceeded}"
                connection.execute(
                    """
                    UPDATE run_budget_ledgers SET stop_reason=?, updated_at=?
                    WHERE root_run_id=?
                    """,
                    (stop_reason, now, self.root_run_id),
                )
                row = self._load(connection)
            snapshot = self._snapshot_from_row(row, now=now)
            if stop_reason is not None:
                dimension = str(stop_reason).removeprefix("budget_exhausted:")
                if dimension in _LIMIT_FIELDS:
                    if persist:
                        connection.commit()
                    raise BudgetExceeded(dimension, snapshot)
            if exceeded is not None:
                snapshot["stop_reason"] = f"budget_exhausted:{exceeded}"
                if persist:
                    connection.commit()
                raise BudgetExceeded(exceeded, snapshot)
            return snapshot

    def set_stop_reason(self, stop_reason: str) -> dict[str, Any]:
        if not isinstance(stop_reason, str) or not stop_reason.startswith(
            "budget_exhausted:"
        ):
            raise ValueError("budget stop reason must name an exhausted dimension")
        dimension = stop_reason.split(":", 1)[1]
        if dimension not in _LIMIT_FIELDS:
            raise ValueError("unknown budget exhaustion dimension")
        now = self.state_store.now_iso()
        with self.state_store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._load(connection)
            resolved = row["stop_reason"] or stop_reason
            connection.execute(
                """
                UPDATE run_budget_ledgers SET stop_reason=?, updated_at=?
                WHERE root_run_id=?
                """,
                (resolved, now, self.root_run_id),
            )
            return self._snapshot_from_row(self._load(connection), now=now)

    def snapshot(self) -> dict[str, Any]:
        now = self.state_store.now_iso()
        with self.state_store.connect() as connection:
            row = self._load(connection)
        return self._snapshot_from_row(row, now=row["finalized_at"] or now)

    def operation(
        self,
        operation_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        if connection is None:
            with self.state_store.connect() as active:
                return self.operation(operation_id, connection=active)
        row = connection.execute(
                """
                SELECT * FROM run_budget_operations
                WHERE root_run_id=? AND operation_id=?
                """,
                (self.root_run_id, operation_id),
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        for field in ("reserved_json", "actual_json", "metadata_json"):
            target = field.removesuffix("_json")
            value[target] = json.loads(value.pop(field)) if value[field] else None
        return value

    def _load(self, connection: sqlite3.Connection):
        row = connection.execute(
            """
            SELECT ledger.*,
                   EXISTS(
                       SELECT 1 FROM run_budget_operations AS operation
                       WHERE operation.root_run_id=ledger.root_run_id
                         AND operation.status='reserved'
                         AND operation.cost_status='unknown'
                   ) AS unknown_reserved_cost
            FROM run_budget_ledgers AS ledger
            WHERE ledger.root_run_id=?
            """,
            (self.root_run_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"run budget ledger does not exist: {self.root_run_id}")
        self._check_identity(row)
        return row

    def _operation(self, connection: sqlite3.Connection, operation_id: str):
        row = connection.execute(
            """
            SELECT * FROM run_budget_operations
            WHERE root_run_id=? AND operation_id=?
            """,
            (self.root_run_id, operation_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"budget operation does not exist: {operation_id}")
        return row

    @staticmethod
    def _elapsed_ms(row: Mapping[str, Any], now: str) -> int:
        started = datetime.fromisoformat(str(row["created_at"]))
        current = datetime.fromisoformat(str(now))
        return max(0, int((current - started).total_seconds() * 1000))

    @staticmethod
    def _exceeded(
        limits: BudgetLimits,
        projected: BudgetAmounts,
        *,
        elapsed_ms: int,
    ) -> str | None:
        for dimension in _EXHAUSTION_ORDER:
            limit = getattr(limits, _LIMIT_FIELDS[dimension])
            if limit is None:
                continue
            value = (
                elapsed_ms + projected.wall_time_ms
                if dimension == "wall_time_ms"
                else getattr(projected, dimension)
            )
            if value > limit or (dimension == "wall_time_ms" and value >= limit):
                return dimension
        return None

    def _snapshot_from_row(self, row: Mapping[str, Any], *, now: str) -> dict[str, Any]:
        used = BudgetAmounts.from_value(json.loads(row["used_json"])).to_dict()
        reserved = BudgetAmounts.from_value(json.loads(row["reserved_json"])).to_dict()
        limits = BudgetLimits.from_dict(json.loads(row["limits_json"])).to_dict()
        elapsed = self._elapsed_ms(row, now)
        used["wall_time_ms"] = elapsed
        unknown_cost = (
            int(row["unknown_cost_operations"]) > 0
            or bool(row["unknown_reserved_cost"])
        )
        snapshot: dict[str, Any] = {
            "schema_version": int(row["schema_version"]),
            "root_run_id": row["root_run_id"],
            **used,
            **limits,
            "reserved": reserved,
            "estimated": int(row["estimated_operations"]) > 0,
            "estimated_operations": int(row["estimated_operations"]),
            "cost_status": "unknown" if unknown_cost else "known",
            "cost_usd": None if unknown_cost else _microusd_to_usd(used["cost_microusd"]),
            "known_cost_usd": _microusd_to_usd(used["cost_microusd"]),
            "max_cost_usd": (
                _microusd_to_usd(limits["max_cost_microusd"])
                if limits["max_cost_microusd"] is not None
                else None
            ),
            "max_wall_time_seconds": limits["max_wall_time_ms"] / 1000,
            "wall_time_seconds": elapsed / 1000,
            "stop_reason": row["stop_reason"],
            "finalized": row["finalized_at"] is not None,
            "pricing_version": row["pricing_version"],
        }
        return snapshot

    def _insert_trace(
        self,
        connection: sqlite3.Connection,
        *,
        event: str,
        operation_id: str,
        details: Mapping[str, Any],
        now: str,
    ) -> None:
        run = connection.execute(
            "SELECT 1 FROM runs WHERE id=?",
            (self.root_run_id,),
        ).fetchone()
        if run is None:
            return
        connection.execute(
            """
            INSERT INTO provider_events(
                run_id, provider, event, error_class, attempt, details_json, created_at
            ) VALUES (?, 'budget', ?, NULL, 0, ?, ?)
            """,
            (
                self.root_run_id,
                event,
                _json({"operation_id": operation_id, **dict(details)}),
                now,
            ),
        )


def runtime_budget_limits(runtime, delegation=None) -> BudgetLimits:
    max_model_calls = int(runtime.max_model_calls)
    max_tool_calls = int(runtime.max_tool_calls)
    max_input_tokens = int(runtime.max_input_tokens)
    max_output_tokens = int(runtime.max_output_tokens)
    max_total_tokens = int(runtime.max_total_tokens)
    max_cost_usd = runtime.max_cost_usd
    if delegation is not None and getattr(delegation, "enabled", False):
        max_model_calls = min(max_model_calls, int(delegation.max_root_model_calls))
        max_tool_calls = min(max_tool_calls, int(delegation.max_root_tool_calls))
        max_total_tokens = min(max_total_tokens, int(delegation.max_root_tokens))
        max_input_tokens = min(max_input_tokens, max_total_tokens)
        max_output_tokens = min(max_output_tokens, max_total_tokens)
        delegation_cost = float(delegation.max_root_cost_usd)
        max_cost_usd = (
            delegation_cost
            if max_cost_usd is None
            else min(float(max_cost_usd), delegation_cost)
        )
    return BudgetLimits(
        max_model_calls=max_model_calls,
        max_tool_calls=max_tool_calls,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        max_total_tokens=max_total_tokens,
        max_cost_microusd=(
            _usd_to_microusd(max_cost_usd) if max_cost_usd is not None else None
        ),
        max_wall_time_ms=max(1, int(math.ceil(runtime.max_wall_time_seconds * 1000))),
    )


def _stable_id(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError(f"budget {name} must be a non-empty stable string")
    return value


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _finite_decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite non-negative number")
    try:
        number = Decimal(str(value))
    except Exception as error:
        raise ValueError(f"{name} must be a finite non-negative number") from error
    if not number.is_finite() or number < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return number


def _decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == 0:
        return "0"
    return format(normalized, "f")


def _usd_to_microusd(value: Any) -> int:
    return int(
        (_finite_decimal(value, "cost_usd") * Decimal(1_000_000)).to_integral_value(
            rounding=ROUND_CEILING
        )
    )


def _microusd_to_usd(value: int) -> float:
    return float(Decimal(value) / Decimal(1_000_000))


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _safe_metadata(value: Mapping[str, Any] | None) -> dict[str, Any]:
    source = dict(value or {})
    result: dict[str, Any] = {}
    for key in sorted(set(source) & _SAFE_METADATA_KEYS):
        item = source[key]
        if item is None or isinstance(item, (bool, int)):
            result[key] = item
        elif isinstance(item, float) and math.isfinite(item):
            result[key] = item
        elif isinstance(item, str):
            result[key] = item[:256]
    return result


def _add(*values: BudgetAmounts) -> BudgetAmounts:
    return BudgetAmounts(
        **{name: sum(getattr(value, name) for value in values) for name in _DIMENSIONS}
    )


def _subtract(left: BudgetAmounts, right: BudgetAmounts) -> BudgetAmounts:
    values = {name: getattr(left, name) - getattr(right, name) for name in _DIMENSIONS}
    if any(value < 0 for value in values.values()):
        raise BudgetOperationConflict("budget reservation counters are inconsistent")
    return BudgetAmounts(**values)


__all__ = [
    "BUDGET_LEDGER_MIGRATION",
    "BUDGET_LEDGER_SCHEMA_VERSION",
    "BudgetAmounts",
    "BudgetExceeded",
    "BudgetIdentityError",
    "BudgetLedgerError",
    "BudgetLimits",
    "BudgetOperationConflict",
    "DEFAULT_PRICING_VERSION",
    "ModelPriceCatalog",
    "RunBudgetLedger",
    "initialize_run_budget_schema",
    "runtime_budget_limits",
]
