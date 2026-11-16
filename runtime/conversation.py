"""Conversation Identity & Metadata Subsystem
Provides unique conversation identity, lineage tracking (parent-child relationships),
and workspace topology associations.
"""

import uuid
import datetime
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional, Dict, Any


@dataclass
class ConversationMetadata:
    """Represents core metadata and lineage for an autonomous conversation."""
    conversation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    title: str = "Untitled Conversation"
    created_at: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())
    last_modified_at: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())
    parent_conversation_id: str = ""
    nesting_depth: int = 0
    workspace_path: str = ""
    workspace_mode: str = "inherit"  # 'inherit' | 'branch' | 'share'
    status: str = "ACTIVE"           # 'ACTIVE' | 'IDLE' | 'COMPLETED' | 'KILLED'

    def update_timestamp(self) -> None:
        """Updates last_modified_at to current UTC timestamp."""
        self.last_modified_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    def to_dict(self) -> Dict[str, Any]:
        """Converts metadata to dictionary representation."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ConversationMetadata":
        """Reconstructs metadata from dictionary."""
        return cls(
            conversation_id=data.get("conversation_id", str(uuid.uuid4())),
            title=data.get("title", "Untitled Conversation"),
            created_at=data.get("created_at", datetime.datetime.now(datetime.timezone.utc).isoformat()),
            last_modified_at=data.get("last_modified_at", datetime.datetime.now(datetime.timezone.utc).isoformat()),
            parent_conversation_id=data.get("parent_conversation_id", ""),
            nesting_depth=int(data.get("nesting_depth", 0)),
            workspace_path=data.get("workspace_path", ""),
            workspace_mode=data.get("workspace_mode", "inherit"),
            status=data.get("status", "ACTIVE")
        )

    def spawn_child(self, title: str, workspace_mode: str = "inherit", workspace_path: Optional[str] = None) -> "ConversationMetadata":
        """Spawns child conversation metadata linking back to this conversation."""
        return ConversationMetadata(
            conversation_id=str(uuid.uuid4()),
            title=title,
            parent_conversation_id=self.conversation_id,
            nesting_depth=self.nesting_depth + 1,
            workspace_path=workspace_path or self.workspace_path,
            workspace_mode=workspace_mode,
            status="ACTIVE"
        )
