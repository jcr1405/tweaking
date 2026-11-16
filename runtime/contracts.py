"""Future Integration Contracts
Formal interface definitions (Python Protocol/ABC) decoupling the
Presence Runtime from future visual UI, AI agents, context sensors,
and external application adapters.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List


class IPresenceSurface(ABC):
    """Contract for future desktop UI manifestations (HUD, Island, Tray)."""

    @abstractmethod
    def initialize(self) -> bool:
        """Initializes visual surface without stealing keyboard focus."""
        pass

    @abstractmethod
    def set_presence_state(self, state: str, details: Optional[Dict[str, Any]] = None) -> None:
        """Updates ambient visual state (e.g. 'IDLE', 'THINKING', 'EXECUTING')."""
        pass

    @abstractmethod
    def show_card(self, title: str, content: str, actions: Optional[List[str]] = None) -> None:
        """Presents an expandable interaction or notification card."""
        pass

    @abstractmethod
    def hide(self) -> None:
        """Collapses back to ambient resting state."""
        pass


class IContextProvider(ABC):
    """Contract for context providers (Active Window, Browser Tab, Git Workspace)."""

    @property
    @abstractmethod
    def provider_id(self) -> str:
        pass

    @abstractmethod
    def get_current_context(self) -> Dict[str, Any]:
        """Returns structured active computing context."""
        pass


class IApplicationAdapter(ABC):
    """Contract for universal application connectors (Chrome, VS Code, Spotify)."""

    @property
    @abstractmethod
    def application_id(self) -> str:
        pass

    @abstractmethod
    def get_declared_tools(self) -> List[Dict[str, Any]]:
        """Returns list of exposed capabilities / MCP schemas."""
        pass

    @abstractmethod
    def execute_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Executes a declared tool within bounded permissions."""
        pass


class IAgentBackend(ABC):
    """Contract for isolated agent runtimes (NVIDIA OpenShell, LangChain)."""

    @property
    @abstractmethod
    def backend_id(self) -> str:
        pass

    @abstractmethod
    def execute_task(self, task_id: str, goal: str, workspace_path: str) -> Any:
        """Dispatches an autonomous multi-step task inside an isolated sandbox."""
        pass

    @abstractmethod
    def cancel_task(self, task_id: str) -> bool:
        """Aborts an active agent task."""
        pass


class IMemoryProvider(ABC):
    """Contract for persistent memory and recall systems."""

    @abstractmethod
    def store_fact(self, key: str, value: Any, category: str = "core") -> None:
        pass

    @abstractmethod
    def query_facts(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        pass


class IModelProvider(ABC):
    """Contract for pluggable LLM inference providers."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        pass

    @abstractmethod
    def generate(
        self,
        system_prompt: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, Any]:
        """Dispatches prompt context to inference backend."""
        pass


class ITrajectoryStore(ABC):
    """Contract for conversation trajectory persistence."""

    @property
    @abstractmethod
    def conversation_id(self) -> str:
        pass

    @abstractmethod
    def append_step(self, step: Any) -> int:
        pass

    @abstractmethod
    def list_steps(self, start_idx: int = 0, limit: Optional[int] = None) -> List[Any]:
        pass

