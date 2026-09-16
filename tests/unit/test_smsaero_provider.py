"""Contract tests for the SMS Aero v2 OTP transport."""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from tourism_backend.modules.identity.infrastructure.sms_provider import (
    SmsAeroProvider,
    SmsProviderError,
    normalize_smsaero_phone,
    reset_sms_provider_state_for_tests,
)


@pytest.fixture(autouse=True)
def _reset_circuit() -> None:
    reset_sms_provider_state_for_tests()


async def test_send_uses_basic_auth_json_and_digit_only_phone() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://gate.smsaero.ru/v2/sms/send"
        expected = base64.b64encode(b"owner@example.com:test-api-key").decode()
        assert request.headers["authorization"] == f"Basic {expected}"
        assert json.loads(await request.aread()) == {
            "number": 79001234567,
            "text": "Код подтверждения: 1234",
            "sign": "КРЫМТРИП",
        }
        return httpx.Response(200, json={"success": True, "data": {"id": 98765}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await SmsAeroProvider(
            email="owner@example.com",
            api_key="test-api-key",
            client=client,
        ).send(
            phone_e164="+79001234567",
            text="Код подтверждения: 1234",
            sender="КРЫМТРИП",
        )

    assert result.provider_sms_id == "98765"


async def test_http_200_with_success_false_is_failure() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": False, "message": "bad sign"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = SmsAeroProvider(email="a@b.ru", api_key="secret", client=client)
        with pytest.raises(SmsProviderError, match="bad sign"):
            await provider.send(phone_e164="+79001234567", text="Код: 1234", sender="КрымТрип")


async def test_missing_message_id_is_failure() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = SmsAeroProvider(email="a@b.ru", api_key="secret", client=client)
        with pytest.raises(SmsProviderError, match="message id"):
            await provider.send(phone_e164="+79001234567", text="Код: 1234", sender="КрымТрип")


async def test_timeout_is_marked_ambiguous_and_not_safe_to_retry() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("late response", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = SmsAeroProvider(email="a@b.ru", api_key="secret", client=client)
        with pytest.raises(SmsProviderError) as error:
            await provider.send(phone_e164="+79001234567", text="Код: 1234", sender="КрымТрип")

    assert error.value.ambiguous is True
    assert "secret" not in str(error.value)


async def test_circuit_opens_after_three_failures() -> None:
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"success": False, "message": "unavailable"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = SmsAeroProvider(email="a@b.ru", api_key="secret", client=client)
        for _ in range(3):
            with pytest.raises(SmsProviderError):
                await provider.send(phone_e164="+79001234567", text="Код: 1234", sender="КрымТрип")
        with pytest.raises(SmsProviderError, match="circuit is open"):
            await provider.send(phone_e164="+79001234567", text="Код: 1234", sender="КрымТрип")
    assert calls == 3


@pytest.mark.parametrize("value", ["", "+abc", "+123456", "+" + "1" * 16])
def test_invalid_phone_is_rejected(value: str) -> None:
    with pytest.raises(SmsProviderError, match="invalid SMS recipient"):
        normalize_smsaero_phone(value)
