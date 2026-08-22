from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
import uuid
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Iterator

from .events import RuntimeEvent, SCHEMA_VERSION
from .redaction import RedactionPolicy

_SOURCE_ORDER = {
    "run.queued": 10,
    "run.started": 20,
    "message.committed": 30,
    "provider.attempt": 40,
    "plan.created": 50,
    "plan.step": 60,
    "tool.completed": 70,
    "sandbox.completed": 71,
    "operation.state": 80,
    "evidence.recorded": 90,
    "artifact.created": 100,
    "subagent.state": 110,
    "session.lease": 120,
    "run.finished": 130,
    "schedule.state": 140,
    "audit.decision": 150,
}


def _loads(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _duration_ms(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        return max(0.0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() * 1000)
    except ValueError:
        return None


def _event_id(source: str, identity: Any, event_type: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"edu-agent:{source}:{identity}:{event_type}").hex


@dataclass(frozen=True)
class TracePage:
    events: list[RuntimeEvent]
    next_cursor: str | None
    total: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "events": [event.to_dict() for event in self.events],
            "next_cursor": self.next_cursor,
            "total": self.total,
        }


class TraceCursor(str):
    """Opaque v1 cursor with equality compatibility for the stage-7 offset test."""

    def __new__(cls, value: str, offset: int):
        instance = super().__new__(cls, value)
        instance.offset = offset
        return instance

    def __eq__(self, other: object) -> bool:
        if isinstance(other, int):
            return self.offset == other
        return super().__eq__(other)

    __hash__ = str.__hash__


class TraceRepository:
    """Projects durable state tables into the deterministic RuntimeEvent v1 trace.

    RunEventBus transport events are deliberately not a source: persisted domain
    and audit rows remain the only Trace truth and can rebuild the derived index.
    """

    def __init__(self, state_store, *, redaction: RedactionPolicy | None = None):
        self.state_store = state_store
        self.redaction = redaction or RedactionPolicy()
        self.cursor_ttl_seconds = 3600
        with self.state_store.connect() as connection:
            row = connection.execute(
                "SELECT value FROM trace_index_metadata WHERE key='cursor_hmac_v1'"
            ).fetchone()
        if row is None:
            raise RuntimeError("trace cursor key is missing; run the stage-8 state migration")
        self._cursor_key = bytes(row["value"])
        self.last_query_stats: dict[str, int] = {}

    @staticmethod
    def _tables(connection: sqlite3.Connection) -> set[str]:
        return {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }

    def _scope(
        self,
        connection: sqlite3.Connection,
        *,
        actor_id: str,
        tenant_id: str,
        run_id: str | None,
        session_id: str | None,
    ) -> tuple[str | None, str | None, dict[str, Any] | None]:
        run = None
        if run_id:
            row = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None and "delegation_runs" in self._tables(connection):
                row = connection.execute(
                    "SELECT id, session_id, actor_id, tenant_id FROM delegation_runs WHERE id=?",
                    (run_id,),
                ).fetchone()
            if row is None:
                raise KeyError(f"run not found: {run_id}")
            if row["actor_id"] != actor_id or row["tenant_id"] != tenant_id:
                raise PermissionError("trace run does not belong to actor/tenant")
            run = dict(row)
            session_id = session_id or row["session_id"]
            if session_id != row["session_id"]:
                raise PermissionError("trace run/session scope mismatch")
        if session_id:
            row = connection.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                delegated = bool(
                    "delegation_runs" in self._tables(connection)
                    and connection.execute(
                        "SELECT 1 FROM delegation_runs WHERE session_id=? AND actor_id=? AND tenant_id=?",
                        (session_id, actor_id, tenant_id),
                    ).fetchone()
                )
                if not delegated:
                    raise KeyError(f"session not found: {session_id}")
            elif row["actor_id"] != actor_id or row["tenant_id"] != tenant_id:
                raise PermissionError("trace session does not belong to actor/tenant")
        return run_id, session_id, run

    def _new_event(
        self,
        *,
        source: str,
        identity: Any,
        timestamp: str,
        run_id: str | None,
        root_run_id: str | None,
        parent_run_id: str | None,
        session_id: str | None,
        actor_id: str,
        tenant_id: str,
        component: str,
        event_type: str,
        status: str | None = None,
        duration_ms: float | None = None,
        usage: dict | None = None,
        error: dict | None = None,
        attributes: dict | None = None,
    ) -> RuntimeEvent:
        return RuntimeEvent(
            event_id=_event_id(source, identity, event_type),
            timestamp=timestamp,
            sequence=0,
            run_id=run_id,
            root_run_id=root_run_id or run_id,
            parent_run_id=parent_run_id,
            session_id=session_id,
            actor_id=actor_id,
            tenant_id=tenant_id,
            component=component,
            event_type=event_type,
            status=status,
            duration_ms=duration_ms,
            usage=self.redaction.redact(usage or {}),
            error=self.redaction.redact(error),
            attributes=self.redaction.redact(attributes or {}),
        )

    def _project(
        self,
        *,
        actor_id: str,
        tenant_id: str,
        run_id: str | None,
        session_id: str | None,
    ) -> list[RuntimeEvent]:
        events: list[RuntimeEvent] = []
        with self.state_store.connect() as connection:
            run_id, session_id, _ = self._scope(
                connection,
                actor_id=actor_id,
                tenant_id=tenant_id,
                run_id=run_id,
                session_id=session_id,
            )
            tables = self._tables(connection)
            params: list[Any] = [actor_id, tenant_id]
            clauses = ["actor_id=?", "tenant_id=?"]
            if run_id:
                clauses.append("id=?")
                params.append(run_id)
            if session_id:
                clauses.append("session_id=?")
                params.append(session_id)
            runs = connection.execute(
                f"SELECT * FROM runs WHERE {' AND '.join(clauses)}", params
            ).fetchall()
            run_map = {row["id"]: dict(row) for row in runs}
            selected_run_ids = set(run_map)

            delegation_by_run: dict[str, dict] = {}
            if "delegation_runs" in tables:
                dparams: list[Any] = [actor_id, tenant_id]
                dclauses = ["actor_id=?", "tenant_id=?"]
                if run_id:
                    dclauses.append("(id=? OR root_run_id=? OR parent_run_id=?)")
                    dparams.extend((run_id, run_id, run_id))
                elif session_id:
                    dclauses.append("session_id=?")
                    dparams.append(session_id)
                delegation = connection.execute(
                    f"SELECT * FROM delegation_runs WHERE {' AND '.join(dclauses)}", dparams
                ).fetchall()
                delegation_by_run = {row["id"]: dict(row) for row in delegation}
                selected_run_ids.update(delegation_by_run)

            for row in runs:
                lineage = delegation_by_run.get(row["id"], {})
                common = {
                    "run_id": row["id"],
                    "root_run_id": lineage.get("root_run_id") or row["id"],
                    "parent_run_id": lineage.get("parent_run_id"),
                    "session_id": row["session_id"],
                    "actor_id": actor_id,
                    "tenant_id": tenant_id,
                }
                queued_at = row["queued_at"] or row["started_at"]
                events.append(self._new_event(
                    source="runs", identity=f"{row['id']}:queued", timestamp=queued_at,
                    component="runtime", event_type="run.queued", status="queued",
                    attributes={"role": row["role"]}, **common,
                ))
                events.append(self._new_event(
                    source="runs", identity=f"{row['id']}:started", timestamp=row["started_at"],
                    component="runtime", event_type="run.started", status="running",
                    attributes={
                        "model": row["model"], "context_tokens": row["context_tokens"],
                        "omitted_messages": row["omitted_messages"],
                        "fencing_token": row["fencing_token"], "owner_id": row["owner_id"],
                        "request_sha256": hashlib.sha256((row["request_text"] or "").encode()).hexdigest(),
                        "request_characters": len(row["request_text"] or ""),
                    }, **common,
                ))
                if row["finished_at"] or row["status"] in {"failed", "interrupted", "abandoned", "completed"}:
                    events.append(self._new_event(
                        source="runs", identity=f"{row['id']}:finished",
                        timestamp=row["finished_at"] or row["heartbeat_at"] or row["started_at"],
                        component="runtime", event_type="run.finished", status=row["status"],
                        duration_ms=_duration_ms(row["started_at"], row["finished_at"]),
                        usage=_loads(row["budget_json"], {}),
                        error={"message": row["error"]} if row["error"] else None,
                        attributes={
                            "recovery_reason": row["recovery_reason"],
                            "recovery_recommendation": row["recovery_recommendation"],
                        }, **common,
                    ))

            if selected_run_ids:
                placeholders = ",".join("?" for _ in selected_run_ids)
                selected = tuple(selected_run_ids)
                messages = connection.execute(
                    f"SELECT * FROM messages WHERE run_id IN ({placeholders})", selected
                ).fetchall()
                for row in messages:
                    owner = run_map.get(row["run_id"], delegation_by_run.get(row["run_id"], {}))
                    tool_calls = _loads(row["tool_calls_json"], [])
                    events.append(self._new_event(
                        source="messages", identity=row["id"], timestamp=row["created_at"],
                        run_id=row["run_id"], root_run_id=owner.get("root_run_id") or row["run_id"],
                        parent_run_id=owner.get("parent_run_id"), session_id=row["session_id"],
                        actor_id=actor_id, tenant_id=tenant_id, component="conversation",
                        event_type="message.committed", status="active" if row["active"] else "compacted",
                        attributes={
                            "message_sequence": row["sequence"], "role": row["role"],
                            "name": row["name"], "tool_call_id": row["tool_call_id"],
                            "tool_names": [item.get("function", {}).get("name") for item in tool_calls],
                            "content_characters": len(row["content"] or ""),
                            "fencing_token": row["fencing_token"],
                        },
                    ))

                provider_rows = connection.execute(
                    f"SELECT * FROM provider_events WHERE run_id IN ({placeholders})", selected
                ).fetchall()
                for row in provider_rows:
                    owner = run_map.get(row["run_id"], delegation_by_run.get(row["run_id"], {}))
                    details = _loads(row["details_json"], {})
                    status = "failed" if row["error_class"] else details.get("status", "ok")
                    events.append(self._new_event(
                        source="provider_events", identity=row["id"], timestamp=row["created_at"],
                        run_id=row["run_id"], root_run_id=owner.get("root_run_id") or row["run_id"],
                        parent_run_id=owner.get("parent_run_id"), session_id=owner.get("session_id"),
                        actor_id=actor_id, tenant_id=tenant_id, component="provider",
                        event_type="provider.attempt", status=status,
                        error={"class": row["error_class"]} if row["error_class"] else None,
                        attributes={"provider": row["provider"], "event": row["event"],
                                    "attempt": row["attempt"], "details": details},
                    ))

                tool_rows = connection.execute(
                    f"SELECT * FROM tool_events WHERE run_id IN ({placeholders})", selected
                ).fetchall()
                for row in tool_rows:
                    owner = run_map.get(row["run_id"], delegation_by_run.get(row["run_id"], {}))
                    outcome = _loads(row["outcome_json"], {})
                    error = outcome.get("error")
                    sandbox = row["tool_name"] == "run_code"
                    events.append(self._new_event(
                        source="tool_events", identity=row["id"], timestamp=row["created_at"],
                        run_id=row["run_id"], root_run_id=owner.get("root_run_id") or row["run_id"],
                        parent_run_id=owner.get("parent_run_id"), session_id=row["session_id"],
                        actor_id=actor_id, tenant_id=tenant_id,
                        component="sandbox" if sandbox else "tool",
                        event_type="sandbox.completed" if sandbox else "tool.completed",
                        status="ok" if outcome.get("ok", not error) else "failed",
                        duration_ms=row["duration_ms"], error=error,
                        attributes={
                            "tool_event_id": row["id"], "tool_call_id": row["tool_call_id"],
                            "operation_id": row["operation_id"],
                            "operation_status": row["operation_status"],
                            "tool": row["tool_name"],
                            "arguments": _loads(row["arguments_json"], {}),
                            "outcome_meta": outcome.get("meta", {}),
                        },
                    ))

                op_rows = connection.execute(
                    f"SELECT * FROM tool_operation_refs WHERE run_id IN ({placeholders})", selected
                ).fetchall()
                for row in op_rows:
                    owner = run_map.get(row["run_id"], delegation_by_run.get(row["run_id"], {}))
                    events.append(self._new_event(
                        source="tool_operation_refs", identity=row["operation_id"], timestamp=row["updated_at"],
                        run_id=row["run_id"], root_run_id=owner.get("root_run_id") or row["run_id"],
                        parent_run_id=owner.get("parent_run_id"), session_id=row["session_id"],
                        actor_id=actor_id, tenant_id=tenant_id, component="transaction",
                        event_type="operation.state", status=row["status"],
                        attributes={
                            "operation_id": row["operation_id"], "tool": row["tool_name"],
                            "plan_step_id": row["plan_step_id"], "tool_call_id": row["tool_call_id"],
                            "payload_hash": row["payload_hash"],
                        },
                    ))

                plan_rows = connection.execute(
                    f"SELECT * FROM plans WHERE run_id IN ({placeholders})", selected
                ).fetchall()
                plan_ids = [row["id"] for row in plan_rows]
                for row in plan_rows:
                    owner = run_map.get(row["run_id"], {})
                    events.append(self._new_event(
                        source="plans", identity=row["id"], timestamp=row["created_at"],
                        run_id=row["run_id"], root_run_id=owner.get("root_run_id") or row["run_id"],
                        parent_run_id=owner.get("parent_run_id"), session_id=row["session_id"],
                        actor_id=actor_id, tenant_id=tenant_id, component="planning",
                        event_type="plan.created", status=row["status"],
                        usage={"iterations_used": row["iterations_used"],
                               "max_iterations": row["max_iterations"]},
                        error={"message": row["failure_reason"]} if row["failure_reason"] else None,
                        attributes={"plan_id": row["id"], "goal": row["goal"]},
                    ))
                if plan_ids:
                    plan_marks = ",".join("?" for _ in plan_ids)
                    plan_owner = {row["id"]: dict(row) for row in plan_rows}
                    steps = connection.execute(
                        f"SELECT * FROM plan_steps WHERE plan_id IN ({plan_marks})", tuple(plan_ids)
                    ).fetchall()
                    for row in steps:
                        plan = plan_owner[row["plan_id"]]
                        events.append(self._new_event(
                            source="plan_steps", identity=f"{row['plan_id']}:{row['step_id']}",
                            timestamp=row["updated_at"], run_id=plan["run_id"],
                            root_run_id=plan["run_id"], parent_run_id=None,
                            session_id=plan["session_id"], actor_id=actor_id, tenant_id=tenant_id,
                            component="planning", event_type="plan.step", status=row["status"],
                            attributes={
                                "plan_id": row["plan_id"], "step_id": row["step_id"],
                                "position": row["position"], "goal": row["goal"],
                                "depends_on": _loads(row["depends_on_json"], []),
                                "retry_count": row["retry_count"], "event_cursor": row["event_cursor"],
                            },
                        ))
                    evidence = connection.execute(
                        f"SELECT * FROM evidence WHERE plan_id IN ({plan_marks})", tuple(plan_ids)
                    ).fetchall()
                    for row in evidence:
                        events.append(self._new_event(
                            source="evidence", identity=row["id"], timestamp=row["created_at"],
                            run_id=row["run_id"], root_run_id=row["run_id"], parent_run_id=None,
                            session_id=row["session_id"], actor_id=actor_id, tenant_id=tenant_id,
                            component="evidence", event_type="evidence.recorded", status=row["status"],
                            error={"message": row["failure_reason"]} if row["failure_reason"] else None,
                            attributes={
                                "evidence_id": row["id"], "plan_id": row["plan_id"],
                                "step_id": row["step_id"], "kind": row["kind"],
                                "tool": row["tool_name"], "tool_event_id": row["tool_event_id"],
                                "operation_id": row["operation_id"], "artifact_id": row["artifact_id"],
                                "citation": row["citation"], "payload": _loads(row["payload_json"], {}),
                            },
                        ))

                artifacts = connection.execute(
                    f"SELECT * FROM artifacts WHERE run_id IN ({placeholders})", selected
                ).fetchall()
                for row in artifacts:
                    owner = run_map.get(row["run_id"], delegation_by_run.get(row["run_id"], {}))
                    events.append(self._new_event(
                        source="artifacts", identity=row["id"], timestamp=row["created_at"],
                        run_id=row["run_id"], root_run_id=owner.get("root_run_id") or row["run_id"],
                        parent_run_id=owner.get("parent_run_id"), session_id=row["session_id"],
                        actor_id=actor_id, tenant_id=tenant_id, component="artifact",
                        event_type="artifact.created", status="available",
                        attributes={
                            "artifact_id": row["id"], "kind": row["kind"],
                            "sha256": row["sha256"], "size_bytes": row["size_bytes"],
                            "metadata": _loads(row["metadata_json"], {}),
                        },
                    ))

            for row in delegation_by_run.values():
                if run_id and not (
                    row["id"] == run_id or row["root_run_id"] == run_id or row["parent_run_id"] == run_id
                ):
                    continue
                events.append(self._new_event(
                    source="delegation_runs", identity=row["id"],
                    timestamp=row["finished_at"] or row["started_at"] or row["created_at"],
                    run_id=row["id"], root_run_id=row["root_run_id"], parent_run_id=row["parent_run_id"],
                    session_id=row["session_id"], actor_id=actor_id, tenant_id=tenant_id,
                    component="delegation", event_type="subagent.state", status=row["status"],
                    duration_ms=_duration_ms(row["started_at"], row["finished_at"]),
                    usage=_loads(row["usage_json"], {}),
                    error={"message": row["failure_reason"] or row["cancel_reason"]}
                    if row["failure_reason"] or row["cancel_reason"] else None,
                    attributes={
                        "task_key": row["task_key"], "task_kind": row["task_kind"],
                        "depth": row["depth"], "model": row["model"],
                        "result_artifact_id": row["result_artifact_id"],
                    },
                ))

            if session_id:
                lease = connection.execute(
                    "SELECT * FROM session_leases WHERE session_id=?", (session_id,)
                ).fetchone()
                if lease:
                    events.append(self._new_event(
                        source="session_leases", identity=session_id,
                        timestamp=lease["heartbeat_at"], run_id=lease["active_run_id"],
                        root_run_id=lease["active_run_id"], parent_run_id=None,
                        session_id=session_id, actor_id=actor_id, tenant_id=tenant_id,
                        component="runtime", event_type="session.lease",
                        status="active" if lease["lease_owner"] else "released",
                        attributes={
                            "fencing_token": lease["fencing_token"],
                            "lease_owner": lease["lease_owner"], "expires_at": lease["expires_at"],
                        },
                    ))

            if not run_id and not session_id:
                job_rows = connection.execute(
                    "SELECT * FROM scheduled_jobs WHERE actor_id=? AND tenant_id=?",
                    (actor_id, tenant_id),
                ).fetchall()
                for row in job_rows:
                    events.append(self._new_event(
                        source="scheduled_jobs", identity=row["id"], timestamp=row["updated_at"],
                        run_id=None, root_run_id=None, parent_run_id=None, session_id=None,
                        actor_id=actor_id, tenant_id=tenant_id, component="scheduler",
                        event_type="schedule.state", status=row["status"],
                        error={"message": row["last_error"]} if row["last_error"] else None,
                        attributes={
                            "job_id": row["id"], "name": row["name"],
                            "attempt_count": row["attempt_count"], "max_attempts": row["max_attempts"],
                            "next_run_at": row["next_run_at"], "execution_key": row["execution_key"],
                        },
                    ))
                audits = connection.execute(
                    "SELECT * FROM audit_events WHERE actor_id=? AND tenant_id=?",
                    (actor_id, tenant_id),
                ).fetchall()
                for row in audits:
                    events.append(self._new_event(
                        source="audit_events", identity=row["id"], timestamp=row["created_at"],
                        run_id=None, root_run_id=None, parent_run_id=None, session_id=None,
                        actor_id=actor_id, tenant_id=tenant_id, component="security",
                        event_type="audit.decision", status=row["decision"],
                        attributes={"action": row["action"], "resource": row["resource"],
                                    "details": _loads(row["details_json"], {})},
                    ))

        events.sort(key=lambda event: (
            event.timestamp,
            _SOURCE_ORDER.get(event.event_type, 999),
            int(event.attributes.get("fencing_token") or 0),
            int(event.attributes.get("message_sequence") or event.attributes.get("tool_event_id") or 0),
            event.event_id,
        ))
        return [replace(event, sequence=index) for index, event in enumerate(events, 1)]

    @staticmethod
    def _cursor_scope(
        *,
        actor_id: str,
        tenant_id: str,
        run_id: str | None,
        session_id: str | None,
        status: str | None,
        error: str | None,
        tool: str | None,
        provider: str | None,
        component: str | None,
    ) -> str:
        payload = json.dumps(
            {
                "actor_id": actor_id,
                "tenant_id": tenant_id,
                "run_id": run_id,
                "session_id": session_id,
                "status": status,
                "error": error,
                "tool": tool,
                "provider": provider,
                "component": component,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def _encode_cursor(self, payload: dict[str, Any]) -> str:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
        signature = hmac.new(self._cursor_key, encoded.encode(), hashlib.sha256).hexdigest()
        return f"v1.{encoded}.{signature}"

    def _decode_cursor(self, cursor: str, *, expected_scope: str) -> dict[str, Any]:
        try:
            version, encoded, signature = cursor.split(".", 2)
            expected = hmac.new(self._cursor_key, encoded.encode(), hashlib.sha256).hexdigest()
            if version != "v1" or not hmac.compare_digest(signature, expected):
                raise ValueError
            padding = "=" * (-len(encoded) % 4)
            payload = json.loads(base64.urlsafe_b64decode(encoded + padding))
            if payload.get("v") != 1 or payload.get("scope") != expected_scope:
                raise ValueError
            if float(payload["expires_at"]) <= datetime.now(UTC).timestamp():
                raise ValueError("trace cursor has expired")
            last = payload["last"]
            if not isinstance(last, list) or len(last) != 4:
                raise ValueError
            return payload
        except ValueError as error:
            if str(error) == "trace cursor has expired":
                raise
            raise ValueError("invalid trace cursor") from error
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise ValueError("invalid trace cursor") from error

    @staticmethod
    def _trace_clauses(
        *,
        actor_id: str,
        tenant_id: str,
        run_id: str | None,
        session_id: str | None,
        status: str | None,
        error: str | None,
        tool: str | None,
        provider: str | None,
        component: str | None,
        snapshot: int,
    ) -> tuple[list[str], list[Any]]:
        clauses = ["actor_id=?", "tenant_id=?", "id<=?"]
        params: list[Any] = [actor_id, tenant_id, snapshot]
        if run_id:
            clauses.append("(run_id=? OR root_run_id=? OR parent_run_id=?)")
            params.extend((run_id, run_id, run_id))
        if session_id:
            clauses.append("session_id=?")
            params.append(session_id)
        for column, value in (
            ("status", status),
            ("tool", tool),
            ("provider", provider),
            ("component", component),
        ):
            if value is not None:
                clauses.append(f"{column}=?")
                params.append(value)
        if error:
            clauses.append(
                "LOWER(COALESCE(error_text, '') || ' ' || payload_json) LIKE ? ESCAPE '\\'"
            )
            escaped = error.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append(f"%{escaped}%")
        return clauses, params

    def _indexed_event(self, row: sqlite3.Row) -> RuntimeEvent:
        payload = _loads(row["payload_json"], {})
        attributes: dict[str, Any] = {}
        usage: dict[str, Any] = {}
        error: dict[str, Any] | None = None
        duration_ms: float | None = None
        status = row["status"]
        source = row["source"]
        if row["event_type"] == "run.queued":
            attributes = {"role": payload.get("role")}
        elif row["event_type"] == "run.started":
            attributes = {
                "model": payload.get("model"),
                "context_tokens": payload.get("context_tokens"),
                "omitted_messages": payload.get("omitted_messages"),
                "fencing_token": payload.get("fencing_token"),
                "owner_id": payload.get("owner_id"),
                "request_sha256": payload.get("request_sha256"),
                "request_characters": payload.get("request_characters", 0),
            }
        elif row["event_type"] == "run.finished":
            duration_ms = _duration_ms(payload.get("started_at"), payload.get("finished_at"))
            usage = _loads(payload.get("budget_json"), {})
            error = {"message": payload["error"]} if payload.get("error") else None
            attributes = {
                "recovery_reason": payload.get("recovery_reason"),
                "recovery_recommendation": payload.get("recovery_recommendation"),
            }
        elif source == "messages":
            calls = _loads(payload.get("tool_calls_json"), [])
            attributes = {
                "message_sequence": payload.get("message_sequence"),
                "role": payload.get("role"),
                "name": payload.get("name"),
                "tool_call_id": payload.get("tool_call_id"),
                "tool_names": [item.get("function", {}).get("name") for item in calls],
                "content_characters": payload.get("content_characters", 0),
                "fencing_token": payload.get("fencing_token"),
            }
        elif source == "provider_events":
            details = _loads(payload.get("details_json"), {})
            status = "failed" if payload.get("error_class") else details.get("status", "ok")
            error = {"class": payload["error_class"]} if payload.get("error_class") else None
            attributes = {
                "provider": payload.get("provider"),
                "event": payload.get("event"),
                "attempt": payload.get("attempt"),
                "details": details,
            }
        elif source == "tool_events":
            outcome = _loads(payload.get("outcome_json"), {})
            tool_error = outcome.get("error")
            status = "ok" if outcome.get("ok", not tool_error) else "failed"
            error = tool_error
            duration_ms = payload.get("duration_ms")
            attributes = {
                "tool_event_id": payload.get("tool_event_id"),
                "tool_call_id": payload.get("tool_call_id"),
                "operation_id": payload.get("operation_id"),
                "operation_status": payload.get("operation_status"),
                "tool": payload.get("tool"),
                "arguments": _loads(payload.get("arguments_json"), {}),
                "outcome_meta": outcome.get("meta", {}),
            }
        elif source == "plans":
            usage = {
                "iterations_used": payload.get("iterations_used"),
                "max_iterations": payload.get("max_iterations"),
            }
            error = {"message": payload["failure_reason"]} if payload.get("failure_reason") else None
            attributes = {"plan_id": payload.get("plan_id"), "goal": payload.get("goal")}
        elif source == "plan_steps":
            error = {"message": payload["failure_reason"]} if payload.get("failure_reason") else None
            attributes = {
                "plan_id": payload.get("plan_id"),
                "step_id": payload.get("step_id"),
                "position": payload.get("position"),
                "goal": payload.get("goal"),
                "depends_on": _loads(payload.get("depends_on_json"), []),
                "retry_count": payload.get("retry_count"),
                "event_cursor": payload.get("event_cursor"),
            }
        elif source == "evidence":
            error = {"message": payload["failure_reason"]} if payload.get("failure_reason") else None
            attributes = {
                "evidence_id": payload.get("evidence_id"),
                "plan_id": payload.get("plan_id"),
                "step_id": payload.get("step_id"),
                "kind": payload.get("kind"),
                "tool": payload.get("tool"),
                "tool_event_id": payload.get("tool_event_id"),
                "operation_id": payload.get("operation_id"),
                "artifact_id": payload.get("artifact_id"),
                "citation": payload.get("citation"),
                "payload": _loads(payload.get("payload_json"), {}),
            }
        elif source == "artifacts":
            attributes = {
                "artifact_id": payload.get("artifact_id"),
                "kind": payload.get("kind"),
                "sha256": payload.get("sha256"),
                "size_bytes": payload.get("size_bytes"),
                "metadata": _loads(payload.get("metadata_json"), {}),
            }
        elif source == "delegation_runs":
            duration_ms = _duration_ms(payload.get("started_at"), payload.get("finished_at"))
            usage = _loads(payload.get("usage_json"), {})
            message = payload.get("failure_reason") or payload.get("cancel_reason")
            error = {"message": message} if message else None
            attributes = {
                "task_key": payload.get("task_key"),
                "task_kind": payload.get("task_kind"),
                "depth": payload.get("depth"),
                "model": payload.get("model"),
                "result_artifact_id": payload.get("result_artifact_id"),
            }
        elif source == "scheduled_jobs":
            error = {"message": payload["last_error"]} if payload.get("last_error") else None
            attributes = {key: payload.get(key) for key in (
                "job_id", "name", "attempt_count", "max_attempts", "next_run_at",
                "execution_key",
            )}
        elif source == "audit_events":
            attributes = {
                "action": payload.get("action"),
                "resource": payload.get("resource"),
                "details": _loads(payload.get("details_json"), {}),
            }
        elif source == "api_requests":
            attributes = {
                "request_id": payload.get("request_id"),
                "request_hash": payload.get("request_hash"),
                "run_id": payload.get("run_id"),
                "attempt": payload.get("attempt"),
                "response_hash": payload.get("response_hash"),
            }
        else:
            attributes = payload
        return self._new_event(
            source=source,
            identity=row["source_key"],
            timestamp=row["timestamp"],
            run_id=row["run_id"],
            root_run_id=row["root_run_id"],
            parent_run_id=row["parent_run_id"],
            session_id=row["session_id"],
            actor_id=row["actor_id"],
            tenant_id=row["tenant_id"],
            component=row["component"],
            event_type=row["event_type"],
            status=status,
            duration_ms=duration_ms,
            usage=usage,
            error=error,
            attributes=attributes,
        )

    def list_events(
        self,
        *,
        actor_id: str,
        tenant_id: str = "default",
        run_id: str | None = None,
        session_id: str | None = None,
        status: str | None = None,
        error: str | None = None,
        tool: str | None = None,
        provider: str | None = None,
        component: str | None = None,
        cursor: str | int | None = None,
        limit: int = 100,
    ) -> TracePage:
        if limit <= 0 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        if isinstance(cursor, int) and cursor != 0:
            raise ValueError("legacy trace cursor only supports zero; use the returned versioned cursor")
        scope = self._cursor_scope(
            actor_id=actor_id, tenant_id=tenant_id, run_id=run_id,
            session_id=session_id, status=status, error=error, tool=tool,
            provider=provider, component=component,
        )
        with self.state_store.connect() as connection:
            self._scope(
                connection,
                actor_id=actor_id,
                tenant_id=tenant_id,
                run_id=run_id,
                session_id=session_id,
            )
            if cursor in {None, "", 0}:
                snapshot = int(connection.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM trace_event_index"
                ).fetchone()[0])
                last: list[Any] | None = None
                offset = 0
                clauses, params = self._trace_clauses(
                    actor_id=actor_id, tenant_id=tenant_id, run_id=run_id,
                    session_id=session_id, status=status, error=error, tool=tool,
                    provider=provider, component=component, snapshot=snapshot,
                )
                total = int(connection.execute(
                    f"SELECT COUNT(*) FROM trace_event_index WHERE {' AND '.join(clauses)}",
                    params,
                ).fetchone()[0])
                query_count = 3
            else:
                decoded = self._decode_cursor(str(cursor), expected_scope=scope)
                snapshot = int(decoded["snapshot"])
                last = decoded["last"]
                offset = int(decoded["offset"])
                total = int(decoded["total"])
                clauses, params = self._trace_clauses(
                    actor_id=actor_id, tenant_id=tenant_id, run_id=run_id,
                    session_id=session_id, status=status, error=error, tool=tool,
                    provider=provider, component=component, snapshot=snapshot,
                )
                query_count = 2
            if last is not None:
                clauses.append(
                    """(
                        timestamp>? OR
                        (timestamp=? AND source_priority>?) OR
                        (timestamp=? AND source_priority=? AND fencing_sequence>?) OR
                        (timestamp=? AND source_priority=? AND fencing_sequence=? AND event_id>?)
                    )"""
                )
                params.extend((
                    last[0], last[0], last[1], last[0], last[1], last[2],
                    last[0], last[1], last[2], last[3],
                ))
            rows = connection.execute(
                f"""
                SELECT * FROM trace_event_index
                WHERE {' AND '.join(clauses)}
                ORDER BY timestamp, source_priority, fencing_sequence, event_id
                LIMIT ?
                """,
                (*params, limit + 1),
            ).fetchall()
        has_more = len(rows) > limit
        selected = rows[:limit]
        events = [
            replace(self._indexed_event(row), sequence=offset + index)
            for index, row in enumerate(selected, 1)
        ]
        next_cursor = None
        if has_more and selected:
            row = selected[-1]
            encoded_cursor = self._encode_cursor({
                "v": 1,
                "scope": scope,
                "snapshot": snapshot,
                "last": [
                    row["timestamp"], row["source_priority"],
                    row["fencing_sequence"], row["event_id"],
                ],
                "offset": offset + len(selected),
                "total": total,
                "expires_at": datetime.now(UTC).timestamp() + self.cursor_ttl_seconds,
            })
            next_cursor = TraceCursor(encoded_cursor, offset + len(selected))
        self.last_query_stats = {
            "sql_queries": query_count,
            "rows_loaded": len(rows),
            "snapshot": snapshot,
            "total": total,
        }
        return TracePage(events=events, next_cursor=next_cursor, total=total)

    def iter_export(self, *, format: str = "jsonl", page_size: int = 100, **query) -> Iterator[str]:
        if format not in {"json", "jsonl"}:
            raise ValueError("format must be json or jsonl")
        cursor: str | int | None = None
        first = True
        if format == "json":
            yield '{"schema_version":' + json.dumps(SCHEMA_VERSION) + ',"events":['
        while True:
            page = self.list_events(cursor=cursor, limit=page_size, **query)
            for event in page.events:
                encoded = json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True)
                if format == "jsonl":
                    yield encoded + "\n"
                else:
                    if not first:
                        yield ","
                    yield encoded
                    first = False
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        if format == "json":
            yield "]}"

    def inspect_run(self, run_id: str, *, actor_id: str, tenant_id: str = "default") -> dict:
        page = self.list_events(
            actor_id=actor_id, tenant_id=tenant_id, run_id=run_id, limit=500
        )
        events = page.events
        components = Counter(event.component for event in events)
        statuses = Counter(event.status for event in events if event.status)
        latencies = [event.duration_ms for event in events if event.duration_ms is not None]
        artifacts = [
            event.attributes for event in events if event.event_type == "artifact.created"
        ]
        plan_tree = [
            event.attributes | {"status": event.status}
            for event in events if event.event_type == "plan.step"
        ]
        subagents = [
            {
                "run_id": event.run_id,
                "parent_run_id": event.parent_run_id,
                "status": event.status,
                **event.attributes,
            }
            for event in events if event.event_type == "subagent.state"
        ]
        terminal = next((event for event in reversed(events) if event.event_type == "run.finished"), None)
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "summary": {
                "events": len(events), "components": dict(components), "statuses": dict(statuses),
                "duration_ms": terminal.duration_ms if terminal else None,
                "latency_ms": {
                    "total": round(sum(latencies), 3),
                    "max": round(max(latencies), 3) if latencies else 0.0,
                },
                "budget": terminal.usage if terminal else {},
                "recovery_recommendation": (
                    (terminal.attributes.get("recovery_recommendation") or "none")
                    if terminal else "wait_or_resume"
                ),
            },
            "plan_tree": plan_tree,
            "subagent_tree": subagents,
            "artifacts": artifacts,
            "timeline": [event.to_dict() for event in events],
        }
