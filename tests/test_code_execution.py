from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from edu_agent.code_execution import (
    DockerCodeExecutionProvider,
    ExecutionRequest,
    ExecutionResult,
    JobeCodeExecutionProvider,
    ProviderCapabilities,
    ProviderHealth,
    build_code_execution_provider,
)
from edu_agent.runtime.artifacts import ArtifactStore, ToolResultBudget
from edu_agent.runtime.cancellation import CancellationRequested, CancellationToken
from edu_agent.runtime.config import CodeExecutionConfig
from edu_agent.runtime.models import RunContext
from edu_agent.runtime.tool_executor import ExecutionPolicy, PolicyToolExecutor
from edu_agent.state import RunCancelled, StateStore
from edu_agent.tools import registry


def _capabilities(**overrides):
    values = {
        "provider": "fake-isolated",
        "languages": frozenset({"python"}),
        "trusted_isolation": True,
        "supports_health_check": True,
        "supports_wall_time": True,
        "supports_cpu_time": True,
        "supports_memory": True,
        "supports_process_limit": True,
        "supports_file_size_limit": True,
        "supports_output_limit": True,
        "supports_network_policy": True,
        "supports_network_allowlist": False,
        "supports_cancellation": True,
    }
    values.update(overrides)
    return ProviderCapabilities(**values)


class FakeProvider:
    name = "fake-isolated"

    def __init__(self, result=None, *, capabilities=None, healthy=True):
        self.result = result or ExecutionResult(status="success", stdout="2\n", provider=self.name)
        self._capabilities = capabilities or _capabilities()
        self.healthy = healthy
        self.requests = []

    def capabilities(self):
        return self._capabilities

    def health_check(self, *, force=False):
        return ProviderHealth(
            healthy=self.healthy,
            checked_at=1.0,
            message="healthy" if self.healthy else "down",
            capabilities=self._capabilities,
            backend_languages=("python3",),
        )

    def execute(self, request, *, cancel_event=None):
        self.requests.append(request)
        return self.result


@pytest.fixture(autouse=True)
def _reset_provider():
    registry.configure_code_execution(None)
    yield
    registry.configure_code_execution(None)


def _context(role="teacher"):
    return RunContext.create(
        session_id="sandbox-session",
        actor_id="teacher-1",
        role=role,
        tenant_id="school-1",
    )


def test_no_healthy_provider_hides_schema_and_denies_execution():
    visible = {item["function"]["name"] for item in registry.openai_tools()}
    assert "run_code" not in visible
    result = registry.dispatch("run_code", {"source_code": "print(2)"})
    assert "error" in result


def test_forged_health_without_required_capabilities_is_rejected():
    provider = FakeProvider(capabilities=_capabilities(trusted_isolation=False))
    registry.configure_code_execution(provider)
    visible = {
        item["function"]["name"]
        for item in registry.openai_tools(allow_local_code_execution=True)
    }
    assert "run_code" not in visible
    assert "error" in registry.dispatch("run_code", {"source_code": "print(2)"})
    assert provider.requests == []


def test_healthy_capable_provider_exposes_schema_and_approval_binds_request():
    provider = FakeProvider()
    registry.configure_code_execution(provider)
    visible = {
        item["function"]["name"]
        for item in registry.openai_tools(
            role="teacher", allow_local_code_execution=True,
        )
    }
    assert "run_code" in visible

    approvals = []
    executor = PolicyToolExecutor(
        registry,
        policy=ExecutionPolicy(allow_local_code_execution=True),
        approval_handler=lambda request: approvals.append(request) or True,
    )
    outcome = executor.execute(
        "run_code",
        {
            "source_code": "print(1 + 1)",
            "language": "python",
            "memory_limit_mb": 512,
            "network_policy": "disabled",
        },
        _context(),
    )
    assert outcome.ok is True
    assert provider.requests[0].memory_limit_mb == 512
    approval = approvals[0]
    assert approval.arguments["source_sha256"] == hashlib.sha256(
        b"print(1 + 1)"
    ).hexdigest()
    assert "print(1 + 1)" not in str(approval.arguments)
    assert approval.arguments["network_policy"] == "disabled"


def test_run_code_does_not_open_the_teaching_database(monkeypatch):
    provider = FakeProvider()
    registry.configure_code_execution(provider)

    def fail_if_opened(*args, **kwargs):
        raise AssertionError("run_code must not open the teaching database")

    monkeypatch.setattr(registry.db, "connect", fail_if_opened)
    outcome = PolicyToolExecutor(
        registry,
        policy=ExecutionPolicy(
            allow_local_code_execution=True,
            require_code_execution_approval=False,
        ),
    ).execute("run_code", {"source_code": "print(2)"}, _context())
    assert outcome.ok is True
    assert len(provider.requests) == 1


def test_changed_source_changes_approval_payload_hash():
    provider = FakeProvider()
    registry.configure_code_execution(provider)
    approvals = []
    executor = PolicyToolExecutor(
        registry,
        policy=ExecutionPolicy(allow_local_code_execution=True),
        approval_handler=lambda request: approvals.append(request) or False,
    )
    for source in ("print(1)", "print(2)"):
        outcome = executor.execute("run_code", {"source_code": source}, _context())
        assert outcome.error["code"] == "APPROVAL_REQUIRED"
    assert approvals[0].payload_hash != approvals[1].payload_hash
    assert provider.requests == []


def test_expired_code_approval_is_rejected_before_provider_call():
    provider = FakeProvider()
    registry.configure_code_execution(provider)
    executor = PolicyToolExecutor(
        registry,
        policy=ExecutionPolicy(
            allow_local_code_execution=True,
            approval_ttl_seconds=0,
        ),
        approval_handler=lambda request: True,
    )
    outcome = executor.execute("run_code", {"source_code": "print(1)"}, _context())
    assert outcome.ok is False
    assert outcome.error["code"] == "APPROVAL_EXPIRED"
    assert provider.requests == []


@pytest.mark.parametrize(
    ("status", "error_code"),
    [
        ("timeout", "TIMEOUT"),
        ("memory_limit", "MEMORY_LIMIT"),
        ("security_denied", "SECURITY_DENIED"),
    ],
)
def test_failed_execution_is_not_tool_success(status, error_code):
    provider = FakeProvider(
        ExecutionResult(status=status, provider="fake-isolated", message="denied")
    )
    registry.configure_code_execution(provider)
    outcome = PolicyToolExecutor(
        registry,
        policy=ExecutionPolicy(
            allow_local_code_execution=True,
            require_code_execution_approval=False,
        ),
    ).execute("run_code", {"source_code": "while True: pass"}, _context())
    assert outcome.ok is False
    assert outcome.error["code"] == error_code


def test_large_output_is_redacted_and_spilled_to_scoped_artifact(tmp_path):
    secret = "api_key=super-secret-value"
    provider = FakeProvider(
        ExecutionResult(status="success", stdout=secret + "x" * 5000, provider="fake-isolated")
    )
    registry.configure_code_execution(provider)
    state = StateStore(tmp_path / "state.db")
    budget = ToolResultBudget(
        ArtifactStore(tmp_path / "artifacts", state),
        inline_chars=256,
        preview_chars=80,
    )
    context = _context()
    outcome = PolicyToolExecutor(
        registry,
        policy=ExecutionPolicy(
            allow_local_code_execution=True,
            require_code_execution_approval=False,
        ),
        state_store=state,
        result_budget=budget,
    ).execute("run_code", {"source_code": "print('large')"}, context)
    assert outcome.ok is True
    artifact_id = outcome.data["artifact_id"]
    stored = budget.artifact_store.read_text(artifact_id, context=context)
    assert "super-secret-value" not in stored
    assert "[REDACTED]" in stored


def test_jobe_protocol_uses_native_parameter_limits(monkeypatch):
    provider = JobeCodeExecutionProvider(
        "http://127.0.0.1:4010", security_attested=True,
    )
    calls = []

    def request(method, path, payload=None):
        calls.append((method, path, payload))
        if method == "GET":
            return [["python3", "3.12.3"]]
        if "EDU_AGENT_JOBE_HEALTHY" in payload["run_spec"]["sourcecode"]:
            return {"outcome": 15, "stdout": "EDU_AGENT_JOBE_HEALTHY\n", "stderr": ""}
        return {"outcome": 15, "stdout": "ok\n", "stderr": "", "cmpinfo": ""}

    monkeypatch.setattr(provider, "_request", request)
    result = provider.execute(
        ExecutionRequest(
            language="python",
            source="print('ok')",
            cpu_time_limit_seconds=3,
            wall_time_limit_seconds=6,
            memory_limit_mb=512,
            process_limit=4,
            file_size_limit_mb=8,
        )
    )
    assert result.success is True
    run_spec = calls[-1][2]["run_spec"]
    assert run_spec["parameters"]["cputime"] == 3
    assert run_spec["parameters"]["memorylimit"] == 512
    assert run_spec["parameters"]["numprocs"] == 4
    assert run_spec["parameters"]["disklimit"] == 8
    assert "cputimelimitsecs" not in run_spec
    assert "memorylimitmb" not in run_spec


def test_jobe_rejects_args_and_network_allowlist_without_http_call(monkeypatch):
    provider = JobeCodeExecutionProvider(
        "http://127.0.0.1:4010", security_attested=True,
    )
    def request(method, path, payload=None):
        if method == "GET":
            return [["python3", "3.12"]]
        return {"outcome": 15, "stdout": "EDU_AGENT_JOBE_HEALTHY\n", "stderr": ""}

    monkeypatch.setattr(provider, "_request", request)
    args_result = provider.execute(
        ExecutionRequest(language="python", source="print(1)", args=("--unsafe",))
    )
    network_result = provider.execute(
        ExecutionRequest(
            language="python",
            source="print(1)",
            network_policy="allowlist",
            network_allowlist=("example.com",),
        )
    )
    assert args_result.status == "security_denied"
    assert args_result.message == "ARGS_UNSUPPORTED"
    assert network_result.status == "security_denied"
    assert network_result.message == "NETWORK_POLICY_DENIED"


def test_config_defaults_disabled_and_rejects_unenforced_network_mode():
    assert CodeExecutionConfig().enabled is False
    with pytest.raises(ValueError, match="默认禁网"):
        CodeExecutionConfig(network_policy="allowlist")


def test_health_loss_removes_schema():
    provider = FakeProvider()
    registry.configure_code_execution(provider)
    assert registry.code_execution_available() is True
    provider.healthy = False
    assert registry.code_execution_available() is False
    assert "run_code" not in {
        item["function"]["name"]
        for item in registry.openai_tools(allow_local_code_execution=True)
    }


def test_capability_contract_rejects_missing_process_limit():
    request = ExecutionRequest(language="python", source="print(1)")
    ok, reason = replace(_capabilities(), supports_process_limit=False).satisfies(request)
    assert ok is False
    assert reason == "PROCESS_LIMIT_UNSUPPORTED"


def test_docker_config_requires_digest_and_factory_builds_hardened_provider():
    with pytest.raises(ValueError, match="sha256"):
        CodeExecutionConfig(enabled=True, provider="docker", image="python:3.12")
    config = CodeExecutionConfig(
        enabled=True,
        provider="docker",
        image="example/python@sha256:" + "a" * 64,
        security_attested=True,
    )
    provider = build_code_execution_provider(config)
    assert isinstance(provider, DockerCodeExecutionProvider)
    assert provider.image == config.image
    assert provider.capabilities().trusted_isolation is True


def test_docker_container_contract_has_no_caller_controlled_escape_surface():
    provider = DockerCodeExecutionProvider(
        "example/python@sha256:" + "a" * 64,
        security_attested=True,
    )
    request = ExecutionRequest(
        language="python",
        source="print('ok')",
        args=("--example",),
        memory_limit_mb=512,
        process_limit=7,
        file_size_limit_mb=9,
    )
    config = provider._container_config(request)
    host = config["HostConfig"]
    assert config["Image"] == provider.image
    assert config["User"] == "65534:65534"
    assert config["NetworkDisabled"] is True
    assert config["WorkingDir"] == "/tmp"
    assert "Volumes" not in config
    assert "Binds" not in host
    assert host["NetworkMode"] == "none"
    assert host["ReadonlyRootfs"] is True
    assert host["CapDrop"] == ["ALL"]
    assert host["SecurityOpt"] == ["no-new-privileges:true"]
    assert host["PidsLimit"] == 7
    assert host["Memory"] == 512 * 1024 * 1024
    assert host["MemorySwap"] == host["Memory"]
    assert host["Tmpfs"]["/tmp"].startswith("rw,noexec,nosuid,nodev,size=11m")


def test_docker_cancel_kills_and_deletes_container(monkeypatch):
    provider = DockerCodeExecutionProvider(
        "example/python@sha256:" + "a" * 64,
        security_attested=True,
    )
    health = ProviderHealth(
        healthy=True,
        checked_at=1.0,
        message="healthy",
        capabilities=provider.capabilities(),
        backend_languages=("python",),
    )
    monkeypatch.setattr(provider, "health_check", lambda force=False: health)
    api_calls = []

    def api(method, path, payload=None, **kwargs):
        api_calls.append((method, path))
        return b""

    def json_call(method, path, payload=None, **kwargs):
        if method == "POST":
            return {"Id": "container-1"}
        if len([item for item in api_calls if "/kill" in item[1]]) == 0:
            return {"State": {"Running": True}}
        return {"State": {"Running": False, "ExitCode": 137, "OOMKilled": False}}

    class CancelAfterCreate:
        calls = 0

        def is_set(self):
            self.calls += 1
            return self.calls > 1

    monkeypatch.setattr(provider, "_api", api)
    monkeypatch.setattr(provider, "_json", json_call)
    result = provider.execute(
        ExecutionRequest(language="python", source="while True: pass"),
        cancel_event=CancelAfterCreate(),
    )
    assert result.status == "cancelled"
    assert any("/kill?signal=KILL" in path for _, path in api_calls)
    assert api_calls[-1] == (
        "DELETE",
        "/containers/container-1?force=1&v=1",
    )


def test_run_code_polls_runtime_control_during_provider_execution():
    provider = FakeProvider()

    def execute(request, *, cancel_event=None):
        assert cancel_event is not None
        assert cancel_event.is_set() is False
        return provider.result

    provider.execute = execute
    registry.configure_code_execution(provider)
    boundaries = []
    context = _context()
    context.bind_control_check(boundaries.append)
    outcome = PolicyToolExecutor(
        registry,
        policy=ExecutionPolicy(
            allow_local_code_execution=True,
            require_code_execution_approval=False,
        ),
    ).execute("run_code", {"source_code": "print(2)"}, context)
    assert outcome.ok is True
    assert "code_execution.poll" in boundaries


def test_runtime_cancellation_propagates_out_of_registry():
    provider = FakeProvider()

    def execute(request, *, cancel_event=None):
        cancel_event.is_set()
        raise AssertionError("control check should have raised")

    provider.execute = execute
    registry.configure_code_execution(provider)
    context = _context()

    def cancel(boundary):
        if boundary == "code_execution.poll":
            raise RunCancelled(boundary)

    context.bind_control_check(cancel)
    executor = PolicyToolExecutor(
        registry,
        policy=ExecutionPolicy(
            allow_local_code_execution=True,
            require_code_execution_approval=False,
        ),
    )
    with pytest.raises(RunCancelled, match="code_execution.poll"):
        executor.execute("run_code", {"source_code": "while True: pass"}, context)


def test_sync_sandbox_result_is_rejected_when_token_cancels_during_call():
    provider = FakeProvider()
    token = CancellationToken()

    def execute(request, *, cancel_event=None):
        assert cancel_event is not None
        token.cancel("client disconnected", source="client_disconnect")
        return ExecutionResult(status="success", stdout="late\n", provider=provider.name)

    provider.execute = execute
    registry.configure_code_execution(provider)
    context = RunContext.create(
        session_id="sandbox-session",
        actor_id="teacher-1",
        role="teacher",
        tenant_id="school-1",
        cancellation_token=token,
    )
    executor = PolicyToolExecutor(
        registry,
        policy=ExecutionPolicy(
            allow_local_code_execution=True,
            require_code_execution_approval=False,
        ),
    )
    with pytest.raises(CancellationRequested, match="code_execution.after_call"):
        executor.execute("run_code", {"source_code": "print('late')"}, context)
