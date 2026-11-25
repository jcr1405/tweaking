"""Fake Deterministic Model Provider for Automated Testing
Simulates streaming, function-calling loops, errors, and cancellation without network
requests or API key dependencies.
"""

from typing import Optional, Dict, Any, List, Iterator, Tuple

from runtime.providers.base import IModelProvider, ModelStreamChunk


class FakeModelProvider(IModelProvider):
    """Deterministic model provider for automated unit and integration tests."""

    def __init__(
        self,
        scripted_responses: Optional[List[List[ModelStreamChunk]]] = None,
        simulate_error: Optional[Exception] = None
    ):
        self._scripted_responses = scripted_responses or []
        self._simulate_error = simulate_error
        self._call_count = 0
        self.recorded_requests: List[Dict[str, Any]] = []

    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def model_id(self) -> str:
        return "fake-deterministic-v1"

    def validate_configuration(self) -> Tuple[bool, str]:
        return True, "Fake provider always valid."

    def queue_response(self, chunks: List[ModelStreamChunk]) -> None:
        """Appends a scripted multi-chunk response to the queue."""
        self._scripted_responses.append(chunks)

    def generate_stream(
        self,
        system_prompt: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        cancellation_token: Optional[Any] = None,
        **kwargs: Any
    ) -> Iterator[ModelStreamChunk]:
        """Yields predetermined chunks or simulated errors."""
        self._call_count += 1
        self.recorded_requests.append({
            "system_prompt": system_prompt,
            "messages": messages,
            "tools": tools
        })

        if self._simulate_error:
            raise self._simulate_error

        # If scripted responses are provided, pop the next response
        if self._scripted_responses:
            chunks = self._scripted_responses.pop(0)
            for chunk in chunks:
                if cancellation_token and getattr(cancellation_token, "is_cancelled", False):
                    break
                yield chunk
            return

        # Default fallback behavior: inspect last message
        last_msg = messages[-1] if messages else {}
        role = last_msg.get("role", "")
        content = last_msg.get("content", "")

        # If last message is a tool result, synthesize a final answer
        if role == "tool":
            yield ModelStreamChunk(text_delta="Based on the tool output: ")
            yield ModelStreamChunk(text_delta=f"{content}. ")
            yield ModelStreamChunk(
                text_delta="Task completed successfully.",
                usage={"prompt_tokens": 120, "completion_tokens": 30, "cached_tokens": 0},
                finish_reason="STOP"
            )
            return

        # Default text response
        yield ModelStreamChunk(text_delta="Hello! ")
        yield ModelStreamChunk(text_delta="I am your ")
        yield ModelStreamChunk(text_delta="Persistent Intelligent Presence.")
        yield ModelStreamChunk(
            text_delta="",
            usage={"prompt_tokens": 50, "completion_tokens": 15, "cached_tokens": 0},
            finish_reason="STOP"
        )
