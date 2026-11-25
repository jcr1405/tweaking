"""Gemini Model Provider Adapter
Integrates Google's official Gen AI SDK (google-genai) behind the IModelProvider contract.
Enforces manual function calling, local context reconstruction, and credential isolation.
"""

import os
import logging
from typing import Optional, Dict, Any, List, Iterator, Tuple

from runtime.providers.base import IModelProvider, ModelStreamChunk

logger = logging.getLogger("presence.providers.gemini")

# Lazy import check for google-genai SDK
_GENAI_AVAILABLE = False
try:
    from google import genai
    from google.genai import types
    _GENAI_AVAILABLE = True
except ImportError:
    genai = None
    types = None
    _GENAI_AVAILABLE = False


class GeminiModelProvider(IModelProvider):
    """Google Gemini model provider using the official google-genai SDK."""

    def __init__(
        self,
        model_id: Optional[str] = None,
        api_key: Optional[str] = None,
        temperature: float = 0.7,
        max_output_tokens: int = 4096
    ):
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass

        self._model_id = model_id or os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
        self._api_key = api_key if api_key is not None else os.environ.get("GEMINI_API_KEY", "")
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self._client = None

    @property
    def provider_name(self) -> str:
        return "gemini"

    @property
    def model_id(self) -> str:
        return self._model_id

    def validate_configuration(self) -> Tuple[bool, str]:
        """Validates SDK installation and API key presence without leaking secrets."""
        from runtime.config import is_local_only_mode
        if is_local_only_mode():
            return False, "Cloud inference disabled: Local-only mode is active (PRESENCE_LOCAL_ONLY=1)."
        if not _GENAI_AVAILABLE:
            return False, "Official SDK 'google-genai' is not installed in the current environment."
        if not self._api_key or not self._api_key.strip():
            return False, "GEMINI_API_KEY environment variable is not configured."
        return True, "Gemini provider configuration valid."

    def _get_client(self):
        """Initializes and caches the Google GenAI Client."""
        if self._client is None:
            valid, msg = self.validate_configuration()
            if not valid:
                raise RuntimeError(f"Cannot initialize Gemini client: {msg}")
            self._client = genai.Client(api_key=self._api_key)
        return self._client

    def generate_stream(
        self,
        system_prompt: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        cancellation_token: Optional[Any] = None
    ) -> Iterator[ModelStreamChunk]:
        """Streams normalized response chunks from the Gemini API."""
        client = self._get_client()

        # Build contents from messages
        contents = []
        extra_system_parts = []

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if role == "system":
                # Collect system messages to prepend to system instruction
                if content:
                    extra_system_parts.append(content)
            elif role == "user":
                contents.append(
                    types.Content(
                        role="user",
                        parts=[types.Part(text=content)]
                    )
                )
            elif role == "assistant":
                parts = []
                if content:
                    parts.append(types.Part(text=content))
                if msg.get("tool_calls"):
                    for tc in msg["tool_calls"]:
                        ts_val = tc.get("thought_signature")
                        ts_bytes = bytes.fromhex(ts_val) if isinstance(ts_val, str) else ts_val
                        parts.append(types.Part(
                            function_call=types.FunctionCall(
                                id=tc.get("call_id", ""),
                                name=tc.get("name", ""),
                                args=tc.get("args", {})
                            ),
                            thought_signature=ts_bytes
                        ))
                if parts:
                    contents.append(types.Content(role="model", parts=parts))
            elif role == "tool":
                # Function response payload
                tool_call_id = msg.get("tool_call_id", "")
                tool_name = msg.get("name", "")
                resp_payload = {"output": content}
                if isinstance(content, str):
                    try:
                        parsed = json.loads(content)
                        if isinstance(parsed, dict):
                            resp_payload = parsed
                    except Exception:
                        pass
                elif isinstance(content, dict):
                    resp_payload = content

                contents.append(types.Content(
                    role="user",
                    parts=[types.Part(
                        function_response=types.FunctionResponse(
                            id=tool_call_id,
                            name=tool_name,
                            response=resp_payload
                        )
                    )]
                ))

        # Compose system instruction
        combined_sys_instruction = system_prompt
        if extra_system_parts:
            combined_sys_instruction += "\n\n" + "\n\n".join(extra_system_parts)

        # Configure tools & manual function execution
        sdk_tools = None
        if tools:
            func_decls = []
            for t in tools:
                func_decls.append(types.FunctionDeclaration(
                    name=t["name"],
                    description=t.get("description", ""),
                    parameters_json_schema=t.get("parameters", {})
                ))
            sdk_tools = [types.Tool(function_declarations=func_decls)]

        config = types.GenerateContentConfig(
            system_instruction=combined_sys_instruction,
            temperature=self.temperature,
            max_output_tokens=self.max_output_tokens,
            # Explicitly disable SDK automatic execution so our engine mediates every call
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            tools=sdk_tools
        )

        models_to_try = [self._model_id]
        for fallback_cand in ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.5-flash"]:
            if fallback_cand not in models_to_try:
                models_to_try.append(fallback_cand)

        for attempt_idx, attempt_model in enumerate(models_to_try):
            try:
                stream = client.models.generate_content_stream(
                    model=attempt_model,
                    contents=contents,
                    config=config
                )

                if attempt_model != self._model_id:
                    logger.warning(
                        f"Primary Gemini model '{self._model_id}' unavailable. "
                        f"Automatically fell back to '{attempt_model}'."
                    )
                    yield ModelStreamChunk(
                        text_delta=f"*[Notice: Temporarily routed to {attempt_model} due to quota / demand limits on {self._model_id}]*\n\n"
                    )

                for response in stream:
                    if cancellation_token and getattr(cancellation_token, "is_cancelled", False):
                        logger.info("Gemini stream cancelled by cancellation token.")
                        break

                    text_delta = response.text or ""
                    tool_calls_extracted = []

                    if response.function_calls:
                        for fc in response.function_calls:
                            ts = None
                            if hasattr(response, "candidates") and response.candidates:
                                for cand in response.candidates:
                                    if hasattr(cand, "content") and cand.content and cand.content.parts:
                                        for part in cand.content.parts:
                                            if part.function_call and (part.function_call.id == fc.id or part.function_call.name == fc.name):
                                                ts = getattr(part, "thought_signature", None)
                                                break
                            tool_calls_extracted.append({
                                "call_id": fc.id or f"call_{fc.name}",
                                "name": fc.name,
                                "args": fc.args or {},
                                "thought_signature": ts.hex() if isinstance(ts, bytes) else ts
                            })

                    # Token usage metadata
                    usage_dict = None
                    if hasattr(response, "usage_metadata") and response.usage_metadata:
                        um = response.usage_metadata
                        usage_dict = {
                            "prompt_tokens": getattr(um, "prompt_token_count", 0) or 0,
                            "completion_tokens": getattr(um, "candidates_token_count", 0) or 0,
                            "cached_tokens": getattr(um, "cached_content_token_count", 0) or 0
                        }

                    yield ModelStreamChunk(
                        text_delta=text_delta,
                        tool_calls=tool_calls_extracted,
                        usage=usage_dict
                    )
                # Successful stream completed
                return

            except Exception as e:
                clean_error = str(e)
                if self._api_key:
                    clean_error = clean_error.replace(self._api_key, "[REDACTED_API_KEY]")

                is_transient = any(
                    code in clean_error
                    for code in [
                        "503", "UNAVAILABLE", "high demand",
                        "404", "no longer available",
                        "429", "RESOURCE_EXHAUSTED", "Quota exceeded", "rate-limits", "quota"
                    ]
                )
                if is_transient and attempt_idx < len(models_to_try) - 1:
                    reason = "quota exhaustion" if ("429" in clean_error or "quota" in clean_error.lower()) else "high demand"
                    logger.warning(
                        f"Model '{attempt_model}' failed due to {reason} ({clean_error[:80]}). "
                        f"Attempting fallback to '{models_to_try[attempt_idx + 1]}'..."
                    )
                    continue

                logger.error(f"Gemini generation error: {clean_error}")
                raise RuntimeError(f"Gemini API generation failed: {clean_error}") from None

    def close(self) -> None:
        """Cleans up client resources."""
        self._client = None
