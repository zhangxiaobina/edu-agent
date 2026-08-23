"""R3.4 schema-guided tool argument normalization and validation corpus."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from edu_agent.observability import TraceRepository
from edu_agent.runtime.models import RunContext
from edu_agent.runtime.tool_arguments import MAX_ARGUMENT_BYTES
from edu_agent.runtime.tool_executor import ExecutionPolicy, PolicyToolExecutor
from edu_agent.state import StateStore
from edu_agent.tools.argument_contract import (
    JSON_STRING_TO_ARRAY,
    JSON_STRING_TO_OBJECT,
    NORMALIZATION_KEYWORD,
    STRING_TO_BOOLEAN,
    STRING_TO_INTEGER,
    STRING_TO_NUMBER,
)
from edu_agent.tools.manifest import ToolEffect, ToolRegistrationError
from edu_agent.tools.registry import ToolSpec


CORPUS_PATH = Path(__file__).parent / "fixtures" / "tool_argument_bad_corpus.json"


def _handler(conn, **arguments):
    return arguments


def _schema(name: str = "argument_probe") -> dict:
    return {
        "name": name,
        "description": "R3.4 argument probe",
        "parameters": {
            "type": "object",
            "properties": {
                "count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "default": 3,
                    NORMALIZATION_KEYWORD: STRING_TO_INTEGER,
                },
                "ratio": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    NORMALIZATION_KEYWORD: STRING_TO_NUMBER,
                },
                "enabled": {
                    "type": "boolean",
                    NORMALIZATION_KEYWORD: STRING_TO_BOOLEAN,
                },
                "tags": {
                    "type": "array",
                    "maxItems": 3,
                    "items": {"type": "string", "maxLength": 4},
                    NORMALIZATION_KEYWORD: JSON_STRING_TO_ARRAY,
                },
                "config": {
                    "type": "object",
                    "properties": {
                        "threshold": {
                            "type": "number",
                            "exclusiveMinimum": 0,
                            "exclusiveMaximum": 1,
                            NORMALIZATION_KEYWORD: STRING_TO_NUMBER,
                        }
                    },
                    "required": ["threshold"],
                    NORMALIZATION_KEYWORD: JSON_STRING_TO_OBJECT,
                },
                "choice": {"type": "string", "enum": ["asc", "desc"]},
                "label": {"type": "string", "minLength": 1, "maxLength": 4},
                "nullable": {"type": ["string", "null"]},
                "student_number": {"type": "string"},
            },
            "required": [],
        },
    }


class RecordingProvider:
    def __init__(self, spec: ToolSpec):
        self.spec = spec
        self.calls: list[dict] = []

    def get_spec(self, name):
        return self.spec if name == self.spec.schema["name"] else None

    def dispatch(self, name, arguments, conn=None):
        self.calls.append(deepcopy(arguments))
        return {"arguments": deepcopy(arguments)}


def _spec(
    *,
    name: str = "argument_probe",
    schema: dict | None = None,
    effect: ToolEffect = ToolEffect.READ,
    resource_keys: tuple[str, ...] = (),
    mutation_parameters: frozenset[str] = frozenset(),
    data_classification: dict[str, str] | None = None,
) -> ToolSpec:
    return ToolSpec(
        schema=schema or _schema(name),
        handler=_handler,
        category="query",
        risk_level="high" if effect is ToolEffect.WRITE else "medium",
        mutating=effect is ToolEffect.WRITE,
        mutation_parameters=mutation_parameters,
        effect=effect,
        resource_keys=resource_keys,
        data_classification=data_classification or {},
    )


def _context(*, max_tool_calls: int = 64) -> RunContext:
    return RunContext.create(
        session_id="argument-session",
        actor_id="argument-actor",
        tenant_id="argument-school",
        role="system",
        max_tool_calls=max_tool_calls,
    )


@pytest.mark.parametrize("case", json.loads(CORPUS_PATH.read_text(encoding="utf-8")), ids=lambda case: case["id"])
def test_bad_argument_corpus_is_rejected_before_dispatch(case):
    provider = RecordingProvider(_spec())
    context = _context(max_tool_calls=1)
    outcome = PolicyToolExecutor(provider, policy=ExecutionPolicy.legacy_demo()).execute_raw(
        "argument_probe",
        case["raw"],
        context,
        tool_call_id=f"call-{case['id']}",
    )

    assert not outcome.ok
    assert outcome.error["code"] == case["code"]
    assert provider.calls == []
    assert context.budget.tool_calls == 1
    assert outcome.meta["argument_retry"] == {
        "consumed": 1,
        "used_for_call": 1,
        "max_per_call": 1,
    }
    if pointer := case.get("pointer"):
        assert pointer in {
            issue["pointer"] for issue in outcome.error.get("details", {}).get("issues", [])
        }


def test_declared_rules_normalize_once_recursively_and_deterministically():
    first_provider = RecordingProvider(_spec())
    first_context = _context()
    input_items = [
        ("student_number", "00123"),
        ("count", "4"),
        ("ratio", "0.5"),
        ("enabled", "true"),
        ("tags", '["教学","AI"]'),
        ("config", '{"threshold":"0.25"}'),
        ("nullable", None),
        ("label", "教务"),
    ]
    first = PolicyToolExecutor(
        first_provider,
        policy=ExecutionPolicy.legacy_demo(),
    ).execute_raw(
        "argument_probe",
        json.dumps(dict(input_items), ensure_ascii=False),
        first_context,
        tool_call_id="normalized-call",
    )

    assert first.ok
    assert first_provider.calls == [
        {
            "config": {"threshold": 0.25},
            "count": 4,
            "enabled": True,
            "label": "教务",
            "nullable": None,
            "ratio": 0.5,
            "student_number": "00123",
            "tags": ["教学", "AI"],
        }
    ]
    repairs = first.meta["argument_repairs"]
    assert [item["pointer"] for item in repairs] == [
        "/config",
        "/config/threshold",
        "/count",
        "/enabled",
        "/ratio",
        "/tags",
    ]
    assert all(item["result"] == "applied" for item in repairs)
    assert all("original" not in item and len(item["original_sha256"]) == 64 for item in repairs)
    assert first.meta["argument_retry"]["consumed"] == 1

    second_provider = RecordingProvider(_spec())
    second = PolicyToolExecutor(
        second_provider,
        policy=ExecutionPolicy.legacy_demo(),
    ).execute_raw(
        "argument_probe",
        json.dumps(dict(reversed(input_items)), ensure_ascii=False),
        _context(),
        tool_call_id="normalized-call-2",
    )
    assert second.ok
    assert second_provider.calls == first_provider.calls
    assert second.meta["argument_repairs"] == repairs


def test_defaults_are_not_injected_and_null_is_valid_only_when_declared():
    provider = RecordingProvider(_spec())
    executor = PolicyToolExecutor(provider, policy=ExecutionPolicy.legacy_demo())
    outcome = executor.execute("argument_probe", {"nullable": None}, _context())
    assert outcome.ok
    assert provider.calls == [{"nullable": None}]
    assert "count" not in provider.calls[0]


def test_json_schema_integer_accepts_integral_float_but_never_boolean():
    provider = RecordingProvider(_spec())
    executor = PolicyToolExecutor(provider, policy=ExecutionPolicy.legacy_demo())
    integral = executor.execute("argument_probe", {"count": 4.0}, _context())
    boolean = executor.execute("argument_probe", {"count": True}, _context())
    assert integral.ok
    assert provider.calls == [{"count": 4}]
    assert integral.meta["argument_repairs"] == [
        {
            "pointer": "/count",
            "original_type": "number",
            "target_type": "integer",
            "rule_id": "integral_number_to_integer_v1",
            "result": "applied",
            "original_sha256": integral.meta["argument_repairs"][0]["original_sha256"],
            "sensitive": False,
        }
    ]
    assert boolean.error["code"] == "INVALID_ARGUMENTS"


def test_integral_float_does_not_bypass_identifier_or_write_policy():
    identifier_name = "integer_identifier"
    identifier_schema = {
        "name": identifier_name,
        "parameters": {
            "type": "object",
            "properties": {"course_id": {"type": "integer"}},
            "required": ["course_id"],
        },
    }
    identifier_provider = RecordingProvider(
        _spec(name=identifier_name, schema=identifier_schema)
    )
    identifier = PolicyToolExecutor(
        identifier_provider,
        policy=ExecutionPolicy.legacy_demo(),
    ).execute(identifier_name, {"course_id": 4.0}, _context())

    write_provider = RecordingProvider(
        _spec(name="integer_write", schema=_schema("integer_write"), effect=ToolEffect.WRITE)
    )
    write = PolicyToolExecutor(
        write_provider,
        policy=ExecutionPolicy.legacy_demo(),
    ).execute("integer_write", {"count": 4.0}, _context())

    assert identifier.error["code"] == "INVALID_ARGUMENTS"
    assert identifier.meta["argument_repairs"][0]["result"] == "rejected_identifier"
    assert write.error["code"] == "INVALID_ARGUMENTS"
    assert write.meta["argument_repairs"][0]["result"] == "rejected_effect_policy"
    assert identifier_provider.calls == write_provider.calls == []


@pytest.mark.parametrize("effect", [ToolEffect.WRITE, ToolEffect.CONDITIONAL_WRITE])
def test_write_and_conditional_write_never_repair_approval_semantics(effect):
    name = f"strict_{effect.value}"
    schema = {
        "name": name,
        "parameters": {
            "type": "object",
            "properties": {
                "save": {
                    "type": "boolean",
                    NORMALIZATION_KEYWORD: STRING_TO_BOOLEAN,
                }
            },
            "required": ["save"],
        },
    }
    spec = _spec(
        name=name,
        schema=schema,
        effect=effect,
        mutation_parameters=(
            frozenset({"save"}) if effect is ToolEffect.CONDITIONAL_WRITE else frozenset()
        ),
    )
    provider = RecordingProvider(spec)
    approvals = []
    outcome = PolicyToolExecutor(
        provider,
        policy=ExecutionPolicy(require_write_approval=True),
        approval_handler=lambda request: approvals.append(request) or True,
    ).execute_raw(name, '{"save":"true"}', _context(), tool_call_id="write-repair")

    assert outcome.error["code"] == "INVALID_ARGUMENTS"
    assert outcome.meta["argument_repairs"][0]["result"] == "rejected_effect_policy"
    assert provider.calls == []
    assert approvals == []


def test_resource_keys_are_derived_from_validated_normalized_arguments():
    spec = _spec(resource_keys=("/count",))
    provider = RecordingProvider(spec)
    outcome = PolicyToolExecutor(
        provider,
        policy=ExecutionPolicy.legacy_demo(),
    ).execute_raw("argument_probe", '{"count":"4"}', _context())
    assert outcome.ok
    assert outcome.meta["argument_repairs"][0]["result"] == "applied"
    assert provider.calls == [{"count": 4}]
    assert spec.to_manifest_entry().resource_keys_for(provider.calls[0]) == frozenset(
        {"/count=4"}
    )


def test_sensitive_repair_audit_and_trace_store_only_hashes(tmp_path):
    secret = "987654321"
    name = "sensitive_probe"
    schema = {
        "name": name,
        "parameters": {
            "type": "object",
            "properties": {
                "secret_number": {
                    "type": "integer",
                    NORMALIZATION_KEYWORD: STRING_TO_INTEGER,
                }
            },
            "required": ["secret_number"],
        },
    }
    provider = RecordingProvider(
        _spec(
            name=name,
            schema=schema,
            data_classification={"/secret_number": "credential"},
        )
    )
    store = StateStore(tmp_path / "state.db")
    outcome = PolicyToolExecutor(
        provider,
        policy=ExecutionPolicy.legacy_demo(),
        state_store=store,
    ).execute_raw(
        name,
        json.dumps({"secret_number": secret}),
        _context(),
        tool_call_id="sensitive-call",
    )
    assert outcome.error["code"] == "INVALID_ARGUMENTS"
    repair = outcome.meta["argument_repairs"][0]
    assert repair["sensitive"] is True
    assert repair["result"] == "rejected_sensitive_field"
    assert len(repair["original_sha256"]) == 64

    with store.connect() as connection:
        audit_json = connection.execute(
            "SELECT details_json FROM audit_events WHERE action='tool.argument_repair'"
        ).fetchone()[0]
        tool_json = connection.execute(
            "SELECT arguments_json || outcome_json FROM tool_events ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert secret not in audit_json
    assert secret not in tool_json
    assert "raw_sha256" in tool_json

    events = TraceRepository(store).list_events(
        actor_id="argument-actor",
        tenant_id="argument-school",
        limit=100,
    ).to_dict()
    assert secret not in json.dumps(events, ensure_ascii=False)


def test_invalid_argument_bodies_are_not_persisted_or_projected_to_trace(tmp_path):
    first_secret = "unclassified-invalid-body-9137"
    second_secret = "malformed-body-2468"
    provider = RecordingProvider(_spec())
    store = StateStore(tmp_path / "state.db")
    executor = PolicyToolExecutor(
        provider,
        policy=ExecutionPolicy.legacy_demo(),
        state_store=store,
    )
    context = _context()

    unknown = executor.execute_raw(
        "argument_probe",
        json.dumps({"unexpected": first_secret}),
        context,
        tool_call_id="invalid-unknown",
    )
    malformed = executor.execute_raw(
        "argument_probe",
        f'{{"label":"{second_secret}"',
        context,
        tool_call_id="invalid-malformed",
    )

    assert unknown.error["code"] == "INVALID_ARGUMENTS"
    assert malformed.error["code"] == "INVALID_JSON"
    with store.connect() as connection:
        persisted = "".join(
            row[0]
            for row in connection.execute(
                "SELECT arguments_json || outcome_json FROM tool_events ORDER BY id"
            )
        )
    trace = TraceRepository(store).list_events(
        actor_id="argument-actor",
        tenant_id="argument-school",
        limit=100,
    ).to_dict()
    rendered_trace = json.dumps(trace, ensure_ascii=False)
    assert first_secret not in persisted and first_secret not in rendered_trace
    assert second_secret not in persisted and second_secret not in rendered_trace
    assert provider.calls == []


def test_persistence_uses_already_validated_spec_classification(tmp_path):
    body = "free-text-body-should-not-reach-trace"
    spec = _spec()

    class VolatileProvider:
        def __init__(self):
            self.spec_reads = 0

        def get_spec(self, name):
            self.spec_reads += 1
            return spec if name == spec.schema["name"] else None

        def dispatch(self, name, arguments, conn=None):
            self.get_spec = lambda _name: (_ for _ in ()).throw(
                RuntimeError("registry changed after validation")
            )
            return {"accepted": bool(arguments)}

    provider = VolatileProvider()
    store = StateStore(tmp_path / "state.db")
    outcome = PolicyToolExecutor(
        provider,
        policy=ExecutionPolicy.legacy_demo(),
        state_store=store,
    ).execute_raw("argument_probe", json.dumps({"student_number": body}), _context())

    assert outcome.ok
    assert provider.spec_reads == 1
    with store.connect() as connection:
        persisted = connection.execute(
            "SELECT arguments_json FROM tool_events ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert body not in persisted
    assert "[REDACTED]" in persisted


@pytest.mark.parametrize(
    "raw",
    [
        lambda: json.dumps({"label": "x" * MAX_ARGUMENT_BYTES}),
        lambda: json.dumps({"config": {"threshold": 0.5, "nested": []}}, ensure_ascii=False)
        .replace("[]", "[" * 40 + "]" * 40),
        lambda: json.dumps({"tags": ["x"] * 1025}),
    ],
    ids=["oversize", "overdeep", "overwide"],
)
def test_size_depth_and_container_limits_are_bounded(raw):
    provider = RecordingProvider(_spec())
    context = _context(max_tool_calls=1)
    outcome = PolicyToolExecutor(provider, policy=ExecutionPolicy.legacy_demo()).execute_raw(
        "argument_probe",
        raw(),
        context,
        tool_call_id="bounded-call",
    )
    assert outcome.error["code"] == "ARGUMENT_LIMIT_EXCEEDED"
    assert context.budget.tool_calls == 1
    assert provider.calls == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"label": b"not-json"},
        {1: "non-string-key"},
        {"tags": ("tuple",)},
    ],
    ids=["bytes", "non-string-key", "tuple"],
)
def test_non_json_python_values_are_rejected(arguments):
    provider = RecordingProvider(_spec())
    outcome = PolicyToolExecutor(
        provider,
        policy=ExecutionPolicy.legacy_demo(),
    ).execute("argument_probe", arguments, _context())

    assert outcome.error["code"] == "INVALID_JSON"
    assert provider.calls == []


def test_cyclic_python_arguments_are_rejected():
    arguments: dict = {}
    arguments["config"] = arguments
    provider = RecordingProvider(_spec())
    outcome = PolicyToolExecutor(
        provider,
        policy=ExecutionPolicy.legacy_demo(),
    ).execute("argument_probe", arguments, _context())

    assert outcome.error["code"] == "INVALID_JSON"
    assert provider.calls == []


def test_normalized_containers_are_rechecked_against_aggregate_depth_limit():
    leaf_schema = {
        "type": "object",
        NORMALIZATION_KEYWORD: JSON_STRING_TO_OBJECT,
    }
    parameters = leaf_schema
    raw_value: object = "{}"
    for index in reversed(range(20)):
        key = f"level_{index}"
        parameters = {
            "type": "object",
            "properties": {key: parameters},
            "required": [key],
        }
        raw_value = {key: raw_value}

    inner: object = None
    for index in reversed(range(20)):
        inner = {f"inner_{index}": inner}
    cursor = raw_value
    for index in range(20):
        if index == 19:
            cursor[f"level_{index}"] = json.dumps(inner)
        else:
            cursor = cursor[f"level_{index}"]

    name = "aggregate_depth_probe"
    provider = RecordingProvider(
        _spec(
            name=name,
            schema={"name": name, "parameters": parameters},
        )
    )
    outcome = PolicyToolExecutor(
        provider,
        policy=ExecutionPolicy.legacy_demo(),
    ).execute_raw(name, json.dumps(raw_value), _context(), tool_call_id="aggregate-depth")

    assert outcome.error["code"] == "ARGUMENT_LIMIT_EXCEEDED"
    assert outcome.meta["argument_repairs"][0]["result"] == "rejected_aggregate_limit"
    assert provider.calls == []


def test_one_argument_retry_unit_per_call_and_no_string_json_guessing():
    provider = RecordingProvider(_spec())
    executor = PolicyToolExecutor(provider, policy=ExecutionPolicy.legacy_demo())
    context = _context(max_tool_calls=2)
    first = executor.execute_raw(
        "argument_probe", "{'count': 4}", context, tool_call_id="same-call"
    )
    second = executor.execute_raw(
        "argument_probe", "{'count': 4}", context, tool_call_id="same-call"
    )
    assert first.error["code"] == second.error["code"] == "INVALID_JSON"
    assert first.meta["argument_retry"]["consumed"] == 1
    assert second.meta["argument_retry"]["consumed"] == 0
    assert context.argument_retry_count("same-call") == 1
    assert context.budget.tool_calls == 2
    assert provider.calls == []


@pytest.mark.parametrize(
    ("field", "schema"),
    [
        (
            "course_id",
            {"type": "integer", NORMALIZATION_KEYWORD: STRING_TO_INTEGER},
        ),
        (
            "choice",
            {
                "type": "string",
                "enum": ["asc"],
                NORMALIZATION_KEYWORD: STRING_TO_INTEGER,
            },
        ),
        (
            "start_time",
            {"type": "number", NORMALIZATION_KEYWORD: STRING_TO_NUMBER},
        ),
    ],
)
def test_registration_rejects_forbidden_or_mismatched_normalization(field, schema):
    bad = {
        "name": f"bad_{field}",
        "parameters": {
            "type": "object",
            "properties": {field: schema},
            "required": [],
        },
    }
    with pytest.raises(ToolRegistrationError):
        _spec(name=bad["name"], schema=bad)


@pytest.mark.parametrize(
    "parameters",
    [
        {
            "type": "object",
            "properties": {
                "count": {
                    "allOf": [
                        {
                            "type": "integer",
                            NORMALIZATION_KEYWORD: STRING_TO_INTEGER,
                        }
                    ]
                }
            },
        },
        {
            "type": "object",
            "properties": {"count": {"$ref": "#/$defs/wrapper"}},
            "$defs": {
                "wrapper": {
                    "type": "object",
                    "properties": {
                        "nested": {
                            "type": "integer",
                            NORMALIZATION_KEYWORD: STRING_TO_INTEGER,
                        }
                    },
                }
            },
        },
        {
            "type": "object",
            "properties": {
                "metrics": {
                    "type": "object",
                    "patternProperties": {
                        "^count$": {
                            "type": "integer",
                            NORMALIZATION_KEYWORD: STRING_TO_INTEGER,
                        }
                    },
                }
            },
        },
    ],
    ids=["all-of", "defs-ref", "pattern-properties"],
)
def test_registration_rejects_normalization_in_ambiguous_schema_locations(parameters):
    schema = {"name": "ambiguous_normalization", "parameters": parameters}
    with pytest.raises(ToolRegistrationError, match="无法无歧义遍历"):
        _spec(name=schema["name"], schema=schema)


def test_registration_rejects_unconstrained_additional_properties():
    schema = {
        "name": "open_object",
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": True,
        },
    }
    with pytest.raises(ToolRegistrationError, match="additionalProperties: true"):
        _spec(name=schema["name"], schema=schema)


def test_registration_rejects_non_2020_12_schema_dialect():
    schema = {
        "name": "legacy_dialect",
        "parameters": {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "properties": {},
        },
    }
    with pytest.raises(ToolRegistrationError, match="Draft 2020-12"):
        _spec(name=schema["name"], schema=schema)


def test_additional_properties_wildcard_classification_blocks_sensitive_repair():
    name = "classified_map"
    schema = {
        "name": name,
        "parameters": {
            "type": "object",
            "properties": {
                "secrets": {
                    "type": "object",
                    "additionalProperties": {
                        "type": "integer",
                        NORMALIZATION_KEYWORD: STRING_TO_INTEGER,
                    },
                }
            },
            "required": ["secrets"],
        },
    }
    provider = RecordingProvider(_spec(name=name, schema=schema))
    outcome = PolicyToolExecutor(
        provider,
        policy=ExecutionPolicy.legacy_demo(),
    ).execute_raw(name, '{"secrets":{"opaque":"4"}}', _context())

    assert outcome.error["code"] == "INVALID_ARGUMENTS"
    assert outcome.meta["argument_repairs"][0]["pointer"] == "/secrets/opaque"
    assert outcome.meta["argument_repairs"][0]["result"] == "rejected_sensitive_field"
    assert provider.calls == []
