"""Custom SQLAdmin filters for ops views."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Select, String, false, select
from starlette.requests import Request

from tourism_backend.modules.identity.infrastructure.models import (
    AuthOtpChallenge,
    User,
)


class OtpLinkedUserIdFilter:
    """Filter OTP challenges by the user who owns the same phone_e164.

    Auth OTP rows have no user_id until verify; ops still need to find codes
    for an existing account via users.id → users.phone_e164.
    """

    has_operator = True
    template = "sqladmin/filters/operation_filter.html"
    title = "ID пользователя"
    parameter_name = "user_id"
    # Present for SQLAdmin filter scaffolding; filtering is custom.
    column = AuthOtpChallenge.phone_e164

    def get_operation_options(self, column_obj: Any) -> list[tuple[str, str]]:
        return [
            ("equals", "Equals"),
            ("contains", "Contains"),
            ("starts_with", "Starts with"),
        ]

    def get_operation_options_for_model(self, model: Any) -> list[tuple[str, str]]:
        return self.get_operation_options(None)

    async def lookups(
        self,
        request: Request,
        model: Any,
        run_query: Any,
    ) -> list[tuple[str, str]]:
        return []

    async def get_filtered_query(
        self,
        query: Select[Any],
        operation: str,
        value: Any,
        model: Any,
    ) -> Select[Any]:
        raw = str(value or "").strip()
        if not raw or not operation:
            return query

        phones = select(User.phone_e164)
        if operation == "equals":
            try:
                uid = UUID(raw)
            except (TypeError, ValueError):
                return query.where(false())
            phones = phones.where(User.id == uid)
        elif operation == "contains":
            phones = phones.where(User.id.cast(String).ilike(f"%{raw}%"))
        elif operation == "starts_with":
            phones = phones.where(User.id.cast(String).ilike(f"{raw}%"))
        else:
            return query

        return query.where(AuthOtpChallenge.phone_e164.in_(phones))
