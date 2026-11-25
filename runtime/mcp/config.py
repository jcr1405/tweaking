"""MCP Configuration Persistence and Security Boundary
Manages loading, validation, and serialization of external MCP server configurations
from disk (%LOCALAPPDATA%\\Presence\\mcp_servers.json).

Security Invariants:
1. Versioned schema ("1.0"). Unknown versions are rejected.
2. Missing configuration fails closed (returns empty configuration).
3. Tool approval defaults to default-deny (auto_approve_tools=False).
4. Command and argument parameters are strictly separated (no shell execution).
5. Environment variable overrides are sanitized via Policy A and Policy B.
6. Local authority tier definitions strictly override server-advertised metadata.
"""

import os
import json
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List

from runtime.permissions import PermissionTier
from runtime.mcp.models import MCPServerConfig
from runtime.mcp.client import validate_server_environment_overrides

logger = logging.getLogger("presence.mcp.config")

CURRENT_CONFIG_VERSION = "1.0"


def get_default_mcp_config_path() -> Path:
    """Returns standard OS path for Presence MCP server configurations.
    Prioritizes local workspace mcp_servers.json if present, then %LOCALAPPDATA%\\Presence\\mcp_servers.json.
    """
    local_cwd = Path.cwd() / "mcp_servers.json"
    if local_cwd.exists():
        return local_cwd

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        appdata_path = Path(local_app_data) / "Presence" / "mcp_servers.json"
        if appdata_path.exists():
            return appdata_path
        return appdata_path
    return Path.home() / ".presence" / "mcp_servers.json"


def _parse_permission_tier(tier_str: Optional[str], default: PermissionTier = PermissionTier.L0_OBSERVE) -> PermissionTier:
    """Safely converts string permission tier representation to PermissionTier enum."""
    if not tier_str:
        return default
    clean = tier_str.strip().upper()
    try:
        return PermissionTier[clean]
    except KeyError:
        # Also support integer string or name matching
        for tier in PermissionTier:
            if tier.name == clean or str(tier.value) == clean:
                return tier
        logger.warning(f"Unknown permission tier '{tier_str}'; defaulting to {default.name}")
        return default


def parse_mcp_server_config(raw: Dict[str, Any]) -> MCPServerConfig:
    """Validates and parses a raw dictionary into a strongly typed MCPServerConfig.

    Raises:
        ValueError: If mandatory fields are missing or invalid.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"Server configuration must be a dictionary, got {type(raw).__name__}")

    server_id = str(raw.get("server_id", "")).strip()
    if not server_id:
        raise ValueError("MCP server configuration missing required 'server_id'")

    # Disallow dangerous characters in server_id (used in filenames and tool namespaces)
    if not server_id.replace("_", "").replace("-", "").isalnum():
        raise ValueError(f"Invalid 'server_id' '{server_id}': must contain only alphanumeric characters, dashes, and underscores")

    command = str(raw.get("command", "")).strip()
    if not command:
        raise ValueError(f"MCP server '{server_id}' missing required 'command' executable path")
    import sys
    if command.lower() in ("python", "python.exe", "python3"):
        command = sys.executable

    raw_args = raw.get("args", [])
    if not isinstance(raw_args, list):
        raise ValueError(f"MCP server '{server_id}' 'args' must be a list of strings")
    args = [str(a) for a in raw_args]

    cwd = raw.get("cwd")
    if cwd is not None:
        cwd = str(cwd).strip()
        if not cwd:
            cwd = None

    raw_env = raw.get("env", {})
    if not isinstance(raw_env, dict):
        raw_env = {}
    # Validate environment overrides via client security policy
    validated_env = validate_server_environment_overrides(raw_env, strict=False)

    default_tier_str = raw.get("default_authority_tier", "L0_OBSERVE")
    default_tier = _parse_permission_tier(default_tier_str, default=PermissionTier.L0_OBSERVE)

    raw_tool_tiers = raw.get("tool_tiers", {})
    tool_tiers: Dict[str, PermissionTier] = {}
    if isinstance(raw_tool_tiers, dict):
        for tool_name, tier_val in raw_tool_tiers.items():
            tool_tiers[str(tool_name)] = _parse_permission_tier(str(tier_val), default=default_tier)

    approved_tools = raw.get("approved_tools")
    if approved_tools is not None:
        if isinstance(approved_tools, list):
            approved_tools = [str(t).strip() for t in approved_tools if str(t).strip()]
        else:
            approved_tools = None

    return MCPServerConfig(
        server_id=server_id,
        name=str(raw.get("name", server_id)).strip() or server_id,
        command=command,
        args=args,
        cwd=cwd,
        env=validated_env,
        startup_timeout=float(raw.get("startup_timeout", 10.0)),
        request_timeout=float(raw.get("request_timeout", 30.0)),
        shutdown_timeout=float(raw.get("shutdown_timeout", 5.0)),
        max_restarts=int(raw.get("max_restarts", 3)),
        restart_backoff_seconds=float(raw.get("restart_backoff_seconds", 1.0)),
        enabled=bool(raw.get("enabled", True)),
        default_authority_tier=default_tier,
        tool_tiers=tool_tiers,
        approved_tools=approved_tools,
        auto_approve_tools=bool(raw.get("auto_approve_tools", False)),
    )


def load_mcp_configuration(config_path: Optional[Path] = None) -> List[MCPServerConfig]:
    """Loads and validates MCP server configurations from disk.

    Returns:
        List of MCPServerConfig objects. Fails closed (empty list) if file is missing.

    Raises:
        ValueError: If file is malformed JSON or has an unsupported version.
    """
    path = Path(config_path) if config_path else get_default_mcp_config_path()
    if not path.exists():
        logger.debug(f"MCP configuration file not found at {path}; starting with 0 external servers.")
        return []

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        err_msg = f"Malformed JSON in MCP configuration file at {path}: {exc}"
        logger.error(err_msg)
        raise ValueError(err_msg) from exc
    except Exception as exc:
        err_msg = f"Error reading MCP configuration file at {path}: {exc}"
        logger.error(err_msg)
        raise ValueError(err_msg) from exc

    if not isinstance(data, dict):
        raise ValueError(f"Root of MCP configuration at {path} must be a JSON object")

    version = str(data.get("version", "")).strip()
    if version != CURRENT_CONFIG_VERSION:
        raise ValueError(
            f"Unsupported MCP configuration version '{version}' at {path} (expected '{CURRENT_CONFIG_VERSION}')"
        )

    raw_servers = data.get("servers", [])
    configs: List[MCPServerConfig] = []

    # Support either list of server objects or dict of {server_id: server_object}
    if isinstance(raw_servers, dict):
        for sid, sval in raw_servers.items():
            if isinstance(sval, dict) and "server_id" not in sval:
                sval["server_id"] = sid
            configs.append(parse_mcp_server_config(sval))
    elif isinstance(raw_servers, list):
        for sval in raw_servers:
            if isinstance(sval, dict):
                configs.append(parse_mcp_server_config(sval))
    else:
        raise ValueError(f"'servers' field in {path} must be a list or dictionary")

    logger.info(f"Loaded {len(configs)} MCP server configuration(s) from {path}")
    return configs


def save_mcp_configuration(
    configs: List[MCPServerConfig],
    config_path: Optional[Path] = None
) -> Path:
    """Serializes a list of MCPServerConfig objects to JSON on disk."""
    path = Path(config_path) if config_path else get_default_mcp_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    data = {
        "version": CURRENT_CONFIG_VERSION,
        "servers": [cfg.to_dict() for cfg in configs]
    }

    temp_path = path.with_suffix(".tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    temp_path.replace(path)
    logger.info(f"Saved {len(configs)} MCP server configuration(s) to {path}")
    return path
