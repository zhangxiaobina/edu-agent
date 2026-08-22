"""教学教务工具集：mirror 真实平台 Controller 的入参/语义（合成数据后端）。"""

from .manifest import (
    ToolCapability,
    ToolEffect,
    ToolManifest,
    ToolManifestEntry,
    ToolManifestError,
    ToolManifestMismatch,
    ToolRegistrationError,
    ToolRisk,
    canonical_schema,
    canonical_schema_hash,
    enabled_capability_set,
)

__all__ = [
    "ToolCapability",
    "ToolEffect",
    "ToolManifest",
    "ToolManifestEntry",
    "ToolManifestError",
    "ToolManifestMismatch",
    "ToolRegistrationError",
    "ToolRisk",
    "canonical_schema",
    "canonical_schema_hash",
    "enabled_capability_set",
]
