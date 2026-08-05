"""Optional FCM HTTP v1 sender. Disabled unless service account is configured."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from tourism_backend.config import Settings

logger = logging.getLogger(__name__)


def _load_service_account(settings: Settings) -> dict[str, Any] | None:
    raw = (settings.fcm_service_account_json or "").strip()
    if not raw and settings.fcm_service_account_file:
        try:
            with open(settings.fcm_service_account_file, encoding="utf-8") as fh:
                raw = fh.read()
        except OSError:
            logger.warning("FCM service account file unreadable")
            return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("FCM service account JSON invalid")
        return None
    if not isinstance(data, dict) or "project_id" not in data:
        return None
    return data


async def send_data_message(
    settings: Settings,
    *,
    tokens: list[str],
    title: str,
    body: str,
    data: dict[str, str],
) -> int:
    """Best-effort FCM send. Returns number of accepted tokens (0 if disabled)."""
    if not tokens:
        return 0
    account = _load_service_account(settings)
    if account is None:
        return 0

    # Lazy import: google-auth is optional until FCM is enabled in deploy.
    try:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account
    except ImportError:
        logger.warning("google-auth not installed; skip FCM send")
        return 0

    # google-auth stubs omit typed from_service_account_info.
    factory: Any = service_account.Credentials.from_service_account_info
    creds = factory(
        account,
        scopes=["https://www.googleapis.com/auth/firebase.messaging"],
    )
    creds.refresh(Request())
    project_id = str(account["project_id"])
    url = f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
    headers = {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json; charset=UTF-8",
    }

    sent = 0
    async with httpx.AsyncClient(timeout=10.0) as client:
        for token in tokens:
            payload = {
                "message": {
                    "token": token,
                    "notification": {"title": title, "body": body},
                    "data": data,
                    "android": {"priority": "HIGH"},
                    "apns": {
                        "payload": {
                            "aps": {"sound": "default", "content-available": 1},
                        }
                    },
                }
            }
            try:
                response = await client.post(url, headers=headers, json=payload)
                if response.status_code < 300:
                    sent += 1
                else:
                    logger.info(
                        "fcm_send_failed",
                        extra={"status": response.status_code},
                    )
            except httpx.HTTPError:
                logger.info("fcm_send_error")
    return sent
