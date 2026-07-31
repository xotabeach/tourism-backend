"""Regressions for OTP auth shortcuts that must never reach a deployed contour.

`AUTH_OTP_ACCEPT_ANY` skips code verification and disables OTP rate limiting;
`AUTH_OTP_STORE_DEBUG_CODE` keeps the code readable in the database. Both are
local/test conveniences while no SMS provider exists, so the guard rails are:
they never default on outside those environments, and startup refuses them.
"""

from __future__ import annotations

import pytest

from tourism_backend.config import AppEnvironment, Settings, validate_settings
from tourism_backend.modules.identity.application.crypto import (
    digest_matches,
    digest_token,
    new_otp_code,
)

_PROD_SETTINGS = {
    "database_url": "postgresql+asyncpg://app:strong@db:5432/tourism",
    "database_url_sync": "postgresql+psycopg://app:strong@db:5432/tourism",
    "redis_url": "redis://:strong@redis:6379/0",
    "jwt_signing_key": "x" * 48,
}


@pytest.mark.parametrize("env", [AppEnvironment.STAGING, AppEnvironment.PRODUCTION])
def test_accept_any_otp_is_refused_on_deployed_environments(env: AppEnvironment) -> None:
    settings = Settings(app_env=env, auth_otp_accept_any=True, **_PROD_SETTINGS)
    with pytest.raises(RuntimeError, match="AUTH_OTP_ACCEPT_ANY"):
        validate_settings(settings)


@pytest.mark.parametrize("env", [AppEnvironment.STAGING, AppEnvironment.PRODUCTION])
def test_cleartext_otp_storage_is_refused_on_deployed_environments(env: AppEnvironment) -> None:
    settings = Settings(app_env=env, auth_otp_store_debug_code=True, **_PROD_SETTINGS)
    with pytest.raises(RuntimeError, match="AUTH_OTP_STORE_DEBUG_CODE"):
        validate_settings(settings)


@pytest.mark.parametrize("env", [AppEnvironment.STAGING, AppEnvironment.PRODUCTION])
def test_deployed_environments_default_to_no_shortcuts(env: AppEnvironment) -> None:
    settings = Settings(app_env=env, **_PROD_SETTINGS)
    assert settings.otp_accept_any_enabled is False
    assert settings.otp_store_debug_code_enabled is False
    validate_settings(settings)


def test_remote_test_contour_requires_a_real_code_by_default() -> None:
    """APP_ENV=test is internet-facing, so only the readable-code aid stays on."""
    settings = Settings(app_env=AppEnvironment.TEST)
    assert settings.otp_accept_any_enabled is False
    assert settings.otp_store_debug_code_enabled is True


def test_local_keeps_both_developer_shortcuts() -> None:
    settings = Settings(app_env=AppEnvironment.LOCAL)
    assert settings.otp_accept_any_enabled is True
    assert settings.otp_store_debug_code_enabled is True


def test_generated_code_is_four_digits_and_verifies_only_against_itself() -> None:
    code = new_otp_code()
    assert len(code) == 4
    assert code.isdigit()
    digest = digest_token(code)
    assert digest_matches(code, digest)
    assert not digest_matches("0000" if code != "0000" else "1111", digest)
