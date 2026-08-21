"""Phase 8B route planning chat sessions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.api.errors import AppError
from tourism_backend.config import Settings, get_settings
from tourism_backend.modules.identity.infrastructure.models import User
from tourism_backend.modules.route_builder.application import generate_service
from tourism_backend.modules.route_builder.application.ai import ChatMessage
from tourism_backend.modules.route_builder.application.schemas import (
    RouteGenerateIn,
    RouteMatchParamsIn,
    RoutePlanningMessageIn,
    RoutePlanningMessageOut,
    RoutePlanningSessionCreateIn,
    RoutePlanningSessionOut,
)
from tourism_backend.modules.route_builder.application.topic_guard import (
    ai_unavailable_fallback,
    canned_reply_for_intent,
    classify_chat_intent,
)
from tourism_backend.modules.route_builder.infrastructure.ai_factory import (
    get_ai_planning_provider,
)
from tourism_backend.modules.route_builder.infrastructure.ai_mock import (
    MockAIPlanningProvider,
)
from tourism_backend.modules.route_builder.infrastructure.models import (
    RoutePlanningMessage,
    RoutePlanningSession,
)
from tourism_backend.modules.subscriptions.application.entitlements import require_ai_chat
from tourism_backend.modules.subscriptions.application.service import (
    refresh_user_travel_plus,
)

_HISTORY_LIMIT = 12


async def create_session(
    session: AsyncSession,
    *,
    user_id: UUID,
    payload: RoutePlanningSessionCreateIn,
    settings: Settings | None = None,
) -> RoutePlanningSessionOut:
    cfg = settings or get_settings()
    user = await session.get(User, user_id)
    if user is None:
        raise AppError(code="user_not_found", message="User not found", status_code=404)
    await refresh_user_travel_plus(session, user=user)
    require_ai_chat(user)

    now = datetime.now(UTC)
    row = RoutePlanningSession(
        id=uuid4(),
        user_id=user_id,
        status="active",
        constraints=payload.params.model_dump(mode="json"),
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return _session_out(row, ai_planning_enabled=cfg.ai_planning_enabled)


async def get_session(
    session: AsyncSession,
    *,
    user_id: UUID,
    session_id: UUID,
    settings: Settings | None = None,
) -> RoutePlanningSessionOut:
    cfg = settings or get_settings()
    user = await session.get(User, user_id)
    if user is None:
        raise AppError(code="user_not_found", message="User not found", status_code=404)
    await refresh_user_travel_plus(session, user=user)
    require_ai_chat(user)

    row = await _owned_session(session, user_id=user_id, session_id=session_id)
    return _session_out(row, ai_planning_enabled=cfg.ai_planning_enabled)


async def post_message(
    session: AsyncSession,
    *,
    user_id: UUID,
    session_id: UUID,
    payload: RoutePlanningMessageIn,
    settings: Settings | None = None,
) -> RoutePlanningMessageOut:
    cfg = settings or get_settings()
    user = await session.get(User, user_id)
    if user is None:
        raise AppError(code="user_not_found", message="User not found", status_code=404)
    await refresh_user_travel_plus(session, user=user)
    require_ai_chat(user)

    planning = await _owned_session(session, user_id=user_id, session_id=session_id)
    if planning.status != "active":
        raise AppError(
            code="session_closed",
            message="Planning session is closed",
            status_code=409,
        )

    intent = classify_chat_intent(payload.text)
    if payload.want_generate:
        intent = "generate"

    now = datetime.now(UTC)
    user_msg = RoutePlanningMessage(
        id=uuid4(),
        session_id=planning.id,
        user_id=user_id,
        role="user",
        text=payload.text,
        intent=intent,
        proposal_id=None,
        payload=None,
        created_at=now,
        updated_at=now,
    )
    session.add(user_msg)
    await session.flush()

    constraints = RouteMatchParamsIn.model_validate(planning.constraints)
    proposal_out = None
    provider_name: str | None = None
    fallback = False
    assistant_text = ""

    if intent in {"crisis", "greeting", "off_topic", "injection_attempt"}:
        assistant_text = canned_reply_for_intent(intent)
    elif intent == "generate":
        generated = await generate_service.generate_route(
            session,
            user_id=user_id,
            payload=RouteGenerateIn(channel="chat", params=constraints),
        )
        await session.refresh(planning)
        proposal_out = generated.proposal
        assistant_text = generated.proposal.assistant_text
        provider_name = "deterministic_generate"
    else:
        assistant_text, provider_name, fallback = await _assistant_from_ai(
            session,
            planning=planning,
            constraints=planning.constraints,
            settings=cfg,
        )

    assistant_msg = RoutePlanningMessage(
        id=uuid4(),
        session_id=planning.id,
        user_id=user_id,
        role="assistant",
        text=assistant_text,
        intent=intent,
        proposal_id=UUID(proposal_out.proposal_id) if proposal_out else None,
        payload={
            "provider": provider_name,
            "fallback": fallback,
            "blocks": (
                [block.model_dump(mode="json") for block in proposal_out.blocks]
                if proposal_out is not None
                else []
            ),
        },
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add(assistant_msg)
    planning.updated_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(assistant_msg)

    return RoutePlanningMessageOut(
        message_id=str(assistant_msg.id),
        session_id=str(planning.id),
        role="assistant",
        text=assistant_text,
        intent=intent,
        proposed_constraints=None,
        proposal=proposal_out,
        blocks=list(proposal_out.blocks) if proposal_out is not None else [],
        provider=provider_name,
        fallback=fallback,
    )


async def _assistant_from_ai(
    session: AsyncSession,
    *,
    planning: RoutePlanningSession,
    constraints: dict[str, Any],
    settings: Settings,
) -> tuple[str, str | None, bool]:
    history_rows = (
        await session.scalars(
            select(RoutePlanningMessage)
            .where(RoutePlanningMessage.session_id == planning.id)
            .order_by(RoutePlanningMessage.created_at.asc())
        )
    ).all()
    chat_messages: list[ChatMessage] = []
    for row in history_rows:
        if row.role not in {"user", "assistant"}:
            continue
        chat_messages.append(ChatMessage(role=row.role, content=row.text))
    chat_messages = chat_messages[-_HISTORY_LIMIT:]

    if not settings.ai_planning_enabled:
        result = await MockAIPlanningProvider().chat_turn(
            messages=chat_messages,
            constraints=constraints,
        )
        return result.assistant_text, result.provider, True

    try:
        provider = get_ai_planning_provider(settings)
        result = await provider.chat_turn(
            messages=chat_messages,
            constraints=constraints,
        )
        return result.assistant_text, result.provider, False
    except Exception:  # noqa: BLE001 — soft fallback for home-lab outages
        return ai_unavailable_fallback(), None, True


async def _owned_session(
    session: AsyncSession,
    *,
    user_id: UUID,
    session_id: UUID,
) -> RoutePlanningSession:
    row = await session.get(RoutePlanningSession, session_id)
    if row is None or row.user_id != user_id:
        raise AppError(
            code="session_not_found",
            message="Planning session not found",
            status_code=404,
        )
    return row


def _session_out(
    row: RoutePlanningSession,
    *,
    ai_planning_enabled: bool,
) -> RoutePlanningSessionOut:
    return RoutePlanningSessionOut(
        session_id=str(row.id),
        status=row.status,  # type: ignore[arg-type]
        constraints=RouteMatchParamsIn.model_validate(row.constraints),
        ai_planning_enabled=ai_planning_enabled,
    )
