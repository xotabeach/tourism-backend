"""Provider-neutral contracts for AI-assisted route planning."""

from dataclasses import dataclass
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
    provider: str = "mock"


class AIPlanningProvider(Protocol):
    async def probe(self) -> AIProviderProbeResult:
        """Verify transport, configured model and one bounded inference call."""
        ...

    async def chat_turn(
        self,
        *,
        messages: list[ChatMessage],
        constraints: dict[str, Any],
        max_tokens: int = 256,
    ) -> ChatTurnResult:
        """One bounded assistant turn for Crimea route planning chat."""
        ...
