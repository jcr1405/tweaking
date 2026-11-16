"""MCP Schema Caching and Lazy Hydration Infrastructure
Caches discovered MCP tool schemas to local disk (%LOCALAPPDATA%\\Presence\\mcp\\schemas)
allowing the Presence engine to present tool declarations to models without
requiring persistent execution of all underlying server subprocesses.
"""

import os
import json
import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List

from runtime.mcp.models import MCPToolInfo

logger = logging.getLogger("presence.mcp.schemas")


def get_default_schemas_dir() -> Path:
    """Returns standard OS path for Presence MCP schema caches."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "Presence" / "mcp" / "schemas"
    return Path.home() / ".presence" / "mcp" / "schemas"


class SchemaCache:
    """Manages disk-persisted MCP tool schemas with SHA-256 integrity verification."""

    def __init__(self, cache_dir: Optional[Path] = None):
        self.cache_dir = cache_dir or get_default_schemas_dir()
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _get_schema_file(self, server_id: str, tool_name: str) -> Path:
        """Computes safe sanitized filename for a cached tool schema."""
        safe_server = "".join(c for c in server_id if c.isalnum() or c in ("-", "_"))
        safe_tool = "".join(c for c in tool_name if c.isalnum() or c in ("-", "_"))
        return self.cache_dir / f"{safe_server}_{safe_tool}.json"

    def _compute_hash(self, content_dict: Dict[str, Any]) -> str:
        """Computes deterministic SHA-256 hash of schema dict."""
        serialized = json.dumps(content_dict, sort_keys=True).encode("utf-8")
        return hashlib.sha256(serialized).hexdigest()

    def save_tool_schema(self, tool_info: MCPToolInfo) -> Path:
        """Persists a single tool schema to disk with metadata and integrity hash."""
        schema_file = self._get_schema_file(tool_info.server_id, tool_info.tool_name)
        schema_data = {
            "server_id": tool_info.server_id,
            "tool_name": tool_info.tool_name,
            "namespaced_name": tool_info.namespaced_name,
            "description": tool_info.description,
            "input_schema": tool_info.input_schema,
            "cached_at": datetime.now(timezone.utc).isoformat(),
        }
        schema_data["schema_hash"] = self._compute_hash(tool_info.input_schema)

        temp_file = schema_file.with_suffix(".tmp")
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(schema_data, f, indent=2)
            temp_file.replace(schema_file)
            logger.debug(f"Cached schema for '{tool_info.namespaced_name}' at {schema_file}")
            return schema_file
        except Exception as e:
            if temp_file.exists():
                temp_file.unlink(missing_ok=True)
            logger.error(f"Failed to cache schema for '{tool_info.namespaced_name}': {e}")
            raise

    def save_server_schemas(self, server_id: str, tools: List[MCPToolInfo]) -> None:
        """Batch saves all schemas discovered for a given server."""
        for tool in tools:
            self.save_tool_schema(tool)
        logger.info(f"Cached {len(tools)} tool schema(s) for server '{server_id}'")

    def load_tool_schema(self, server_id: str, tool_name: str) -> Optional[MCPToolInfo]:
        """Loads and verifies a cached tool schema from disk."""
        schema_file = self._get_schema_file(server_id, tool_name)
        if not schema_file.exists():
            return None

        try:
            with open(schema_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Integrity check
            expected_hash = data.get("schema_hash")
            input_schema = data.get("input_schema", {})
            if expected_hash and self._compute_hash(input_schema) != expected_hash:
                logger.warning(f"Integrity check failed for cached schema {schema_file}; invalidating.")
                schema_file.unlink(missing_ok=True)
                return None

            return MCPToolInfo(
                server_id=data["server_id"],
                tool_name=data["tool_name"],
                description=data.get("description", ""),
                input_schema=input_schema
            )
        except Exception as e:
            logger.error(f"Error reading schema cache {schema_file}: {e}")
            return None

    def load_server_schemas(self, server_id: str) -> List[MCPToolInfo]:
        """Loads all cached schemas for a server."""
        safe_server = "".join(c for c in server_id if c.isalnum() or c in ("-", "_"))
        prefix = f"{safe_server}_"
        tools: List[MCPToolInfo] = []

        if not self.cache_dir.exists():
            return tools

        for file_path in self.cache_dir.glob(f"{prefix}*.json"):
            tool_name = file_path.stem[len(prefix):]
            tool_info = self.load_tool_schema(server_id, tool_name)
            if tool_info:
                tools.append(tool_info)

        return tools

    def invalidate_server(self, server_id: str) -> int:
        """Deletes all cached schemas for a server."""
        safe_server = "".join(c for c in server_id if c.isalnum() or c in ("-", "_"))
        prefix = f"{safe_server}_"
        count = 0
        if not self.cache_dir.exists():
            return count

        for file_path in self.cache_dir.glob(f"{prefix}*.json"):
            try:
                file_path.unlink()
                count += 1
            except Exception as e:
                logger.warning(f"Failed to delete cached schema {file_path}: {e}")
        logger.info(f"Invalidated {count} cached schema(s) for server '{server_id}'")
        return count

    def invalidate_all(self) -> int:
        """Deletes all cached schemas across all servers."""
        count = 0
        if not self.cache_dir.exists():
            return count

        for file_path in self.cache_dir.glob("*.json"):
            try:
                file_path.unlink()
                count += 1
            except Exception as e:
                logger.warning(f"Failed to delete cached schema {file_path}: {e}")
        logger.info(f"Invalidated all ({count}) cached MCP tool schemas.")
        return count

    @staticmethod
    def to_declaration(tool_info: MCPToolInfo) -> Dict[str, Any]:
        """Converts an MCPToolInfo to model function declaration schema."""
        params = tool_info.input_schema if isinstance(tool_info.input_schema, dict) else {}
        # Ensure schema structure conforms to JSON Schema / Gemini declaration format
        if "type" not in params:
            params = {
                "type": "object",
                "properties": params.get("properties", {}),
                "required": params.get("required", [])
            }

        return {
            "name": tool_info.namespaced_name,
            "description": tool_info.description or f"MCP tool '{tool_info.tool_name}' from server '{tool_info.server_id}'",
            "parameters": params
        }
