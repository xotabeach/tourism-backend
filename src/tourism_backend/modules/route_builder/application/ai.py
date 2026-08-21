"""Provider-neutral contracts for AI-assisted route planning."""

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class AIProviderProbeResult:
    provider: str
    configured_model: str
    available_models: tuple[str, ...]
    response_text: str


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: str  # system | user | assistant
    content: str


@dataclass(frozen=True, slots=True)
class ChatTurnResult:
    assistant_text: str
    proposed_constraints: dict[str, Any] | None = None
    ask_field: str | None = None
    action_ids: tuple[str, ...] = ()
    tool_requests: tuple[dict[str, Any], ...] = ()
    provider: str = "mock"


@dataclass(frozen=True, slots=True)
class StructuredChatTurn:
    """Parsed allowlisted JSON from the model (or mock)."""

    assistant_text: str
    ask_field: str | None = None
    action_ids: tuple[str, ...] = ()
    constraint_patch: dict[str, Any] = field(default_factory=dict)
    tool_requests: tuple[dict[str, Any], ...] = ()


class AIPlanningProvider(Protocol):
    async def probe(self) -> AIProviderProbeResult:
        """Verify transport, configured model and one bounded inference call."""
        ...

    async def chat_turn(
        self,
        *,
        messages: list[ChatMessage],
        constraints: dict[str, Any],
        confirmed_fields: list[str] | None = None,
        place_hints: list[dict[str, str]] | None = None,
        tool_context: dict[str, Any] | None = None,
        max_tokens: int = 320,
    ) -> ChatTurnResult:
        """One bounded assistant turn for Crimea route planning chat."""
        ...
