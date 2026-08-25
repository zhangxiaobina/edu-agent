"""agentic 评测：多工具多步任务集 + 指标（口径对齐 BFCL V4）+ 引擎无关运行器。

  from edu_agent.eval import build_tasks, run_eval, format_report, make_oracle_engine

离线（无 key）用 make_oracle_engine 验证框架；接真引擎后用同一 run_eval 出真数。
"""
from .corpus import build_lineage_corpus, tasks_for_split
from .context_fidelity import (
    ContextFidelityCase,
    ContextFidelityMetrics,
    ContextFidelityObservation,
    assert_context_fidelity_thresholds,
    build_context_fidelity_corpus,
    build_context_fidelity_manifest,
    evaluate_context_fidelity,
    observe_context_fidelity_case,
    render_context_fidelity_summary,
    validate_context_fidelity_corpus,
)
from .harness import format_report, run_eval
from .lineage import (
    DATA_SOURCE,
    DATA_VERSION,
    LINEAGE_SCHEMA_VERSION,
    SPLITS,
    LineageValidationError,
    SampleLineage,
    audit_lineage,
    build_lineage_manifest,
    lineage_gate_passed,
    validate_lineage,
)
from .oracle import make_oracle_engine, oracle_policy_for
from .report import (
    COMPAT_SCHEMA_VERSION,
    OFFLINE_REQUIRED_SECTIONS,
    REPORT_SCHEMA_VERSION,
    REPORT_SECTIONS,
    REPORT_STATUS_VALUES,
    report_gate_passed,
    report_section,
    validate_report,
)
from .tasks import CATEGORIES, EvalTask, ExpectedCall, SuccessSpec, build_tasks
from .tasks_derived import build_derived_tasks
from .tasks_test import build_test_tasks

__all__ = [
    "CATEGORIES", "DATA_SOURCE", "DATA_VERSION", "LINEAGE_SCHEMA_VERSION", "SPLITS",
    "ContextFidelityCase", "ContextFidelityMetrics", "ContextFidelityObservation",
    "EvalTask", "ExpectedCall", "LineageValidationError", "SampleLineage", "SuccessSpec",
    "audit_lineage", "build_derived_tasks", "build_lineage_corpus", "build_lineage_manifest",
    "build_tasks", "build_test_tasks", "format_report", "lineage_gate_passed",
    "assert_context_fidelity_thresholds", "build_context_fidelity_corpus",
    "build_context_fidelity_manifest",
    "evaluate_context_fidelity", "observe_context_fidelity_case",
    "render_context_fidelity_summary",
    "validate_context_fidelity_corpus",
    "make_oracle_engine", "oracle_policy_for", "run_eval", "tasks_for_split", "validate_lineage",
    "COMPAT_SCHEMA_VERSION", "OFFLINE_REQUIRED_SECTIONS", "REPORT_SCHEMA_VERSION",
    "REPORT_SECTIONS", "REPORT_STATUS_VALUES", "report_gate_passed", "report_section",
    "validate_report",
]
