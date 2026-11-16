"""Artifact Infrastructure Models
Data structures defining persistent artifacts, metadata sidecars, chunked read results,
and offloading parameters for large tool execution payloads.
"""

from dataclasses import dataclass, field
from typing import Optional, Dict, Any


@dataclass
class ArtifactMetadata:
    """Persistent metadata describing an offloaded tool output artifact."""
    artifact_id: str
    conversation_id: str
    tool_call_id: str
    tool_name: str
    created_at: str
    byte_size: int
    line_count: int
    sha256: str
    file_path: str
    content_type: str = "text/plain"
    preview: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "conversation_id": self.conversation_id,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "created_at": self.created_at,
            "byte_size": self.byte_size,
            "line_count": self.line_count,
            "sha256": self.sha256,
            "file_path": self.file_path,
            "content_type": self.content_type,
            "preview": self.preview
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ArtifactMetadata":
        return cls(
            artifact_id=data["artifact_id"],
            conversation_id=data["conversation_id"],
            tool_call_id=data.get("tool_call_id", ""),
            tool_name=data.get("tool_name", ""),
            created_at=data["created_at"],
            byte_size=data["byte_size"],
            line_count=data.get("line_count", 0),
            sha256=data["sha256"],
            file_path=data.get("file_path", ""),
            content_type=data.get("content_type", "text/plain"),
            preview=data.get("preview", "")
        )


@dataclass
class ArtifactReadResult:
    """Result of a controlled chunked read operation on a persistent artifact."""
    artifact_id: str
    offset: int
    limit: int
    mode: str
    total_bytes: int
    total_lines: int
    returned_count: int
    content: str
    has_more: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "offset": self.offset,
            "limit": self.limit,
            "mode": self.mode,
            "total_bytes": self.total_bytes,
            "total_lines": self.total_lines,
            "returned_count": self.returned_count,
            "content": self.content,
            "has_more": self.has_more
        }
