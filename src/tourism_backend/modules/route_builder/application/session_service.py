"""Phase 8B route planning chat sessions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.api.errors import AppError
from tourism_backend.config import Settings, get_settings
from tourism_backend.modules.identity.infrastructure.models import User
from tourism_backend.modules.route_builder.application import generate_service
from tourism_backend.modules.route_builder.application.ai import ChatMessage, ChatTurnResult
from tourism_backend.modules.route_builder.application.chat_actions import (
    clarification_action_blocks,
    field_for_action,
    fields_touched_by_patch,
    first_missing_ask_field,
    form_draft_constraints,
    interactive_control_blocks,
    merge_constraint_patch,
    normalize_action_id,
    patch_for_action,
    prefer_ready_ask_field,
    sanitize_confirmed_fields,
)
from tourism_backend.modules.route_builder.application.place_picker import (
    pick_places_for_params,
)
from tourism_backend.modules.route_builder.application.schemas import (
    ActionsBlockOut,
    ChatBlockOut,
    PlaceChipBlockOut,
    RecommendationCardBlockOut,
    RouteGenerateIn,
    RouteMatchParamsIn,
    RoutePlanningMessageIn,
    RoutePlanningMessageListOut,
    RoutePlanningMessageOut,
    RoutePlanningSessionCreateIn,
    RoutePlanningSessionListOut,
    RoutePlanningSessionOut,
    RoutePlanningStoredMessageOut,
    RouteProposalCardBlockOut,
    SliderBlockOut,
    ToggleBlockOut,
)
from tourism_backend.modules.route_builder.application.tool_registry import (
    execute_tool,
    parse_tool_calls,
    prefetch_context,
    recommendation_accept_patch,
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
_SESSION_LIST_MAX = 50
_MESSAGE_LIST_MAX = 100


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
        confirmed_fields=sanitize_confirmed_fields(payload.confirmed_fields),
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return _session_out(row, ai_planning_enabled=cfg.ai_planning_enabled)


async def list_sessions(
    session: AsyncSession,
    *,
    user_id: UUID,
    limit: int = 20,
    offset: int = 0,
    settings: Settings | None = None,
) -> RoutePlanningSessionListOut:
    cfg = settings or get_settings()
    user = await session.get(User, user_id)
    if user is None:
        raise AppError(code="user_not_found", message="User not found", status_code=404)
    await refresh_user_travel_plus(session, user=user)
    require_ai_chat(user)

    bounded_limit = max(1, min(limit, _SESSION_LIST_MAX))
    bounded_offset = max(0, offset)
    total = int(
        await session.scalar(
            select(func.count())
            .select_from(RoutePlanningSession)
            .where(RoutePlanningSession.user_id == user_id)
        )
        or 0
    )
    rows = (
        await session.scalars(
            select(RoutePlanningSession)
            .where(RoutePlanningSession.user_id == user_id)
            .order_by(RoutePlanningSession.updated_at.desc())
            .offset(bounded_offset)
            .limit(bounded_limit)
        )
    ).all()
    return RoutePlanningSessionListOut(
        items=[_session_out(row, ai_planning_enabled=cfg.ai_planning_enabled) for row in rows],
        total=total,
        limit=bounded_limit,
        offset=bounded_offset,
    )


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


async def close_session(
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
    if row.status != "closed":
        row.status = "closed"
        row.updated_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(row)
    return _session_out(row, ai_planning_enabled=cfg.ai_planning_enabled)


async def list_messages(
    session: AsyncSession,
    *,
    user_id: UUID,
    session_id: UUID,
    limit: int = 50,
    offset: int = 0,
    settings: Settings | None = None,
) -> RoutePlanningMessageListOut:
    cfg = settings or get_settings()
    _ = cfg
    user = await session.get(User, user_id)
    if user is None:
        raise AppError(code="user_not_found", message="User not found", status_code=404)
    await refresh_user_travel_plus(session, user=user)
    require_ai_chat(user)

    await _owned_session(session, user_id=user_id, session_id=session_id)
    bounded_limit = max(1, min(limit, _MESSAGE_LIST_MAX))
    bounded_offset = max(0, offset)
    total = int(
        await session.scalar(
            select(func.count())
            .select_from(RoutePlanningMessage)
            .where(RoutePlanningMessage.session_id == session_id)
        )
        or 0
    )
    rows = (
        await session.scalars(
            select(RoutePlanningMessage)
            .where(RoutePlanningMessage.session_id == session_id)
            .order_by(RoutePlanningMessage.created_at.asc())
            .offset(bounded_offset)
            .limit(bounded_limit)
        )
    ).all()
    return RoutePlanningMessageListOut(
        items=[_stored_message_out(row) for row in rows],
        total=total,
        limit=bounded_limit,
        offset=bounded_offset,
    )


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

    confirmed = sanitize_confirmed_fields(
        list(planning.confirmed_fields) if isinstance(planning.confirmed_fields, list) else []
    )
    constraints_dict = dict(planning.constraints)

    # Chip / control / recommendation accept → merge allowlisted patch.
    if payload.action_id:
        rec_patch = recommendation_accept_patch(payload.action_id)
        if rec_patch is not None:
            constraints_dict = merge_constraint_patch(
                constraints_dict,
                rec_patch,
                previously_confirmed=confirmed,
            )
            touched = fields_touched_by_patch(rec_patch)
            confirmed = sanitize_confirmed_fields([*confirmed, *touched])
        else:
            control_patch = _control_patch(payload.action_id, payload.control_value)
            if control_patch:
                constraints_dict = merge_constraint_patch(
                    constraints_dict,
                    control_patch,
                    previously_confirmed=confirmed,
                )
                touched = fields_touched_by_patch(control_patch)
                confirmed = sanitize_confirmed_fields([*confirmed, *touched])
            else:
                canonical = normalize_action_id(payload.action_id)
                if canonical:
                    action_patch = patch_for_action(canonical)
                    if action_patch:
                        constraints_dict = merge_constraint_patch(
                            constraints_dict,
                            action_patch,
                            previously_confirmed=confirmed,
                        )
                        touched = fields_touched_by_patch(action_patch)
                        field = field_for_action(canonical)
                        if field:
                            touched = sanitize_confirmed_fields([*touched, field])
                        confirmed = sanitize_confirmed_fields([*confirmed, *touched])

    intent = classify_chat_intent(payload.text)
    if payload.want_generate or (
        payload.action_id and normalize_action_id(payload.action_id) == "want_generate"
    ):
        intent = "generate"

    now = datetime.now(UTC)
    user_payload: dict[str, Any] | None = None
    if payload.action_id or payload.control_value is not None:
        user_payload = {
            "action_id": payload.action_id,
            "control_value": payload.control_value,
        }
    user_msg = RoutePlanningMessage(
        id=uuid4(),
        session_id=planning.id,
        user_id=user_id,
        role="user",
        text=payload.text,
        intent=intent,
        proposal_id=None,
        payload=user_payload,
        created_at=now,
        updated_at=now,
    )
    session.add(user_msg)
    await session.flush()

    proposal_out = None
    provider_name: str | None = None
    fallback = False
    assistant_text = ""
    blocks: list[ChatBlockOut] = []
    ask_field: str | None = None
    proposed: dict[str, Any] | None = None
    prefetch: dict[str, Any] = {}

    if intent in {"crisis", "off_topic", "injection_attempt"}:
        assistant_text = canned_reply_for_intent(intent)
        ask_field = first_missing_ask_field(confirmed)
        if intent == "off_topic":
            blocks = list(
                clarification_action_blocks(
                    constraints_dict,
                    confirmed_fields=confirmed,
                    ask_field=ask_field,
                )
            )
    elif intent == "generate":
        params = RouteMatchParamsIn.model_validate(constraints_dict)
        generated = await generate_service.generate_route(
            session,
            user_id=user_id,
            payload=RouteGenerateIn(channel="chat", params=params),
        )
        await session.refresh(planning)
        proposal_out = generated.proposal
        assistant_text = generated.proposal.assistant_text
        provider_name = "deterministic_generate"
        blocks = list(proposal_out.blocks)
    else:
        turn, provider_name, fallback, prefetch = await _assistant_from_ai(
            session,
            planning=planning,
            constraints=constraints_dict,
            confirmed_fields=confirmed,
            settings=cfg,
        )
        assistant_text = turn.assistant_text
        ask_field = turn.ask_field or prefer_ready_ask_field(confirmed)
        if turn.proposed_constraints:
            constraints_dict = merge_constraint_patch(
                constraints_dict,
                turn.proposed_constraints,
                previously_confirmed=confirmed,
            )
            touched = fields_touched_by_patch(turn.proposed_constraints)
            confirmed = sanitize_confirmed_fields([*confirmed, *touched])
            proposed = dict(turn.proposed_constraints)
            ask_field = prefer_ready_ask_field(confirmed)
        blocks = _compose_assistant_blocks(
            constraints=constraints_dict,
            confirmed_fields=confirmed,
            ask_field=ask_field,
            action_ids=list(turn.action_ids) if turn.action_ids else None,
            prefetch=prefetch,
        )

    # Persist merged constraints / confirmed after the turn.
    try:
        planning.constraints = RouteMatchParamsIn.model_validate(constraints_dict).model_dump(
            mode="json"
        )
        planning.confirmed_fields = confirmed
    except Exception:  # noqa: BLE001,S110 — keep previous constraints if patch invalid
        pass

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
            "ask_field": ask_field,
            "confirmed_fields": confirmed,
            "blocks": [block.model_dump(mode="json") for block in blocks],
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
        proposed_constraints=proposed,
        confirmed_fields=confirmed,
        ask_field=ask_field,
        proposal=proposal_out,
        blocks=blocks,
        provider=provider_name,
        fallback=fallback,
    )


async def _assistant_from_ai(
    session: AsyncSession,
    *,
    planning: RoutePlanningSession,
    constraints: dict[str, Any],
    confirmed_fields: list[str],
    settings: Settings,
) -> tuple[ChatTurnResult, str | None, bool, dict[str, Any]]:
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

    tool_context = await prefetch_context(
        session,
        constraints=constraints,
        confirmed_fields=confirmed_fields,
    )
    draft = form_draft_constraints(constraints, confirmed_fields)
    if draft:
        tool_context = {**tool_context, "form_draft_not_facts": draft}
    place_hints = list(tool_context.get("place_candidates") or [])
    if not place_hints and "city" in confirmed_fields:
        place_hints = await _place_hints(session, constraints)

    async def _once(provider: Any, ctx: dict[str, Any]) -> ChatTurnResult:
        return cast(
            ChatTurnResult,
            await provider.chat_turn(
                messages=chat_messages,
                constraints=constraints,
                confirmed_fields=confirmed_fields,
                place_hints=place_hints,
                tool_context=ctx,
            ),
        )

    if not settings.ai_planning_enabled:
        provider: Any = MockAIPlanningProvider()
        result = await _once(provider, tool_context)
        result, tool_context = await _run_tool_rounds(
            session,
            provider=provider,
            result=result,
            constraints=constraints,
            confirmed_fields=confirmed_fields,
            chat_messages=chat_messages,
            place_hints=place_hints,
            tool_context=tool_context,
        )
        return result, result.provider, True, tool_context

    try:
        provider = get_ai_planning_provider(settings)
        result = await _once(provider, tool_context)
        result, tool_context = await _run_tool_rounds(
            session,
            provider=provider,
            result=result,
            constraints=constraints,
            confirmed_fields=confirmed_fields,
            chat_messages=chat_messages,
            place_hints=place_hints,
            tool_context=tool_context,
        )
        return result, result.provider, False, tool_context
    except Exception:  # noqa: BLE001 — soft fallback for home-lab outages
        return (
            ChatTurnResult(
                assistant_text=ai_unavailable_fallback(),
                ask_field=prefer_ready_ask_field(confirmed_fields),
                action_ids=("want_generate",),
                provider="fallback",
            ),
            None,
            True,
            tool_context,
        )


async def _run_tool_rounds(
    session: AsyncSession,
    *,
    provider: Any,
    result: ChatTurnResult,
    constraints: dict[str, Any],
    confirmed_fields: list[str],
    chat_messages: list[ChatMessage],
    place_hints: list[dict[str, str]],
    tool_context: dict[str, Any],
) -> tuple[ChatTurnResult, dict[str, Any]]:
    calls = parse_tool_calls(list(result.tool_requests))
    if not calls:
        return result, tool_context
    tool_payloads: list[dict[str, Any]] = []
    for call in calls:
        executed = await execute_tool(session, call, constraints=constraints)
        tool_payloads.append({"name": executed.name, "ok": executed.ok, "data": executed.data})
        if executed.ok and call.name == "seasonal_recommendations":
            tool_context = {
                **tool_context,
                "seasonal_recommendations": executed.data.get("items") or [],
                "season": executed.data.get("season") or tool_context.get("season"),
            }
        if executed.ok and call.name == "search_places":
            places = executed.data.get("places") or []
            tool_context = {**tool_context, "place_candidates": places}
            place_hints = list(places)
    follow_messages = [
        *chat_messages,
        ChatMessage(
            role="system",
            content="tool_results DATA: " + str(tool_payloads)[:1200],
        ),
    ]
    follow = await provider.chat_turn(
        messages=follow_messages,
        constraints=constraints,
        confirmed_fields=confirmed_fields,
        place_hints=place_hints,
        tool_context=tool_context,
    )
    # Do not recurse infinitely: ignore further tool_requests on follow-up.
    return (
        ChatTurnResult(
            assistant_text=follow.assistant_text,
            proposed_constraints=follow.proposed_constraints,
            ask_field=follow.ask_field,
            action_ids=follow.action_ids,
            tool_requests=(),
            provider=follow.provider,
        ),
        tool_context,
    )


def _compose_assistant_blocks(
    *,
    constraints: dict[str, Any],
    confirmed_fields: list[str],
    ask_field: str | None,
    action_ids: list[str] | None,
    prefetch: dict[str, Any],
) -> list[ChatBlockOut]:
    blocks: list[ChatBlockOut] = []
    for tip in (prefetch.get("seasonal_recommendations") or [])[:2]:
        if not isinstance(tip, dict):
            continue
        title = str(tip.get("title") or "").strip()
        body = str(tip.get("body") or "").strip()
        accept = str(tip.get("accept_action") or "").strip()
        tip_id = str(tip.get("id") or accept).strip()
        if not title or not body or not accept:
            continue
        blocks.append(
            RecommendationCardBlockOut(
                id=tip_id[:64],
                title=title[:120],
                body=body[:500],
                accept_action_id=accept[:64],
            )
        )
    for place in (prefetch.get("place_candidates") or [])[:4]:
        if not isinstance(place, dict):
            continue
        place_id = str(place.get("place_id") or "").strip()
        title = str(place.get("title") or "").strip()
        if not place_id or not title:
            continue
        subtitle = place.get("subtitle")
        blocks.append(
            PlaceChipBlockOut(
                place_id=place_id,
                title=title[:80],
                subtitle=str(subtitle)[:120] if subtitle else None,
            )
        )
    blocks.extend(interactive_control_blocks(ask_field=ask_field, constraints=constraints))
    blocks.extend(
        clarification_action_blocks(
            constraints,
            confirmed_fields=confirmed_fields,
            ask_field=ask_field,
            action_ids=action_ids,
        )
    )
    return blocks


def _control_patch(
    action_id: str,
    control_value: float | bool | None,
) -> dict[str, Any] | None:
    if action_id == "budget_amount" and isinstance(control_value, (int, float)):
        amount = int(control_value)
        amount = max(0, min(amount, 1_000_000))
        return {"budget_amount": amount}
    if action_id == "with_children" and isinstance(control_value, bool):
        return {"with_children": control_value}
    if action_id == "with_pets" and isinstance(control_value, bool):
        return {"with_pets": control_value}
    if action_id == "with_children":
        return {"with_children": True}
    if action_id == "with_pets":
        return {"with_pets": True}
    return None


async def _place_hints(
    session: AsyncSession,
    constraints: dict[str, Any],
) -> list[dict[str, str]]:
    try:
        params = RouteMatchParamsIn.model_validate(constraints)
        picked = await pick_places_for_params(
            session,
            params=params,
            max_points=8,
        )
    except Exception:  # noqa: BLE001 — hints are optional context only
        return []
    return [{"place_id": str(item.place_id), "title": item.name[:80]} for item in picked[:8]]


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
    confirmed = sanitize_confirmed_fields(
        list(row.confirmed_fields) if isinstance(row.confirmed_fields, list) else []
    )
    return RoutePlanningSessionOut(
        session_id=str(row.id),
        status=row.status,  # type: ignore[arg-type]
        constraints=RouteMatchParamsIn.model_validate(row.constraints),
        confirmed_fields=confirmed,
        ai_planning_enabled=ai_planning_enabled,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _stored_message_out(row: RoutePlanningMessage) -> RoutePlanningStoredMessageOut:
    payload_blocks = row.payload.get("blocks") if isinstance(row.payload, dict) else None
    blocks = _parse_blocks(payload_blocks if isinstance(payload_blocks, list) else [])
    return RoutePlanningStoredMessageOut(
        message_id=str(row.id),
        session_id=str(row.session_id),
        role=row.role,  # type: ignore[arg-type]
        text=row.text,
        intent=row.intent,  # type: ignore[arg-type]
        proposal_id=str(row.proposal_id) if row.proposal_id else None,
        blocks=blocks,
        created_at=row.created_at,
    )


def _parse_blocks(raw: object) -> list[ChatBlockOut]:
    if not isinstance(raw, list):
        return []
    out: list[ChatBlockOut] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        parsed = _try_parse_block(item)
        if parsed is not None:
            out.append(parsed)
    return out


def _try_parse_block(item: dict[str, Any]) -> ChatBlockOut | None:
    block_type = item.get("type")
    try:
        if block_type == "place_chip":
            return PlaceChipBlockOut.model_validate(item)
        if block_type == "route_proposal_card":
            return RouteProposalCardBlockOut.model_validate(item)
        if block_type == "actions":
            return ActionsBlockOut.model_validate(item)
        if block_type == "slider":
            return SliderBlockOut.model_validate(item)
        if block_type == "toggle":
            return ToggleBlockOut.model_validate(item)
        if block_type == "recommendation_card":
            return RecommendationCardBlockOut.model_validate(item)
    except Exception:  # noqa: BLE001 — allowlist: skip unknown/invalid blocks
        return None
    return None
