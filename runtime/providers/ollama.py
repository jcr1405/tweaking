"""Ollama Local Model Provider Adapter
Integrates locally hosted Ollama instances via the native `/api/chat` interface behind the IModelProvider contract.
Supports Qwen 3.5 9B with two distinct operational modes:
- Mode A (Routine Operations, default): Thinking disabled (`think: False`), low latency, direct tool calls.
- Mode B (Complex Local Reasoning): Thinking enabled (`think: True`), bounded inference budget.
Strictly isolates reasoning tokens to prevent leakage into user chat, persistent steps, or task ledgers.
"""

import os
import sys
import json
import socket
import logging
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List, Iterator, Tuple

import httpx

from runtime.providers.base import IModelProvider, ModelStreamChunk
from runtime.providers.inference_semaphore import (
    get_shared_inference_semaphore,
    InferenceSemaphoreError,
    LocalInferenceSemaphore,
)

logger = logging.getLogger("presence.providers.ollama")


@dataclass
class OllamaConfig:
    """Configuration schema for Ollama inference provider."""
    enabled: bool = True
    base_url: str = "http://localhost:11434"
    model_id: str = "qwen3.5:9b"
    mode: str = "routine"  # "routine" (Mode A) or "reasoning" (Mode B)
    think: Optional[bool] = None  # Explicit override: False = no think, True = think
    temperature: float = 0.2
    top_p: float = 0.95
    max_tokens: int = 4096
    connect_timeout: float = 5.0
    read_timeout: float = 120.0


def normalize_schema_types(schema: Any) -> Any:
    """Recursively normalizes JSON Schema casing (e.g. UPPERCASE types to standard lowercase)."""
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


def _is_port_open(host: str, port: int, timeout: float = 0.3) -> bool:
    """Fast check whether a local TCP port is accepting connections."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


class OllamaModelProvider(IModelProvider):
    """Local inference provider for Ollama instances via native /api/chat interface."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        model_id: Optional[str] = None,
        mode: Optional[str] = None,
        think: Optional[bool] = None,
        temperature: Optional[float] = None,
        max_tokens: int = 4096,
        connect_timeout: float = 5.0,
        read_timeout: float = 120.0,
        semaphore: Optional[LocalInferenceSemaphore] = None,
    ):
        raw_url = base_url or os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        self._base_url = raw_url.rstrip("/")
        # If user passed /api or /v1 in base_url, strip it for consistent root base
        if self._base_url.endswith("/api"):
            self._base_url = self._base_url[:-4]
        elif self._base_url.endswith("/v1"):
            self._base_url = self._base_url[:-3]

        self._model_id = (model_id or os.environ.get("OLLAMA_MODEL", "qwen3.5:9b")).strip()
        
        # Shared local inference semaphore
        self._semaphore = semaphore or get_shared_inference_semaphore(
            provider="ollama",
            model_id=self._model_id,
            max_concurrent=1,
        )
        
        # Determine operational mode: "routine" (Mode A) vs "reasoning" (Mode B)
        env_mode = os.environ.get("PRESENCE_LOCAL_MODE", "routine").lower()
        self._mode = (mode or env_mode).strip().lower()
        if self._mode not in ("routine", "reasoning"):
            self._mode = "routine"

        # Explicit think flag override if provided
        if think is not None:
            self._think = bool(think)
        elif os.environ.get("OLLAMA_THINK") is not None:
            self._think = os.environ.get("OLLAMA_THINK", "false").lower() in ("true", "1", "yes")
        else:
            self._think = (self._mode == "reasoning")

        # Set default temperature based on mode if not explicitly overridden
        if temperature is not None:
            self.temperature = float(temperature)
        else:
            self.temperature = 0.7 if self._think else 0.2

        self.top_p = 0.95
        self.max_tokens = max_tokens
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self._client: Optional[httpx.Client] = None

    @property
    def provider_name(self) -> str:
        return "ollama"

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def think_enabled(self) -> bool:
        return self._think

    @property
    def semaphore(self) -> LocalInferenceSemaphore:
        return self._semaphore

    def set_mode(self, mode: str, think: Optional[bool] = None) -> None:
        """Dynamically switches between Mode A (routine) and Mode B (reasoning)."""
        clean_mode = mode.strip().lower()
        if clean_mode not in ("routine", "reasoning"):
            raise ValueError(f"Invalid mode '{mode}'. Expected 'routine' or 'reasoning'.")
        self._mode = clean_mode
        if think is not None:
            self._think = bool(think)
        else:
            self._think = (self._mode == "reasoning")
        self.temperature = 0.7 if self._think else 0.2

    def _get_client(self) -> httpx.Client:
        if self._client is None or self._client.is_closed:
            self._client = httpx.Client(
                timeout=httpx.Timeout(self.read_timeout, connect=self.connect_timeout)
            )
        return self._client

    def validate_configuration(self) -> Tuple[bool, str]:
        """Validates that Ollama daemon is reachable and the configured model is installed."""
        try:
            client = self._get_client()
            resp = client.get(f"{self._base_url}/api/tags", timeout=3.0)
            if resp.status_code != 200:
                return False, f"Ollama HTTP {resp.status_code} on {self._base_url}/api/tags"
            
            data = resp.json()
            models = [m.get("name", "") for m in data.get("models", [])]
            # Match exact or base tag (e.g. qwen3.5:9b or qwen3.5:9b-latest)
            matched = any(
                m == self._model_id or m.startswith(f"{self._model_id}:") or self._model_id.startswith(f"{m}:")
                for m in models
            )
            if not matched:
                avail = ", ".join(models) if models else "none"
                return False, f"Model '{self._model_id}' is not installed in Ollama. Available models: {avail}"
            
            return True, f"Ollama daemon reachable at {self._base_url} with model '{self._model_id}' (Mode: {self._mode.upper()}, Think: {self._think})"
        except Exception as e:
            return False, f"Cannot connect to Ollama at {self._base_url}: {e}"

    def generate_stream(
        self,
        system_prompt: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        cancellation_token: Optional[Any] = None,
        think: Optional[bool] = None,
        **kwargs: Any
    ) -> Iterator[ModelStreamChunk]:
        """Streams normalized response chunks from the local Ollama instance via /api/chat.

        Yields:
            ModelStreamChunk with strictly separated text_delta, thinking_status, and tool_calls.
        """
        token = cancellation_token

        # 1. Prepare messages list
        ollama_messages: List[Dict[str, Any]] = []
        if system_prompt:
            # Check if first message is already system
            has_system = messages and messages[0].get("role") == "system"
            if not has_system:
                ollama_messages.append({"role": "system", "content": system_prompt})

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if role == "system":
                ollama_messages.append({"role": "system", "content": content or ""})
            elif role == "user":
                user_msg: Dict[str, Any] = {"role": "user", "content": content or ""}
                raw_images = msg.get("images")
                if raw_images:
                    import base64
                    b64_list = []
                    for img in raw_images:
                        if isinstance(img, bytes):
                            b64_list.append(base64.b64encode(img).decode("utf-8"))
                        elif isinstance(img, str):
                            b64_list.append(img)
                    user_msg["images"] = b64_list
                ollama_messages.append(user_msg)
            elif role == "assistant":
                tc_list = msg.get("tool_calls")
                if tc_list:
                    formatted_tcs = []
                    for tc in tc_list:
                        raw_args = tc.get("args", {})
                        formatted_tcs.append({
                            "id": tc.get("call_id") or f"call_{tc.get('name')}",
                            "type": "function",
                            "function": {
                                "name": tc.get("name", ""),
                                "arguments": raw_args if isinstance(raw_args, dict) else {}
                            }
                        })
                    ollama_messages.append({
                        "role": "assistant",
                        "content": content or "",
                        "tool_calls": formatted_tcs
                    })
                else:
                    ollama_messages.append({"role": "assistant", "content": content or ""})
            elif role == "tool":
                content_str = json.dumps(content) if isinstance(content, (dict, list)) else str(content)
                ollama_messages.append({
                    "role": "tool",
                    "content": content_str
                })

        # 2. Prepare tools definitions
        formatted_tools = None
        if tools:
            formatted_tools = []
            for t in tools:
                norm_params = normalize_schema_types(t.get("parameters", {}))
                formatted_tools.append({
                    "type": "function",
                    "function": {
                        "name": t.get("name", ""),
                        "description": t.get("description", ""),
                        "parameters": norm_params
                    }
                })

        # 3. Build /api/chat request payload
        effective_think = self._think if think is None else bool(think)
        num_ctx = int(os.environ.get("OLLAMA_NUM_CTX", "16384"))
        payload: Dict[str, Any] = {
            "model": self._model_id,
            "messages": ollama_messages,
            "stream": True,
            "think": effective_think,
            "options": {
                "temperature": self.temperature,
                "top_p": self.top_p,
                "num_ctx": num_ctx,
                "num_predict": self.max_tokens
            }
        }
        if formatted_tools:
            payload["tools"] = formatted_tools

        endpoint = f"{self._base_url}/api/chat"
        client = self._get_client()

        # 4. Stream consumption & normalized chunk yielding
        in_residual_think = False
        try:
            with self._semaphore.acquire_slot(
                caller_id=f"ollama_{self._model_id}",
                timeout=self.read_timeout,
                cancellation_token=token,
            ), client.stream("POST", endpoint, json=payload, timeout=self.read_timeout) as resp:
                if resp.status_code != 200:
                    err_body = resp.read().decode("utf-8", errors="ignore")
                    raise RuntimeError(f"Ollama error (HTTP {resp.status_code}): {err_body}")

                for line in resp.iter_lines():
                    if token and token.is_cancelled:
                        return

                    if not line:
                        continue

                    try:
                        chunk_data = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    msg = chunk_data.get("message", {})

                    # A. Thinking tokens isolation
                    thinking_delta = msg.get("thinking")
                    if thinking_delta:
                        yield ModelStreamChunk(thinking_status=thinking_delta)

                    # B. Content tokens (with defense-in-depth against simulated <think> tags)
                    content_delta = msg.get("content", "")
                    if content_delta:
                        # Check for simulated <think> or </think> tags in content
                        if "<think>" in content_delta and "</think>" in content_delta:
                            # Full block inside delta
                            p1, rest = content_delta.split("<think>", 1)
                            think_inner, p2 = rest.split("</think>", 1)
                            if p1:
                                yield ModelStreamChunk(text_delta=p1)
                            if think_inner:
                                yield ModelStreamChunk(thinking_status=think_inner)
                            if p2:
                                yield ModelStreamChunk(text_delta=p2)
                        elif "<think>" in content_delta:
                            in_residual_think = True
                            p1, think_part = content_delta.split("<think>", 1)
                            if p1:
                                yield ModelStreamChunk(text_delta=p1)
                            if think_part:
                                yield ModelStreamChunk(thinking_status=think_part)
                        elif "</think>" in content_delta:
                            in_residual_think = False
                            think_part, p2 = content_delta.split("</think>", 1)
                            if think_part:
                                yield ModelStreamChunk(thinking_status=think_part)
                            if p2:
                                yield ModelStreamChunk(text_delta=p2)
                        elif in_residual_think:
                            yield ModelStreamChunk(thinking_status=content_delta)
                        else:
                            yield ModelStreamChunk(text_delta=content_delta)

                    # C. Tool calls handling
                    tool_calls_raw = msg.get("tool_calls")
                    if tool_calls_raw:
                        normalized_tcs = []
                        for tc in tool_calls_raw:
                            func_info = tc.get("function", {})
                            name = func_info.get("name") or tc.get("name", "")
                            args_obj = func_info.get("arguments", {})
                            if isinstance(args_obj, str):
                                try:
                                    args_obj = json.loads(args_obj)
                                except Exception:
                                    args_obj = {"_raw": args_obj}
                            elif not isinstance(args_obj, dict):
                                args_obj = {}
                            call_id = tc.get("id") or f"call_{name}"
                            normalized_tcs.append({
                                "call_id": call_id,
                                "name": name,
                                "args": args_obj
                            })
                        yield ModelStreamChunk(
                            tool_calls=normalized_tcs,
                            finish_reason="tool_calls"
                        )

                    # D. Final done chunk usage accounting
                    if chunk_data.get("done"):
                        prompt_eval = chunk_data.get("prompt_eval_count", 0)
                        eval_cnt = chunk_data.get("eval_count", 0)
                        usage = {
                            "prompt_tokens": prompt_eval,
                            "completion_tokens": eval_cnt,
                            "total_tokens": prompt_eval + eval_cnt
                        }
                        yield ModelStreamChunk(
                            usage=usage,
                            finish_reason=chunk_data.get("done_reason", "stop")
                        )

        except httpx.ConnectError:
            raise RuntimeError(
                f"Ollama connection failed: Cannot connect to '{self._base_url}'. "
                "Ensure the Ollama daemon is running ('ollama serve')."
            ) from None
        except httpx.TimeoutException:
            raise RuntimeError(f"Ollama request timed out after {self.read_timeout}s.") from None
        except Exception as e:
            if isinstance(e, (RuntimeError, InferenceSemaphoreError)):
                raise
            raise RuntimeError(f"Ollama generation failed: {e}") from None

    def generate_multimodal(
        self,
        prompt: str,
        images: List[Any],
        system_prompt: str = "",
        model: Optional[str] = None,
        model_name: Optional[str] = None,
        task_id: Optional[str] = None,
        think: Optional[bool] = False,
        temperature: Optional[float] = 0.2,
        max_tokens: Optional[int] = 2048,
        cancellation_token: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Synchronously performs multimodal vision inference through Ollama /api/chat.
        Protected by the shared LocalInferenceSemaphore with zero cloud fallback.

        Returns:
            Dict containing:
            - 'content': Generated response text (clean, excluding internal thinking).
            - 'thinking': Reasoning trace if thinking was active.
            - 'usage': Token usage dictionary.
            - 'model_id': Model identifier used.
        """
        import base64

        if cancellation_token and getattr(cancellation_token, "is_cancelled", False):
            raise RuntimeError("Multimodal inference cancelled before execution.")

        b64_images: List[str] = []
        for img in images:
            if isinstance(img, bytes):
                b64_images.append(base64.b64encode(img).decode("utf-8"))
            elif isinstance(img, str):
                b64_images.append(img)

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({
            "role": "user",
            "content": prompt,
            "images": b64_images,
        })

        effective_think = think if think is not None else self._think
        num_ctx = int(os.environ.get("OLLAMA_NUM_CTX", "16384"))
        options: Dict[str, Any] = {
            "temperature": temperature if temperature is not None else self._temperature,
            "num_ctx": num_ctx,
            "num_predict": max_tokens if max_tokens is not None else self._max_tokens,
        }
        if effective_think is not None:
            options["think"] = effective_think

        target_model = model or model_name or self._model_id
        req_payload = {
            "model": target_model,
            "messages": messages,
            "stream": False,
            "options": options,
        }

        # Acquire shared local inference semaphore
        sem_task = task_id or f"vision_{os.getpid()}"
        acquired_wait = self._semaphore.acquire(caller_id=sem_task, timeout=self.read_timeout, cancellation_token=cancellation_token)

        try:
            client = self._get_client()
            resp = client.post(
                f"{self._base_url}/api/chat",
                json=req_payload,
                timeout=self.read_timeout,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"Ollama multimodal inference HTTP {resp.status_code}: {resp.text}")

            data = resp.json()
            msg = data.get("message", {})
            content = msg.get("content", "")
            thinking = msg.get("thinking", "")
            prompt_eval = data.get("prompt_eval_count", 0)
            eval_cnt = data.get("eval_count", 0)

            return {
                "content": content,
                "thinking": thinking,
                "usage": {
                    "prompt_tokens": prompt_eval,
                    "completion_tokens": eval_cnt,
                    "total_tokens": prompt_eval + eval_cnt,
                },
                "model_id": self._model_id,
            }
        except httpx.ConnectError:
            raise RuntimeError(
                f"Ollama connection failed: Cannot connect to '{self._base_url}'. "
                "Ensure Ollama daemon is running ('ollama serve')."
            ) from None
        except httpx.TimeoutException:
            raise RuntimeError(f"Ollama multimodal vision request timed out after {self.read_timeout}s.") from None
        finally:
            self._semaphore.release(caller_id=sem_task)

    def close(self) -> None:
        """Closes cached HTTP client and releases network sockets."""
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
