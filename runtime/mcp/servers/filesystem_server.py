"""Presence Filesystem Model Context Protocol (MCP) Server
Provides persistent, safe, and robust Windows filesystem operations for Presence agents.
Implements stdio transport via the official MCP 2.x SDK (mcp.server.mcpserver.MCPServer).
"""

import os
import sys
import glob
import shutil
import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

from mcp.server.mcpserver import MCPServer

# Initialize MCP Server for Filesystem
server = MCPServer("filesystem")


def _resolve_safe_path(raw_path: str) -> str:
    """Normalizes and resolves absolute file path on Windows."""
    expanded = os.path.expanduser(os.path.expandvars(raw_path.strip()))
    return os.path.abspath(expanded)


@server.tool()
def read_file(path: str, offset: int = 0, limit: int = 1000) -> str:
    """Reads lines from a file with optional 0-indexed line offset and limit.
    
    Args:
        path: Path to the target file.
        offset: Starting line number (0-indexed, default: 0).
        limit: Maximum number of lines to return (default: 1000).
    """
    abs_path = _resolve_safe_path(path)
    if not os.path.exists(abs_path):
        return f"ERROR: File does not exist: {abs_path}"
    if os.path.isdir(abs_path):
        return f"ERROR: Path is a directory, not a file: {abs_path}"

    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        
        total_lines = len(lines)
        if offset < 0:
            offset = 0
        end = offset + limit
        selected = lines[offset:end]
        
        header = f"# File: {abs_path} (Showing lines {offset + 1}-{min(end, total_lines)} of {total_lines})\n"
        return header + "".join(selected)
    except Exception as exc:
        return f"ERROR: Failed to read file '{abs_path}': {exc}"


@server.tool()
def read_multiple_files(paths: List[str]) -> Dict[str, str]:
    """Reads the contents of multiple files in a single invocation.
    
    Args:
        paths: List of file paths to read.
    """
    results: Dict[str, str] = {}
    for p in paths:
        abs_p = _resolve_safe_path(p)
        if not os.path.exists(abs_p):
            results[p] = f"ERROR: File not found: {abs_p}"
            continue
        if os.path.isdir(abs_p):
            results[p] = f"ERROR: Path is a directory: {abs_p}"
            continue
        try:
            with open(abs_p, "r", encoding="utf-8", errors="replace") as f:
                results[p] = f.read()
        except Exception as exc:
            results[p] = f"ERROR: {exc}"
    return results


@server.tool()
def write_file(path: str, content: str, append: bool = False) -> str:
    """Creates a new file or overwrites/appends to an existing file.
    Automatically creates missing parent directories.
    
    Args:
        path: Path to the file to create or write.
        content: Text content to write into the file.
        append: If True, appends to the file instead of overwriting.
    """
    abs_path = _resolve_safe_path(path)
    try:
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        mode = "a" if append else "w"
        with open(abs_path, mode, encoding="utf-8") as f:
            f.write(content)
        
        action = "Appended to" if append else "Successfully wrote"
        byte_count = len(content.encode("utf-8"))
        return f"{action} file '{abs_path}' ({byte_count:,} bytes written)."
    except Exception as exc:
        return f"ERROR: Failed to write file '{abs_path}': {exc}"


@server.tool()
def edit_file(path: str, target: str, replacement: str) -> str:
    """Performs exact text substitution in an existing file.
    
    Args:
        path: Target file path.
        target: Exact substring sequence to be replaced.
        replacement: Drop-in replacement string.
    """
    abs_path = _resolve_safe_path(path)
    if not os.path.exists(abs_path):
        return f"ERROR: File does not exist: {abs_path}"
    if not target:
        return "ERROR: 'target' string cannot be empty."

    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            content = f.read()
        
        count = content.count(target)
        if count == 0:
            return f"ERROR: Target string not found in '{abs_path}'."
        
        new_content = content.replace(target, replacement)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(new_content)
        
        return f"Successfully replaced {count} occurrence(s) of target in '{abs_path}'."
    except Exception as exc:
        return f"ERROR: Failed to edit file '{abs_path}': {exc}"


DEFAULT_IGNORED_DIRS = {
    ".git", ".venv", "venv", "__pycache__", "node_modules",
    ".pytest_cache", ".gemini", "dist", "build", ".egg-info"
}


@server.tool()
def list_directory(
    path: str = ".",
    recursive: bool = False,
    max_depth: int = 2,
    include_ignored: bool = False
) -> List[Dict[str, Any]]:
    """Lists files and folders in a directory with file sizes and timestamps.
    Automatically excludes noise directories (.git, .venv, node_modules) unless include_ignored is True.
    
    Args:
        path: Directory path to inspect (defaults to current working directory).
        recursive: Whether to traverse subdirectories.
        max_depth: Maximum recursion depth when recursive is True.
        include_ignored: If True, includes development directories (.git, .venv, etc.).
    """
    abs_path = _resolve_safe_path(path)
    if not os.path.exists(abs_path):
        return [{"error": f"Directory not found: {abs_path}"}]
    if not os.path.isdir(abs_path):
        return [{"error": f"Path is not a directory: {abs_path}"}]

    entries: List[Dict[str, Any]] = []

    def scan(current_dir: str, depth: int):
        if len(entries) >= 150:
            return
        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    if len(entries) >= 150:
                        break
                    try:
                        name_lower = entry.name.lower()
                        if not include_ignored and name_lower in DEFAULT_IGNORED_DIRS:
                            continue
                        stat = entry.stat()
                        is_dir = entry.is_dir()
                        entries.append({
                            "name": entry.name,
                            "path": entry.path,
                            "is_dir": is_dir,
                            "size_bytes": 0 if is_dir else stat.st_size,
                            "modified": datetime.datetime.fromtimestamp(stat.st_mtime).isoformat()
                        })
                        if recursive and is_dir and depth < max_depth:
                            scan(entry.path, depth + 1)
                    except (PermissionError, OSError):
                        continue
        except (PermissionError, OSError):
            pass

    scan(abs_path, 1)
    if len(entries) >= 150:
        entries.append({
            "name": "[TRUNCATED]",
            "path": abs_path,
            "is_dir": False,
            "size_bytes": 0,
            "modified": datetime.datetime.now().isoformat(),
            "_notice": "Listing capped at 150 entries to conserve context tokens. Inspect specific subdirectories for deeper traversal."
        })
    # Sort directories first, then alphabetical
    entries.sort(key=lambda x: (not x.get("is_dir", False), x.get("name", "").lower()))
    return entries



@server.tool()
def get_file_info(path: str) -> Dict[str, Any]:
    """Retrieves detailed file or directory metadata.
    
    Args:
        path: Path to inspect.
    """
    abs_path = _resolve_safe_path(path)
    if not os.path.exists(abs_path):
        return {"error": f"Path does not exist: {abs_path}"}

    try:
        stat = os.stat(abs_path)
        is_dir = os.path.isdir(abs_path)
        info: Dict[str, Any] = {
            "path": abs_path,
            "name": os.path.basename(abs_path),
            "is_dir": is_dir,
            "is_file": os.path.isfile(abs_path),
            "size_bytes": stat.st_size,
            "created": datetime.datetime.fromtimestamp(stat.st_ctime).isoformat(),
            "modified": datetime.datetime.fromtimestamp(stat.st_mtime).isoformat(),
        }
        if not is_dir and stat.st_size < 10 * 1024 * 1024:  # under 10MB
            try:
                with open(abs_path, "r", encoding="utf-8", errors="ignore") as f:
                    info["line_count"] = sum(1 for _ in f)
            except Exception:
                pass
        return info
    except Exception as exc:
        return {"error": str(exc)}


@server.tool()
def search_files(path: str = ".", query: str = "", extension: str = "", max_results: int = 50) -> List[Dict[str, Any]]:
    """Searches for files matching a name query or containing text content.
    
    Args:
        path: Root directory to search from.
        query: Substring to match against filename or file text content.
        extension: Optional file extension filter (e.g. '.py', '.json').
        max_results: Maximum number of matches to return (default: 50).
    """
    abs_path = _resolve_safe_path(path)
    if not os.path.isdir(abs_path):
        return [{"error": f"Invalid directory: {abs_path}"}]

    results: List[Dict[str, Any]] = []
    clean_ext = extension.strip().lower()
    if clean_ext and not clean_ext.startswith("."):
        clean_ext = "." + clean_ext

    for root, dirs, files in os.walk(abs_path):
        # Filter out noisy directories from traversing
        dirs[:] = [d for d in dirs if d.lower() not in DEFAULT_IGNORED_DIRS]
        for f in files:

            if clean_ext and not f.lower().endswith(clean_ext):
                continue
            full_path = os.path.join(root, f)
            matched = False
            match_reason = ""

            if not query or query.lower() in f.lower():
                matched = True
                match_reason = "Filename match"
            elif query:
                # Check file contents if small text file
                try:
                    if os.path.getsize(full_path) < 2 * 1024 * 1024:  # Under 2MB
                        with open(full_path, "r", encoding="utf-8", errors="ignore") as content_file:
                            if query.lower() in content_file.read().lower():
                                matched = True
                                match_reason = "Content match"
                except Exception:
                    pass

            if matched:
                results.append({
                    "name": f,
                    "path": full_path,
                    "reason": match_reason,
                    "size_bytes": os.path.getsize(full_path)
                })
                if len(results) >= max_results:
                    return results

    return results


@server.tool()
def create_directory(path: str) -> str:
    """Creates a directory and any necessary intermediate parent directories.
    
    Args:
        path: Directory path to create.
    """
    abs_path = _resolve_safe_path(path)
    try:
        os.makedirs(abs_path, exist_ok=True)
        return f"Directory created successfully: {abs_path}"
    except Exception as exc:
        return f"ERROR: Failed to create directory '{abs_path}': {exc}"


@server.tool()
def move_file(source: str, destination: str) -> str:
    """Moves or renames a file or directory.
    
    Args:
        source: Source file or directory path.
        destination: Target destination path.
    """
    abs_src = _resolve_safe_path(source)
    abs_dst = _resolve_safe_path(destination)
    if not os.path.exists(abs_src):
        return f"ERROR: Source path does not exist: {abs_src}"

    try:
        os.makedirs(os.path.dirname(abs_dst), exist_ok=True)
        shutil.move(abs_src, abs_dst)
        return f"Successfully moved '{abs_src}' -> '{abs_dst}'."
    except Exception as exc:
        return f"ERROR: Failed to move '{abs_src}': {exc}"


@server.tool()
def delete_file(path: str) -> str:
    """Deletes a single file from the filesystem.
    Fails safely if the target is a directory to prevent accidental tree deletion.
    
    Args:
        path: Path to the file to delete.
    """
    abs_path = _resolve_safe_path(path)
    if not os.path.exists(abs_path):
        return f"ERROR: File does not exist: {abs_path}"
    if os.path.isdir(abs_path):
        return f"ERROR: Path is a directory. Refusing to delete directory via delete_file: {abs_path}"

    try:
        os.remove(abs_path)
        return f"Successfully deleted file: {abs_path}"
    except Exception as exc:
        return f"ERROR: Failed to delete file '{abs_path}': {exc}"


if __name__ == "__main__":
    server.run("stdio")
