from __future__ import annotations

import json
import sqlite3

import pytest

from edu_agent.agent.graph import run_agent
from edu_agent.agent.loop_journal import AgentLoopJournal
from edu_agent.data import db, generate
from edu_agent.engine.base import Engine, EngineResponse, ToolCall
from edu_agent.runtime.config import AppConfig, RuntimeConfig, SecurityConfig, StorageConfig
from edu_agent.runtime.models import RunContext
from edu_agent.runtime.tool_executor import ExecutionPolicy, PolicyToolExecutor
from edu_agent.runtime.transactions import InjectedFault, NamedFaultInjector
from edu_agent.runtime.transactions import (
    OperationUnavailable,
    TransactionalToolRuntime,
    approval_scope,
    idempotency_key,
    payload_hash,
)
from edu_agent.service import EduAgentService
from edu_agent.state import (
    AGENT_TOOL_MESSAGES_MIGRATION,
    FencingTokenRejected,
    RunCancelled,
    StateStore,
    ToolMessageConflict,
    ToolMessagePairingError,
)
from edu_agent.tools import registry
from edu_agent.tools.registry import ToolSpec


class ReadProvider:
    def __init__(self):
        self.calls = 0
        self.specs = {
            "read_once": ToolSpec(
                schema={
                    "name": "read_once",
                    "description": "read",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                },
                handler=lambda connection, **arguments: {},
                category="query",
            )
        }

    def openai_tools(self, **kwargs):
        return [
            {"type": "function", "function": spec.schema}
            for spec in self.specs.values()
        ]

    def get_spec(self, name):
        return self.specs.get(name)

    def dispatch(self, name, arguments, conn=None):
        self.calls += 1
        return {"value": self.calls}


class OneToolEngine(Engine):
    name = "one-tool"

    def __init__(self, *, tool_name: str = "read_once", call_id: str = "call-1"):
        self.tool_name = tool_name
        self.call_id = call_id
        self.calls = 0

    def chat(self, messages, tools):
        self.calls += 1
        if any(
            message.get("role") == "tool"
            and message.get("tool_call_id") == self.call_id
            for message in messages
        ):
            return EngineResponse(content="done")
        return EngineResponse(
            tool_calls=[ToolCall(self.call_id, self.tool_name, {})]
        )


class PlainEngine(Engine):
    name = "plain"

    def __init__(self):
        self.calls = 0

    def chat(self, messages, tools):
        self.calls += 1
        return EngineResponse(content=f"plain-{self.calls}")


class RawArgumentsEngine(OneToolEngine):
    def __init__(self, arguments):
        super().__init__()
        self.arguments = arguments

    def chat(self, messages, tools):
        if any(message.get("tool_call_id") == self.call_id for message in messages):
            return EngineResponse(content="done")
        return EngineResponse(
            tool_calls=[ToolCall(self.call_id, self.tool_name, self.arguments)]
        )


class TwoCallsInOneEnvelopeEngine(Engine):
    name = "two-calls"

    def chat(self, messages, tools):
        return EngineResponse(
            tool_calls=[
                ToolCall("call-a", "read_once", {}),
                ToolCall("call-b", "read_once", {}),
            ]
        )


def _active_context(
    store: StateStore,
    *,
    session_id: str = "session-1",
    run_id: str = "run-1",
    owner: str = "worker-1",
    role: str = "admin",
    replay_scope: str | None = None,
) -> RunContext:
    context = RunContext.create(
        session_id=session_id,
        run_id=run_id,
        actor_id="teacher-1",
        tenant_id="school-1",
        role=role,
        replay_scope=replay_scope,
        max_model_calls=12,
        max_tool_calls=12,
    )
    store.ensure_session(
        session_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        role=role,
    )
    store.enqueue_run(context, request_text="test")
    claim = store.acquire_session_lease(
        session_id=session_id,
        run_id=run_id,
        owner_id=owner,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        lease_seconds=60,
    )
    context.bind_runtime_control(
        lease_owner=owner,
        fencing_token=int(claim["fencing_token"]),
        control_check=lambda boundary: store.assert_run_writable(
            context,
            boundary=boundary,
        ),
    )
    store.start_run(
        run_id=run_id,
        session_id=session_id,
        model="test",
        context_tokens=0,
        omitted_messages=0,
        context=context,
    )
    return context


def _journal(store, context, provider, engine, *, faults=None):
    tools = provider.openai_tools()
    journal = AgentLoopJournal(
        store,
        context,
        tools=tools,
        engine=engine,
        fault_injector=faults,
    )
    journal.enter_planning()
    return journal


def _envelope(*call_ids: str) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": "read_once", "arguments": "{}"},
            }
            for call_id in call_ids
        ],
    }


def _result(call_id: str, value: int) -> dict:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": "read_once",
        "content": json.dumps(
            {"ok": True, "data": {"value": value}, "error": None, "meta": {}},
            ensure_ascii=False,
        ),
    }


def test_tool_message_migration_and_atomic_pairing_constraints(tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = _active_context(store)
    provider = ReadProvider()
    journal = _journal(store, context, provider, OneToolEngine())
    attempt = journal.start_model_attempt()

    committed = store.append_assistant_tool_envelope(
        context,
        _envelope("call-a", "call-b"),
        model_attempt=attempt,
    )
    replayed = store.append_assistant_tool_envelope(
        context,
        _envelope("call-a", "call-b"),
        model_attempt=attempt,
    )
    assert committed.replayed is False and replayed.replayed is True
    assert store.count("messages") == 1
    with pytest.raises(ToolMessagePairingError, match="paired append API"):
        store.append_messages(
            context.session_id,
            [_result("call-a", 1)],
            context=context,
        )

    with pytest.raises(ToolMessagePairingError, match="call order"):
        store.append_tool_result(
            context,
            _result("call-b", 2),
            model_attempt=attempt,
        )
    with pytest.raises(ToolMessagePairingError, match="model attempt"):
        store.append_tool_result(
            context,
            _result("call-a", 1),
            model_attempt=attempt + 1,
        )
    with pytest.raises(ToolMessagePairingError, match="name does not match"):
        store.append_tool_result(
            context,
            {**_result("call-a", 1), "name": "different-tool"},
            model_attempt=attempt,
        )
    first = store.append_tool_result(
        context,
        _result("call-a", 1),
        model_attempt=attempt,
    )
    first_replay = store.append_tool_result(
        context,
        _result("call-a", 1),
        model_attempt=attempt,
    )
    assert first.replayed is False and first_replay.replayed is True
    with pytest.raises(ToolMessageConflict, match="different result"):
        store.append_tool_result(
            context,
            _result("call-a", 99),
            model_attempt=attempt,
        )
    store.append_tool_result(
        context,
        _result("call-b", 2),
        model_attempt=attempt,
    )
    store.complete_tool_batch(context, model_attempt=attempt)

    assert [message["role"] for message in store.get_run_messages(context.run_id)] == [
        "assistant",
        "tool",
        "tool",
    ]
    records = store.list_tool_call_records(
        run_id=context.run_id,
        session_id=context.session_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
    )
    assert [(record["tool_call_id"], record["status"]) for record in records] == [
        ("call-a", "completed"),
        ("call-b", "completed"),
    ]
    idempotent_message = {
        "role": "assistant",
        "content": "plain",
        "idempotency_key": "plain-message",
    }
    store.append_messages(context.session_id, [idempotent_message], context=context)
    store.append_messages(context.session_id, [idempotent_message], context=context)
    assert store.count("messages") == 4
    with pytest.raises(ValueError, match="绑定不同消息"):
        store.append_messages(
            context.session_id,
            [{**idempotent_message, "content": "different"}],
            context=context,
        )
    snapshot = store.get_run_journal_snapshot(
        context.run_id,
        session_id=context.session_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
    )
    assert snapshot.phase.value == "verifying"
    assert snapshot.loop_cursor == 5
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM state_schema_migrations WHERE version=?",
            (AGENT_TOOL_MESSAGES_MIGRATION,),
        ).fetchone()[0] == 1
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(messages)")
        }
    assert {"idempotency_key", "model_attempt", "loop_cursor"} <= columns


def test_tool_result_json_is_structurally_redacted_before_persistence(tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = _active_context(store)
    provider = ReadProvider()
    journal = _journal(store, context, provider, OneToolEngine())
    attempt = journal.start_model_attempt()
    store.append_assistant_tool_envelope(
        context,
        _envelope("secret-result"),
        model_attempt=attempt,
    )
    secret = "result-secret-value"
    committed = store.append_tool_result(
        context,
        {
            "role": "tool",
            "tool_call_id": "secret-result",
            "name": "read_once",
            "content": json.dumps(
                {
                    "ok": True,
                    "data": {"api_key": secret},
                    "error": None,
                    "meta": {},
                }
            ),
        },
        model_attempt=attempt,
    )

    assert json.loads(committed.message["content"])["data"]["api_key"] == "[REDACTED]"
    assert secret.encode() not in (tmp_path / "state.db").read_bytes()


def test_duplicate_or_orphan_call_and_cross_run_result_are_rejected(tmp_path):
    store = StateStore(tmp_path / "state.db")
    first_context = _active_context(store)
    provider = ReadProvider()
    first = _journal(store, first_context, provider, OneToolEngine())
    attempt = first.start_model_attempt()

    with pytest.raises(ToolMessagePairingError, match="duplicate call id"):
        store.append_assistant_tool_envelope(
            first_context,
            _envelope("duplicate", "duplicate"),
            model_attempt=attempt,
        )
    store.append_assistant_tool_envelope(
        first_context,
        _envelope("owned-call"),
        model_attempt=attempt,
    )
    with pytest.raises(ToolMessageConflict, match="different assistant envelope"):
        store.append_assistant_tool_envelope(
            first_context,
            _envelope("other-call"),
            model_attempt=attempt,
        )
    with pytest.raises(ToolMessagePairingError, match="orphan"):
        store.append_tool_result(
            first_context,
            _result("missing-call", 1),
            model_attempt=attempt,
        )

    second_context = _active_context(
        store,
        session_id="session-2",
        run_id="run-2",
        owner="worker-2",
    )
    second = _journal(store, second_context, provider, OneToolEngine())
    second_attempt = second.start_model_attempt()
    with pytest.raises(ToolMessagePairingError, match="across runs"):
        store.append_tool_result(
            second_context,
            _result("owned-call", 1),
            model_attempt=second_attempt,
        )


def test_stale_fence_cannot_commit_tool_result(tmp_path):
    store = StateStore(tmp_path / "state.db")
    stale = _active_context(store)
    provider = ReadProvider()
    journal = _journal(store, stale, provider, OneToolEngine())
    attempt = journal.start_model_attempt()
    store.append_assistant_tool_envelope(
        stale,
        _envelope("fenced-call"),
        model_attempt=attempt,
    )
    assert store.release_session_lease(
        session_id=stale.session_id,
        run_id=stale.run_id,
        owner_id=stale.lease_owner,
        fencing_token=stale.fencing_token,
    )
    current = RunContext.create(
        session_id=stale.session_id,
        run_id=stale.run_id,
        actor_id=stale.actor_id,
        tenant_id=stale.tenant_id,
        role=stale.role,
    )
    claim = store.acquire_session_lease(
        session_id=current.session_id,
        run_id=current.run_id,
        owner_id="worker-2",
        actor_id=current.actor_id,
        tenant_id=current.tenant_id,
        lease_seconds=60,
    )
    current.bind_runtime_control(
        lease_owner="worker-2",
        fencing_token=int(claim["fencing_token"]),
        control_check=lambda boundary: None,
    )

    with pytest.raises(FencingTokenRejected):
        store.append_tool_result(
            stale,
            _result("fenced-call", 1),
            model_attempt=attempt,
        )
    store.append_tool_result(
        current,
        _result("fenced-call", 1),
        model_attempt=attempt,
    )
    assert store.count("messages") == 2


def test_cancelled_batch_closes_every_declared_call_without_running_the_rest(tmp_path):
    store = StateStore(tmp_path / "state.db")
    context = _active_context(store)
    provider = ReadProvider()

    def cancel_after_first(name, arguments, conn=None):
        provider.calls += 1
        store.cancel_run(
            context.run_id,
            actor_id=context.actor_id,
            tenant_id=context.tenant_id,
        )
        return {"value": 1}

    provider.dispatch = cancel_after_first
    with pytest.raises(RunCancelled):
        run_agent(
            "read twice",
            TwoCallsInOneEnvelopeEngine(),
            run_context=context,
            tools_provider=provider,
            tool_executor=PolicyToolExecutor(
                provider,
                policy=ExecutionPolicy.legacy_demo(),
                state_store=store,
            ),
            state_store=store,
        )
    protocol = store.get_run_messages(context.run_id)
    assert [message["role"] for message in protocol] == ["assistant", "tool", "tool"]
    assert [message["tool_call_id"] for message in protocol[1:]] == ["call-a", "call-b"]
    assert all(
        json.loads(message["content"])["error"]["code"] == "CANCELLED"
        for message in protocol[1:]
    )
    assert provider.calls == 1
    assert all(
        record["status"] == "completed"
        for record in store.list_tool_call_records(
            run_id=context.run_id,
            session_id=context.session_id,
            actor_id=context.actor_id,
            tenant_id=context.tenant_id,
        )
    )


@pytest.mark.parametrize(
    ("fault_point", "expected_messages", "expected_tool_calls"),
    [
        ("before_assistant_envelope_commit", 0, 1),
        ("after_assistant_envelope_commit", 1, 1),
        ("before_read_tool_result_commit", 1, 2),
        ("after_read_tool_result_commit", 2, 1),
    ],
)
def test_fault_windows_reenter_without_duplicate_messages(
    tmp_path,
    fault_point,
    expected_messages,
    expected_tool_calls,
):
    store = StateStore(tmp_path / "state.db")
    context = _active_context(store)
    provider = ReadProvider()
    engine = OneToolEngine()

    with pytest.raises(InjectedFault, match=fault_point):
        run_agent(
            "read",
            engine,
            run_context=context,
            tools_provider=provider,
            tool_executor=PolicyToolExecutor(
                provider,
                policy=ExecutionPolicy.legacy_demo(),
                state_store=store,
            ),
            state_store=store,
            loop_fault_injector=NamedFaultInjector(fault_point),
        )
    assert store.count("messages") == expected_messages

    result = run_agent(
        "read",
        engine,
        run_context=context,
        tools_provider=provider,
        tool_executor=PolicyToolExecutor(
            provider,
            policy=ExecutionPolicy.legacy_demo(),
            state_store=store,
        ),
        state_store=store,
    )
    assert result["final_answer"] == "done"
    assert provider.calls == expected_tool_calls
    assert [message["role"] for message in store.get_run_messages(context.run_id)] == [
        "assistant",
        "tool",
    ]
    assert store.count("agent_tool_envelopes") == 1
    assert store.count("agent_tool_calls") == 1


class CreateExamEngine(Engine):
    name = "create-exam"

    def __init__(self, call_id: str = "write-call"):
        self.call_id = call_id

    def chat(self, messages, tools):
        if any(message.get("tool_call_id") == self.call_id for message in messages):
            return EngineResponse(content="created")
        return EngineResponse(
            tool_calls=[
                ToolCall(
                    self.call_id,
                    "create_exam",
                    {"exam_name": "fault-window-exam", "class_id": 3, "course_id": 1},
                )
            ]
        )


def test_write_commit_fault_reentry_has_one_side_effect_and_paired_result(tmp_path):
    state = StateStore(tmp_path / "state.db")
    context = _active_context(state)
    business_path = tmp_path / "edu.db"
    generate.build(seed=42, out_path=business_path)
    connection = db.connect(business_path)
    engine = CreateExamEngine()
    try:
        with pytest.raises(InjectedFault):
            run_agent(
                "create",
                engine,
                db_conn=connection,
                run_context=context,
                tool_executor=PolicyToolExecutor(
                    registry,
                    policy=ExecutionPolicy.legacy_demo(),
                    state_store=state,
                ),
                state_store=state,
                loop_fault_injector=NamedFaultInjector(
                    "after_write_operation_commit_before_result"
                ),
            )
        assert connection.execute(
            "SELECT COUNT(*) FROM exams WHERE exam_name='fault-window-exam'"
        ).fetchone()[0] == 1
        assert state.count("messages") == 1

        result = run_agent(
            "create",
            engine,
            db_conn=connection,
            run_context=context,
            tool_executor=PolicyToolExecutor(
                registry,
                policy=ExecutionPolicy.legacy_demo(),
                state_store=state,
            ),
            state_store=state,
        )
        assert result["final_answer"] == "created"
        assert connection.execute(
            "SELECT COUNT(*) FROM exams WHERE exam_name='fault-window-exam'"
        ).fetchone()[0] == 1
        call = state.get_tool_call_record(
            run_id=context.run_id,
            tool_call_id="write-call",
            session_id=context.session_id,
            actor_id=context.actor_id,
            tenant_id=context.tenant_id,
        )
        assert call["status"] == "completed" and call["operation_id"]
        payload = json.loads(call["result_message"]["content"])
        assert payload["meta"]["operation_id"] == call["operation_id"]
    finally:
        connection.close()


def test_cross_run_idempotent_operation_receipt_can_pair_to_its_own_call(tmp_path):
    state = StateStore(tmp_path / "state.db")
    business_path = tmp_path / "edu.db"
    generate.build(seed=42, out_path=business_path)
    connection = db.connect(business_path)
    first = _active_context(state, replay_scope="scheduled:exam")
    second = _active_context(
        state,
        session_id="session-2",
        run_id="run-2",
        owner="worker-2",
        replay_scope="scheduled:exam",
    )
    try:
        for context, call_id in ((first, "first-call"), (second, "second-call")):
            result = run_agent(
                "create",
                CreateExamEngine(call_id),
                db_conn=connection,
                run_context=context,
                tool_executor=PolicyToolExecutor(
                    registry,
                    policy=ExecutionPolicy.legacy_demo(),
                    state_store=state,
                ),
                state_store=state,
            )
            assert result["final_answer"] == "created"
        assert connection.execute(
            "SELECT COUNT(*) FROM exams WHERE exam_name='fault-window-exam'"
        ).fetchone()[0] == 1
        second_call = state.get_tool_call_record(
            run_id=second.run_id,
            tool_call_id="second-call",
            session_id=second.session_id,
            actor_id=second.actor_id,
            tenant_id=second.tenant_id,
        )
        receipt = json.loads(second_call["result_message"]["content"])
        assert receipt["meta"]["idempotent_replay"] is True
        assert second_call["operation_id"] == state.get_tool_call_record(
            run_id=first.run_id,
            tool_call_id="first-call",
            session_id=first.session_id,
            actor_id=first.actor_id,
            tenant_id=first.tenant_id,
        )["operation_id"]
    finally:
        connection.close()


def test_uncertain_write_operation_is_never_executed_again(tmp_path):
    state = StateStore(tmp_path / "state.db")
    context = _active_context(state)
    business_path = tmp_path / "edu.db"
    generate.build(seed=42, out_path=business_path)
    connection = db.connect(business_path)
    runtime = TransactionalToolRuntime(state_store=state)
    arguments = {"exam_name": "uncertain", "class_id": 3, "course_id": 1}
    digest = payload_hash("create_exam", arguments)
    caller_key = "uncertain-operation-key"
    operation = runtime.prepare(
        connection,
        key=idempotency_key(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            session_id=context.session_id,
            run_id=context.run_id,
            plan_step_id=None,
            tool_call_id="uncertain-call",
            tool_name="create_exam",
            arguments=arguments,
            caller_key=caller_key,
        ),
        digest=digest,
        tool_name="create_exam",
        arguments=arguments,
        context=context,
        tool_call_id="uncertain-call",
        plan_step_id=None,
        scope=approval_scope(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            tool_name="create_exam",
            arguments=arguments,
        ),
    )
    connection.execute(
        "UPDATE tool_operations SET status='executing' WHERE id=?",
        (operation["id"],),
    )
    connection.commit()
    uncertain = runtime.get_operation(connection, operation["id"], context=context)
    state.upsert_tool_operation_ref(uncertain, context=context)
    called = 0

    def handler():
        nonlocal called
        called += 1
        return {"exam_id": 999}

    try:
        with pytest.raises(OperationUnavailable, match="executing"):
            runtime.execute(connection, uncertain, handler, context=context)
        assert called == 0
        outcome = PolicyToolExecutor(
            registry,
            policy=ExecutionPolicy(require_write_approval=False),
            state_store=state,
        ).execute(
            "create_exam",
            arguments,
            context,
            conn=connection,
            tool_call_id="uncertain-call",
            caller_idempotency_key=caller_key,
        )
        assert outcome.ok is False
        assert outcome.error["code"] == "OPERATION_UNAVAILABLE"
        assert outcome.meta["operation_status"] == "executing"
        assert connection.execute(
            "SELECT COUNT(*) FROM exams WHERE exam_name='uncertain'"
        ).fetchone()[0] == 0
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("arguments", "timeout", "expected_code", "expected_dispatches"),
    [
        ("{bad json", False, "INVALID_JSON", 0),
        ({}, True, "TOOL_TIMEOUT", 1),
    ],
)
def test_structured_rejection_and_timeout_each_commit_one_result(
    tmp_path,
    arguments,
    timeout,
    expected_code,
    expected_dispatches,
):
    state = StateStore(tmp_path / "state.db")
    context = _active_context(state)
    provider = ReadProvider()
    if timeout:
        def raise_timeout(name, tool_arguments, conn=None):
            provider.calls += 1
            raise TimeoutError("read deadline exceeded")

        provider.dispatch = raise_timeout
    result = run_agent(
        "read",
        RawArgumentsEngine(arguments),
        run_context=context,
        tools_provider=provider,
        tool_executor=PolicyToolExecutor(
            provider,
            policy=ExecutionPolicy.legacy_demo(),
            state_store=state,
        ),
        state_store=state,
    )
    assert result["final_answer"] == "done"
    protocol = state.get_run_messages(context.run_id)
    assert [message["role"] for message in protocol] == ["assistant", "tool"]
    assert json.loads(protocol[-1]["content"])["error"]["code"] == expected_code
    assert provider.calls == expected_dispatches


def test_service_persists_tool_protocol_once_and_keeps_model_order(tmp_path):
    provider = ReadProvider()
    engine = OneToolEngine()
    service = EduAgentService(
        engine,
        config=AppConfig(
            runtime=RuntimeConfig(max_model_calls=4, max_tool_calls=4),
            security=SecurityConfig(),
            storage=StorageConfig(state_path=str(tmp_path / "state.db")),
        ),
        tools_provider=provider,
    )
    result = service.chat(
        "read",
        actor_id="teacher-1",
        tenant_id="school-1",
        role="teacher",
    )
    messages = service.state_store.get_run_messages(result.run_id)
    assert [message["role"] for message in messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert messages[1]["tool_calls"][0]["id"] == messages[2]["tool_call_id"]
    assert provider.calls == 1
    with service.state_store.connect() as connection:
        envelope = connection.execute(
            "SELECT * FROM agent_tool_envelopes WHERE run_id=?",
            (result.run_id,),
        ).fetchone()
        journal = connection.execute(
            "SELECT * FROM run_journals WHERE run_id=?",
            (result.run_id,),
        ).fetchone()
        assert envelope["tool_manifest_hash"] == journal["tool_manifest_hash"]
        assert json.loads(envelope["provider_route_json"]) == json.loads(
            journal["provider_route_json"]
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM messages WHERE run_id=? AND role='tool'",
            (result.run_id,),
        ).fetchone()[0] == 1


def test_service_plain_answers_stay_on_the_single_compatibility_append_path(tmp_path):
    engine = PlainEngine()
    service = EduAgentService(
        engine,
        config=AppConfig(
            runtime=RuntimeConfig(max_model_calls=4, max_tool_calls=4),
            security=SecurityConfig(),
            storage=StorageConfig(state_path=str(tmp_path / "state.db")),
        ),
    )

    first = service.chat(
        "first",
        actor_id="teacher-1",
        tenant_id="school-1",
        role="teacher",
    )
    second = service.chat(
        "second",
        session_id=first.session_id,
        actor_id="teacher-1",
        tenant_id="school-1",
        role="teacher",
    )

    assert first.final_answer == "plain-1"
    assert second.final_answer == "plain-2"
    assert service.state_store.get_run_messages(second.run_id) == [
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "plain-2"},
    ]
    assert service.state_store.count("agent_tool_envelopes") == 0
    assert service.state_store.count("agent_tool_calls") == 0


def test_legacy_database_gets_r23_schema_without_losing_messages(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
                title TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                sequence INTEGER NOT NULL, role TEXT NOT NULL, content TEXT,
                name TEXT, tool_call_id TEXT, tool_calls_json TEXT,
                created_at TEXT NOT NULL, UNIQUE(session_id, sequence)
            );
            INSERT INTO sessions VALUES ('legacy', 'actor', 'tenant', 'old', 't0', 't0');
            INSERT INTO messages(session_id, sequence, role, content, created_at)
            VALUES ('legacy', 0, 'user', 'keep-me', 't0');
            """
        )
    store = StateStore(path)
    StateStore(path)
    assert store.get_messages("legacy") == [{"role": "user", "content": "keep-me"}]
    with store.connect() as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"agent_tool_envelopes", "agent_tool_calls"} <= tables
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 10
        assert connection.execute(
            "SELECT COUNT(*) FROM state_schema_migrations WHERE version=?",
            (AGENT_TOOL_MESSAGES_MIGRATION,),
        ).fetchone()[0] == 1
