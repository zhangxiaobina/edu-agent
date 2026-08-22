from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from .models import RunContext
from .security import redact_sensitive, redact_sensitive_text

_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class ArtifactRef:
    id: str
    path: str
    sha256: str
    size_bytes: int


class ArtifactStore:
    """持久化大结果；数据库只保存可审计索引，正文留在受控目录。"""

    def __init__(self, root: str | Path, state_store=None):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_store = state_store

    def write_text(
        self,
        content: str,
        *,
        context: RunContext,
        kind: str,
        metadata: dict | None = None,
    ) -> ArtifactRef:
        context.check_control("artifact.before_write")
        if self.state_store is not None:
            self.state_store.assert_run_writable(
                context,
                boundary="artifact.before_write",
            )
        artifact_id = uuid.uuid4().hex
        safe_kind = _SAFE_COMPONENT.sub("_", kind).strip("._") or "artifact"
        relative = Path(context.tenant_id) / context.actor_id / context.session_id
        directory = (self.root / relative).resolve()
        if self.root not in directory.parents:
            raise ValueError("artifact 路径越界")
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
        path = directory / f"{artifact_id}-{safe_kind}.json"
        payload = redact_sensitive_text(content).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        path.write_bytes(payload)
        path.chmod(0o600)
        if self.state_store is not None:
            try:
                self.state_store.record_artifact(
                    artifact_id=artifact_id,
                    run_id=context.run_id,
                    session_id=context.session_id,
                    actor_id=context.actor_id,
                    tenant_id=context.tenant_id,
                    kind=kind,
                    path=str(path),
                    sha256=digest,
                    size_bytes=len(payload),
                    metadata=metadata or {},
                    context=context,
                )
            except BaseException:
                path.unlink(missing_ok=True)
                raise
        return ArtifactRef(artifact_id, str(path), digest, len(payload))

    def read_text(self, artifact_id: str, *, context: RunContext) -> str:
        content, truncated = self.read_text_chunk(
            artifact_id, context=context, offset=0, limit=None
        )
        if truncated:
            raise RuntimeError("unexpected truncated artifact read")
        return content

    def read_text_chunk(
        self,
        artifact_id: str,
        *,
        context: RunContext,
        offset: int = 0,
        limit: int | None = 64 * 1024,
    ) -> tuple[str, bool]:
        """Verify the complete hash while retaining only the requested byte range."""
        if self.state_store is None:
            raise RuntimeError("ArtifactStore 未绑定状态存储")
        if offset < 0 or (limit is not None and limit <= 0):
            raise ValueError("artifact offset/limit 非法")
        record = self.state_store.get_artifact(
            artifact_id,
            actor_id=context.actor_id,
            tenant_id=context.tenant_id,
        )
        if record is None:
            raise PermissionError("artifact 不存在或不属于当前 actor/tenant")
        path = Path(record["path"]).resolve()
        if self.root not in path.parents:
            raise ValueError("artifact 索引路径越界")
        digest = hashlib.sha256()
        selected = bytearray()
        position = 0
        end = None if limit is None else offset + limit
        with path.open("rb") as artifact_file:
            while chunk := artifact_file.read(64 * 1024):
                digest.update(chunk)
                chunk_end = position + len(chunk)
                if chunk_end > offset and (end is None or position < end):
                    start_at = max(0, offset - position)
                    end_at = len(chunk) if end is None else min(len(chunk), end - position)
                    selected.extend(chunk[start_at:end_at])
                position = chunk_end
        if digest.hexdigest() != record["sha256"]:
            raise RuntimeError("artifact 完整性校验失败")
        truncated = offset + len(selected) < position
        return selected.decode("utf-8", errors="replace"), truncated


class ToolResultBudget:
    def __init__(
        self,
        artifact_store: ArtifactStore,
        *,
        inline_chars: int = 12_000,
        preview_chars: int = 1_500,
        turn_budget_chars: int = 32_000,
    ):
        if inline_chars <= 0 or preview_chars <= 0:
            raise ValueError("工具结果预算必须大于 0")
        self.artifact_store = artifact_store
        self.inline_chars = inline_chars
        self.preview_chars = min(preview_chars, inline_chars)
        self.turn_budget_chars = max(inline_chars, turn_budget_chars)

    def apply(self, outcome: dict, *, context: RunContext, tool_name: str) -> dict:
        outcome = redact_sensitive(outcome)
        serialized = json.dumps(outcome, ensure_ascii=False, default=str)
        if len(serialized) <= self.inline_chars:
            return outcome
        try:
            artifact = self.artifact_store.write_text(
                serialized,
                context=context,
                kind="tool-result",
                metadata={"tool": tool_name, "characters": len(serialized)},
            )
        except OSError as error:
            return {
                "ok": outcome.get("ok", True),
                "data": {
                    "preview": serialized[: self.preview_chars],
                    "truncated": True,
                    "original_characters": len(serialized),
                },
                "error": outcome.get("error"),
                "meta": {
                    **outcome.get("meta", {}),
                    "spilled": False,
                    "spill_error": type(error).__name__,
                },
            }
        preview = serialized[: self.preview_chars]
        return {
            "ok": outcome.get("ok", True),
            "data": {
                "preview": preview,
                "truncated": True,
                "artifact_id": artifact.id,
                "artifact_path": artifact.path,
                "sha256": artifact.sha256,
                "original_characters": len(serialized),
            },
            "error": outcome.get("error"),
            "meta": {**outcome.get("meta", {}), "spilled": True},
        }

    def enforce_turn(self, messages: list[dict], *, context: RunContext) -> list[dict]:
        total = sum(len(message.get("content", "")) for message in messages)
        if total <= self.turn_budget_chars:
            return messages
        candidates = sorted(
            enumerate(messages),
            key=lambda item: len(item[1].get("content", "")),
            reverse=True,
        )
        for index, message in candidates:
            if total <= self.turn_budget_chars:
                break
            content = message.get("content", "")
            if not content or '"spilled": true' in content:
                continue
            try:
                artifact = self.artifact_store.write_text(
                    content,
                    context=context,
                    kind="tool-turn-result",
                    metadata={"tool": message.get("name"), "characters": len(content)},
                )
            except OSError:
                message["content"] = content[: self.preview_chars] + "\n[tool result truncated]"
                total += len(message["content"]) - len(content)
                continue
            replacement = json.dumps(
                {
                    "ok": True,
                    "data": {
                        "preview": content[: self.preview_chars],
                        "truncated": True,
                        "artifact_id": artifact.id,
                        "artifact_path": artifact.path,
                        "sha256": artifact.sha256,
                        "original_characters": len(content),
                    },
                    "error": None,
                    "meta": {"spilled": True, "reason": "turn_budget"},
                },
                ensure_ascii=False,
            )
            message["content"] = replacement
            total += len(replacement) - len(content)
        return messages

    def enforce_incremental(
        self,
        message: dict,
        *,
        prior_messages: list[dict],
        context: RunContext,
    ) -> dict:
        """Apply the turn cap without rewriting results already durably committed."""
        content = message.get("content", "")
        used = sum(len(item.get("content", "")) for item in prior_messages)
        if used + len(content) <= self.turn_budget_chars or '"spilled": true' in content:
            return message
        try:
            outcome = json.loads(content)
        except (TypeError, ValueError, json.JSONDecodeError):
            outcome = {"ok": True, "data": None, "error": None, "meta": {}}
        if not isinstance(outcome, dict):
            outcome = {"ok": True, "data": outcome, "error": None, "meta": {}}
        try:
            artifact = self.artifact_store.write_text(
                content,
                context=context,
                kind="tool-turn-result",
                metadata={"tool": message.get("name"), "characters": len(content)},
            )
        except OSError as error:
            replacement = {
                "ok": outcome.get("ok", True),
                "data": {
                    "preview": content[: self.preview_chars],
                    "truncated": True,
                    "original_characters": len(content),
                },
                "error": outcome.get("error"),
                "meta": {
                    **(outcome.get("meta") or {}),
                    "spilled": False,
                    "spill_error": type(error).__name__,
                    "reason": "turn_budget",
                },
            }
        else:
            replacement = {
                "ok": outcome.get("ok", True),
                "data": {
                    "preview": content[: self.preview_chars],
                    "truncated": True,
                    "artifact_id": artifact.id,
                    "artifact_path": artifact.path,
                    "sha256": artifact.sha256,
                    "original_characters": len(content),
                },
                "error": outcome.get("error"),
                "meta": {
                    **(outcome.get("meta") or {}),
                    "spilled": True,
                    "reason": "turn_budget",
                },
            }
        return {**message, "content": json.dumps(replacement, ensure_ascii=False)}
