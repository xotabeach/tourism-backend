"""SMS provider port and SMS Aero v2 transport.

Credentials are sent with HTTP Basic auth headers, never embedded in a URL.
The API response is application-level JSON, so HTTP 2xx alone is not success.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

_SEND_PATH = "/sms/send"
_MAX_RESPONSE_BYTES = 1_000_000
_CIRCUIT_FAILURE_THRESHOLD = 3
_CIRCUIT_COOLDOWN_SECONDS = 20.0


@dataclass(frozen=True)
class SmsSendResult:
    provider_sms_id: str


class SmsProviderError(RuntimeError):
    def __init__(self, message: str, *, ambiguous: bool = False) -> None:
        super().__init__(message)
        self.ambiguous = ambiguous


class SmsProvider(Protocol):
    async def send(self, *, phone_e164: str, text: str, sender: str) -> SmsSendResult: ...


class _CircuitBreaker:
    def __init__(self) -> None:
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= _CIRCUIT_COOLDOWN_SECONDS:
            self._opened_at = None
            self._consecutive_failures = 0
            return False
        return True

    def success(self) -> None:
        self._consecutive_failures = 0
        self._opened_at = None

    def failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= _CIRCUIT_FAILURE_THRESHOLD:
            self._opened_at = time.monotonic()


_circuit = _CircuitBreaker()


def reset_sms_provider_state_for_tests() -> None:
    global _circuit
    _circuit = _CircuitBreaker()


def normalize_smsaero_phone(phone_e164: str) -> int:
    """Convert stored E.164 into the digit-only integer required by SMS Aero."""
    value = phone_e164.strip()
    if value.startswith("+"):
        value = value[1:]
    if not value.isdigit() or not 7 <= len(value) <= 15:
        raise SmsProviderError("invalid SMS recipient number")
    return int(value)


def _parse_success(response: httpx.Response) -> SmsSendResult:
    if len(response.content) > _MAX_RESPONSE_BYTES:
        raise SmsProviderError("SMS Aero response is too large")
    try:
        payload = response.json()
    except ValueError as exc:
        raise SmsProviderError("SMS Aero returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise SmsProviderError("SMS Aero returned an invalid response")
    if (
        response.status_code < 200
        or response.status_code >= 300
        or payload.get("success") is not True
    ):
        message = payload.get("message") or payload.get("result") or "request rejected"
        raise SmsProviderError(f"SMS Aero rejected the request: {str(message)[:300]}")
    data = payload.get("data")
    if not isinstance(data, dict) or data.get("id") is None:
        raise SmsProviderError("SMS Aero response is missing message id")
    return SmsSendResult(provider_sms_id=str(data["id"]))


class SmsAeroProvider:
    def __init__(
        self,
        *,
        email: str,
        api_key: str,
        base_url: str = "https://gate.smsaero.ru/v2",
        timeout_seconds: float = 10,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._auth = httpx.BasicAuth(email, api_key)
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._client = client

    async def send(self, *, phone_e164: str, text: str, sender: str) -> SmsSendResult:
        if _circuit.is_open():
            raise SmsProviderError("SMS Aero circuit is open")
        if not 2 <= len(text) <= 640:
            raise SmsProviderError("SMS text length must be between 2 and 640 characters")
        if not 2 <= len(sender) <= 64:
            raise SmsProviderError("SMS sender length must be between 2 and 64 characters")
        payload: dict[str, Any] = {
            "number": normalize_smsaero_phone(phone_e164),
            "text": text,
            "sign": sender,
        }
        try:
            response = await self._post(payload)
            result = _parse_success(response)
        except httpx.TimeoutException as exc:
            _circuit.failure()
            # The gateway may have accepted the billed SMS before our timeout.
            raise SmsProviderError("SMS Aero request timed out", ambiguous=True) from exc
        except (httpx.HTTPError, SmsProviderError) as exc:
            _circuit.failure()
            if isinstance(exc, SmsProviderError):
                raise
            raise SmsProviderError("SMS Aero transport failed") from exc
        _circuit.success()
        return result

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        if self._client is not None:
            return await self._client.post(
                f"{self._base_url}{_SEND_PATH}",
                json=payload,
                auth=self._auth,
                timeout=self._timeout_seconds,
            )
        async with httpx.AsyncClient() as client:
            return await client.post(
                f"{self._base_url}{_SEND_PATH}",
                json=payload,
                auth=self._auth,
                timeout=self._timeout_seconds,
            )


class StubSmsProvider:
    async def send(self, *, phone_e164: str, text: str, sender: str) -> SmsSendResult:
        del phone_e164, text, sender
        return SmsSendResult(provider_sms_id="stub")
