"""FreeLLMAPI Model Provider Adapter
Integrates self-hosted FreeLLMAPI gateways (OpenAI-compatible HTTP API) behind the IModelProvider contract.
Supports dynamic model discovery, SSE streaming, incremental tool-call assembly,
usage accounting, and credential isolation.
"""

import os
import sys
import json
import socket
import logging
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List, Iterator, Tuple
from urllib.parse import urlparse

import httpx

from runtime.providers.base import IModelProvider, ModelStreamChunk
from runtime.platform_win32 import read_windows_credential

logger = logging.getLogger("presence.providers.freellmapi")


@dataclass
class FreeLLMAPIConfig:
    """Configuration schema for FreeLLMAPI inference provider."""
    enabled: bool = True
    base_url: str = "http://127.0.0.1:3001/v1"
    api_key: str = ""
    default_model: str = "auto"
    connect_timeout: float = 5.0
    read_timeout: float = 25.0
    streaming: bool = True
    temperature: float = 0.7
    max_tokens: int = 4096


def _is_port_open(host: str, port: int, timeout: float = 0.2) -> bool:
    """Fast check whether a local TCP port is accepting connections."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


def detect_freellmapi_credentials() -> Tuple[str, str]:
    """Auto-detects FreeLLMAPI base URL and API key from environment and Windows Credential Manager.

    Returns:
        Tuple[str, str]: (base_url, api_key)
    """
    from runtime.config import is_local_only_mode
    if is_local_only_mode():
        return "", ""

    # 1. Base URL detection
    base_url = os.environ.get("FREELLMAPI_BASE_URL", "").strip()
    if not base_url:
        # Check if local desktop gateway is running on port 31415 or port 3001
        if _is_port_open("127.0.0.1", 31415):
            base_url = "http://127.0.0.1:31415/v1"
        else:
            base_url = "http://127.0.0.1:3001/v1"

    # 2. API Key detection
    api_key = os.environ.get("FREELLMAPI_API_KEY", "").strip()
    if not api_key and sys.platform == "win32":
        # Check Windows Credential Manager targets
        for target in ["aether_assistant", "freellmapi_key@aether_assistant", "freellmapi"]:
            cred = read_windows_credential(target)
            if cred and cred.strip():
                api_key = cred.strip()
                break

    return base_url, api_key


def normalize_schema_types(schema: Any) -> Any:
    """Recursively converts schema type strings to lowercase for strict OpenAI JSON schema compliance."""
    if not isinstance(schema, dict):
        return schema
    normalized = {}
    for k, v in schema.items():
        if k == "type" and isinstance(v, str):
            normalized[k] = v.lower()
        elif isinstance(v, dict):
            normalized[k] = normalize_schema_types(v)
        elif isinstance(v, list):
            normalized[k] = [normalize_schema_types(item) if isinstance(item, dict) else item for item in v]
        else:
            normalized[k] = v
    return normalized


class FreeLLMAPIProvider(IModelProvider):
    """OpenAI-compatible inference provider for self-hosted FreeLLMAPI gateways."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model_id: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        connect_timeout: float = 5.0,
        read_timeout: float = 25.0
    ):
        det_url, det_key = detect_freellmapi_credentials()
        self._base_url = (base_url or det_url or "http://127.0.0.1:3001/v1").rstrip("/")
        if not self._base_url.endswith("/v1") and not "/v1" in self._base_url:
            self._base_url = f"{self._base_url}/v1"

        env_timeout = os.environ.get("FREELLMAPI_READ_TIMEOUT")
        if env_timeout:
            try:
                read_timeout = float(env_timeout)
            except ValueError:
                pass

        self._api_key = (api_key if api_key is not None else det_key).strip()
        self._model_id = (model_id or os.environ.get("FREELLMAPI_MODEL", "auto")).strip() or "auto"
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout

        self._client: Optional[httpx.Client] = None
        self._cached_models: List[Dict[str, Any]] = []

    @property
    def provider_name(self) -> str:
        return "freellmapi"

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def base_url(self) -> str:
        return self._base_url

    def validate_configuration(self) -> Tuple[bool, str]:
        """Validates endpoint syntax and API key presence without leaking secrets."""
        from runtime.config import is_local_only_mode
        if is_local_only_mode():
            return False, "Cloud inference disabled: Local-only mode is active (PRESENCE_LOCAL_ONLY=1)."
        parsed = urlparse(self._base_url)
        if not parsed.scheme or not parsed.netloc:
            return False, f"Invalid FreeLLMAPI Base URL: '{self._base_url}'"
        if not self._api_key:
            return False, "FREELLMAPI_API_KEY is not configured and no credential found."
        return True, "FreeLLMAPI configuration valid."

    def _get_client(self) -> httpx.Client:
        if self._client is None or self._client.is_closed:
            self._client = httpx.Client(
                timeout=httpx.Timeout(
                    connect=self.connect_timeout,
                    read=self.read_timeout,
                    write=10.0,
                    pool=10.0
                )
            )
        return self._client

    def _redact(self, msg: str) -> str:
        if self._api_key and self._api_key in msg:
            return msg.replace(self._api_key, "[REDACTED_API_KEY]")
        return msg

    def health_check(self, timeout: float = 3.0) -> Tuple[bool, str, Dict[str, Any]]:
        """Lightweight live health check against the FreeLLMAPI GET /models endpoint.

        Returns:
            Tuple[bool, str, Dict[str, Any]]: (is_healthy, message, metadata)
        """
        valid, msg = self.validate_configuration()
        if not valid:
            return False, msg, {}

        endpoint = f"{self._base_url}/models"
        headers = {"Authorization": f"Bearer {self._api_key}"}

        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.get(endpoint, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    models = data.get("data", [])
                    self._cached_models = models
                    return True, f"FreeLLMAPI is online ({len(models)} models available).", {
                        "model_count": len(models),
                        "models": [m.get("id") for m in models if isinstance(m, dict)]
                    }
                elif resp.status_code in (401, 403):
                    return False, "Authentication failed: Invalid FreeLLMAPI API key.", {}
                elif resp.status_code == 404:
                    return False, f"Endpoint not found ({endpoint}). Verify base URL path.", {}
                else:
                    return False, f"FreeLLMAPI returned HTTP status {resp.status_code}.", {}

        except httpx.ConnectError:
            return False, f"FreeLLMAPI service is not reachable at {self._base_url}. Ensure FreeLLMAPI is running.", {}
        except httpx.TimeoutException:
            return False, f"Connection to FreeLLMAPI at {self._base_url} timed out.", {}
        except Exception as e:
            return False, self._redact(f"Health check failed: {e}"), {}

    def get_available_models(self, refresh: bool = False) -> List[Dict[str, Any]]:
        """Fetches and caches available models from the FreeLLMAPI gateway."""
        if self._cached_models and not refresh:
            return self._cached_models

        endpoint = f"{self._base_url}/models"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            client = self._get_client()
            resp = client.get(endpoint, headers=headers, timeout=5.0)
            if resp.status_code == 200:
                data = resp.json()
                self._cached_models = data.get("data", [])
                return self._cached_models
        except Exception as e:
            logger.warning(self._redact(f"Failed to fetch models from FreeLLMAPI: {e}"))

        return self._cached_models

    def generate_stream(
        self,
        system_prompt: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        cancellation_token: Optional[Any] = None
    ) -> Iterator[ModelStreamChunk]:
        """Streams normalized response chunks from FreeLLMAPI via SSE chat completions."""
        valid, msg = self.validate_configuration()
        if not valid:
            raise RuntimeError(f"Cannot initialize FreeLLMAPI stream: {msg}")

        # 1. Map messages to OpenAI format
        openai_messages: List[Dict[str, Any]] = []

        # System prompt first
        combined_sys = system_prompt.strip()
        extra_sys = []

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if role == "system":
                if content:
                    extra_sys.append(content)

            elif role == "user":
                openai_messages.append({"role": "user", "content": content or ""})

            elif role == "assistant":
                tc_list = msg.get("tool_calls")
                if tc_list:
                    openai_tcs = []
                    for tc in tc_list:
                        args_raw = tc.get("args", {})
                        args_str = json.dumps(args_raw) if isinstance(args_raw, (dict, list)) else str(args_raw)
                        openai_tcs.append({
                            "id": tc.get("call_id") or f"call_{tc.get('name')}",
                            "type": "function",
                            "function": {
                                "name": tc.get("name", ""),
                                "arguments": args_str
                            }
                        })
                    openai_messages.append({
                        "role": "assistant",
                        "content": content or None,
                        "tool_calls": openai_tcs
                    })
                else:
                    openai_messages.append({"role": "assistant", "content": content or ""})

            elif role == "tool":
                call_id = msg.get("tool_call_id") or ""
                tool_name = msg.get("name") or ""
                content_str = json.dumps(content) if isinstance(content, (dict, list)) else str(content)
                tool_entry = {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": content_str
                }
                if tool_name:
                    tool_entry["name"] = tool_name
                openai_messages.append(tool_entry)

        if extra_sys:
            combined_sys += "\n\n" + "\n\n".join(extra_sys)

        if combined_sys:
            openai_messages.insert(0, {"role": "system", "content": combined_sys})

        # 2. Map tools to OpenAI function format
        openai_tools = None
        if tools:
            openai_tools = []
            for t in tools:
                raw_params = t.get("parameters", {})
                norm_params = normalize_schema_types(raw_params)
                openai_tools.append({
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": norm_params
                    }
                })

        # 3. Build completion request payload
        payload: Dict[str, Any] = {
            "model": self._model_id,
            "messages": openai_messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": True
        }
        if openai_tools:
            payload["tools"] = openai_tools

        endpoint = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json"
        }

        # 4. Stream consumption & tool call fragment reassembler
        accumulated_tool_calls: Dict[int, Dict[str, Any]] = {}
        last_usage: Optional[Dict[str, int]] = None
        client = self._get_client()

        try:
            with client.stream("POST", endpoint, headers=headers, json=payload, timeout=self.read_timeout) as resp:
                if resp.status_code != 200:
                    err_body = resp.read().decode("utf-8", errors="ignore")
                    redacted_body = self._redact(err_body)
                    if resp.status_code in (401, 403):
                        raise RuntimeError(f"FreeLLMAPI Authentication failed (HTTP {resp.status_code}). Check your API key.")
                    elif resp.status_code == 429:
                        raise RuntimeError(f"FreeLLMAPI Rate limit / quota exceeded (HTTP 429): {redacted_body}")
                    elif resp.status_code == 503:
                        raise RuntimeError(f"FreeLLMAPI Upstream service unavailable (HTTP 503): {redacted_body}")
                    else:
                        raise RuntimeError(f"FreeLLMAPI generation error (HTTP {resp.status_code}): {redacted_body}")

                for line in resp.iter_lines():
                    if cancellation_token and getattr(cancellation_token, "is_cancelled", False):
                        logger.info("FreeLLMAPI stream cancelled by user cancellation token.")
                        break

                    line = line.strip()
                    if not line:
                        continue

                    if line.startswith("data:"):
                        raw_data = line[5:].strip()
                        if raw_data == "[DONE]":
                            break

                        try:
                            chunk_json = json.loads(raw_data)
                        except json.JSONDecodeError:
                            continue

                        # Extract top-level usage metadata if emitted
                        if "usage" in chunk_json and isinstance(chunk_json["usage"], dict):
                            u = chunk_json["usage"]
                            last_usage = {
                                "prompt_tokens": u.get("prompt_tokens", 0) or 0,
                                "completion_tokens": u.get("completion_tokens", 0) or 0,
                                "cached_tokens": u.get("prompt_tokens_details", {}).get("cached_tokens", 0) or 0
                            }

                        choices = chunk_json.get("choices", [])
                        if not choices:
                            # Usage-only chunk or ping
                            if last_usage:
                                yield ModelStreamChunk(usage=last_usage)
                            continue

                        choice = choices[0]
                        delta = choice.get("delta", {})

                        # 1. Incremental text delta & reasoning
                        reasoning_delta = delta.get("reasoning_content") or delta.get("reasoning")
                        content_delta = delta.get("content")

                        if reasoning_delta:
                            yield ModelStreamChunk(
                                thinking_status=reasoning_delta,
                                usage=last_usage
                            )

                        if content_delta:
                            yield ModelStreamChunk(
                                text_delta=content_delta,
                                usage=last_usage
                            )

                        # 2. Fragmented tool calls reassembly
                        delta_tool_calls = delta.get("tool_calls")
                        if delta_tool_calls:
                            for tc_chunk in delta_tool_calls:
                                idx = tc_chunk.get("index", 0)
                                if idx not in accumulated_tool_calls:
                                    accumulated_tool_calls[idx] = {
                                        "id": tc_chunk.get("id", ""),
                                        "name": tc_chunk.get("function", {}).get("name", ""),
                                        "args_buffer": tc_chunk.get("function", {}).get("arguments", "")
                                    }
                                else:
                                    if tc_chunk.get("id"):
                                        accumulated_tool_calls[idx]["id"] = tc_chunk["id"]
                                    fn = tc_chunk.get("function", {})
                                    if fn.get("name"):
                                        accumulated_tool_calls[idx]["name"] = fn["name"]
                                    if fn.get("arguments"):
                                        accumulated_tool_calls[idx]["args_buffer"] += fn["arguments"]

                        finish_reason = choice.get("finish_reason")
                        if finish_reason == "tool_calls" and accumulated_tool_calls:
                            yield self._flush_tool_calls(accumulated_tool_calls, last_usage)
                            accumulated_tool_calls.clear()

                # Flush any remaining tool calls before exit
                if accumulated_tool_calls:
                    yield self._flush_tool_calls(accumulated_tool_calls, last_usage)
                    accumulated_tool_calls.clear()

        except httpx.ConnectError as ce:
            raise RuntimeError(
                f"FreeLLMAPI connection failed: Cannot connect to '{self._base_url}'. "
                "Ensure the FreeLLMAPI service is running."
            ) from None
        except httpx.TimeoutException:
            raise RuntimeError(f"FreeLLMAPI request timed out after {self.read_timeout}s.") from None
        except Exception as e:
            if isinstance(e, RuntimeError):
                raise
            clean_err = self._redact(str(e))
            raise RuntimeError(f"FreeLLMAPI generation failed: {clean_err}") from None

    def _flush_tool_calls(
        self,
        accumulated: Dict[int, Dict[str, Any]],
        usage: Optional[Dict[str, int]]
    ) -> ModelStreamChunk:
        """Assembles accumulated tool call fragments into normalized ModelStreamChunk."""
        final_tool_calls = []
        for idx in sorted(accumulated.keys()):
            tc_data = accumulated[idx]
            name = tc_data.get("name", "tool")
            call_id = tc_data.get("id") or f"call_{name}_{idx}"
            args_str = tc_data.get("args_buffer", "").strip()

            args_dict = {}
            if args_str:
                try:
                    args_dict = json.loads(args_str)
                except json.JSONDecodeError as jde:
                    logger.warning(f"FreeLLMAPI malformed tool call JSON for '{name}': {args_str} ({jde})")
                    args_dict = {"_raw": args_str}

            final_tool_calls.append({
                "call_id": call_id,
                "name": name,
                "args": args_dict
            })

        return ModelStreamChunk(
            tool_calls=final_tool_calls,
            usage=usage,
            finish_reason="tool_calls"
        )

    def close(self) -> None:
        """Closes cached HTTP client and releases network sockets."""
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
