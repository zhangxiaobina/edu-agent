"""AI / 执行类工具：AI 出题（模板化合成，引擎可替换）+ 代码沙箱执行（mirror Jobe）。"""
from __future__ import annotations

from ..code_execution import ExecutionRequest
from ..teaching import TeachingCommandKind
from .teaching_adapter import execute_teaching_command

# Jobe 风格 outcome 码（mirror 真实平台 JobeResponse）
_OUTCOME_TEXT = {
    "AC": "Accepted（正常完成）", "WA": "Wrong Answer（输出不匹配）",
    "CE": "Compile Error（编译/语法错误）", "RE": "Runtime Error（运行时错误）",
    "TLE": "Time Limit Exceeded（超时）",
}
class _ContextCancelEvent:
    def __init__(self, context):
        self.context = context

    def is_set(self) -> bool:
        self.context.check_control("code_execution.poll")
        return False


def generate_questions(
    conn,
    course_id,
    knowledge_point=None,
    count=5,
    question_types=None,
    difficulty_distribution=None,
    save_to_bank=None,
    _provider=None,
    _context=None,
    _operation=None,
) -> dict:
    return execute_teaching_command(
        TeachingCommandKind.GENERATE_QUESTIONS,
        {
            "course_id": course_id,
            "knowledge_point": knowledge_point,
            "count": count,
            "question_types": question_types,
            "difficulty_distribution": difficulty_distribution,
            "save_to_bank": save_to_bank,
        },
        connection=conn,
        context=_context,
        provider=_provider,
        operation=_operation,
    )


def run_code(conn, source_code, language="python", stdin=None, args=None,
             expected_output=None, timeout=5, cpu_time_limit_seconds=2,
             wall_time_limit_seconds=None, memory_limit_mb=512,
             output_limit_bytes=65536, process_limit=16, file_size_limit_mb=16,
             artifact_limit_bytes=262144,
             network_policy="disabled", network_allowlist=None, _provider=None,
             _context=None) -> dict:
    """Execute only through a configured, healthy remote isolation provider."""
    provider = _provider
    if provider is None:
        return {"error": "代码执行后端不可用：未配置真实隔离 provider"}
    wall = int(wall_time_limit_seconds if wall_time_limit_seconds is not None else timeout)
    request = ExecutionRequest(
        language=str(language), source=str(source_code), stdin=str(stdin or ""),
        args=tuple(str(item) for item in (args or ())),
        cpu_time_limit_seconds=int(cpu_time_limit_seconds),
        wall_time_limit_seconds=wall, memory_limit_mb=int(memory_limit_mb),
        output_limit_bytes=int(output_limit_bytes), process_limit=int(process_limit),
        file_size_limit_mb=int(file_size_limit_mb), network_policy=str(network_policy),
        artifact_limit_bytes=int(artifact_limit_bytes),
        network_allowlist=tuple(network_allowlist or ()),
        tenant_id=getattr(_context, "tenant_id", "default"),
        actor_id=getattr(_context, "actor_id", "unknown"),
    )
    cancel_event = _ContextCancelEvent(_context) if _context is not None else None
    result = provider.execute(request, cancel_event=cancel_event)
    if _context is not None:
        _context.check_control("code_execution.after_call")
    if not result.success:
        return {
            "outcome": {"timeout": "TLE", "memory_limit": "MLE"}.get(result.status, result.status.upper()),
            "status": result.status, "status_description": result.message or result.status,
            "stdout": result.stdout, "stderr": result.stderr, "output_truncated": result.output_truncated,
            "provider": result.provider, "run_id": result.run_id, "success": False,
        }
    if expected_output is not None:
        passed = result.stdout.strip() == str(expected_output).strip()
        return _jobe("AC" if passed else "WA", stdout=result.stdout, stderr=result.stderr,
                     passed=passed, expected_output=expected_output,
                     provider=result.provider, run_id=result.run_id,
                     output_truncated=result.output_truncated)
    return _jobe("AC", stdout=result.stdout, stderr=result.stderr,
                 provider=result.provider, run_id=result.run_id,
                 output_truncated=result.output_truncated)


# ---------- 内部 ----------
def _jobe(outcome, stdout="", stderr="", cmpinfo="", passed=None, expected_output=None,
          provider=None, run_id=None, output_truncated=False) -> dict:
    res = {"outcome": outcome, "status_description": _OUTCOME_TEXT.get(outcome, outcome),
           "stdout": stdout, "stderr": stderr, "cmpinfo": cmpinfo,
           "success": outcome == "AC"}
    if provider:
        res["provider"] = provider
    if run_id:
        res["run_id"] = run_id
    if output_truncated:
        res["output_truncated"] = True
    if passed is not None:
        res["passed"] = passed
        res["expected_output"] = expected_output
    return res
