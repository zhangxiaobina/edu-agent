from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..data_classification import content_classifications
from .models import RunContext
from .security import redact_sensitive, redact_sensitive_preview, redact_sensitive_text

_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9_.-]+")
ARTIFACT_REFERENCE_TYPE = "edu-agent.scoped-artifact.v1"


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
        try:
            structured = json.loads(content)
        except (TypeError, ValueError, json.JSONDecodeError):
            safe_content = redact_sensitive_text(content)
        else:
            safe_content = json.dumps(
                redact_sensitive(structured),
                ensure_ascii=False,
                default=str,
            )
        payload = safe_content.encode("utf-8")
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
        if record["session_id"] != context.session_id:
            raise PermissionError("artifact 不属于当前 session")
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

    @staticmethod
    def _decode_content(content: str) -> Any:
        try:
            return json.loads(content)
        except (TypeError, ValueError, json.JSONDecodeError):
            return content

    @staticmethod
    def _classification(value: Any) -> tuple[str, tuple[str, ...]]:
        classifications = content_classifications(value)
        return classifications[0], classifications

    def _preview(self, value: Any) -> str:
        safe = redact_sensitive_preview(value)
        if isinstance(safe, str):
            serialized = safe
        else:
            serialized = json.dumps(safe, ensure_ascii=False, default=str)
        return serialized[: self.preview_chars]

    @staticmethod
    def has_artifact_reference(value: Any) -> bool:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError, json.JSONDecodeError):
                return False
        if isinstance(value, dict):
            if value.get("type") == ARTIFACT_REFERENCE_TYPE:
                return True
            return any(ToolResultBudget.has_artifact_reference(item) for item in value.values())
        if isinstance(value, list):
            return any(ToolResultBudget.has_artifact_reference(item) for item in value)
        return False

    def _replacement(
        self,
        outcome: Any,
        *,
        artifact: ArtifactRef | None,
        kind: str,
        reason: str,
        original_characters: int,
        spill_error: OSError | None = None,
    ) -> dict:
        normalized = outcome if isinstance(outcome, dict) else {
            "ok": True,
            "data": outcome,
            "error": None,
            "meta": {},
        }
        classification, classifications = self._classification(outcome)
        safe_receipt = redact_sensitive_preview(normalized)
        reference = None
        if artifact is not None:
            reference = {
                "type": ARTIFACT_REFERENCE_TYPE,
                "artifact_id": artifact.id,
                "kind": kind,
                "sha256": artifact.sha256,
                "size_bytes": artifact.size_bytes,
                "classification": classification,
                "classifications": list(classifications),
            }
        data = {
            "preview": self._preview(outcome),
            "truncated": True,
            "original_characters": original_characters,
            "classification": classification,
            "classifications": list(classifications),
        }
        if reference is not None:
            data.update(
                {
                    "artifact_ref": reference,
                    # Compatibility fields remain while readers migrate to artifact_ref.
                    "artifact_id": artifact.id,
                    "sha256": artifact.sha256,
                }
            )
        meta = dict(safe_receipt.get("meta") or {})
        meta.update(
            {
                "spilled": artifact is not None,
                "spill_reason": reason,
                "artifact_reference_type": ARTIFACT_REFERENCE_TYPE,
                "classification": classification,
            }
        )
        if spill_error is not None:
            meta["spill_error"] = type(spill_error).__name__
        return {
            "ok": bool(safe_receipt.get("ok", True)),
            "data": data,
            "error": safe_receipt.get("error"),
            "meta": meta,
        }

    def _write_spill(
        self,
        content: str,
        value: Any,
        *,
        context: RunContext,
        tool_name: str | None,
        kind: str,
        reason: str,
        metadata: dict | None = None,
    ) -> tuple[ArtifactRef, dict]:
        classification, classifications = self._classification(value)
        artifact = self.artifact_store.write_text(
            content,
            context=context,
            kind=kind,
            metadata={
                "tool": tool_name,
                "characters": len(content),
                "classification": classification,
                "classifications": list(classifications),
                "reference_type": ARTIFACT_REFERENCE_TYPE,
                **(metadata or {}),
            },
        )
        return artifact, self._replacement(
            value,
            artifact=artifact,
            kind=kind,
            reason=reason,
            original_characters=len(content),
        )

    def apply(self, outcome: dict, *, context: RunContext, tool_name: str) -> dict:
        original = outcome
        outcome = redact_sensitive(outcome)
        serialized = json.dumps(outcome, ensure_ascii=False, default=str)
        if len(serialized) <= self.inline_chars:
            return outcome
        try:
            _, replacement = self._write_spill(
                serialized,
                original,
                context=context,
                tool_name=tool_name,
                kind="tool-result",
                reason="single_result_budget",
            )
        except OSError as error:
            return self._replacement(
                original,
                artifact=None,
                kind="tool-result",
                reason="single_result_budget",
                original_characters=len(serialized),
                spill_error=error,
            )
        return replacement

    def externalize_message(
        self,
        message: dict,
        *,
        context: RunContext,
        kind: str = "tool-turn-result",
        reason: str = "turn_budget",
        metadata: dict | None = None,
        fallback_on_error: bool = True,
    ) -> dict:
        content = str(message.get("content") or "")
        value = self._decode_content(content)
        try:
            _, replacement = self._write_spill(
                content,
                value,
                context=context,
                tool_name=message.get("name"),
                kind=kind,
                reason=reason,
                metadata=metadata,
            )
        except OSError as error:
            if not fallback_on_error:
                raise
            replacement = self._replacement(
                value,
                artifact=None,
                kind=kind,
                reason=reason,
                original_characters=len(content),
                spill_error=error,
            )
        return {**message, "content": json.dumps(replacement, ensure_ascii=False)}

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
            if (
                message.get("role") != "tool"
                or not content
                or self.has_artifact_reference(content)
            ):
                continue
            replacement = self.externalize_message(
                message,
                context=context,
                reason="turn_budget",
            )
            message["content"] = replacement["content"]
            total += len(message["content"]) - len(content)
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
        if (
            used + len(content) <= self.turn_budget_chars
            or self.has_artifact_reference(content)
        ):
            return message
        return self.externalize_message(message, context=context, reason="turn_budget")
