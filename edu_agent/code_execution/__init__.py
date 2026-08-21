"""Trusted remote code-execution providers (no local subprocess fallback)."""

from .provider import (
    CodeExecutionProvider,
    ExecutionRequest,
    ExecutionResult,
    JobeCodeExecutionProvider,
    ProviderCapabilities,
    ProviderHealth,
)
from .docker_provider import DockerCodeExecutionProvider


def build_code_execution_provider(config):
    """Build only an explicitly configured remote/container provider."""
    if not config.enabled or config.provider == "disabled":
        return None
    common = {
        "allowed_languages": tuple(config.allowed_languages),
        "request_timeout_seconds": config.request_timeout_seconds,
        "health_interval_seconds": config.health_interval_seconds,
        "max_source_bytes": config.max_source_bytes,
        "max_stdin_bytes": config.max_stdin_bytes,
        "max_cpu_time_seconds": config.max_cpu_time_seconds,
        "max_wall_time_seconds": config.max_wall_time_seconds,
        "min_memory_mb": config.min_memory_mb,
        "max_memory_mb": config.max_memory_mb,
        "max_output_bytes": config.max_output_bytes,
        "max_processes": config.max_processes,
        "max_file_size_mb": config.max_file_size_mb,
        "max_artifact_bytes": config.max_artifact_bytes,
        "security_attested": config.security_attested,
    }
    if config.provider == "jobe":
        return JobeCodeExecutionProvider(
            config.endpoint,
            token_env=config.token_env,
            **common,
        )
    if config.provider == "docker":
        return DockerCodeExecutionProvider(
            config.image,
            socket_path=config.docker_socket,
            python_path=config.docker_python_path,
            max_cpus=config.max_cpus,
            **common,
        )
    raise ValueError(f"不支持的代码执行 provider：{config.provider}")

__all__ = [
    "CodeExecutionProvider",
    "DockerCodeExecutionProvider",
    "ExecutionRequest",
    "ExecutionResult",
    "JobeCodeExecutionProvider",
    "ProviderCapabilities",
    "ProviderHealth",
    "build_code_execution_provider",
]
