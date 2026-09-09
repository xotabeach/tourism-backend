"""Phase 8B route planning chat sessions."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from redis.asyncio import Redis
from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.api.errors import AppError
from tourism_backend.config import Settings, get_settings
from tourism_backend.modules.identity.application.chat_preferences import (
    apply_chat_preferences,
)
from tourism_backend.modules.identity.infrastructure.models import User
from tourism_backend.modules.knowledge.application.embedder import build_embedder
from tourism_backend.modules.knowledge.infrastructure.faq_cache import RagRetrievalCache
from tourism_backend.modules.knowledge.infrastructure.retriever import (
    RetrievalRequest,
    TourismKnowledgeRetriever,
)
from tourism_backend.modules.route_builder.application import generate_service, match_service
from tourism_backend.modules.route_builder.application.ai import (
    AIProviderBusyError,
    ChatMessage,
    ChatTurnResult,
)
from tourism_backend.modules.route_builder.application.chat_actions import (
    ask_field_from_text,
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
from tourism_backend.modules.route_builder.application.dialogue import fallback_goal
from tourism_backend.modules.route_builder.application.discovery import (
    discovery_patch,
)
from tourism_backend.modules.route_builder.application.schemas import (
    ActionsBlockOut,
    CatalogMatchBlockOut,
    CatalogRouteItemOut,
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
    ToolCall,
    execute_tool,
    parse_tool_calls,
    prefetch_context,
    recommendation_accept_patch,
)
from tourism_backend.modules.route_builder.application.topic_guard import (
    LLM_HISTORY_OMIT_INTENTS,
    REDACTED_USER_TEXT,
    ai_busy_fallback,
    ai_unavailable_fallback,
    canned_reply_for_intent,
    classify_chat_intent,
    include_in_llm_history,
    persistable_user_text,
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
    RouteProposal,
)
from tourism_backend.modules.runtime_config.application.service import (
    effective_ai_provider_settings,
)
from tourism_backend.modules.subscriptions.application.entitlements import require_ai_chat
from tourism_backend.modules.subscriptions.application.service import (
    refresh_user_travel_plus,
)

logger = logging.getLogger(__name__)

# Even a nearly-spent turn gets this much for the tool round: below it the
# call cannot finish anyway (measured tool rounds: 3.4-13.8s), so a shorter
# slice would only burn latency before dropping the round.
_MIN_TOOL_ROUND_SECONDS = 4.0


async def warm_rag_embedder(settings: Settings) -> None:
    """Load the embedding model while the user is still typing.

    Opening the chat is the earliest moment we know a turn is coming, and it
    buys the ~9.5s first load (measured on production; 0.05s once warm) the
    time it takes someone to write their first message. Doing it at process
    start instead would pay that memory on every deploy even when nobody
    opens the chat, and doing it inside the turn is what made the first
    message after a restart time out on the client.

    Best-effort: a failure here only means the turn loads the model itself,
    exactly as it did before.
    """
    if not settings.rag_enabled:
        return
    try:
        await build_embedder(settings).warm()
    except Exception:  # noqa: BLE001 — warmup must never fail the request
        logger.warning("rag_embedder_warmup_failed", exc_info=True)


_HISTORY_LIMIT = 12
_SESSION_LIST_MAX = 50
_MESSAGE_LIST_MAX = 100
_CONTROL_ACTION_IDS = frozenset({"budget_amount", "with_children", "with_pets", "avoid_crowds"})
_MATCH_FIRST_ACTIONS = frozenset({"want_generate"})
_CUSTOM_GENERATE_ACTIONS = frozenset({"build_custom_route"})
_SAVE_PREFERENCES_ACTIONS = frozenset({"save_preferences"})


def llm_history_stmt(
    session_id: UUID,
    *,
    limit: int = _HISTORY_LIMIT,
) -> Select[tuple[RoutePlanningMessage]]:
    """Newest eligible turns first; caller reverses to chronological order.

    Crisis/injection user rows are excluded here so a LIMIT 12 window is not
    filled by redacted turns that would then be dropped in Python.
    """
    omitted = tuple(LLM_HISTORY_OMIT_INTENTS)
    return (
        select(RoutePlanningMessage)
        .where(
            RoutePlanningMessage.session_id == session_id,
            RoutePlanningMessage.role.in_(("user", "assistant")),
            or_(
                RoutePlanningMessage.role != "user",
                and_(
                    or_(
                        RoutePlanningMessage.intent.is_(None),
                        RoutePlanningMessage.intent.notin_(omitted),
                    ),
                    RoutePlanningMessage.text != REDACTED_USER_TEXT,
                ),
            ),
        )
        .order_by(RoutePlanningMessage.created_at.desc())
        .limit(limit)
    )


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
    return _session_out(
        row,
        ai_planning_enabled=cfg.ai_planning_enabled,
        message_limit=cfg.ai_chat_message_limit,
    )


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
    counts: dict[UUID, int] = {}
    if rows:
        counted = await session.execute(
            select(RoutePlanningMessage.session_id, func.count())
            .where(RoutePlanningMessage.session_id.in_([row.id for row in rows]))
            .group_by(RoutePlanningMessage.session_id)
        )
        counts = {session_id: int(count) for session_id, count in counted.all()}
    for row in rows:
        _close_if_stale(row, cfg)
    await session.commit()
    return RoutePlanningSessionListOut(
        items=[
            _session_out(
                row,
                ai_planning_enabled=cfg.ai_planning_enabled,
                message_count=counts.get(row.id, 0),
                message_limit=cfg.ai_chat_message_limit,
            )
            for row in rows
        ],
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

    row = await _owned_session(
        session,
        user_id=user_id,
        session_id=session_id,
        settings=cfg,
    )
    used = await _message_count(session, row.id)
    await session.commit()
    return _session_out(
        row,
        ai_planning_enabled=cfg.ai_planning_enabled,
        message_count=used,
        message_limit=cfg.ai_chat_message_limit,
    )


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

    row = await _owned_session(session, user_id=user_id, session_id=session_id, settings=cfg)
    if row.status != "closed":
        row.status = "closed"
        row.updated_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(row)
    return _session_out(
        row,
        ai_planning_enabled=cfg.ai_planning_enabled,
        message_count=await _message_count(session, row.id),
        message_limit=cfg.ai_chat_message_limit,
    )


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


# Purposes in which a bare "да"/"ок" means "build it" once every field is
# confirmed. Answering "да" while the assistant is telling you about a place
# or comparing two of them is agreement with what was said, not an order to
# generate a route.
#
# An unset goal counts: a session created straight from the params form has
# never had a conversational turn to set one, and its author filled in and
# confirmed the whole form — which is the planning path, not an aside.
_CONFIRMABLE_GOALS = frozenset({"discover", "clarify"})


def _goal_accepts_confirmation(goal: object) -> bool:
    return not isinstance(goal, str) or goal in _CONFIRMABLE_GOALS


async def post_message(
    session: AsyncSession,
    *,
    user_id: UUID,
    session_id: UUID,
    payload: RoutePlanningMessageIn,
    settings: Settings | None = None,
    redis: Redis | None = None,
) -> RoutePlanningMessageOut:
    cfg = settings or get_settings()
    user = await session.get(User, user_id)
    if user is None:
        raise AppError(code="user_not_found", message="User not found", status_code=404)
    await refresh_user_travel_plus(session, user=user)
    require_ai_chat(user)

    planning = await _owned_session(
        session,
        user_id=user_id,
        session_id=session_id,
        settings=cfg,
    )
    if planning.status != "active":
        await session.commit()
        raise AppError(
            code="session_closed",
            message="Planning session is closed",
            status_code=409,
        )
    if await _message_count(session, planning.id) >= cfg.ai_chat_message_limit:
        planning.status = "closed"
        await session.commit()
        raise AppError(
            code="session_message_limit",
            message="Planning session reached its message limit",
            status_code=409,
        )

    confirmed = sanitize_confirmed_fields(
        list(planning.confirmed_fields) if isinstance(planning.confirmed_fields, list) else []
    )
    constraints_dict = _constraints_with_preferences(dict(planning.constraints), confirmed, user)
    turn_explicit_fields: set[str] = set()

    if payload.controls is not None:
        controls_patch = payload.controls.model_dump(exclude_none=True)
        turn_explicit_fields.update(fields_touched_by_patch(controls_patch))
        constraints_dict = merge_constraint_patch(constraints_dict, controls_patch)
        confirmed = sanitize_confirmed_fields(
            [*confirmed, *fields_touched_by_patch(controls_patch)]
        )

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
            turn_explicit_fields.update(touched)
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
                turn_explicit_fields.update(touched)
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
                        turn_explicit_fields.update(touched)
                        field = field_for_action(canonical)
                        if field:
                            touched = sanitize_confirmed_fields([*touched, field])
                        confirmed = sanitize_confirmed_fields([*confirmed, *touched])

    intent = classify_chat_intent(
        payload.text,
        generate_confirm_ok=(
            _goal_accepts_confirmation(constraints_dict.get("dialogue_goal"))
            and prefer_ready_ask_field(confirmed) == "ready"
        ),
    )
    flow: str = intent
    goal = fallback_goal(payload.text, str(constraints_dict.get("dialogue_goal") or "clarify"))
    if payload.controls is not None or payload.action_id in _CONTROL_ACTION_IDS:
        goal = "custom" if constraints_dict.get("planning_mode") == "custom" else "discover"
    constraints_dict["dialogue_goal"] = goal
    if (
        intent not in {"crisis", "off_topic", "injection_attempt"}
        and not payload.controls
        and goal in {"discover", "custom"}
    ):
        search_patch = discovery_patch(payload.text)
        if search_patch:
            constraints_dict = merge_constraint_patch(constraints_dict, search_patch)
            touched = fields_touched_by_patch(search_patch)
            confirmed = sanitize_confirmed_fields([*confirmed, *touched])
            turn_explicit_fields.update(touched)
    canonical_action = normalize_action_id(payload.action_id) if payload.action_id else None
    is_control_only = payload.action_id in _CONTROL_ACTION_IDS and payload.control_value is not None
    if payload.want_generate or canonical_action in _MATCH_FIRST_ACTIONS:
        goal = "discover"
        constraints_dict["dialogue_goal"] = goal
        constraints_dict["planning_mode"] = "discover"
        flow = "generate"
        intent = "generate"
    elif canonical_action in _CUSTOM_GENERATE_ACTIONS:
        goal = "custom"
        constraints_dict["dialogue_goal"] = goal
        constraints_dict["planning_mode"] = "custom"
        flow = "generate_custom"
        intent = "generate"
    elif canonical_action == "clear_params":
        flow = "clear_params"
    elif canonical_action in _SAVE_PREFERENCES_ACTIONS:
        flow = "save_preferences"
    elif is_control_only or payload.controls is not None or canonical_action == "reply":
        flow = "on_topic_travel"

    # Old clients may still ask for a match early. Form defaults are not a
    # confirmed transport/duration and must not silently become trip facts.
    if flow == "generate_custom" and not {
        "city",
        "transport_mode",
        "duration",
    }.issubset(confirmed):
        flow = "on_topic_travel"

    now = datetime.now(UTC)
    user_payload: dict[str, Any] | None = None
    if payload.action_id or payload.control_value is not None or payload.controls is not None:
        user_payload = {
            "action_id": payload.action_id,
            "control_value": payload.control_value,
            "controls": payload.controls.model_dump(exclude_none=True)
            if payload.controls
            else None,
        }
    user_msg = RoutePlanningMessage(
        id=uuid4(),
        session_id=planning.id,
        user_id=user_id,
        role="user",
        text=persistable_user_text(intent, payload.text),
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

    if flow in {"crisis", "off_topic", "injection_attempt"}:
        assistant_text = canned_reply_for_intent(intent)
        ask_field = first_missing_ask_field(confirmed)
        if flow == "off_topic":
            blocks = list(
                clarification_action_blocks(
                    constraints_dict,
                    confirmed_fields=confirmed,
                    ask_field=ask_field,
                )
            )
    elif flow == "clear_params":
        constraints_dict = RouteMatchParamsIn(city="Крым").model_dump(mode="json")
        confirmed = []
        assistant_text = "Очистил параметры. Выбери из предложенного или опиши идеальный маршрут."
        ask_field = "pace"
        blocks = [
            ActionsBlockOut(
                layout="stack",
                actions=[
                    {"id": "pace_calm", "label": "Спокойный маршрут"},
                    {"id": "pace_active", "label": "Активный маршрут"},
                    {"id": "interest_mountains", "label": "Маршрут по горам"},
                    {"id": "interest_sea", "label": "Путешествие к морю"},
                    {"id": "interest_food", "label": "Гастрономический тур"},
                ],
            )
        ]
    elif flow == "save_preferences":
        # Explicit confirmation only — never triggered by a plain chat turn.
        # Mutates `user` in place; the outer commit below persists it.
        changed = apply_chat_preferences(
            user,
            constraints=constraints_dict,
            confirmed_fields=confirmed,
        )
        provider_name = "save_preferences_ack"
        ask_field = prefer_ready_ask_field(confirmed)
        if changed:
            assistant_text = "Запомнил: " + ", ".join(changed) + ". Учту это в следующий раз."
        else:
            assistant_text = (
                "Пока нечего запомнить — сначала подтверди пару предпочтений в этом чате."
            )
        blocks = _compose_assistant_blocks(
            constraints=constraints_dict,
            confirmed_fields=confirmed,
            ask_field=ask_field,
            action_ids=["want_generate"] if ask_field == "ready" else None,
            tool_context={},
            include_recommendations=False,
        )
    elif flow in {"generate", "generate_custom"}:
        params = RouteMatchParamsIn.model_validate(constraints_dict)
        force_custom = flow == "generate_custom"
        if not force_custom:
            matched = await match_service.match_routes(
                session,
                user_id=user_id,
                params=_discovery_params(constraints_dict, confirmed),
                ai_planning_enabled=cfg.ai_planning_enabled,
                confirmed_fields=confirmed,
            )
            catalog_block = _catalog_match_block(
                matched, locality_label=_discovery_params(constraints_dict, confirmed).search_area
            )
            if catalog_block is not None:
                assistant_text = "Вот подобранные маршруты по выбранным параметрам:"
                provider_name = "catalog_match"
                blocks = [
                    catalog_block,
                    ActionsBlockOut(
                        layout="stack",
                        actions=[
                            {
                                "id": "build_custom_route",
                                "label": "Собрать собственный маршрут",
                            },
                            {
                                "id": "clear_params",
                                "label": "Очистить мои параметры",
                            },
                        ],
                    ),
                ]
                ask_field = "ready"
            else:
                assistant_text = (
                    "В каталоге пока нет подходящих маршрутов по этим условиям. "
                    "Можем изменить параметры поиска или собрать свой маршрут из доступных мест."
                )
                provider_name = "catalog_match"
                ask_field = "ready"
                blocks = [
                    ActionsBlockOut(
                        actions=[
                            {"id": "reply", "label": "Хочу изменить условия поиска"},
                            {"id": "build_custom_route", "label": "Собрать свой маршрут"},
                        ]
                    )
                ]
        if force_custom:
            recent_place_ids = await _recent_proposal_place_ids(
                session, user_id=user_id, session_id=planning.id
            )
            generated = await generate_service.generate_route(
                session,
                user_id=user_id,
                payload=RouteGenerateIn(channel="chat", params=params),
                recent_place_ids=recent_place_ids,
            )
            await session.refresh(planning)
            proposal_out = generated.proposal
            assistant_text = "Собрал маршрут по твоим параметрам:"
            provider_name = "deterministic_generate"
            blocks = list(proposal_out.blocks)
            ask_field = "ready"
    else:
        turn, provider_name, fallback, prefetch, explicit_places = await _assistant_from_ai(
            session,
            planning=planning,
            constraints=constraints_dict,
            confirmed_fields=confirmed,
            settings=cfg,
            user=user,
            redis=redis,
        )
        assistant_text = turn.assistant_text
        if turn.goal and canonical_action not in _CUSTOM_GENERATE_ACTIONS and not payload.controls:
            goal = turn.goal
        constraints_dict["dialogue_goal"] = goal
        if goal == "custom":
            constraints_dict["planning_mode"] = "custom"
        elif goal == "discover":
            constraints_dict["planning_mode"] = "discover"
        refreshed_catalog = False
        discovery_replies = None
        ask_field = turn.ask_field or prefer_ready_ask_field(confirmed)
        # Модель нередко спрашивает город прозой, не проставив ask_field —
        # тогда доверяем тексту, иначе человек видит вопрос, на который нечем ответить.
        ask_field = ask_field_from_text(assistant_text, ask_field)
        if turn.proposed_constraints:
            # A later spoken correction may revise an earlier choice. Only
            # this turn's exact UI choices outrank model extraction; otherwise
            # «теперь поедем на машине» silently kept the old walking setting.
            effective_patch = {
                key: value
                for key, value in turn.proposed_constraints.items()
                if ("interests" if key == "interests_add" else key) not in turn_explicit_fields
            }
            constraints_dict = merge_constraint_patch(
                constraints_dict,
                effective_patch,
                previously_confirmed=confirmed,
            )
            touched = fields_touched_by_patch(effective_patch)
            confirmed = sanitize_confirmed_fields([*confirmed, *touched])
            proposed = effective_patch
            # Keep the question the model actually asked. Replacing it with
            # ready after any patch produced controls unrelated to its text.
            if ask_field in touched:
                ask_field = prefer_ready_ask_field(confirmed)
        required_question = (
            next(
                (
                    field
                    for field in ("city", "transport_mode", "duration")
                    if field not in confirmed
                ),
                None,
            )
            if goal == "custom"
            else None
        )
        if goal == "discover":
            # A model-extracted destination/filter was not available to prefetch.
            # Never attach old-area cards, or call an unsearched catalogue empty.
            if prefetch.get("_catalog_signature") != _discovery_signature(
                constraints_dict, confirmed
            ):
                prefetch = {
                    **{key: value for key, value in prefetch.items() if key != "catalog_preview"},
                    **await _catalog_discovery_context(
                        session,
                        user_id=user.id,
                        constraints=constraints_dict,
                        confirmed_fields=confirmed,
                        settings=cfg,
                    ),
                }
                refreshed_catalog = True
            if (
                ask_field == "city"
                or turn.structured_parse == "fallback"
                or refreshed_catalog
                or (ask_field != "ready" and not turn.clarification_reason)
            ):
                ask_field = "ready"
                preview = prefetch.get("catalog_preview")
                assistant_text = (
                    "Вот готовые варианты по вашим пожеланиям; "
                    "можно открыть карточки и сравнить их."
                    if preview
                    else "В проверенной части каталога "
                    "не нашлось готовых "
                    "маршрутов по этим условиям. Можем расширить поиск или обсудить свой маршрут."
                    if "search_context" in prefetch
                    else "Точный город старта пока не нужен. Можно поискать готовые варианты "
                    "в выбранном районе. Показать маршруты?"
                )
                discovery_replies = (
                    [
                        {"id": "reply", "label": "Сравни эти варианты"},
                        {"id": "reply", "label": "Хочу изменить пожелания"},
                    ]
                    if preview
                    else [
                        {"id": "reply", "label": "Расширить поиск по Крыму"},
                        {"id": "build_custom_route", "label": "Собрать свой маршрут"},
                    ]
                )
        elif goal == "compare":
            if "comparison_routes" not in prefetch:
                prefetch.update(await _comparison_context(session, planning.id))
            ask_field = "ready"
            if not prefetch.get("comparison_routes"):
                assistant_text = (
                    "Какие маршруты сравнить? Пришлите названия или сначала попросите варианты."
                )
            elif fallback:
                assistant_text = (
                    "Не удалось получить сравнение помощника. "
                    "Можно повторить вопрос или открыть показанные карточки."
                )
            discovery_replies = [{"id": "reply", "label": "Предложи варианты"}]
        elif goal in {"place_info", "clarify"}:
            # No route questionnaire/CTA after a factual answer or open clarification.
            if fallback:
                assistant_text = (
                    "Сейчас не удалось получить ответ помощника. Попробуйте повторить вопрос."
                )
            elif ask_field != "ready":
                assistant_text = (
                    "Что хочется узнать об этом месте?"
                    if goal == "place_info"
                    else "Помочь с идеями поездки, сравнить маршруты или рассказать о месте?"
                )
            ask_field = "ready"
            discovery_replies = [{"id": "reply", "label": "Предложи идеи поездки"}]
        repair_question = required_question if ask_field == "ready" else None
        if goal == "custom" and required_question:
            repair_question = required_question
        if (
            not repair_question
            and payload.controls is not None
            and ask_field == "budget"
            and "budget_amount" in confirmed
        ):
            repair_question = "ready" if goal == "discover" else prefer_ready_ask_field(confirmed)
        if repair_question:
            ask_field = repair_question
            assistant_text = {
                "city": "Откуда начнём поездку? Выбери город или напиши место старта.",
                "transport_mode": (
                    "Как будем передвигаться — пешком, на машине или общественным транспортом?"
                ),
                "duration": "Сколько дней отведём на поездку?",
                "interests": "Что хочется увидеть — побережье, горы или исторические места?",
                "people": "Сколько человек едет?",
                "ready": "Теперь можно сравнить готовые маршруты. Показать варианты?",
            }.get(ask_field, "Что ещё важно учесть в поездке?")
        blocks = _compose_assistant_blocks(
            constraints=constraints_dict,
            confirmed_fields=confirmed,
            ask_field=ask_field,
            action_ids=(
                list(turn.action_ids)
                if turn.action_ids and not repair_question and not discovery_replies
                else None
            ),
            quick_replies=discovery_replies
            or (list(turn.quick_replies) if not repair_question else None),
            tool_context=prefetch,
            include_recommendations=False,
            place_candidates=explicit_places,
        )
        preview = prefetch.get("catalog_preview")
        if goal == "discover" and isinstance(preview, dict):
            blocks.insert(0, CatalogMatchBlockOut.model_validate(preview))

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
    await session.flush()
    # The turn that fills the chat is still answered — cutting it off would
    # lose the reply the user just waited for — and the session closes right
    # behind it, so the app can offer a fresh one.
    used = await _message_count(session, planning.id)
    if used >= cfg.ai_chat_message_limit:
        planning.status = "closed"
    await session.commit()
    await session.refresh(assistant_msg)

    return RoutePlanningMessageOut(
        session_status=planning.status,  # type: ignore[arg-type]
        session_message_count=used,
        session_message_limit=cfg.ai_chat_message_limit,
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


async def _recent_proposal_place_ids(
    session: AsyncSession, *, user_id: UUID, session_id: UUID
) -> frozenset[UUID]:
    """A bounded, session-owned diversity signal, not a global exclusion list."""
    rows = (
        await session.scalars(
            select(RouteProposal.place_ids)
            .join(RoutePlanningMessage, RoutePlanningMessage.proposal_id == RouteProposal.id)
            .where(
                RoutePlanningMessage.session_id == session_id,
                RoutePlanningMessage.user_id == user_id,
                RoutePlanningMessage.role == "assistant",
                RouteProposal.user_id == user_id,
            )
            .order_by(RoutePlanningMessage.created_at.desc())
            .limit(3)
        )
    ).all()
    return frozenset(place_id for ids in rows for place_id in ids)


async def _assistant_from_ai(
    session: AsyncSession,
    *,
    planning: RoutePlanningSession,
    constraints: dict[str, Any],
    confirmed_fields: list[str],
    settings: Settings,
    user: User,
    redis: Redis | None = None,
) -> tuple[ChatTurnResult, str | None, bool, dict[str, Any], list[dict[str, str]]]:
    history_rows = list((await session.scalars(llm_history_stmt(planning.id))).all())
    history_rows.reverse()
    chat_messages: list[ChatMessage] = []
    for row in history_rows:
        if not include_in_llm_history(role=row.role, intent=row.intent, text=row.text):
            continue
        chat_messages.append(ChatMessage(role=row.role, content=row.text))

    tool_context = await prefetch_context(
        session,
        constraints=constraints,
        confirmed_fields=confirmed_fields,
    )
    goal = constraints.get("dialogue_goal") or fallback_goal(
        next((msg.content for msg in reversed(chat_messages) if msg.role == "user"), "")
    )
    if goal == "discover":
        tool_context.update(
            await _catalog_discovery_context(
                session,
                user_id=user.id,
                constraints=constraints,
                confirmed_fields=confirmed_fields,
                settings=settings,
            )
        )
    elif goal == "compare":
        tool_context.update(await _comparison_context(session, planning.id, rows=history_rows))
    draft = form_draft_constraints(constraints, confirmed_fields)
    if draft:
        tool_context = {**tool_context, "form_draft_not_facts": draft}
    # Workstream C: cross-session soft prior from the persisted profile — the
    # same source the catalog ranker already uses (see
    # 2gis-personalization-offline-plan, 5.2). Advisory only; the prompt
    # tells the model this loses to anything the user actually says here.
    preferences_prior = _persisted_preferences_prior(user)
    if preferences_prior:
        tool_context = {**tool_context, "user_preferences_prior": preferences_prior}
    place_hints = list(tool_context.get("place_candidates") or [])
    if not place_hints and "city" in confirmed_fields:
        place_hints = await _place_hints(session, constraints)

    # Phase 2: retrieve narrative chunks (RAG / pgvector) and feed them as
    # untrusted DATA when enabled. Hard facts still come from PostGIS tools.
    if settings.rag_enabled:
        try:
            retriever = TourismKnowledgeRetriever(embedder=build_embedder(settings))
            query = _retrieval_query(
                chat_messages,
                {key: value for key, value in constraints.items() if key in confirmed_fields},
            )
            request = RetrievalRequest(
                query=query,
                top_k=settings.rag_top_k,
                region=str(constraints.get("region_slug") or "crimea")[:64],
                locality=(
                    str(constraints["city"])[:120]
                    if not constraints.get("search_area")
                    and "city" in confirmed_fields
                    and constraints.get("city") not in {None, "", "Крым"}
                    else None
                ),
            )
            cache = (
                RagRetrievalCache(redis, ttl_seconds=settings.rag_faq_cache_ttl_seconds)
                if redis is not None
                else None
            )
            rag = await cache.get(request) if cache is not None else None
            if rag is None:
                rag = await retriever.retrieve(session, request=request)
                if cache is not None:
                    await cache.set(request, rag)
            if rag.chunks:
                tool_context = {
                    **tool_context,
                    "knowledge": [
                        {
                            "title": chunk.title[:120],
                            "body": chunk.body[:1600],
                            "source": chunk.source,
                            "content_type": chunk.content_type,
                        }
                        for chunk in rag.chunks[: settings.rag_top_k]
                    ],
                }
        except Exception:  # noqa: BLE001,S110 — RAG must never break the chat turn
            pass

    rag_hit = bool(tool_context.get("knowledge"))
    started = time.perf_counter()

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

    async def _ground(provider: Any, result: ChatTurnResult) -> ChatTurnResult:
        nonlocal tool_context
        semantic_goal = result.goal or goal
        patch = result.proposed_constraints or {}
        updated = merge_constraint_patch(constraints, patch, previously_confirmed=confirmed_fields)
        updated_fields = sanitize_confirmed_fields(
            [*confirmed_fields, *fields_touched_by_patch(patch)]
        )
        needs_data = False
        if semantic_goal == "discover" and tool_context.get(
            "_catalog_signature"
        ) != _discovery_signature(updated, updated_fields):
            tool_context = {
                **{key: value for key, value in tool_context.items() if key != "catalog_preview"},
                **await _catalog_discovery_context(
                    session,
                    user_id=user.id,
                    constraints=updated,
                    confirmed_fields=updated_fields,
                    settings=settings,
                ),
            }
            needs_data = True
        elif semantic_goal == "compare" and "comparison_routes" not in tool_context:
            tool_context.update(await _comparison_context(session, planning.id, rows=history_rows))
            needs_data = True
        if not needs_data or result.structured_parse == "fallback":
            return result
        # One bounded synthesis over actual search results after semantic extraction.
        # No unbounded agent loop, and never claim an ungrounded comparison.
        remaining = settings.ai_turn_budget_seconds - (time.perf_counter() - started)
        if remaining <= _MIN_TOOL_ROUND_SECONDS:
            return replace(result, structured_parse="fallback")
        try:
            follow: ChatTurnResult = await asyncio.wait_for(
                provider.chat_turn(
                    messages=chat_messages,
                    constraints={**updated, "dialogue_goal": semantic_goal},
                    confirmed_fields=updated_fields,
                    place_hints=place_hints,
                    tool_context=tool_context,
                ),
                timeout=remaining,
            )
        except Exception:  # noqa: BLE001 — deterministic grounded response remains available
            return replace(result, structured_parse="fallback")
        return replace(
            follow,
            goal=result.goal or follow.goal,
            proposed_constraints={**patch, **(follow.proposed_constraints or {})} or None,
        )

    if not settings.ai_planning_enabled:
        provider: Any = MockAIPlanningProvider()
        result = await _once(provider, tool_context)
        result = await _ground(provider, result)
        tools_round = 1 if parse_tool_calls(list(result.tool_requests)) else 0
        result, tool_context, explicit_places = await _run_tool_rounds(
            session,
            provider=provider,
            result=result,
            constraints=constraints,
            confirmed_fields=confirmed_fields,
            chat_messages=chat_messages,
            place_hints=place_hints,
            tool_context=tool_context,
        )
        _log_ai_chat_turn(
            provider=result.provider,
            latency_ms=_elapsed_ms(started),
            structured_parse=result.structured_parse,
            tools_round=tools_round,
            rag_hit=rag_hit,
            outage_fallback=True,
        )
        return result, result.provider, True, tool_context, explicit_places

    try:
        effective_settings = await effective_ai_provider_settings(session, settings)
        provider = get_ai_planning_provider(effective_settings)
        result = await _once(provider, tool_context)
        result = await _ground(provider, result)
        tools_round = 1 if parse_tool_calls(list(result.tool_requests)) else 0
        result, tool_context, explicit_places = await _run_tool_rounds(
            session,
            provider=provider,
            result=result,
            constraints=constraints,
            confirmed_fields=confirmed_fields,
            chat_messages=chat_messages,
            place_hints=place_hints,
            tool_context=tool_context,
            budget_seconds=max(
                _MIN_TOOL_ROUND_SECONDS,
                settings.ai_turn_budget_seconds - (time.perf_counter() - started),
            ),
        )
        _log_ai_chat_turn(
            provider=result.provider,
            latency_ms=_elapsed_ms(started),
            structured_parse=result.structured_parse,
            tools_round=tools_round,
            rag_hit=rag_hit,
            outage_fallback=result.structured_parse == "fallback",
        )
        return (
            result,
            result.provider,
            result.structured_parse == "fallback",
            tool_context,
            explicit_places,
        )
    except Exception as exc:  # noqa: BLE001 — soft fallback for home-lab outages
        turn = _provider_error_turn(exc, confirmed_fields)
        busy = isinstance(exc, AIProviderBusyError)
        _log_ai_chat_turn(
            provider="lmstudio" if busy else "fallback",
            latency_ms=_elapsed_ms(started),
            structured_parse="fallback",
            tools_round=0,
            rag_hit=rag_hit,
            outage_fallback=not busy,
        )
        return turn, None, True, tool_context, []


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _log_ai_chat_turn(
    *,
    provider: str | None,
    latency_ms: int,
    structured_parse: str,
    tools_round: int,
    rag_hit: bool,
    outage_fallback: bool,
) -> None:
    logger.info(
        "ai_chat_turn",
        extra={
            "provider": provider or "none",
            "latency_ms": latency_ms,
            "structured_parse": structured_parse,
            "tools_round": tools_round,
            "rag_hit": rag_hit,
            "outage_fallback": outage_fallback,
        },
    )


def _provider_error_turn(exc: BaseException, confirmed_fields: list[str]) -> ChatTurnResult:
    """Map LM Studio failures to a canned assistant turn.

    A busy GPU must not look like a full outage: the user can retry immediately
    instead of hanging on the 60s HTTP timeout and then seeing the offline copy.
    """
    busy = isinstance(exc, AIProviderBusyError)
    return ChatTurnResult(
        assistant_text=ai_busy_fallback() if busy else ai_unavailable_fallback(),
        ask_field=prefer_ready_ask_field(confirmed_fields),
        action_ids=(),
        provider="fallback",
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
    budget_seconds: float | None = None,
) -> tuple[ChatTurnResult, dict[str, Any], list[dict[str, str]]]:
    calls = parse_tool_calls(list(result.tool_requests))
    if not calls:
        return result, tool_context, []
    tool_payloads: list[dict[str, Any]] = []
    # Only a `search_places` call the model makes *this* turn means it wants
    # place chips rendered now — background prefetch just grounds the prompt
    # and must never leak into the reply's blocks (it would re-attach the
    # same place carousel to every later clarifying question once a city is
    # known).
    explicit_places: list[dict[str, str]] = []
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
            explicit_places = list(places)
    # All adapters receive complete bounded tool DATA. Gemini intentionally
    # drops history's system-role messages, so results must live in context.
    tool_context = {**tool_context, "tool_results": tool_payloads}
    follow_call = provider.chat_turn(
        messages=chat_messages,
        constraints=constraints,
        confirmed_fields=confirmed_fields,
        place_hints=place_hints,
        tool_context=tool_context,
    )
    try:
        if budget_seconds is None:
            follow = await follow_call
        else:
            follow = await asyncio.wait_for(follow_call, timeout=budget_seconds)
    except TimeoutError:
        # The turn is out of budget. The first call already produced a usable
        # reply, so send that rather than failing the whole turn — the tool
        # results only would have enriched it.
        logger.info("ai_chat_tool_round_dropped", extra={"budget_seconds": budget_seconds})
        return result, tool_context, explicit_places
    if follow.structured_parse == "fallback":
        return result, tool_context, explicit_places
    # Do not recurse infinitely: ignore further tool_requests on follow-up.
    return (
        ChatTurnResult(
            assistant_text=follow.assistant_text,
            proposed_constraints={
                **(result.proposed_constraints or {}),
                **(follow.proposed_constraints or {}),
            }
            or None,
            ask_field=follow.ask_field,
            action_ids=follow.action_ids,
            quick_replies=follow.quick_replies,
            tool_requests=(),
            provider=follow.provider,
            structured_parse=follow.structured_parse,
            goal=follow.goal or result.goal,
            clarification_reason=follow.clarification_reason or result.clarification_reason,
        ),
        tool_context,
        explicit_places,
    )


def _compose_assistant_blocks(
    *,
    constraints: dict[str, Any],
    confirmed_fields: list[str],
    ask_field: str | None,
    action_ids: list[str] | None,
    tool_context: dict[str, Any],
    include_recommendations: bool = False,
    place_candidates: list[dict[str, str]] | None = None,
    quick_replies: list[dict[str, str]] | None = None,
) -> list[ChatBlockOut]:
    blocks: list[ChatBlockOut] = []
    # Seasonal tip cards only when AI is unavailable (fallback) — not on every
    # live agent turn (product: «2 карточки Летом» only as offline help).
    if include_recommendations:
        seen_tips: set[str] = set()
        for tip in (tool_context.get("seasonal_recommendations") or [])[:2]:
            if not isinstance(tip, dict):
                continue
            title = str(tip.get("title") or "").strip()
            body = str(tip.get("body") or "").strip()
            accept = str(tip.get("accept_action") or "").strip()
            tip_id = str(tip.get("id") or accept).strip()
            if not title or not body or not accept:
                continue
            if tip_id in seen_tips:
                continue
            seen_tips.add(tip_id)
            blocks.append(
                RecommendationCardBlockOut(
                    id=tip_id[:64],
                    title=title[:120],
                    body=body[:500],
                    accept_action_id=accept[:64],
                )
            )
    for place in (place_candidates or [])[:4]:
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
    controls = interactive_control_blocks(ask_field=ask_field, constraints=constraints)
    blocks.extend(controls)
    # Рядом с выпадающим списком чипы не нужны: выбор варианта сам уходит в
    # чат, и «Подбери маршрут» под вопросом «в какой город?» читался как
    # предложение пропустить ответ.
    has_select = any(getattr(block, "type", None) == "select" for block in controls)
    if quick_replies and not controls:
        replies = [
            reply
            for reply in quick_replies
            if reply["id"] != "want_generate" or ask_field == "ready"
        ]
        if replies:
            blocks.append(ActionsBlockOut(actions=replies[:4]))
            return blocks
    if not has_select and ask_field not in {"budget", "with_children"}:
        blocks.extend(
            clarification_action_blocks(
                constraints,
                confirmed_fields=confirmed_fields,
                ask_field=ask_field,
                action_ids=action_ids,
            )
        )
    return blocks


def _catalog_match_block(
    matched: object, *, locality_label: str | None = None
) -> CatalogMatchBlockOut | None:
    """Build screen-2 carousel from algorithmic match hits (ideal then close)."""
    ideal = getattr(matched, "ideal", None) or []
    close = getattr(matched, "close", None) or []
    hits = list(ideal)[:5]
    if len(hits) < 5:
        hits.extend(list(close)[: 5 - len(hits)])
    if not hits:
        return None
    routes: list[CatalogRouteItemOut] = []
    for hit in hits:
        route = getattr(hit, "route", None)
        if route is None:
            continue
        distance_km = None
        meters = getattr(route, "distance_meters", None)
        if isinstance(meters, int) and meters > 0:
            distance_km = round(meters / 1000.0, 1)
        tags: list[str] = []
        transport = getattr(route, "transport_mode", None)
        mode_labels = {
            "walk": "Пешком",
            "walking": "Пешком",
            "car": "Авто",
            "public": "Общ. транспорт",
            "mixed": "Смешанный",
        }
        if isinstance(transport, str) and transport in mode_labels:
            tags.append(mode_labels[transport])
        if getattr(route, "suitable_for_children", None) is True:
            tags.append("С детьми")
        seasonality = getattr(route, "seasonality", None) or []
        for raw in list(seasonality)[:2]:
            label = str(raw).strip().capitalize()
            if label and label not in tags:
                tags.append(label)
        difficulty = getattr(route, "difficulty", None)
        difficulty_label = None
        if isinstance(difficulty, str) and difficulty.strip():
            difficulty_label = difficulty.strip()[:40]
        elif isinstance(difficulty, int):
            difficulty_label = f"{difficulty}/5"
        routes.append(
            CatalogRouteItemOut(
                route_id=str(route.id),
                title=str(route.name)[:120],
                cover_url=getattr(route, "cover_image_url", None),
                rating=None,
                distance_km=distance_km,
                locality_label=locality_label[:120] if locality_label else None,
                tags=tags[:8],
                budget_label=None,
                difficulty_label=difficulty_label,
                stops_count=int(getattr(route, "stops_count", 0) or 0),
                duration_minutes=int(getattr(route, "estimated_duration_minutes", 0) or 0),
            )
        )
    if not routes:
        return None
    return CatalogMatchBlockOut(routes=routes)


def _control_patch(
    action_id: str,
    control_value: float | bool | None,
) -> dict[str, Any] | None:
    if (
        action_id == "budget_amount"
        and isinstance(control_value, (int, float))
        and not isinstance(control_value, bool)
    ):
        amount = int(control_value)
        amount = max(0, min(amount, 1_000_000))
        return {"budget_amount": amount}
    if action_id == "with_children" and isinstance(control_value, bool):
        return {"with_children": control_value}
    if action_id == "with_pets" and isinstance(control_value, bool):
        return {"with_pets": control_value}
    if action_id == "avoid_crowds" and isinstance(control_value, bool):
        return {"avoid_crowds": control_value}
    if action_id == "with_children":
        return {"with_children": True}
    if action_id == "with_pets":
        return {"with_pets": True}
    if action_id == "avoid_crowds":
        return {"avoid_crowds": True}
    return None


def _constraints_with_preferences(
    constraints: dict[str, Any], confirmed: list[str], user: User
) -> dict[str, Any]:
    prior = _persisted_preferences_prior(user)
    pace = {"easy": "calm", "moderate": "moderate", "hard": "active"}.get(
        prior.pop("pace_hint", None)
    )
    if pace:
        prior["pace"] = pace
    return {**constraints, **{key: value for key, value in prior.items() if key not in confirmed}}


async def _catalog_discovery_context(
    session: AsyncSession,
    *,
    user_id: UUID,
    constraints: dict[str, Any],
    confirmed_fields: list[str],
    settings: Settings,
) -> dict[str, Any]:
    params = _discovery_params(constraints, confirmed_fields)
    matched = await match_service.match_routes(
        session,
        user_id=user_id,
        params=params,
        confirmed_fields=confirmed_fields,
        ai_planning_enabled=settings.ai_planning_enabled,
    )
    preview = _catalog_match_block(matched, locality_label=params.search_area)
    return {
        "_catalog_signature": _discovery_signature(constraints, confirmed_fields),
        "search_context": {
            "area": params.search_area,
            "area_is_default": "search_area" not in confirmed_fields
            and "city" not in confirmed_fields,
            "preferred_localities": constraints.get("preferred_localities") or [],
            "flexible_start": constraints.get("flexible_start", False),
            "mode": "discover_catalogue_not_build_itinerary",
            "exact_start_required": False,
        },
        "catalog_routes": preview.model_dump(mode="json")["routes"] if preview else [],
        **({"catalog_preview": preview.model_dump(mode="json")} if preview else {}),
    }


def _discovery_params(constraints: dict[str, Any], confirmed: list[str]) -> RouteMatchParamsIn:
    # Application scope, NOT a guessed departure city or a user-confirmed preference.
    area = constraints.get("search_area") if "search_area" in confirmed else None
    area = area or (constraints.get("city") if "city" in confirmed else None) or "Крым"
    return RouteMatchParamsIn.model_validate(
        {**constraints, "city": constraints.get("city") or "Крым", "search_area": area}
    )


def _discovery_signature(constraints: dict[str, Any], confirmed: list[str]) -> str:
    params = _discovery_params(constraints, confirmed).model_dump(mode="json")
    params.pop("dialogue_goal", None)
    params.pop("planning_mode", None)
    return json.dumps([params, sorted(confirmed)], sort_keys=True, ensure_ascii=False)


async def _comparison_context(
    session: AsyncSession, planning_id: UUID, *, rows: list[Any] | None = None
) -> dict[str, Any]:
    if rows is None:
        rows = list((await session.scalars(llm_history_stmt(planning_id))).all())
        rows.reverse()
    ids: list[UUID] = []
    for row in reversed(rows):
        payload = getattr(row, "payload", None)
        if getattr(row, "role", None) != "assistant" or not isinstance(payload, dict):
            continue
        for block in payload.get("blocks") or []:
            if isinstance(block, dict) and block.get("type") == "catalog_match":
                for route in (block.get("routes") or [])[:5]:
                    try:
                        ids.append(UUID(route["route_id"]))
                    except (ValueError, KeyError, TypeError):
                        continue
        if ids:
            break
    # Re-read public status; conversation snapshots are not publication authority.
    routes = await match_service.public_catalogue_routes(session, ids)
    return {
        "comparison_routes": [
            {
                "route_id": str(route.id),
                "title": route.name,
                "duration_minutes": route.estimated_duration_minutes,
                "transport_mode": route.transport_mode,
                "difficulty": route.difficulty,
                "stops_count": route.stops_count,
            }
            for route in routes
        ]
    }


def _retrieval_query(messages: list[ChatMessage], constraints: dict[str, Any]) -> str:
    """Retrieve for the actual question, retaining geographic/trip context."""
    recent = [message.content for message in messages if message.role == "user"][-2:]
    interests = constraints.get("interests") or []
    return " ".join(
        [
            recent[-1][:260] if recent else "",
            str(constraints.get("search_area") or constraints.get("city") or "Крым"),
            " ".join(constraints.get("preferred_localities") or []),
            " ".join(str(item) for item in interests[:3]),
            recent[-2][:80] if len(recent) > 1 else "",
        ]
    )[:400]


def _persisted_preferences_prior(user: User) -> dict[str, Any]:
    """Cross-session soft signal for the chat prompt (Workstream C).

    Same fields the catalog ranker already uses as a soft prior — see
    ``identity.application.chat_preferences`` for the write side. Deliberately
    a plain dict of only the signals that are actually set, so an empty
    profile adds nothing to the prompt instead of a block of nulls.
    """
    prior: dict[str, Any] = {}
    if user.preferred_categories:
        prior["interests"] = list(user.preferred_categories)[:6]
    if user.preferred_difficulty:
        prior["pace_hint"] = user.preferred_difficulty
    if user.travels_with_kids:
        prior["with_children"] = True
    if user.travels_with_pets:
        prior["with_pets"] = True
    return prior


async def _place_hints(
    session: AsyncSession,
    constraints: dict[str, Any],
) -> list[dict[str, str]]:
    city = constraints.get("city")
    if not isinstance(city, str) or not city.strip():
        return []
    try:
        result = await execute_tool(
            session,
            ToolCall(
                name="search_places",
                arguments={"city": city.strip(), "limit": 6},
            ),
            constraints=constraints,
        )
    except Exception:  # noqa: BLE001 — hints are optional context only
        return []
    if not result.ok:
        return []
    places = result.data.get("places") or []
    return [place for place in places if isinstance(place, dict)][:8]


async def _owned_session(
    session: AsyncSession,
    *,
    user_id: UUID,
    session_id: UUID,
    settings: Settings | None = None,
) -> RoutePlanningSession:
    row = await session.get(RoutePlanningSession, session_id)
    if row is None or row.user_id != user_id:
        raise AppError(
            code="session_not_found",
            message="Planning session not found",
            status_code=404,
        )
    _close_if_stale(row, settings or get_settings())
    return row


def _close_if_stale(row: RoutePlanningSession, settings: Settings) -> None:
    """Retire a chat nobody has touched for `ai_chat_session_ttl_hours`.

    Done on access rather than on a schedule: the only reader of a session is
    the person who owns it, so the next visit is the first moment the state
    matters. The caller's own commit persists it.
    """
    if row.status != "active":
        return
    touched = row.updated_at or row.created_at
    if touched is None:
        return
    if touched.tzinfo is None:
        touched = touched.replace(tzinfo=UTC)
    if datetime.now(UTC) - touched >= timedelta(hours=settings.ai_chat_session_ttl_hours):
        row.status = "closed"


async def _message_count(session: AsyncSession, session_id: UUID) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(RoutePlanningMessage)
            .where(RoutePlanningMessage.session_id == session_id)
        )
        or 0
    )


def _session_out(
    row: RoutePlanningSession,
    *,
    ai_planning_enabled: bool,
    message_count: int = 0,
    message_limit: int = 0,
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
        message_count=message_count,
        message_limit=message_limit,
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
        if block_type == "catalog_match":
            return CatalogMatchBlockOut.model_validate(item)
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
