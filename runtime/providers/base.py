"""Model Provider Base Interface & Stream Contracts
Defines provider-independent abstractions for model requests, normalized stream
chunks, and token usage accounting.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List, Iterator, Tuple


@dataclass
class ModelStreamChunk:
    """Normalized response chunk yielded during model streaming."""
    text_delta: str = ""
    thinking_status: str = ""
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    usage: Optional[Dict[str, int]] = None
    finish_reason: Optional[str] = None

    def has_content(self) -> bool:
        """Returns True if chunk contains text or tool calls."""
        return bool(self.text_delta or self.tool_calls)


class IModelProvider(ABC):
    """Abstract contract for model reasoning providers."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Returns provider identifier (e.g. 'gemini', 'openai', 'fake')."""
        pass

    @property
    @abstractmethod
    def model_id(self) -> str:
        """Returns configured model identifier."""
        pass

    @abstractmethod
    def validate_configuration(self) -> Tuple[bool, str]:
        """Validates provider credentials and SDK readiness."""
        pass

    @abstractmethod
    def generate_stream(
        self,
        system_prompt: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        cancellation_token: Optional[Any] = None
    ) -> Iterator[ModelStreamChunk]:
        """Streams normalized response chunks from the model provider."""
        pass

    def close(self) -> None:
        """Closes provider client and cleans up network connections."""
        pass
