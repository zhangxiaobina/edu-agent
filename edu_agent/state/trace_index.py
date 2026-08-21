"""Transactional, rebuildable trace projection index.

The source tables remain authoritative.  Triggers append immutable projection
rows in the same SQLite transaction so trace readers never mutate business
state and can paginate with a stable index-id snapshot.
"""

from __future__ import annotations

import sqlite3


def initialize_trace_index(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS trace_event_index (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL,
            source_key TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            source_priority INTEGER NOT NULL,
            fencing_sequence INTEGER NOT NULL DEFAULT 0,
            actor_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            run_id TEXT,
            root_run_id TEXT,
            parent_run_id TEXT,
            session_id TEXT,
            component TEXT NOT NULL,
            event_type TEXT NOT NULL,
            status TEXT,
            tool TEXT,
            provider TEXT,
            error_text TEXT,
            payload_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS trace_index_metadata (
            key TEXT PRIMARY KEY,
            value BLOB NOT NULL
        );
        INSERT OR IGNORE INTO trace_index_metadata(key, value)
            VALUES ('cursor_hmac_v1', randomblob(32));

        CREATE INDEX IF NOT EXISTS idx_trace_scope_order
            ON trace_event_index(
                actor_id, tenant_id, timestamp, source_priority,
                fencing_sequence, event_id
            );
        CREATE INDEX IF NOT EXISTS idx_trace_run_order
            ON trace_event_index(
                actor_id, tenant_id, run_id, timestamp, source_priority,
                fencing_sequence, event_id
            );
        CREATE INDEX IF NOT EXISTS idx_trace_session_order
            ON trace_event_index(
                actor_id, tenant_id, session_id, timestamp, source_priority,
                fencing_sequence, event_id
            );

        CREATE TRIGGER IF NOT EXISTS trace_runs_insert
        AFTER INSERT ON runs BEGIN
            INSERT INTO trace_event_index(
                event_id, source, source_key, timestamp, source_priority,
                fencing_sequence, actor_id, tenant_id, run_id, root_run_id,
                session_id, component, event_type, status, payload_json
            ) VALUES (
                'runs:' || NEW.id || ':queued', 'runs', NEW.id || ':queued',
                COALESCE(NEW.queued_at, NEW.started_at), 10, COALESCE(NEW.fencing_token, 0),
                NEW.actor_id, NEW.tenant_id, NEW.id, NEW.id, NEW.session_id,
                'runtime', 'run.queued', 'queued',
                json_object('role', NEW.role)
            );
        END;

        CREATE TRIGGER IF NOT EXISTS trace_runs_update
        AFTER UPDATE ON runs BEGIN
            INSERT OR IGNORE INTO trace_event_index(
                event_id, source, source_key, timestamp, source_priority,
                fencing_sequence, actor_id, tenant_id, run_id, root_run_id,
                session_id, component, event_type, status, error_text, payload_json
            ) SELECT
                'runs:' || NEW.id || ':started', 'runs', NEW.id || ':started',
                NEW.started_at, 20, COALESCE(NEW.fencing_token, 0), NEW.actor_id,
                NEW.tenant_id, NEW.id, NEW.id, NEW.session_id, 'runtime',
                'run.started', 'running', NULL,
                json_object(
                    'model', NEW.model, 'context_tokens', NEW.context_tokens,
                    'omitted_messages', NEW.omitted_messages,
                    'fencing_token', NEW.fencing_token, 'owner_id', NEW.owner_id,
                    'request_characters', LENGTH(COALESCE(NEW.request_text, ''))
                )
            WHERE NEW.status IN ('running', 'cancel_requested', 'completed', 'failed',
                                 'interrupted', 'abandoned');
            INSERT OR IGNORE INTO trace_event_index(
                event_id, source, source_key, timestamp, source_priority,
                fencing_sequence, actor_id, tenant_id, run_id, root_run_id,
                session_id, component, event_type, status, error_text, payload_json
            ) SELECT
                'runs:' || NEW.id || ':finished', 'runs', NEW.id || ':finished',
                COALESCE(NEW.finished_at, NEW.heartbeat_at, NEW.started_at), 130,
                COALESCE(NEW.fencing_token, 0), NEW.actor_id, NEW.tenant_id,
                NEW.id, NEW.id, NEW.session_id, 'runtime', 'run.finished',
                NEW.status, NEW.error,
                json_object(
                    'started_at', NEW.started_at, 'finished_at', NEW.finished_at,
                    'budget_json', NEW.budget_json, 'error', NEW.error,
                    'recovery_reason', NEW.recovery_reason,
                    'recovery_recommendation', NEW.recovery_recommendation
                )
            WHERE NEW.status IN ('completed', 'failed', 'interrupted', 'abandoned');
        END;

        CREATE TRIGGER IF NOT EXISTS trace_messages_insert
        AFTER INSERT ON messages WHEN NEW.run_id IS NOT NULL BEGIN
            INSERT OR IGNORE INTO trace_event_index(
                event_id, source, source_key, timestamp, source_priority,
                fencing_sequence, actor_id, tenant_id, run_id, root_run_id,
                session_id, component, event_type, status, payload_json
            ) SELECT
                'messages:' || NEW.id, 'messages', CAST(NEW.id AS TEXT), NEW.created_at,
                30, COALESCE(NEW.fencing_token, NEW.sequence), r.actor_id, r.tenant_id,
                NEW.run_id, NEW.run_id, NEW.session_id, 'conversation',
                'message.committed', CASE WHEN NEW.active THEN 'active' ELSE 'compacted' END,
                json_object(
                    'message_sequence', NEW.sequence, 'role', NEW.role, 'name', NEW.name,
                    'tool_call_id', NEW.tool_call_id, 'tool_calls_json', NEW.tool_calls_json,
                    'content_characters', LENGTH(COALESCE(NEW.content, '')),
                    'fencing_token', NEW.fencing_token
                )
            FROM runs r WHERE r.id=NEW.run_id;
        END;

        CREATE TRIGGER IF NOT EXISTS trace_provider_insert
        AFTER INSERT ON provider_events WHEN NEW.run_id IS NOT NULL BEGIN
            INSERT OR IGNORE INTO trace_event_index(
                event_id, source, source_key, timestamp, source_priority,
                fencing_sequence, actor_id, tenant_id, run_id, root_run_id,
                session_id, component, event_type, status, provider, error_text, payload_json
            ) SELECT
                'provider_events:' || NEW.id, 'provider_events', CAST(NEW.id AS TEXT),
                NEW.created_at, 40, NEW.id, r.actor_id, r.tenant_id, NEW.run_id,
                NEW.run_id, r.session_id, 'provider', 'provider.attempt',
                CASE WHEN NEW.error_class IS NULL THEN 'ok' ELSE 'failed' END,
                NEW.provider, NEW.error_class,
                json_object(
                    'provider', NEW.provider, 'event', NEW.event, 'attempt', NEW.attempt,
                    'error_class', NEW.error_class, 'details_json', NEW.details_json
                )
            FROM runs r WHERE r.id=NEW.run_id;
        END;

        CREATE TRIGGER IF NOT EXISTS trace_tool_insert
        AFTER INSERT ON tool_events BEGIN
            INSERT OR IGNORE INTO trace_event_index(
                event_id, source, source_key, timestamp, source_priority,
                fencing_sequence, actor_id, tenant_id, run_id, root_run_id,
                session_id, component, event_type, status, tool, error_text, payload_json
            ) SELECT
                'tool_events:' || NEW.id, 'tool_events', CAST(NEW.id AS TEXT),
                NEW.created_at, CASE WHEN NEW.tool_name='run_code' THEN 71 ELSE 70 END,
                NEW.id, r.actor_id, r.tenant_id, NEW.run_id, NEW.run_id, NEW.session_id,
                CASE WHEN NEW.tool_name='run_code' THEN 'sandbox' ELSE 'tool' END,
                CASE WHEN NEW.tool_name='run_code' THEN 'sandbox.completed' ELSE 'tool.completed' END,
                NEW.operation_status, NEW.tool_name, NEW.outcome_json,
                json_object(
                    'tool_event_id', NEW.id, 'tool_call_id', NEW.tool_call_id,
                    'operation_id', NEW.operation_id, 'operation_status', NEW.operation_status,
                    'tool', NEW.tool_name, 'arguments_json', NEW.arguments_json,
                    'outcome_json', NEW.outcome_json, 'duration_ms', NEW.duration_ms
                )
            FROM runs r WHERE r.id=NEW.run_id;
        END;

        CREATE TRIGGER IF NOT EXISTS trace_operation_insert
        AFTER INSERT ON tool_operation_refs BEGIN
            INSERT INTO trace_event_index(
                event_id, source, source_key, timestamp, source_priority,
                fencing_sequence, actor_id, tenant_id, run_id, root_run_id,
                session_id, component, event_type, status, tool, payload_json
            ) VALUES (
                'tool_operation_refs:' || NEW.operation_id || ':' || NEW.updated_at || ':' || NEW.status,
                'tool_operation_refs', NEW.operation_id || ':' || NEW.updated_at || ':' || NEW.status,
                NEW.updated_at, 80, 0, NEW.actor_id, NEW.tenant_id, NEW.run_id, NEW.run_id,
                NEW.session_id, 'transaction', 'operation.state', NEW.status, NEW.tool_name,
                json_object(
                    'operation_id', NEW.operation_id, 'tool', NEW.tool_name,
                    'plan_step_id', NEW.plan_step_id, 'tool_call_id', NEW.tool_call_id,
                    'payload_hash', NEW.payload_hash
                )
            ) ON CONFLICT(event_id) DO NOTHING;
        END;

        CREATE TRIGGER IF NOT EXISTS trace_operation_update
        AFTER UPDATE ON tool_operation_refs BEGIN
            INSERT INTO trace_event_index(
                event_id, source, source_key, timestamp, source_priority,
                fencing_sequence, actor_id, tenant_id, run_id, root_run_id,
                session_id, component, event_type, status, tool, payload_json
            ) VALUES (
                'tool_operation_refs:' || NEW.operation_id || ':' || NEW.updated_at || ':' || NEW.status,
                'tool_operation_refs', NEW.operation_id || ':' || NEW.updated_at || ':' || NEW.status,
                NEW.updated_at, 80, 0, NEW.actor_id, NEW.tenant_id, NEW.run_id, NEW.run_id,
                NEW.session_id, 'transaction', 'operation.state', NEW.status, NEW.tool_name,
                json_object(
                    'operation_id', NEW.operation_id, 'tool', NEW.tool_name,
                    'plan_step_id', NEW.plan_step_id, 'tool_call_id', NEW.tool_call_id,
                    'payload_hash', NEW.payload_hash
                )
            ) ON CONFLICT(event_id) DO NOTHING;
        END;

        CREATE TRIGGER IF NOT EXISTS trace_plans_insert
        AFTER INSERT ON plans BEGIN
            INSERT OR IGNORE INTO trace_event_index(
                event_id, source, source_key, timestamp, source_priority,
                fencing_sequence, actor_id, tenant_id, run_id, root_run_id,
                session_id, component, event_type, status, error_text, payload_json
            ) VALUES (
                'plans:' || NEW.id || ':created', 'plans', NEW.id || ':created', NEW.created_at,
                50, 0, NEW.actor_id, NEW.tenant_id, NEW.run_id, NEW.run_id, NEW.session_id,
                'planning', 'plan.created', NEW.status, NEW.failure_reason,
                json_object(
                    'plan_id', NEW.id, 'goal', NEW.goal, 'iterations_used', NEW.iterations_used,
                    'max_iterations', NEW.max_iterations, 'failure_reason', NEW.failure_reason
                )
            );
        END;

        CREATE TRIGGER IF NOT EXISTS trace_plans_update
        AFTER UPDATE ON plans BEGIN
            INSERT OR IGNORE INTO trace_event_index(
                event_id, source, source_key, timestamp, source_priority,
                fencing_sequence, actor_id, tenant_id, run_id, root_run_id,
                session_id, component, event_type, status, error_text, payload_json
            ) VALUES (
                'plans:' || NEW.id || ':' || NEW.updated_at || ':' || NEW.status,
                'plans', NEW.id || ':' || NEW.updated_at || ':' || NEW.status,
                NEW.updated_at, 55, NEW.iterations_used, NEW.actor_id, NEW.tenant_id,
                NEW.run_id, NEW.run_id, NEW.session_id, 'planning', 'plan.state',
                NEW.status, NEW.failure_reason,
                json_object(
                    'plan_id', NEW.id, 'goal', NEW.goal,
                    'iterations_used', NEW.iterations_used,
                    'max_iterations', NEW.max_iterations,
                    'failure_reason', NEW.failure_reason
                )
            );
        END;

        CREATE TRIGGER IF NOT EXISTS trace_plan_steps_insert
        AFTER INSERT ON plan_steps BEGIN
            INSERT OR IGNORE INTO trace_event_index(
                event_id, source, source_key, timestamp, source_priority,
                fencing_sequence, actor_id, tenant_id, run_id, root_run_id,
                session_id, component, event_type, status, error_text, payload_json
            ) SELECT
                'plan_steps:' || NEW.plan_id || ':' || NEW.step_id || ':' || NEW.updated_at || ':' || NEW.status,
                'plan_steps', NEW.plan_id || ':' || NEW.step_id || ':' || NEW.updated_at || ':' || NEW.status,
                NEW.updated_at, 60, NEW.position, p.actor_id, p.tenant_id, p.run_id, p.run_id,
                p.session_id, 'planning', 'plan.step', NEW.status, NEW.failure_reason,
                json_object(
                    'plan_id', NEW.plan_id, 'step_id', NEW.step_id, 'position', NEW.position,
                    'goal', NEW.goal, 'depends_on_json', NEW.depends_on_json,
                    'retry_count', NEW.retry_count, 'event_cursor', NEW.event_cursor,
                    'failure_reason', NEW.failure_reason
                )
            FROM plans p WHERE p.id=NEW.plan_id;
        END;

        CREATE TRIGGER IF NOT EXISTS trace_plan_steps_update
        AFTER UPDATE ON plan_steps BEGIN
            INSERT OR IGNORE INTO trace_event_index(
                event_id, source, source_key, timestamp, source_priority,
                fencing_sequence, actor_id, tenant_id, run_id, root_run_id,
                session_id, component, event_type, status, error_text, payload_json
            ) SELECT
                'plan_steps:' || NEW.plan_id || ':' || NEW.step_id || ':' || NEW.updated_at || ':' || NEW.status,
                'plan_steps', NEW.plan_id || ':' || NEW.step_id || ':' || NEW.updated_at || ':' || NEW.status,
                NEW.updated_at, 60, NEW.position, p.actor_id, p.tenant_id, p.run_id, p.run_id,
                p.session_id, 'planning', 'plan.step', NEW.status, NEW.failure_reason,
                json_object(
                    'plan_id', NEW.plan_id, 'step_id', NEW.step_id, 'position', NEW.position,
                    'goal', NEW.goal, 'depends_on_json', NEW.depends_on_json,
                    'retry_count', NEW.retry_count, 'event_cursor', NEW.event_cursor,
                    'failure_reason', NEW.failure_reason
                )
            FROM plans p WHERE p.id=NEW.plan_id;
        END;

        CREATE TRIGGER IF NOT EXISTS trace_evidence_insert
        AFTER INSERT ON evidence BEGIN
            INSERT OR IGNORE INTO trace_event_index(
                event_id, source, source_key, timestamp, source_priority,
                fencing_sequence, actor_id, tenant_id, run_id, root_run_id,
                session_id, component, event_type, status, tool, error_text, payload_json
            ) VALUES (
                'evidence:' || NEW.id, 'evidence', CAST(NEW.id AS TEXT), NEW.created_at,
                90, NEW.id, NEW.actor_id, NEW.tenant_id, NEW.run_id, NEW.run_id,
                NEW.session_id, 'evidence', 'evidence.recorded', NEW.status, NEW.tool_name,
                NEW.failure_reason,
                json_object(
                    'evidence_id', NEW.id, 'plan_id', NEW.plan_id, 'step_id', NEW.step_id,
                    'kind', NEW.kind, 'tool', NEW.tool_name,
                    'tool_event_id', NEW.tool_event_id, 'operation_id', NEW.operation_id,
                    'artifact_id', NEW.artifact_id, 'citation', NEW.citation,
                    'payload_json', NEW.payload_json, 'failure_reason', NEW.failure_reason
                )
            );
        END;

        CREATE TRIGGER IF NOT EXISTS trace_artifacts_insert
        AFTER INSERT ON artifacts BEGIN
            INSERT OR IGNORE INTO trace_event_index(
                event_id, source, source_key, timestamp, source_priority,
                fencing_sequence, actor_id, tenant_id, run_id, root_run_id,
                session_id, component, event_type, status, payload_json
            ) VALUES (
                'artifacts:' || NEW.id, 'artifacts', NEW.id, NEW.created_at, 100, 0,
                NEW.actor_id, NEW.tenant_id, NEW.run_id, NEW.run_id, NEW.session_id,
                'artifact', 'artifact.created', 'available',
                json_object(
                    'artifact_id', NEW.id, 'kind', NEW.kind, 'sha256', NEW.sha256,
                    'size_bytes', NEW.size_bytes, 'metadata_json', NEW.metadata_json
                )
            );
        END;

        CREATE TRIGGER IF NOT EXISTS trace_leases_insert
        AFTER INSERT ON session_leases BEGIN
            INSERT OR IGNORE INTO trace_event_index(
                event_id, source, source_key, timestamp, source_priority,
                fencing_sequence, actor_id, tenant_id, run_id, root_run_id,
                session_id, component, event_type, status, payload_json
            ) SELECT
                'session_leases:' || NEW.session_id || ':' || NEW.heartbeat_at || ':' || NEW.fencing_token,
                'session_leases', NEW.session_id || ':' || NEW.heartbeat_at || ':' || NEW.fencing_token,
                NEW.heartbeat_at, 120, NEW.fencing_token, s.actor_id, s.tenant_id,
                NEW.active_run_id, NEW.active_run_id, NEW.session_id, 'runtime', 'session.lease',
                CASE WHEN NEW.lease_owner IS NULL THEN 'released' ELSE 'active' END,
                json_object(
                    'fencing_token', NEW.fencing_token, 'lease_owner', NEW.lease_owner,
                    'expires_at', NEW.expires_at
                )
            FROM sessions s WHERE s.id=NEW.session_id;
        END;

        CREATE TRIGGER IF NOT EXISTS trace_leases_update
        AFTER UPDATE ON session_leases BEGIN
            INSERT OR IGNORE INTO trace_event_index(
                event_id, source, source_key, timestamp, source_priority,
                fencing_sequence, actor_id, tenant_id, run_id, root_run_id,
                session_id, component, event_type, status, payload_json
            ) SELECT
                'session_leases:' || NEW.session_id || ':' || NEW.heartbeat_at || ':' || NEW.fencing_token || ':' || COALESCE(NEW.lease_owner, 'released'),
                'session_leases', NEW.session_id || ':' || NEW.heartbeat_at || ':' || NEW.fencing_token || ':' || COALESCE(NEW.lease_owner, 'released'),
                NEW.heartbeat_at, 120, NEW.fencing_token, s.actor_id, s.tenant_id,
                NEW.active_run_id, NEW.active_run_id, NEW.session_id, 'runtime', 'session.lease',
                CASE WHEN NEW.lease_owner IS NULL THEN 'released' ELSE 'active' END,
                json_object(
                    'fencing_token', NEW.fencing_token, 'lease_owner', NEW.lease_owner,
                    'expires_at', NEW.expires_at
                )
            FROM sessions s WHERE s.id=NEW.session_id;
        END;

        CREATE TRIGGER IF NOT EXISTS trace_schedule_insert
        AFTER INSERT ON scheduled_jobs BEGIN
            INSERT OR IGNORE INTO trace_event_index(
                event_id, source, source_key, timestamp, source_priority,
                fencing_sequence, actor_id, tenant_id, component, event_type,
                status, error_text, payload_json
            ) VALUES (
                'scheduled_jobs:' || NEW.id || ':' || NEW.updated_at || ':' || NEW.status,
                'scheduled_jobs', NEW.id || ':' || NEW.updated_at || ':' || NEW.status,
                NEW.updated_at, 140, NEW.attempt_count, NEW.actor_id, NEW.tenant_id,
                'scheduler', 'schedule.state', NEW.status, NEW.last_error,
                json_object(
                    'job_id', NEW.id, 'name', NEW.name, 'attempt_count', NEW.attempt_count,
                    'max_attempts', NEW.max_attempts, 'next_run_at', NEW.next_run_at,
                    'execution_key', NEW.execution_key, 'last_error', NEW.last_error
                )
            );
        END;

        CREATE TRIGGER IF NOT EXISTS trace_schedule_update
        AFTER UPDATE ON scheduled_jobs BEGIN
            INSERT OR IGNORE INTO trace_event_index(
                event_id, source, source_key, timestamp, source_priority,
                fencing_sequence, actor_id, tenant_id, component, event_type,
                status, error_text, payload_json
            ) VALUES (
                'scheduled_jobs:' || NEW.id || ':' || NEW.updated_at || ':' || NEW.status,
                'scheduled_jobs', NEW.id || ':' || NEW.updated_at || ':' || NEW.status,
                NEW.updated_at, 140, NEW.attempt_count, NEW.actor_id, NEW.tenant_id,
                'scheduler', 'schedule.state', NEW.status, NEW.last_error,
                json_object(
                    'job_id', NEW.id, 'name', NEW.name, 'attempt_count', NEW.attempt_count,
                    'max_attempts', NEW.max_attempts, 'next_run_at', NEW.next_run_at,
                    'execution_key', NEW.execution_key, 'last_error', NEW.last_error
                )
            );
        END;

        CREATE TRIGGER IF NOT EXISTS trace_audit_insert
        AFTER INSERT ON audit_events BEGIN
            INSERT OR IGNORE INTO trace_event_index(
                event_id, source, source_key, timestamp, source_priority,
                fencing_sequence, actor_id, tenant_id, component, event_type,
                status, payload_json
            ) VALUES (
                'audit_events:' || NEW.id, 'audit_events', CAST(NEW.id AS TEXT),
                NEW.created_at, 150, NEW.id, NEW.actor_id, NEW.tenant_id,
                'security', 'audit.decision', NEW.decision,
                json_object(
                    'action', NEW.action, 'resource', NEW.resource,
                    'details_json', NEW.details_json
                )
            );
        END;

        CREATE TRIGGER IF NOT EXISTS trace_api_request_insert
        AFTER INSERT ON api_requests BEGIN
            INSERT OR IGNORE INTO trace_event_index(
                event_id, source, source_key, timestamp, source_priority,
                fencing_sequence, actor_id, tenant_id, run_id, root_run_id,
                component, event_type, status, error_text, payload_json
            ) VALUES (
                'api_requests:' || NEW.actor_id || ':' || NEW.tenant_id || ':' || NEW.request_id || ':' || NEW.updated_at || ':' || NEW.status,
                'api_requests', NEW.request_id || ':' || NEW.updated_at || ':' || NEW.status,
                NEW.updated_at, 145, NEW.attempt, NEW.actor_id, NEW.tenant_id,
                NEW.run_id, NEW.run_id, 'api', 'api.request', NEW.status, NEW.error_json,
                json_object(
                    'request_id', NEW.request_id, 'request_hash', NEW.request_hash,
                    'run_id', NEW.run_id, 'attempt', NEW.attempt,
                    'response_hash', NEW.response_hash
                )
            );
        END;

        CREATE TRIGGER IF NOT EXISTS trace_api_request_update
        AFTER UPDATE ON api_requests BEGIN
            INSERT OR IGNORE INTO trace_event_index(
                event_id, source, source_key, timestamp, source_priority,
                fencing_sequence, actor_id, tenant_id, run_id, root_run_id,
                component, event_type, status, error_text, payload_json
            ) VALUES (
                'api_requests:' || NEW.actor_id || ':' || NEW.tenant_id || ':' || NEW.request_id || ':' || NEW.updated_at || ':' || NEW.status,
                'api_requests', NEW.request_id || ':' || NEW.updated_at || ':' || NEW.status,
                NEW.updated_at, 145, NEW.attempt, NEW.actor_id, NEW.tenant_id,
                NEW.run_id, NEW.run_id, 'api', 'api.request', NEW.status, NEW.error_json,
                json_object(
                    'request_id', NEW.request_id, 'request_hash', NEW.request_hash,
                    'run_id', NEW.run_id, 'attempt', NEW.attempt,
                    'response_hash', NEW.response_hash
                )
            );
        END;
        """
    )
    _backfill_trace_index(connection)


def _backfill_trace_index(connection: sqlite3.Connection) -> None:
    # Existing histories are migrated without constructing a Python event list.
    connection.executescript(
        """
        INSERT OR IGNORE INTO trace_event_index(
            event_id, source, source_key, timestamp, source_priority, fencing_sequence,
            actor_id, tenant_id, run_id, root_run_id, session_id, component,
            event_type, status, payload_json
        ) SELECT
            'runs:' || id || ':queued', 'runs', id || ':queued',
            COALESCE(queued_at, started_at), 10, COALESCE(fencing_token, 0),
            actor_id, tenant_id, id, id, session_id, 'runtime', 'run.queued', 'queued',
            json_object('role', role)
        FROM runs WHERE actor_id IS NOT NULL AND tenant_id IS NOT NULL;

        INSERT OR IGNORE INTO trace_event_index(
            event_id, source, source_key, timestamp, source_priority, fencing_sequence,
            actor_id, tenant_id, run_id, root_run_id, session_id, component,
            event_type, status, payload_json
        ) SELECT
            'runs:' || id || ':started', 'runs', id || ':started', started_at, 20,
            COALESCE(fencing_token, 0), actor_id, tenant_id, id, id, session_id,
            'runtime', 'run.started', 'running', json_object(
                'model', model, 'context_tokens', context_tokens,
                'omitted_messages', omitted_messages, 'fencing_token', fencing_token,
                'owner_id', owner_id,
                'request_characters', LENGTH(COALESCE(request_text, ''))
            )
        FROM runs WHERE actor_id IS NOT NULL AND tenant_id IS NOT NULL
            AND status != 'queued';

        INSERT OR IGNORE INTO trace_event_index(
            event_id, source, source_key, timestamp, source_priority, fencing_sequence,
            actor_id, tenant_id, run_id, root_run_id, session_id, component,
            event_type, status, error_text, payload_json
        ) SELECT
            'runs:' || id || ':finished', 'runs', id || ':finished',
            COALESCE(finished_at, heartbeat_at, started_at), 130,
            COALESCE(fencing_token, 0), actor_id, tenant_id, id, id, session_id,
            'runtime', 'run.finished', status, error, json_object(
                'started_at', started_at, 'finished_at', finished_at,
                'budget_json', budget_json, 'error', error,
                'recovery_reason', recovery_reason,
                'recovery_recommendation', recovery_recommendation
            )
        FROM runs WHERE actor_id IS NOT NULL AND tenant_id IS NOT NULL
            AND status IN ('completed', 'failed', 'interrupted', 'abandoned');

        INSERT OR IGNORE INTO trace_event_index(
            event_id, source, source_key, timestamp, source_priority, fencing_sequence,
            actor_id, tenant_id, run_id, root_run_id, session_id, component,
            event_type, status, payload_json
        ) SELECT
            'messages:' || m.id, 'messages', CAST(m.id AS TEXT), m.created_at, 30,
            COALESCE(m.fencing_token, m.sequence), r.actor_id, r.tenant_id, m.run_id,
            m.run_id, m.session_id, 'conversation', 'message.committed',
            CASE WHEN m.active THEN 'active' ELSE 'compacted' END,
            json_object(
                'message_sequence', m.sequence, 'role', m.role, 'name', m.name,
                'tool_call_id', m.tool_call_id, 'tool_calls_json', m.tool_calls_json,
                'content_characters', LENGTH(COALESCE(m.content, '')),
                'fencing_token', m.fencing_token
            )
        FROM messages m JOIN runs r ON r.id=m.run_id WHERE m.run_id IS NOT NULL;

        INSERT OR IGNORE INTO trace_event_index(
            event_id, source, source_key, timestamp, source_priority, fencing_sequence,
            actor_id, tenant_id, run_id, root_run_id, session_id, component,
            event_type, status, provider, error_text, payload_json
        ) SELECT
            'provider_events:' || p.id, 'provider_events', CAST(p.id AS TEXT),
            p.created_at, 40, p.id, r.actor_id, r.tenant_id, p.run_id, p.run_id,
            r.session_id, 'provider', 'provider.attempt',
            CASE WHEN p.error_class IS NULL THEN 'ok' ELSE 'failed' END,
            p.provider, p.error_class, json_object(
                'provider', p.provider, 'event', p.event, 'attempt', p.attempt,
                'error_class', p.error_class, 'details_json', p.details_json
            )
        FROM provider_events p JOIN runs r ON r.id=p.run_id WHERE p.run_id IS NOT NULL;

        INSERT OR IGNORE INTO trace_event_index(
            event_id, source, source_key, timestamp, source_priority, fencing_sequence,
            actor_id, tenant_id, run_id, root_run_id, session_id, component,
            event_type, status, tool, error_text, payload_json
        ) SELECT
            'tool_events:' || t.id, 'tool_events', CAST(t.id AS TEXT), t.created_at,
            CASE WHEN t.tool_name='run_code' THEN 71 ELSE 70 END, t.id,
            r.actor_id, r.tenant_id, t.run_id, t.run_id, t.session_id,
            CASE WHEN t.tool_name='run_code' THEN 'sandbox' ELSE 'tool' END,
            CASE WHEN t.tool_name='run_code' THEN 'sandbox.completed' ELSE 'tool.completed' END,
            t.operation_status, t.tool_name, t.outcome_json, json_object(
                'tool_event_id', t.id, 'tool_call_id', t.tool_call_id,
                'operation_id', t.operation_id, 'operation_status', t.operation_status,
                'tool', t.tool_name, 'arguments_json', t.arguments_json,
                'outcome_json', t.outcome_json, 'duration_ms', t.duration_ms
            )
        FROM tool_events t JOIN runs r ON r.id=t.run_id;

        INSERT OR IGNORE INTO trace_event_index(
            event_id, source, source_key, timestamp, source_priority, fencing_sequence,
            actor_id, tenant_id, run_id, root_run_id, session_id, component,
            event_type, status, tool, payload_json
        ) SELECT
            'tool_operation_refs:' || operation_id || ':' || updated_at || ':' || status,
            'tool_operation_refs', operation_id || ':' || updated_at || ':' || status,
            updated_at, 80, 0, actor_id, tenant_id, run_id, run_id, session_id,
            'transaction', 'operation.state', status, tool_name, json_object(
                'operation_id', operation_id, 'tool', tool_name,
                'plan_step_id', plan_step_id, 'tool_call_id', tool_call_id,
                'payload_hash', payload_hash
            )
        FROM tool_operation_refs;

        INSERT OR IGNORE INTO trace_event_index(
            event_id, source, source_key, timestamp, source_priority, fencing_sequence,
            actor_id, tenant_id, run_id, root_run_id, session_id, component,
            event_type, status, error_text, payload_json
        ) SELECT
            'plans:' || id || ':created', 'plans', id || ':created', created_at,
            50, 0, actor_id, tenant_id, run_id, run_id, session_id, 'planning',
            'plan.created', status, failure_reason, json_object(
                'plan_id', id, 'goal', goal, 'iterations_used', iterations_used,
                'max_iterations', max_iterations, 'failure_reason', failure_reason
            )
        FROM plans;

        INSERT OR IGNORE INTO trace_event_index(
            event_id, source, source_key, timestamp, source_priority, fencing_sequence,
            actor_id, tenant_id, run_id, root_run_id, session_id, component,
            event_type, status, error_text, payload_json
        ) SELECT
            'plan_steps:' || s.plan_id || ':' || s.step_id || ':' || s.updated_at || ':' || s.status,
            'plan_steps', s.plan_id || ':' || s.step_id || ':' || s.updated_at || ':' || s.status,
            s.updated_at, 60, s.position, p.actor_id, p.tenant_id, p.run_id, p.run_id,
            p.session_id, 'planning', 'plan.step', s.status, s.failure_reason,
            json_object(
                'plan_id', s.plan_id, 'step_id', s.step_id, 'position', s.position,
                'goal', s.goal, 'depends_on_json', s.depends_on_json,
                'retry_count', s.retry_count, 'event_cursor', s.event_cursor,
                'failure_reason', s.failure_reason
            )
        FROM plan_steps s JOIN plans p ON p.id=s.plan_id;

        INSERT OR IGNORE INTO trace_event_index(
            event_id, source, source_key, timestamp, source_priority, fencing_sequence,
            actor_id, tenant_id, run_id, root_run_id, session_id, component,
            event_type, status, tool, error_text, payload_json
        ) SELECT
            'evidence:' || id, 'evidence', CAST(id AS TEXT), created_at, 90, id,
            actor_id, tenant_id, run_id, run_id, session_id, 'evidence',
            'evidence.recorded', status, tool_name, failure_reason, json_object(
                'evidence_id', id, 'plan_id', plan_id, 'step_id', step_id,
                'kind', kind, 'tool', tool_name, 'tool_event_id', tool_event_id,
                'operation_id', operation_id, 'artifact_id', artifact_id,
                'citation', citation, 'payload_json', payload_json,
                'failure_reason', failure_reason
            )
        FROM evidence;

        INSERT OR IGNORE INTO trace_event_index(
            event_id, source, source_key, timestamp, source_priority, fencing_sequence,
            actor_id, tenant_id, run_id, root_run_id, session_id, component,
            event_type, status, payload_json
        ) SELECT
            'artifacts:' || id, 'artifacts', id, created_at, 100, 0, actor_id,
            tenant_id, run_id, run_id, session_id, 'artifact', 'artifact.created',
            'available', json_object(
                'artifact_id', id, 'kind', kind, 'sha256', sha256,
                'size_bytes', size_bytes, 'metadata_json', metadata_json
            )
        FROM artifacts;

        INSERT OR IGNORE INTO trace_event_index(
            event_id, source, source_key, timestamp, source_priority, fencing_sequence,
            actor_id, tenant_id, run_id, root_run_id, session_id, component,
            event_type, status, payload_json
        ) SELECT
            'session_leases:' || l.session_id || ':' || l.heartbeat_at || ':' ||
                l.fencing_token || ':' || COALESCE(l.lease_owner, 'released'),
            'session_leases', l.session_id || ':' || l.heartbeat_at || ':' ||
                l.fencing_token || ':' || COALESCE(l.lease_owner, 'released'),
            l.heartbeat_at, 120, l.fencing_token, s.actor_id, s.tenant_id,
            l.active_run_id, l.active_run_id, l.session_id, 'runtime', 'session.lease',
            CASE WHEN l.lease_owner IS NULL THEN 'released' ELSE 'active' END,
            json_object(
                'fencing_token', l.fencing_token, 'lease_owner', l.lease_owner,
                'expires_at', l.expires_at
            )
        FROM session_leases l JOIN sessions s ON s.id=l.session_id;

        INSERT OR IGNORE INTO trace_event_index(
            event_id, source, source_key, timestamp, source_priority, fencing_sequence,
            actor_id, tenant_id, component, event_type, status, error_text, payload_json
        ) SELECT
            'scheduled_jobs:' || id || ':' || updated_at || ':' || status,
            'scheduled_jobs', id || ':' || updated_at || ':' || status,
            updated_at, 140, attempt_count, actor_id, tenant_id, 'scheduler',
            'schedule.state', status, last_error, json_object(
                'job_id', id, 'name', name, 'attempt_count', attempt_count,
                'max_attempts', max_attempts, 'next_run_at', next_run_at,
                'execution_key', execution_key, 'last_error', last_error
            )
        FROM scheduled_jobs;

        INSERT OR IGNORE INTO trace_event_index(
            event_id, source, source_key, timestamp, source_priority, fencing_sequence,
            actor_id, tenant_id, run_id, root_run_id, component, event_type,
            status, error_text, payload_json
        ) SELECT
            'api_requests:' || actor_id || ':' || tenant_id || ':' || request_id || ':' ||
                updated_at || ':' || status,
            'api_requests', request_id || ':' || updated_at || ':' || status,
            updated_at, 145, attempt, actor_id, tenant_id, run_id, run_id,
            'api', 'api.request', status, error_json, json_object(
                'request_id', request_id, 'request_hash', request_hash,
                'run_id', run_id, 'attempt', attempt, 'response_hash', response_hash
            )
        FROM api_requests;

        INSERT OR IGNORE INTO trace_event_index(
            event_id, source, source_key, timestamp, source_priority, fencing_sequence,
            actor_id, tenant_id, component, event_type, status, payload_json
        ) SELECT
            'audit_events:' || id, 'audit_events', CAST(id AS TEXT), created_at,
            150, id, actor_id, tenant_id, 'security', 'audit.decision', decision,
            json_object('action', action, 'resource', resource, 'details_json', details_json)
        FROM audit_events;
        """
    )


def initialize_delegation_trace_index(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TRIGGER IF NOT EXISTS trace_delegation_insert
        AFTER INSERT ON delegation_runs BEGIN
            INSERT OR IGNORE INTO trace_event_index(
                event_id, source, source_key, timestamp, source_priority,
                fencing_sequence, actor_id, tenant_id, run_id, root_run_id,
                parent_run_id, session_id, component, event_type, status,
                error_text, payload_json
            ) VALUES (
                'delegation_runs:' || NEW.id || ':' || NEW.created_at || ':' || NEW.status,
                'delegation_runs', NEW.id || ':' || NEW.created_at || ':' || NEW.status,
                NEW.created_at, 110, NEW.depth, NEW.actor_id, NEW.tenant_id,
                NEW.id, NEW.root_run_id, NEW.parent_run_id, NEW.session_id,
                'delegation', 'subagent.state', NEW.status,
                COALESCE(NEW.failure_reason, NEW.cancel_reason),
                json_object(
                    'task_key', NEW.task_key, 'task_kind', NEW.task_kind,
                    'depth', NEW.depth, 'model', NEW.model,
                    'usage_json', NEW.usage_json, 'result_artifact_id', NEW.result_artifact_id,
                    'started_at', NEW.started_at, 'finished_at', NEW.finished_at,
                    'failure_reason', NEW.failure_reason, 'cancel_reason', NEW.cancel_reason
                )
            );
        END;

        CREATE TRIGGER IF NOT EXISTS trace_delegation_update
        AFTER UPDATE ON delegation_runs BEGIN
            INSERT OR IGNORE INTO trace_event_index(
                event_id, source, source_key, timestamp, source_priority,
                fencing_sequence, actor_id, tenant_id, run_id, root_run_id,
                parent_run_id, session_id, component, event_type, status,
                error_text, payload_json
            ) VALUES (
                'delegation_runs:' || NEW.id || ':' || COALESCE(NEW.finished_at, NEW.heartbeat_at, NEW.started_at, NEW.created_at) || ':' || NEW.status,
                'delegation_runs', NEW.id || ':' || COALESCE(NEW.finished_at, NEW.heartbeat_at, NEW.started_at, NEW.created_at) || ':' || NEW.status,
                COALESCE(NEW.finished_at, NEW.heartbeat_at, NEW.started_at, NEW.created_at),
                110, NEW.depth, NEW.actor_id, NEW.tenant_id, NEW.id, NEW.root_run_id,
                NEW.parent_run_id, NEW.session_id, 'delegation', 'subagent.state', NEW.status,
                COALESCE(NEW.failure_reason, NEW.cancel_reason),
                json_object(
                    'task_key', NEW.task_key, 'task_kind', NEW.task_kind,
                    'depth', NEW.depth, 'model', NEW.model,
                    'usage_json', NEW.usage_json, 'result_artifact_id', NEW.result_artifact_id,
                    'started_at', NEW.started_at, 'finished_at', NEW.finished_at,
                    'failure_reason', NEW.failure_reason, 'cancel_reason', NEW.cancel_reason
                )
            );
        END;

        INSERT OR IGNORE INTO trace_event_index(
            event_id, source, source_key, timestamp, source_priority, fencing_sequence,
            actor_id, tenant_id, run_id, root_run_id, parent_run_id, session_id,
            component, event_type, status, error_text, payload_json
        ) SELECT
            'delegation_runs:' || id || ':' || COALESCE(finished_at, heartbeat_at, started_at, created_at) || ':' || status,
            'delegation_runs', id || ':' || COALESCE(finished_at, heartbeat_at, started_at, created_at) || ':' || status,
            COALESCE(finished_at, heartbeat_at, started_at, created_at), 110, depth,
            actor_id, tenant_id, id, root_run_id, parent_run_id, session_id,
            'delegation', 'subagent.state', status,
            COALESCE(failure_reason, cancel_reason), json_object(
                'task_key', task_key, 'task_kind', task_kind, 'depth', depth,
                'model', model, 'usage_json', usage_json,
                'result_artifact_id', result_artifact_id, 'started_at', started_at,
                'finished_at', finished_at, 'failure_reason', failure_reason,
                'cancel_reason', cancel_reason
            )
        FROM delegation_runs;
        """
    )


__all__ = ["initialize_delegation_trace_index", "initialize_trace_index"]
