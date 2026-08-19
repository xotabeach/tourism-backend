"""Provider-neutral contracts for AI-assisted route planning."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class AIProviderProbeResult:
    provider: str
    configured_model: str
    available_models: tuple[str, ...]
    response_text: str


class AIPlanningProvider(Protocol):
    async def probe(self) -> AIProviderProbeResult:
        """Verify transport, configured model and one bounded inference call."""
        ...
