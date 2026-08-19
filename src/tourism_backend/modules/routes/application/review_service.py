from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.api.errors import AppError
from tourism_backend.config import get_settings
from tourism_backend.modules.identity.infrastructure.models import TravelRank, User
from tourism_backend.modules.media.application import service as media_service
from tourism_backend.modules.media.infrastructure.models import MediaAttachment
from tourism_backend.modules.notifications.application import service as notifications_service
from tourism_backend.modules.routes.application.review_media import (
    SavedReviewImage,
    delete_review_image,
)
from tourism_backend.modules.routes.application.review_schemas import (
    MyRouteReviewListOut,
    RouteReviewCreateIn,
    RouteReviewListOut,
    RouteReviewMediaOut,
    RouteReviewOut,
    RouteReviewReplyOut,
)
from tourism_backend.modules.routes.infrastructure.models import Route, RouteReview

_PUBLIC_CATALOG = (
    Route.visibility == "public",
    Route.lifecycle_status == "active",
    Route.publication_status == "published",
)
_REVIEW_DELETE_WINDOW = timedelta(hours=6)
_MAX_REVIEW_IMAGES = 6


async def _ensure_public_route(session: AsyncSession, route_id: UUID) -> Route:
    route = await session.scalar(select(Route).where(Route.id == route_id, *_PUBLIC_CATALOG))
    if route is None:
        raise AppError(code="route_not_found", message="Route not found", status_code=404)
    return route


async def _ensure_reviewable_route(session: AsyncSession, route_id: UUID) -> Route:
    """Like public-catalog lookup, but distinguish missing vs unpublished."""
    route = await session.get(Route, route_id)
    if route is None:
        raise AppError(code="route_not_found", message="Route not found", status_code=404)
    if (
        route.visibility != "public"
        or route.lifecycle_status != "active"
        or route.publication_status != "published"
    ):
        raise AppError(
            code="route_not_published",
            message="Нельзя оставлять отзывы на неопубликованные маршруты",
            status_code=409,
        )
    return route


async def _rank_titles(
    session: AsyncSession,
    users: list[User],
) -> dict[UUID, str]:
    if not users:
        return {}
    points = {user.id: user.travel_points for user in users}
    ranks = list((await session.scalars(select(TravelRank))).all())
    ranks_sorted = sorted(ranks, key=lambda r: r.min_points, reverse=True)
    out: dict[UUID, str] = {}
    for user_id, tp in points.items():
        title = "Новичок"
        for rank in ranks_sorted:
            if tp >= rank.min_points:
                title = rank.title
                break
        out[user_id] = title
    return out


async def _review_out(
    session: AsyncSession,
    review: RouteReview,
    *,
    users: dict[UUID, User],
    rank_titles: dict[UUID, str],
    avatars: dict[UUID, str],
    media: dict[UUID, list[RouteReviewMediaOut]],
    replies: dict[UUID, RouteReviewReplyOut],
) -> RouteReviewOut:
    author = users.get(review.author_user_id)
    return RouteReviewOut(
        id=str(review.id),
        route_id=str(review.route_id),
        author_user_id=str(review.author_user_id),
        author_display_name=author.display_name if author is not None else "Путешественник",
        author_rank_title=rank_titles.get(review.author_user_id, "Новичок"),
        author_avatar_url=avatars.get(review.author_user_id),
        body=review.body,
        rating=int(review.rating),
        status=review.status,  # type: ignore[arg-type]
        created_at=review.created_at,
        media=media.get(review.id, []),
        reply_to=replies.get(review.id),
    )


async def _reply_context(
    session: AsyncSession,
    reviews: list[RouteReview],
) -> dict[UUID, RouteReviewReplyOut]:
    """Resolve reply previews in two bounded queries, avoiding per-card lookups."""
    target_ids = list(
        {review.reply_to_review_id for review in reviews if review.reply_to_review_id is not None}
    )
    if not target_ids:
        return {}
    targets = list(
        (await session.scalars(select(RouteReview).where(RouteReview.id.in_(target_ids)))).all()
    )
    author_ids = list({target.author_user_id for target in targets})
    authors = {
        user.id: user
        for user in (await session.scalars(select(User).where(User.id.in_(author_ids)))).all()
    }
    by_id = {target.id: target for target in targets}
    result: dict[UUID, RouteReviewReplyOut] = {}
    for review in reviews:
        reply_to_review_id = review.reply_to_review_id
        if reply_to_review_id is None:
            continue
        target = by_id.get(reply_to_review_id)
        if target is None:
            continue
        author = authors.get(target.author_user_id)
        result[review.id] = RouteReviewReplyOut(
            review_id=str(target.id),
            author_user_id=str(target.author_user_id),
            author_display_name=(author.display_name if author is not None else "Путешественник"),
            body=target.body,
        )
    return result


async def _review_media(
    session: AsyncSession,
    review_ids: list[UUID],
) -> dict[UUID, list[RouteReviewMediaOut]]:
    if not review_ids:
        return {}
    rows = list(
        (
            await session.scalars(
                select(MediaAttachment)
                .where(
                    MediaAttachment.entity_type == "review",
                    MediaAttachment.entity_id.in_(review_ids),
                    MediaAttachment.role == "gallery",
                    MediaAttachment.status == "active",
                )
                .order_by(
                    MediaAttachment.entity_id,
                    MediaAttachment.sort_order,
                    MediaAttachment.created_at,
                    MediaAttachment.id,
                )
            )
        ).all()
    )
    result: dict[UUID, list[RouteReviewMediaOut]] = {}
    for item in rows:
        result.setdefault(item.entity_id, []).append(
            RouteReviewMediaOut(
                id=str(item.id),
                url=item.public_path,
                width=item.width,
                height=item.height,
                sort_order=item.sort_order,
            )
        )
    return result


async def ensure_own_editable_review(
    session: AsyncSession,
    *,
    route_id: UUID,
    review_id: UUID,
    author_user_id: UUID,
    expired_code: str = "review_edit_window_expired",
    expired_message: str = "Изменять фото отзыва можно только в течение 6 часов",
) -> RouteReview:
    review = await session.scalar(
        select(RouteReview).where(
            RouteReview.id == review_id,
            RouteReview.route_id == route_id,
            RouteReview.author_user_id == author_user_id,
            RouteReview.status != "deleted",
        )
    )
    if review is None:
        raise AppError(code="review_not_found", message="Review not found", status_code=404)
    created = review.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    if datetime.now(UTC) - created > _REVIEW_DELETE_WINDOW:
        raise AppError(
            code=expired_code,
            message=expired_message,
            status_code=409,
        )
    return review


async def ensure_own_mutable_review_media(
    session: AsyncSession,
    *,
    route_id: UUID,
    review_id: UUID,
    author_user_id: UUID,
) -> RouteReview:
    review = await ensure_own_editable_review(
        session,
        route_id=route_id,
        review_id=review_id,
        author_user_id=author_user_id,
    )
    if review.status != "pending_review":
        raise AppError(
            code="review_media_locked",
            message="Фотографии опубликованного отзыва нельзя изменять",
            status_code=409,
        )
    return review


async def list_published_reviews(
    session: AsyncSession,
    *,
    route_id: UUID,
    limit: int,
    offset: int,
) -> RouteReviewListOut:
    await _ensure_public_route(session, route_id)
    published = (
        RouteReview.route_id == route_id,
        RouteReview.status == "published",
    )
    total = int(
        await session.scalar(select(func.count()).select_from(RouteReview).where(*published)) or 0
    )
    root_ratings = (*published, RouteReview.reply_to_review_id.is_(None))
    rating_count = int(
        await session.scalar(select(func.count()).select_from(RouteReview).where(*root_ratings))
        or 0
    )
    avg = await session.scalar(select(func.avg(RouteReview.rating)).where(*root_ratings))
    rows = list(
        (
            await session.scalars(
                select(RouteReview)
                .where(*published)
                .order_by(RouteReview.created_at.desc(), RouteReview.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    author_ids = [row.author_user_id for row in rows]
    users = (
        {
            user.id: user
            for user in (await session.scalars(select(User).where(User.id.in_(author_ids)))).all()
        }
        if author_ids
        else {}
    )
    rank_titles = await _rank_titles(session, list(users.values()))
    avatars = await media_service.resolve_urls(
        session,
        entity_type="user",
        entity_ids=author_ids,
        role="avatar",
    )
    review_media = await _review_media(session, [row.id for row in rows])
    replies = await _reply_context(session, rows)
    items = [
        await _review_out(
            session,
            row,
            users=users,
            rank_titles=rank_titles,
            avatars=avatars,
            media=review_media,
            replies=replies,
        )
        for row in rows
    ]
    average = round(float(avg), 1) if avg is not None and rating_count > 0 else None
    return RouteReviewListOut(
        items=items,
        total=total,
        average_rating=average,
        rating_count=rating_count,
    )


async def upsert_review(
    session: AsyncSession,
    *,
    route_id: UUID,
    author_user_id: UUID,
    payload: RouteReviewCreateIn,
) -> RouteReviewOut:
    """Create a new pending review, or update the author's existing pending one.

    Published / rejected reviews are never overwritten — a new pending row is
    created instead so authors can leave multiple comments over time.
    """
    await _ensure_reviewable_route(session, route_id)
    reply_target: RouteReview | None = None
    if payload.reply_to_review_id is not None:
        reply_target = await session.scalar(
            select(RouteReview).where(
                RouteReview.id == payload.reply_to_review_id,
                RouteReview.route_id == route_id,
                RouteReview.status == "published",
            )
        )
        if reply_target is None:
            raise AppError(
                code="review_reply_target_not_found",
                message="Отзыв, на который вы отвечаете, больше недоступен",
                status_code=404,
            )
    # Only reuse an existing pending row. Published/rejected rows are left intact
    # so a second comment creates a new moderation item.
    pending = await session.scalar(
        select(RouteReview)
        .where(
            RouteReview.route_id == route_id,
            RouteReview.author_user_id == author_user_id,
            RouteReview.status == "pending_review",
            RouteReview.reply_to_review_id == payload.reply_to_review_id,
        )
        .order_by(RouteReview.updated_at.desc(), RouteReview.id.desc())
        .limit(1)
    )
    now = datetime.now(UTC)
    if pending is None:
        review = RouteReview(
            id=uuid4(),
            route_id=route_id,
            author_user_id=author_user_id,
            body=payload.body,
            rating=payload.rating,
            status="pending_review",
            reply_to_review_id=(reply_target.id if reply_target is not None else None),
            created_at=now,
            updated_at=now,
        )
        session.add(review)
    else:
        review = pending
        review.body = payload.body
        review.rating = payload.rating
        review.moderator_note = None
        review.moderated_at = None
        review.updated_at = now

    await session.commit()
    await session.refresh(review)
    author = await session.get(User, author_user_id)
    users = {author_user_id: author} if author is not None else {}
    rank_titles = await _rank_titles(session, list(users.values()))
    avatars = await media_service.resolve_urls(
        session,
        entity_type="user",
        entity_ids=[author_user_id],
        role="avatar",
    )
    review_media = await _review_media(session, [review.id])
    replies = await _reply_context(session, [review])
    return await _review_out(
        session,
        review,
        users=users,
        rank_titles=rank_titles,
        avatars=avatars,
        media=review_media,
        replies=replies,
    )


async def add_review_image(
    session: AsyncSession,
    *,
    route_id: UUID,
    review_id: UUID,
    author_user_id: UUID,
    position: int,
    saved: SavedReviewImage,
) -> RouteReviewMediaOut:
    await ensure_own_mutable_review_media(
        session,
        route_id=route_id,
        review_id=review_id,
        author_user_id=author_user_id,
    )
    active = (
        MediaAttachment.entity_type == "review",
        MediaAttachment.entity_id == review_id,
        MediaAttachment.role == "gallery",
        MediaAttachment.status == "active",
    )
    duplicate = await session.scalar(
        select(MediaAttachment).where(
            *active,
            MediaAttachment.checksum_sha256 == saved.checksum_sha256,
        )
    )
    if duplicate is not None:
        delete_review_image(saved.storage_key, review_id=review_id)
        return RouteReviewMediaOut(
            id=str(duplicate.id),
            url=duplicate.public_path,
            width=duplicate.width,
            height=duplicate.height,
            sort_order=duplicate.sort_order,
        )
    count = int(
        await session.scalar(select(func.count()).select_from(MediaAttachment).where(*active)) or 0
    )
    if count >= _MAX_REVIEW_IMAGES:
        delete_review_image(saved.storage_key, review_id=review_id)
        raise AppError(
            code="review_media_limit",
            message=f"К отзыву можно прикрепить не больше {_MAX_REVIEW_IMAGES} фото",
            status_code=409,
        )
    now = datetime.now(UTC)
    attachment = MediaAttachment(
        id=uuid4(),
        entity_type="review",
        entity_id=review_id,
        role="gallery",
        storage_key=saved.storage_key,
        public_path=saved.public_path,
        content_type=saved.content_type,
        byte_size=saved.byte_size,
        width=saved.width,
        height=saved.height,
        checksum_sha256=saved.checksum_sha256,
        status="active",
        uploaded_by_user_id=author_user_id,
        sort_order=position,
        created_at=now,
        updated_at=now,
    )
    session.add(attachment)
    await session.commit()
    return RouteReviewMediaOut(
        id=str(attachment.id),
        url=attachment.public_path,
        width=attachment.width,
        height=attachment.height,
        sort_order=attachment.sort_order,
    )


async def delete_review_image_attachment(
    session: AsyncSession,
    *,
    route_id: UUID,
    review_id: UUID,
    media_id: UUID,
    author_user_id: UUID,
) -> None:
    await ensure_own_mutable_review_media(
        session,
        route_id=route_id,
        review_id=review_id,
        author_user_id=author_user_id,
    )
    attachment = await session.scalar(
        select(MediaAttachment).where(
            MediaAttachment.id == media_id,
            MediaAttachment.entity_type == "review",
            MediaAttachment.entity_id == review_id,
            MediaAttachment.role == "gallery",
            MediaAttachment.status == "active",
            MediaAttachment.uploaded_by_user_id == author_user_id,
        )
    )
    if attachment is None:
        raise AppError(code="review_media_not_found", message="Photo not found", status_code=404)
    attachment.status = "archived"
    attachment.updated_at = datetime.now(UTC)
    await session.commit()
    delete_review_image(attachment.storage_key, review_id=review_id)


async def delete_own_review(
    session: AsyncSession,
    *,
    route_id: UUID,
    review_id: UUID,
    author_user_id: UUID,
) -> None:
    """Soft-delete own review within 6 hours of creation."""
    review = await ensure_own_editable_review(
        session,
        route_id=route_id,
        review_id=review_id,
        author_user_id=author_user_id,
        expired_code="review_delete_window_expired",
        expired_message="Удалить отзыв можно только в течение 6 часов после публикации",
    )
    now = datetime.now(UTC)

    review.status = "deleted"
    review.updated_at = now
    attachments = list(
        (
            await session.scalars(
                select(MediaAttachment).where(
                    MediaAttachment.entity_type == "review",
                    MediaAttachment.entity_id == review_id,
                    MediaAttachment.status == "active",
                )
            )
        ).all()
    )
    await session.execute(
        update(MediaAttachment)
        .where(
            MediaAttachment.entity_type == "review",
            MediaAttachment.entity_id == review_id,
            MediaAttachment.status == "active",
        )
        .values(status="archived", updated_at=now)
    )
    await session.commit()
    for attachment in attachments:
        delete_review_image(attachment.storage_key, review_id=review_id)


async def list_my_reviews(
    session: AsyncSession,
    *,
    author_user_id: UUID,
    limit: int,
    offset: int,
) -> MyRouteReviewListOut:
    rows = list(
        (
            await session.scalars(
                select(RouteReview)
                .where(
                    RouteReview.author_user_id == author_user_id,
                    RouteReview.status != "deleted",
                )
                .order_by(RouteReview.updated_at.desc(), RouteReview.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    author = await session.get(User, author_user_id)
    users = {author_user_id: author} if author is not None else {}
    rank_titles = await _rank_titles(session, list(users.values()))
    avatars = await media_service.resolve_urls(
        session,
        entity_type="user",
        entity_ids=[author_user_id],
        role="avatar",
    )
    review_media = await _review_media(session, [row.id for row in rows])
    replies = await _reply_context(session, rows)
    items = [
        await _review_out(
            session,
            row,
            users=users,
            rank_titles=rank_titles,
            avatars=avatars,
            media=review_media,
            replies=replies,
        )
        for row in rows
    ]
    return MyRouteReviewListOut(items=items)


async def set_review_status(
    session: AsyncSession,
    *,
    review_ids: list[UUID],
    status: str,
) -> int:
    if status not in {"published", "rejected", "deleted"}:
        raise AppError(code="validation_error", message="Invalid status", status_code=400)
    if not review_ids:
        return 0
    rows = list(
        (await session.scalars(select(RouteReview).where(RouteReview.id.in_(review_ids)))).all()
    )
    now = datetime.now(UTC)
    changed = 0
    for review in rows:
        if status in {"published", "rejected"} and review.status != "pending_review":
            continue
        previous = review.status
        review.status = status
        review.moderated_at = now
        review.updated_at = now
        changed += 1
        if previous != "pending_review":
            continue
        route = await session.get(Route, review.route_id)
        if route is None:
            continue
        if status == "published":
            if review.reply_to_review_id is not None:
                reply_target = await session.get(RouteReview, review.reply_to_review_id)
                if reply_target is not None:
                    reply_notif = await notifications_service.create_review_reply_notification(
                        session,
                        recipient_user_id=reply_target.author_user_id,
                        actor_user_id=review.author_user_id,
                        route_id=route.id,
                        route_name=route.name,
                    )
                    if reply_notif is not None:
                        await notifications_service.maybe_push_notification(
                            session,
                            get_settings(),
                            user_id=reply_target.author_user_id,
                            kind=reply_notif.kind,
                            title=reply_notif.title,
                            body=reply_notif.body,
                            target_type="route",
                            target_id=route.id,
                        )
            if route.owner_user_id is not None:
                owner_notif = await notifications_service.create_route_review_notification(
                    session,
                    owner_user_id=route.owner_user_id,
                    actor_user_id=review.author_user_id,
                    route_id=route.id,
                    route_name=route.name,
                )
                if owner_notif is not None:
                    await notifications_service.maybe_push_notification(
                        session,
                        get_settings(),
                        user_id=route.owner_user_id,
                        kind=owner_notif.kind,
                        title=owner_notif.title,
                        body=owner_notif.body,
                        target_type="route",
                        target_id=route.id,
                    )
            author_notif = await notifications_service.create_review_moderation_notification(
                session,
                author_user_id=review.author_user_id,
                route_id=route.id,
                route_name=route.name,
                approved=True,
            )
            await notifications_service.maybe_push_notification(
                session,
                get_settings(),
                user_id=review.author_user_id,
                kind=author_notif.kind,
                title=author_notif.title,
                body=author_notif.body,
                target_type="route",
                target_id=route.id,
            )
        elif status == "rejected":
            author_notif = await notifications_service.create_review_moderation_notification(
                session,
                author_user_id=review.author_user_id,
                route_id=route.id,
                route_name=route.name,
                approved=False,
            )
            await notifications_service.maybe_push_notification(
                session,
                get_settings(),
                user_id=review.author_user_id,
                kind=author_notif.kind,
                title=author_notif.title,
                body=author_notif.body,
                target_type="route",
                target_id=route.id,
            )
    return changed
