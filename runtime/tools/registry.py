"""Tool Registry & Safe Execution Pipeline
Manages tool definitions, JSON parameter schemas, and enforces deterministic 4-tier
permission authorization before executing any model-requested action.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List, Callable

from runtime.permissions import (
    PermissionBoundary,
    PermissionTier,
    ActionPolicy,
    AuthorizationResult,
    canonicalize_arguments,
)

logger = logging.getLogger("presence.tools.registry")


def normalize_json_schema(schema: Any) -> Any:
    """Recursively normalizes JSON Schema casing (e.g. Gemini UPPERCASE types to standard lowercase)."""
    if not isinstance(schema, dict):
        return schema
    normalized = {}
    for k, v in schema.items():
        if k == "type" and isinstance(v, str):
            normalized[k] = v.lower()
        elif isinstance(v, dict):
            normalized[k] = normalize_json_schema(v)
        elif isinstance(v, list):
            normalized[k] = [normalize_json_schema(item) if isinstance(item, dict) else item for item in v]
        else:
            normalized[k] = v
    return normalized


def validate_tool_arguments_schema(args: Dict[str, Any], schema: Dict[str, Any]) -> None:
    """Validates tool arguments against a JSON schema.
    Raises ValueError on validation failure.
    """
    if not schema or not isinstance(schema, dict):
        return
    norm_schema = normalize_json_schema(schema)
    try:
        import jsonschema
        jsonschema.validate(instance=args, schema=norm_schema)
    except ImportError:
        # Fallback basic required properties check if jsonschema is missing
        required = norm_schema.get("required", [])
        for req in required:
            if req not in args:
                raise ValueError(f"'{req}' is a required property")
    except jsonschema.ValidationError as e:
        raise ValueError(e.message) from e
    except jsonschema.SchemaError as e:
        raise ValueError(f"Malformed schema: {e.message}") from e


@dataclass
class ToolDefinition:
    """Strongly typed tool definition with authority requirements and concurrency metadata."""
    tool_id: str
    name: str
    description: str
    parameters: Dict[str, Any]
    authority_tier: PermissionTier
    handler: Callable[[Dict[str, Any]], Dict[str, Any]]
    timeout_seconds: float = 10.0
    read_only: bool = False
    idempotent: bool = False
    concurrency_group: Optional[str] = None
    max_parallel: int = 5

    def __post_init__(self):
        # Infer read_only automatically for observation/query tools if not explicitly set
        if not self.read_only and self.authority_tier in (PermissionTier.L0_OBSERVE, getattr(PermissionTier, "L1_RECOMMEND", 1)):
            self.read_only = True

    def to_declaration(self) -> Dict[str, Any]:
        """Converts definition to model function declaration schema."""
        return {
            "name": self.tool_id,
            "description": self.description,
            "parameters": self.parameters
        }


class ToolRegistry:
    """Internal registry for tools and safe execution dispatcher."""

    def __init__(self):
        self._tools: Dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition, permission_boundary: Optional[PermissionBoundary] = None) -> None:
        """Registers a tool definition and ensures its policy is known to the permission boundary."""
        self._tools[tool.tool_id] = tool
        if permission_boundary:
            permission_boundary.register_policy(ActionPolicy(
                action_name=tool.tool_id,
                required_level=tool.authority_tier,
                description=tool.description,
                requires_explicit_consent=(tool.authority_tier == PermissionTier.L3_PROTECTED)
            ))
        logger.info(f"Registered tool '{tool.tool_id}' (Tier: {tool.authority_tier.name})")

    def unregister(self, tool_id: str) -> bool:
        """Removes a tool definition."""
        if tool_id in self._tools:
            del self._tools[tool_id]
            logger.info(f"Unregistered tool '{tool_id}'")
            return True
        return False

    def get_tool(self, tool_id: str) -> Optional[ToolDefinition]:
        """Retrieves a tool definition by ID or name."""
        if tool_id in self._tools:
            return self._tools[tool_id]
        for t in self._tools.values():
            if t.name == tool_id:
                return t
        return None

    def list_tools(self) -> List[ToolDefinition]:
        """Returns all registered tool definitions."""
        return list(self._tools.values())

    def get_declarations(self) -> List[Dict[str, Any]]:
        """Returns function declaration list for model providers."""
        return [tool.to_declaration() for tool in self._tools.values()]

    def execute(
        self,
        tool_id: str,
        args: Dict[str, Any],
        permission_boundary: PermissionBoundary,
        consent_token: Optional[str] = None,
        conversation_id: str = "",
        call_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Validates, authorizes, and executes a requested tool.
        Returns a structured result envelope:
        {"status": "SUCCESS" | "ERROR", "result": Any, "error": Optional[str]}
        """
        # Set context variables for delegation tools if provided
        tok_call = None
        tok_conv = None
        try:
            from runtime.tools.delegation_tools import current_tool_call_id, current_conversation_id
            if call_id and current_tool_call_id.get() is None:
                tok_call = current_tool_call_id.set(call_id)
            if conversation_id and current_conversation_id.get() is None:
                tok_conv = current_conversation_id.set(conversation_id)
        except ImportError:
            pass

        tool = self.get_tool(tool_id)
        if not tool:
            err_msg = f"Tool '{tool_id}' is not registered in the tool catalog."
            logger.warning(err_msg)
            return {"status": "ERROR", "error": err_msg}

        # Step 1: Validate incoming arguments and produce an isolated immutable snapshot
        # Guarantees immunity against TOCTOU and concurrent external argument mutations.
        try:
            canonical_json = canonicalize_arguments(args)
            authorized_args = json.loads(canonical_json)
        except Exception as e:
            err_msg = f"Argument validation and canonicalization failed for '{tool_id}': {e}"
            logger.warning(err_msg)
            return {"status": "ERROR", "error": err_msg}

        # Step 1b: Validate arguments against JSON Schema definition if declared
        if tool.parameters and isinstance(tool.parameters, dict):
            schema_props = tool.parameters.get("properties")
            schema_req = tool.parameters.get("required")
            schema_type = tool.parameters.get("type")
            if schema_props is not None or schema_req is not None or schema_type is not None:
                try:
                    validate_tool_arguments_schema(authorized_args, tool.parameters)
                except Exception as val_err:
                    err_msg = f"Argument schema validation failed for '{tool_id}': {val_err}"
                    logger.warning(err_msg)
                    return {"status": "ERROR", "error": err_msg}

        # Step 2: Ensure policy is registered in boundary
        if tool.tool_id not in permission_boundary._policies:
            permission_boundary.register_policy(ActionPolicy(
                action_name=tool.tool_id,
                required_level=tool.authority_tier,
                description=tool.description,
                requires_explicit_consent=(tool.authority_tier == PermissionTier.L3_PROTECTED)
            ))

        # Step 3: Validate explicit user consent token if provided (cryptographic single-use verification)
        granted_consent = False
        if consent_token:
            granted_consent = permission_boundary.verify_consent_token(
                token=consent_token,
                action_name=tool.tool_id,
                context=authorized_args,
                conversation_id=conversation_id,
                expected_tier=tool.authority_tier
            )

        # Step 4: Authorize against the 4-tier permission boundary using authorized snapshot
        auth: AuthorizationResult = permission_boundary.check_permission(
            action_name=tool.tool_id,
            context=authorized_args,
            granted_consent=granted_consent
        )

        if not auth.allowed:
            err_msg = f"Execution blocked by security gate: {auth.reason}"
            logger.warning(f"SECURITY GATE: {tool_id} denied - {auth.reason}")
            return {
                "status": "ERROR",
                "error": err_msg,
                "tier": tool.authority_tier.name,
                "allowed": False
            }

        # Step 5: Execute registered handler strictly with authorized_args snapshot
        try:
            result = tool.handler(authorized_args)
            return {"status": "SUCCESS", "result": result}
        except Exception as e:
            err_msg = f"Error during tool execution: {str(e)}"
            logger.error(f"Tool execution failed for '{tool_id}': {e}")
            return {"status": "ERROR", "error": err_msg}
        finally:
            try:
                from runtime.tools.delegation_tools import current_tool_call_id, current_conversation_id
                if tok_call is not None:
                    current_tool_call_id.reset(tok_call)
                if tok_conv is not None:
                    current_conversation_id.reset(tok_conv)
            except ImportError:
                pass


def build_default_tools(
    runtime_status_fn: Optional[Callable[[], Dict[str, Any]]] = None,
    conversation_info_fn: Optional[Callable[[], Dict[str, Any]]] = None,
    search_conversations_fn: Optional[Callable[[str, int], List[Dict[str, Any]]]] = None,
    read_artifact_fn: Optional[Callable[[str, int, int, str], Dict[str, Any]]] = None,
    permission_boundary: Optional[PermissionBoundary] = None
) -> ToolRegistry:
    """Builds a ToolRegistry populated with the initial harmless L0_OBSERVE tools."""
    registry = ToolRegistry()

    # 1. presence.get_runtime_status
    if runtime_status_fn:
        registry.register(ToolDefinition(
            tool_id="presence.get_runtime_status",
            name="get_runtime_status",
            description="Returns current runtime health, process memory, and uptime status.",
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False
            },
            authority_tier=PermissionTier.L0_OBSERVE,
            handler=lambda args: runtime_status_fn()
        ), permission_boundary=permission_boundary)

    # 2. presence.get_conversation_info
    if conversation_info_fn:
        registry.register(ToolDefinition(
            tool_id="presence.get_conversation_info",
            name="get_conversation_info",
            description="Returns metadata, title, and step count of the active conversation.",
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False
            },
            authority_tier=PermissionTier.L0_OBSERVE,
            handler=lambda args: conversation_info_fn()
        ), permission_boundary=permission_boundary)

    # 3. presence.search_conversations
    if search_conversations_fn:
        registry.register(ToolDefinition(
            tool_id="presence.search_conversations",
            name="search_conversations",
            description="Searches all historical conversation steps and notes using SQLite FTS5 full-text search.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search keyword or phrase to match in past conversations."
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of search results to return (default 5)."
                    }
                },
                "required": ["query"],
                "additionalProperties": False
            },
            authority_tier=PermissionTier.L0_OBSERVE,
            handler=lambda args: search_conversations_fn(args.get("query", ""), int(args.get("limit", 5)))
        ), permission_boundary=permission_boundary)

    # 4. presence.read_artifact
    if read_artifact_fn:
        registry.register(ToolDefinition(
            tool_id="presence.read_artifact",
            name="read_artifact",
            description="Reads chunks or line ranges from an offloaded large tool execution artifact stored on disk.",
            parameters={
                "type": "object",
                "properties": {
                    "artifact_id": {
                        "type": "string",
                        "description": "Identifier of the artifact to read (e.g. 'art_...')."
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Starting line number or byte offset (0-indexed, default 0)."
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of lines or bytes to return (default 100 lines or 4000 bytes)."
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["lines", "bytes"],
                        "description": "Read mode: 'lines' to read by line count (default), or 'bytes' to read by byte offset."
                    }
                },
                "required": ["artifact_id"],
                "additionalProperties": False
            },
            authority_tier=PermissionTier.L0_OBSERVE,
            handler=lambda args: read_artifact_fn(
                args.get("artifact_id", ""),
                int(args.get("offset", 0)),
                int(args.get("limit", 100)),
                args.get("mode", "lines")
            )
        ), permission_boundary=permission_boundary)

    return registry
