"""R3.1 ToolManifest contracts: metadata, freezing and fail-closed registry edges."""
from __future__ import annotations

import copy
import json

import pytest

from edu_agent.engine.mock import MockEngine, final
from edu_agent.knowledge import KnowledgeToolProvider, SQLiteKnowledgeProvider, build_synthetic_corpus
from edu_agent.runtime.config import AppConfig, StorageConfig
from edu_agent.runtime.models import RunContext
from edu_agent.runtime.tool_executor import ExecutionPolicy, PolicyToolExecutor
from edu_agent.service import EduAgentService
from edu_agent.tools import registry
from edu_agent.tools.manifest import (
    ToolEffect,
    ToolManifest,
    ToolManifestEntry,
    ToolRegistrationError,
    ToolRisk,
    canonical_schema_hash,
)


def _context(*, role: str = "teacher", course_ids: set[int] | None = None) -> RunContext:
    return RunContext.create(
        session_id="manifest-session",
        actor_id="actor-1",
        tenant_id="tenant-1",
        role=role,
        course_ids={1} if course_ids is None else course_ids,
    )


def _schema(name: str = "plugin_tool") -> dict:
    return {
        "name": name,
        "description": "test",
        "parameters": {
            "type": "object",
            "properties": {"course_id": {"type": "integer"}},
            "required": [],
            "additionalProperties": False,
        },
    }


def _handler(conn, **kwargs):
    return {"ok": True, "arguments": kwargs}


def test_all_builtin_tools_have_complete_explicit_metadata():
    assert len(registry.TOOL_SPECS) == 16
    entries = registry.manifest_entries()
    assert len(entries) == 16
    assert {entry.name for entry in entries} == set(registry.tool_names())
    for entry in entries:
        assert entry.source.startswith("builtin:")
        assert entry.version
        assert len(entry.canonical_schema_hash) == 64
        assert entry.capabilities
        assert isinstance(entry.effect, ToolEffect)
        assert entry.risk in set(ToolRisk)
        assert isinstance(entry.parallel_safe, bool)
        assert entry.timeout > 0
        assert entry.allowed_roles
        assert entry.data_classification
        assert all(pointer.startswith("/") for pointer in entry.data_classification)
    assert registry.get_spec("run_code").effect is ToolEffect.CODE_EXECUTION
    assert not registry.get_spec("create_exam").parallel_safe
    assert registry.get_spec("generate_questions").effect is ToolEffect.CONDITIONAL_WRITE


def test_schema_hash_is_stable_across_mapping_order_and_manifest_is_immutable():
    first = _schema()
    second = {
        "parameters": {
            "additionalProperties": False,
            "required": [],
            "properties": {"course_id": {"type": "integer"}},
            "type": "object",
        },
        "description": "test",
        "name": "plugin_tool",
    }
    assert canonical_schema_hash(first) == canonical_schema_hash(second)
    entry = ToolManifestEntry(
        name="plugin_tool",
        schema=first,
        source="plugin:test",
        version="1.2.3",
        capability="teaching.query",
        risk="medium",
        effect=ToolEffect.READ,
        parallel_safe=True,
        handler=_handler,
    )
    manifest = ToolManifest((entry,))
    with pytest.raises(TypeError):
        entry.schema["name"] = "changed"
    assert manifest.manifest_hash == ToolManifest((entry,)).manifest_hash
    assert manifest.to_openai_tools()[0]["function"]["name"] == "plugin_tool"


def test_registry_schema_list_hash_keeps_r2_compatibility_and_manifest_hash_is_scope_bound():
    context = _context()
    manifest = registry.build_tool_manifest(context=context)
    schema_list = manifest.to_openai_tools()
    # The legacy list hash is intentionally the same helper used by the R2
    # journal, while the frozen manifest hash additionally binds run scope.
    from edu_agent.agent.loop_journal import tool_manifest_hash

    assert registry.manifest_hash(schema_list) == tool_manifest_hash(schema_list)
    other = registry.build_tool_manifest(
        context=_context(course_ids={2}),
    )
    assert other.manifest_hash != manifest.manifest_hash


def test_registration_validates_schema_handler_conflict_and_metadata(monkeypatch):
    name = "r31_validation_plugin"
    with pytest.raises(ToolRegistrationError):
        registry.register_tool(
            name=name,
            schema={"name": name, "parameters": {"type": "string"}},
            handler=_handler,
            category="query",
            source="plugin:test",
            capability="teaching.query",
            effect=ToolEffect.READ,
        )
    with pytest.raises(ToolRegistrationError):
        registry.register_tool(
            name=name,
            schema=_schema(name),
            handler=lambda conn: {},
            category="query",
            source="plugin:test",
            capability="teaching.query",
            effect=ToolEffect.READ,
        )
    with pytest.raises(ToolRegistrationError):
        registry.register_tool(
            name=name,
            schema=_schema(name),
            handler=_handler,
            category="query",
            source="plugin:test",
            capability="teaching.query",
            effect=ToolEffect.READ,
            parallel_safe=False,
            timeout=0,
        )

    registry.register_tool(
        name=name,
        schema=_schema(name),
        handler=_handler,
        category="query",
        source="plugin:test",
        version="1.0.0",
        capability="teaching.query",
        risk="low",
        effect=ToolEffect.READ,
        parallel_safe=True,
    )
    try:
        with pytest.raises(ToolRegistrationError, match="工具名/source 冲突"):
            registry.register_tool(
                name=name,
                schema=_schema(name),
                handler=_handler,
                category="query",
                source="plugin:other",
                capability="teaching.query",
                effect=ToolEffect.READ,
            )
    finally:
        registry.TOOL_SPECS.pop(name, None)
        registry.TOOL_FUNCTIONS.pop(name, None)


def test_unknown_plugin_defaults_highest_risk_nonparallel_and_hidden():
    name = "r31_unknown_plugin"
    registry.register_tool(
        name=name,
        schema=_schema(name),
        handler=_handler,
        category="query",
        source="plugin:unknown",
    )
    try:
        spec = registry.get_spec(name)
        assert spec.risk_level == "critical"
        assert spec.parallel_safe is False
        assert spec.capability is None
        assert name not in {item["function"]["name"] for item in registry.openai_tools()}
    finally:
        registry.TOOL_SPECS.pop(name, None)
        registry.TOOL_FUNCTIONS.pop(name, None)


def test_effect_is_explicit_and_plugin_side_effects_fail_closed():
    name = "r31_effect_plugin"
    with pytest.raises(ToolRegistrationError, match="写工具"):
        registry.register_tool(
            name=name,
            schema=_schema(name),
            handler=_handler,
            category="operation",
            source="plugin:effect",
            capability="teaching.write",
            risk="high",
            effect=ToolEffect.WRITE,
            parallel_safe=False,
        )
    with pytest.raises(ToolRegistrationError, match="code_execution"):
        registry.register_tool(
            name=name,
            schema=_schema(name),
            handler=_handler,
            category="execution",
            source="plugin:effect",
            capability="code_execution",
            risk="critical",
            effect=ToolEffect.CODE_EXECUTION,
            parallel_safe=False,
        )
    with pytest.raises(ToolRegistrationError, match="critical"):
        ToolManifestEntry(
            name=name,
            schema=_schema(name),
            source="plugin:effect",
            capability="teaching.query",
            risk="low",
            effect=ToolEffect.UNKNOWN,
            handler=_handler,
        )


def test_empty_course_scope_is_bound_and_resource_templates_use_context_identity():
    context = _context(course_ids=set())
    manifest = registry.build_tool_manifest(context=context)
    changed = _context(course_ids={99})
    assert manifest.matches_context(context)
    assert not manifest.matches_context(changed)
    entry = registry.get_spec("run_code")
    assert entry is not None
    resources = entry.to_manifest_entry().resource_keys_for(
        {"tenant_id": "attacker-tenant", "actor_id": "attacker-actor"},
        context=context,
    )
    assert "sandbox:tenant-1:actor-1" in resources
    assert "sandbox:attacker-tenant:attacker-actor" not in resources


def test_code_health_probe_failure_is_fail_closed(monkeypatch):
    class BrokenHealthProvider:
        def health_check(self):
            raise RuntimeError("probe failed")

    monkeypatch.setattr(registry, "_code_execution_provider", BrokenHealthProvider())
    assert registry.code_execution_available() is False
    assert "run_code" not in registry.build_tool_manifest(
        allow_local_code_execution=True,
    ).names


def test_role_and_capability_cropping_is_fail_closed():
    student = registry.build_tool_manifest(role="student")
    teacher = registry.build_tool_manifest(role="teacher")
    assert "batch_grade" not in student.names
    assert "query_student_scores" not in student.names
    assert "query_student_scores" in teacher.names
    assert registry.build_tool_manifest(model_capabilities={"tool_calling": False}).names == ()
    query_only = registry.build_tool_manifest(enabled_capabilities={"teaching.query"})
    assert query_only.names
    assert all(entry.capabilities == frozenset({"teaching.query"}) for entry in query_only)


def test_rag_manifest_is_conditional_and_keeps_base_surface(tmp_path):
    knowledge = SQLiteKnowledgeProvider(build_synthetic_corpus(tmp_path / "knowledge.db"))
    provider = KnowledgeToolProvider(registry, knowledge)
    context = _context(role="student")
    manifest = provider.build_tool_manifest(context=context, role="student")
    assert "retrieve_course_materials" in manifest.names
    assert manifest.get("retrieve_course_materials").capabilities == frozenset({"rag"})
    assert "batch_grade" not in manifest.names


def test_rag_manifest_respects_model_tool_calling_capability(tmp_path):
    knowledge = SQLiteKnowledgeProvider(build_synthetic_corpus(tmp_path / "knowledge.db"))
    provider = KnowledgeToolProvider(registry, knowledge)
    manifest = provider.build_tool_manifest(
        context=_context(role="student"),
        role="student",
        model_capabilities={"tool_calling": False},
    )
    assert manifest.names == ()


def test_plan_scope_only_restricts_frozen_manifest():
    context = _context()
    manifest = registry.build_tool_manifest(context=context)
    restricted = manifest.restrict({"list_exams"})
    assert restricted.names == ("list_exams",)
    assert set(restricted.names) <= set(manifest.names)


def test_executor_rejects_registry_metadata_drift_after_freeze():
    context = _context()
    manifest = registry.build_tool_manifest(context=context)
    executor = PolicyToolExecutor(
        registry,
        policy=ExecutionPolicy.legacy_demo(),
        manifest=manifest,
    )
    original = registry.TOOL_SPECS["list_exams"]
    changed = type(original)(
        schema=copy.deepcopy(original.schema),
        handler=original.handler,
        category=original.category,
        risk_level=original.risk_level,
        mutating=original.mutating,
        mutation_parameters=original.mutation_parameters,
        allowed_roles=original.allowed_roles,
        source=original.source,
        version="9.9.9",
        capability=original.capability,
        effect=original.effect,
        parallel_safe=original.parallel_safe,
        resource_keys=original.resource_keys,
        timeout=original.timeout,
    )
    registry.TOOL_SPECS["list_exams"] = changed
    try:
        outcome = executor.execute("list_exams", {}, context)
        assert outcome.error["code"] == "TOOL_MANIFEST_MISMATCH"
    finally:
        registry.TOOL_SPECS["list_exams"] = original


def test_executor_rejects_tool_outside_empty_frozen_surface_and_bad_json():
    context = _context()
    empty = registry.build_tool_manifest(context=context, model_tool_calling=False)
    executor = PolicyToolExecutor(
        registry,
        policy=ExecutionPolicy.legacy_demo(),
        manifest=empty,
    )
    denied = executor.execute("list_exams", {}, context)
    assert denied.error["code"] == "TOOL_NOT_IN_MANIFEST"

    visible = registry.build_tool_manifest(context=context)
    malformed = executor.execute_raw("list_exams", "{", context, manifest=visible)
    assert malformed.error["code"] == "INVALID_JSON"


def test_executor_rejects_manifest_scope_drift():
    frozen_context = _context(course_ids={1})
    manifest = registry.build_tool_manifest(context=frozen_context)
    changed_context = _context(course_ids={2})
    outcome = PolicyToolExecutor(
        registry,
        policy=ExecutionPolicy.legacy_demo(),
        manifest=manifest,
    ).execute("list_exams", {}, changed_context)
    assert outcome.error["code"] == "TOOL_MANIFEST_MISMATCH"


def test_build_agent_tool_schemas_can_only_narrow_frozen_manifest():
    from edu_agent.agent.graph import build_agent

    context = _context()
    manifest = registry.build_tool_manifest(context=context)
    with pytest.raises(Exception, match="冻结工具"):
        build_agent(
            MockEngine(lambda _messages, _tools, _step: final("done")),
            run_context=context,
            tool_manifest=manifest,
            tool_schemas=[{"type": "function", "function": _schema("not_frozen")}],
        )


def test_run_freezes_hash_in_journal_and_trace_audit(tmp_path):
    def policy(messages, tools, step):
        return final("done")

    service = EduAgentService(
        MockEngine(policy),
        config=AppConfig(storage=StorageConfig(state_path=str(tmp_path / "state.db"))),
    )
    result = service.chat("freeze", actor_id="actor-1", role="teacher", tenant_id="tenant-1")
    journal = service.state_store.get_run_journal_snapshot(
        result.run_id,
        session_id=result.session_id,
        actor_id="actor-1",
        tenant_id="tenant-1",
    )
    assert journal.tool_manifest_hash
    with service.state_store.connect() as connection:
        row = connection.execute(
            "SELECT details_json FROM audit_events WHERE action='tool_manifest.frozen' AND resource=?",
            (f"run:{result.run_id}",),
        ).fetchone()
    details = json.loads(row["details_json"])
    assert details["manifest_hash"] == journal.tool_manifest_hash
    assert set(details["tool_names"]) == set(registry.build_tool_manifest(role="teacher").names)


def test_context_cannot_replace_frozen_manifest():
    context = _context()
    first = registry.build_tool_manifest(context=context)
    context.bind_tool_manifest(first)
    second = registry.build_tool_manifest(role="student", context=context)
    with pytest.raises(Exception, match="manifest"):
        context.bind_tool_manifest(second)


def test_manifest_hash_mismatch_is_rejected_on_recovery(tmp_path):
    def policy(messages, tools, step):
        return final("done")

    service = EduAgentService(
        MockEngine(policy),
        config=AppConfig(storage=StorageConfig(state_path=str(tmp_path / "state.db"))),
    )
    context = _context()
    service.state_store.ensure_session(
        context.session_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
        role=context.role,
        course_ids=context.course_ids,
    )
    service.state_store.enqueue_run(context, request_text="pending")
    # A durable journal with a deliberately foreign hash must fail closed
    # before the resume graph can execute anything.
    service.state_store.create_run_journal(
        context,
        tool_manifest_hash="f" * 64,
        frozen_provider_route={"engine": "mock", "routes": []},
        budget_snapshot=context.budget.usage(),
        writer_id="prelease",
        fencing_token=0,
    )
    decision = service.get_recovery_decision(
        context.run_id,
        actor_id=context.actor_id,
        tenant_id=context.tenant_id,
    )
    fresh = _context()
    fresh.run_id = context.run_id
    with pytest.raises(Exception, match="manifest"):
        service._assert_recovery_runtime_identity(fresh, decision)
