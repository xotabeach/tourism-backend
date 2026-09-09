"""Provider-neutral contracts for AI-assisted route planning."""

from dataclasses import dataclass, field
from typing import Any, Protocol

from tourism_backend.modules.route_builder.application.dialogue import DialogueGoal

# Room for the compact answer, validated patch, tools and contextual replies.
CHAT_MAX_OUTPUT_TOKENS = 1024


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
    quick_replies: tuple[dict[str, str], ...] = ()
    tool_requests: tuple[dict[str, Any], ...] = ()
    provider: str = "mock"
    structured_parse: str = "ok"
    goal: DialogueGoal | None = None
    clarification_reason: str | None = None


class AIProviderBusyError(Exception):
    """Raised when the in-process LM Studio slot is already serving a turn."""


@dataclass(frozen=True, slots=True)
class StructuredChatTurn:
    """Parsed allowlisted JSON from the model (or mock)."""

    assistant_text: str
    ask_field: str | None = None
    action_ids: tuple[str, ...] = ()
    quick_replies: tuple[dict[str, str], ...] = ()
    constraint_patch: dict[str, Any] = field(default_factory=dict)
    tool_requests: tuple[dict[str, Any], ...] = ()
    goal: DialogueGoal | None = None
    clarification_reason: str | None = None


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
        max_tokens: int = CHAT_MAX_OUTPUT_TOKENS,
    ) -> ChatTurnResult:
        """One bounded assistant turn for Crimea route planning chat."""
        ...
