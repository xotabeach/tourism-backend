"""Optional FCM HTTP v1 sender. Disabled unless service account is configured."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx
import jwt

from tourism_backend.config import Settings

logger = logging.getLogger(__name__)

# Must match MainActivity / AndroidManifest default_notification_channel_id.
ANDROID_PUSH_CHANNEL_ID = "crimeatrip_push"

_FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
_TOKEN_URI = "https://oauth2.googleapis.com/token"


def _load_service_account(settings: Settings) -> dict[str, Any] | None:
    raw = (settings.fcm_service_account_json or "").strip()
    if not raw and settings.fcm_service_account_file:
        try:
            with open(settings.fcm_service_account_file, encoding="utf-8") as fh:
                raw = fh.read()
        except OSError:
            logger.warning("fcm_service_account_file_unreadable")
            return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("fcm_service_account_json_invalid")
        return None
    if not isinstance(data, dict) or "project_id" not in data:
        return None
    return data


async def _access_token(account: dict[str, Any], client: httpx.AsyncClient) -> str:
    """OAuth access token via JWT bearer assertion (httpx only — no `requests`)."""
    email = str(account["client_email"])
    now = int(time.time())
    assertion = jwt.encode(
        {
            "iss": email,
            "sub": email,
            "aud": _TOKEN_URI,
            "iat": now,
            "exp": now + 3600,
            "scope": _FCM_SCOPE,
        },
        account["private_key"],
        algorithm="RS256",
    )
    response = await client.post(
        _TOKEN_URI,
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        },
    )
    if response.status_code >= 300:
        raise RuntimeError(f"oauth_token_http_{response.status_code}")
    payload = response.json()
    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("oauth_token_missing")
    return token


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
        # In-app inbox still works; system tray needs FCM_SERVICE_ACCOUNT_*.
        logger.warning("fcm_skipped_no_service_account")
        return 0
    if "client_email" not in account or "private_key" not in account:
        logger.warning("fcm_service_account_incomplete")
        return 0

    project_id = str(account["project_id"])
    url = f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
    sent = 0
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            access_token = await _access_token(account, client)
        except Exception as exc:
            logger.warning("fcm_oauth_failed err=%s", type(exc).__name__)
            return 0
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        for token in tokens:
            payload = {
                "message": {
                    "token": token,
                    "notification": {"title": title, "body": body},
                    "data": data,
                    "android": {
                        "priority": "HIGH",
                        "notification": {
                            "channel_id": ANDROID_PUSH_CHANNEL_ID,
                            "notification_priority": "PRIORITY_HIGH",
                            "default_sound": True,
                            "default_vibrate_timings": True,
                        },
                    },
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
                    # Do not log the device token; body is enough to diagnose.
                    logger.warning(
                        "fcm_send_failed status=%s body=%s",
                        response.status_code,
                        response.text[:400],
                    )
            except httpx.HTTPError as exc:
                logger.warning("fcm_send_error err=%s", type(exc).__name__)
    if sent:
        logger.info("fcm_send_ok count=%s", sent)
    return sent
