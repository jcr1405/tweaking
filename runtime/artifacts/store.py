"""Persistent Artifact Store & Large Output Offloading
Enforces the 40 KB UTF-8 output offload threshold, writes atomic artifacts to disk
at %LOCALAPPDATA%\\Presence\\artifacts\\<conversation_id>\\, computes SHA-256 hashes,
generates preview envelopes, and provides secure chunked artifact reading.
"""

import os
import re
import json
import uuid
import hashlib
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

from runtime.artifacts.models import ArtifactMetadata, ArtifactReadResult

logger = logging.getLogger("presence.artifacts.store")

# Invariants
ARTIFACT_THRESHOLD_BYTES = 6144           # 6 KB (~1,500 tokens inline cap)
MAX_ARTIFACT_SIZE_BYTES = 50 * 1024 * 1024   # 50 MB

MAX_CONVERSATION_BUDGET_BYTES = 200 * 1024 * 1024  # 200 MB

# Regex for safe identifier validation (prevents directory traversal)
SAFE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_\-]+$")


def get_default_artifacts_root() -> Path:
    """Returns standard OS path for Presence persistent artifacts."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "Presence" / "artifacts"
    return Path.home() / ".presence" / "artifacts"


class ArtifactStore:
    """Manages disk persistence, quotas, integrity, and reading of large execution artifacts."""

    def __init__(self, root_dir: Optional[Path] = None):
        self.root_dir = root_dir or get_default_artifacts_root()
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self._locks: Dict[str, threading.Lock] = {}
        self._global_lock = threading.Lock()

    def _get_conversation_lock(self, conversation_id: str) -> threading.Lock:
        """Returns or creates a lock for serializing writes to a specific conversation."""
        with self._global_lock:
            if conversation_id not in self._locks:
                self._locks[conversation_id] = threading.Lock()
            return self._locks[conversation_id]

    def _get_conversation_dir(self, conversation_id: str) -> Path:
        """Resolves and validates the directory for a conversation's artifacts."""
        if not SAFE_ID_PATTERN.match(conversation_id):
            raise ValueError(f"Invalid conversation_id '{conversation_id}': Contains illegal characters.")

        conv_dir = (self.root_dir / conversation_id).resolve()
        # Security assertion: ensure path does not escape root_dir
        if not str(conv_dir).startswith(str(self.root_dir.resolve())):
            raise PermissionError(f"Directory traversal attempt detected: '{conversation_id}'")

        conv_dir.mkdir(parents=True, exist_ok=True)
        return conv_dir

    def _get_artifact_files(self, conversation_id: str, artifact_id: str) -> Tuple[Path, Path]:
        """Resolves and validates paths for content and metadata sidecar."""
        if not SAFE_ID_PATTERN.match(artifact_id):
            raise ValueError(f"Invalid artifact_id '{artifact_id}': Contains illegal characters.")

        conv_dir = self._get_conversation_dir(conversation_id)
        content_path = (conv_dir / f"{artifact_id}.txt").resolve()
        meta_path = (conv_dir / f"{artifact_id}.meta.json").resolve()

        if not str(content_path).startswith(str(conv_dir)):
            raise PermissionError(f"Directory traversal attempt detected in artifact_id '{artifact_id}'")

        return content_path, meta_path

    def get_conversation_storage_usage(self, conversation_id: str) -> int:
        """Calculates total bytes occupied by artifacts in a conversation."""
        conv_dir = self._get_conversation_dir(conversation_id)
        total_bytes = 0
        for entry in conv_dir.iterdir():
            if entry.is_file():
                try:
                    total_bytes += entry.stat().st_size
                except OSError:
                    pass
        return total_bytes

    def should_offload(self, content_str: str) -> bool:
        """Determines if a tool output exceeds the 40 KB threshold."""
        return len(content_str.encode("utf-8")) > ARTIFACT_THRESHOLD_BYTES

    def generate_preview(self, content_str: str, artifact_id: str, byte_size: int, line_count: int) -> str:
        """Formulates a bounded preview envelope with head/tail lines and retrieval instructions."""
        lines = content_str.splitlines()

        if len(lines) <= 40 and len(content_str) <= 4000:
            return content_str

        head_lines = lines[:20]
        tail_lines = lines[-20:]

        head_text = "\n".join(head_lines)
        tail_text = "\n".join(tail_lines)

        truncation_notice = (
            f"\n\n... [TRUNCATED - Full tool output ({byte_size:,} bytes, {line_count:,} lines) "
            f"offloaded to artifact '{artifact_id}'. "
            f"Use 'presence.read_artifact' with artifact_id='{artifact_id}' to retrieve specific line ranges or chunks] ...\n\n"
        )

        return f"{head_text}{truncation_notice}{tail_text}"

    def store_text_artifact(
        self,
        conversation_id: str,
        tool_call_id: str,
        tool_name: str,
        content_str: str
    ) -> ArtifactMetadata:
        """Atomically stores full text content as a persistent artifact on disk."""
        content_bytes = content_str.encode("utf-8")
        byte_size = len(content_bytes)

        # Enforce resource quotas
        if byte_size > MAX_ARTIFACT_SIZE_BYTES:
            raise ValueError(
                f"Artifact size ({byte_size} bytes) exceeds maximum single artifact limit ({MAX_ARTIFACT_SIZE_BYTES} bytes)."
            )

        with self._get_conversation_lock(conversation_id):
            current_usage = self.get_conversation_storage_usage(conversation_id)
            if current_usage + byte_size > MAX_CONVERSATION_BUDGET_BYTES:
                raise ValueError(
                    f"Storing artifact ({byte_size} bytes) would exceed conversation budget ({MAX_CONVERSATION_BUDGET_BYTES} bytes, current: {current_usage} bytes)."
                )

            artifact_id = f"art_{uuid.uuid4().hex[:16]}"
            content_path, meta_path = self._get_artifact_files(conversation_id, artifact_id)

            # Compute SHA-256
            sha256 = hashlib.sha256(content_bytes).hexdigest()
            line_count = len(content_str.splitlines())

            preview = self.generate_preview(content_str, artifact_id, byte_size, line_count)

            metadata = ArtifactMetadata(
                artifact_id=artifact_id,
                conversation_id=conversation_id,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                created_at=datetime.now(timezone.utc).isoformat(),
                byte_size=byte_size,
                line_count=line_count,
                sha256=sha256,
                file_path=str(content_path),
                content_type="text/plain",
                preview=preview
            )

            # Atomic write for content
            tmp_content = content_path.with_suffix(".tmp")
            try:
                with open(tmp_content, "wb") as f:
                    f.write(content_bytes)
                tmp_content.replace(content_path)
            except Exception as e:
                if tmp_content.exists():
                    tmp_content.unlink(missing_ok=True)
                logger.error(f"Failed to write artifact content to {content_path}: {e}")
                raise

            # Atomic write for metadata sidecar
            tmp_meta = meta_path.with_suffix(".tmp")
            try:
                with open(tmp_meta, "w", encoding="utf-8") as f:
                    json.dump(metadata.to_dict(), f, indent=2)
                tmp_meta.replace(meta_path)
            except Exception as e:
                if tmp_meta.exists():
                    tmp_meta.unlink(missing_ok=True)
                logger.error(f"Failed to write artifact metadata to {meta_path}: {e}")
                raise

            logger.info(
                f"Offloaded large output ({byte_size:,} bytes) from '{tool_name}' "
                f"to artifact '{artifact_id}' in conversation '{conversation_id}'."
            )
            return metadata

    def get_metadata(self, conversation_id: str, artifact_id: str) -> Optional[ArtifactMetadata]:
        """Retrieves and verifies metadata for a stored artifact."""
        try:
            _, meta_path = self._get_artifact_files(conversation_id, artifact_id)
            if not meta_path.exists():
                return None

            with open(meta_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return ArtifactMetadata.from_dict(data)
        except Exception as e:
            logger.error(f"Failed to load metadata for artifact '{artifact_id}': {e}")
            return None

    def list_artifacts(self, conversation_id: str) -> List[ArtifactMetadata]:
        """Lists all artifacts stored for a conversation."""
        try:
            conv_dir = self._get_conversation_dir(conversation_id)
            results: List[ArtifactMetadata] = []
            for meta_file in sorted(conv_dir.glob("*.meta.json")):
                try:
                    with open(meta_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    results.append(ArtifactMetadata.from_dict(data))
                except Exception as err:
                    logger.warning(f"Skipping corrupt artifact metadata file {meta_file}: {err}")
            return results
        except Exception as e:
            logger.error(f"Failed to list artifacts for conversation '{conversation_id}': {e}")
            return []

    def read_artifact(
        self,
        conversation_id: str,
        artifact_id: str,
        offset: int = 0,
        limit: int = 100,
        mode: str = "lines"
    ) -> ArtifactReadResult:
        """Reads a chunk from a stored artifact in either 'lines' or 'bytes' mode with boundary protection."""
        content_path, _ = self._get_artifact_files(conversation_id, artifact_id)
        if not content_path.exists():
            raise FileNotFoundError(f"Artifact '{artifact_id}' not found in conversation '{conversation_id}'.")

        meta = self.get_metadata(conversation_id, artifact_id)
        total_bytes = meta.byte_size if meta else content_path.stat().st_size
        total_lines = meta.line_count if meta else 0

        # Validate offset & limit
        offset = max(0, offset)
        limit = max(1, min(limit, 5000 if mode == "lines" else 65536))

        if mode == "lines":
            returned_lines = []
            line_idx = 0
            has_more = False

            with open(content_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line_idx >= offset:
                        if len(returned_lines) < limit:
                            returned_lines.append(line.rstrip("\r\n"))
                        else:
                            has_more = True
                            break
                    line_idx += 1

            chunk_text = "\n".join(returned_lines)
            return ArtifactReadResult(
                artifact_id=artifact_id,
                offset=offset,
                limit=limit,
                mode="lines",
                total_bytes=total_bytes,
                total_lines=line_idx if total_lines == 0 else total_lines,
                returned_count=len(returned_lines),
                content=chunk_text,
                has_more=has_more
            )

        elif mode == "bytes":
            with open(content_path, "rb") as f:
                f.seek(offset)
                chunk_bytes = f.read(limit)
                remaining = f.read(1)
                has_more = len(remaining) > 0

            # Decode safely
            chunk_text = chunk_bytes.decode("utf-8", errors="replace")
            return ArtifactReadResult(
                artifact_id=artifact_id,
                offset=offset,
                limit=limit,
                mode="bytes",
                total_bytes=total_bytes,
                total_lines=total_lines,
                returned_count=len(chunk_bytes),
                content=chunk_text,
                has_more=has_more
            )
        else:
            raise ValueError(f"Unsupported read mode '{mode}'. Expected 'lines' or 'bytes'.")
