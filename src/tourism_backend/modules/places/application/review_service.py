from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.api.errors import AppError
from tourism_backend.modules.identity.infrastructure.models import EXPERT_RANK_ID, TravelRank, User
from tourism_backend.modules.media.application import service as media_service
from tourism_backend.modules.media.infrastructure.models import MediaAttachment
from tourism_backend.modules.places.application.review_schemas import (
    MyPlaceReviewListOut,
    PlaceReviewCreateIn,
    PlaceReviewListOut,
    PlaceReviewMediaOut,
    PlaceReviewOut,
    PlaceReviewReplyOut,
)
from tourism_backend.modules.places.infrastructure.models import Place, PlaceReview
from tourism_backend.modules.routes.application.review_media import (
    SavedReviewImage,
    delete_review_image,
)

_DELETE_WINDOW = timedelta(hours=6)
_MAX_IMAGES = 6


async def _ensure_published_place(session: AsyncSession, place_id: UUID) -> Place:
    place = await session.scalar(
        select(Place).where(Place.id == place_id, Place.publication_status == "published")
    )
    if place is None:
        raise AppError(code="place_not_found", message="Place not found", status_code=404)
    return place


async def _rank_titles(session: AsyncSession, users: list[User]) -> dict[UUID, str]:
    if not users:
        return {}
    ranks = sorted(
        (await session.scalars(select(TravelRank).where(TravelRank.id != EXPERT_RANK_ID))).all(),
        key=lambda rank: rank.min_points,
        reverse=True,
    )
    result: dict[UUID, str] = {}
    for user in users:
        if user.is_expert:
            result[user.id] = "Эксперт"
            continue
        result[user.id] = next(
            (rank.title for rank in ranks if user.travel_points >= rank.min_points),
            "Новичок",
        )
    return result


async def _media(
    session: AsyncSession, review_ids: list[UUID]
) -> dict[UUID, list[PlaceReviewMediaOut]]:
    if not review_ids:
        return {}
    rows = list(
        (
            await session.scalars(
                select(MediaAttachment)
                .where(
                    MediaAttachment.entity_type == "place_review",
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
    result: dict[UUID, list[PlaceReviewMediaOut]] = {}
    for item in rows:
        result.setdefault(item.entity_id, []).append(
            PlaceReviewMediaOut(
                id=str(item.id),
                url=item.public_path,
                width=item.width,
                height=item.height,
                sort_order=item.sort_order,
            )
        )
    return result


async def _reply_context(
    session: AsyncSession, reviews: list[PlaceReview]
) -> dict[UUID, PlaceReviewReplyOut]:
    target_ids = list(
        {review.reply_to_review_id for review in reviews if review.reply_to_review_id is not None}
    )
    if not target_ids:
        return {}
    targets = list(
        (await session.scalars(select(PlaceReview).where(PlaceReview.id.in_(target_ids)))).all()
    )
    author_ids = list({target.author_user_id for target in targets})
    authors = {
        user.id: user
        for user in (await session.scalars(select(User).where(User.id.in_(author_ids)))).all()
    }
    targets_by_id = {target.id: target for target in targets}
    result: dict[UUID, PlaceReviewReplyOut] = {}
    for review in reviews:
        reply_to_id = review.reply_to_review_id
        if reply_to_id is None:
            continue
        target = targets_by_id.get(reply_to_id)
        if target is None:
            continue
        author = authors.get(target.author_user_id)
        result[review.id] = PlaceReviewReplyOut(
            review_id=str(target.id),
            author_user_id=str(target.author_user_id),
            author_display_name=author.display_name if author else "Путешественник",
            body=target.body,
        )
    return result


async def _out_many(session: AsyncSession, reviews: list[PlaceReview]) -> list[PlaceReviewOut]:
    author_ids = list({review.author_user_id for review in reviews})
    users = {
        user.id: user
        for user in (await session.scalars(select(User).where(User.id.in_(author_ids)))).all()
    }
    ranks = await _rank_titles(session, list(users.values()))
    avatars = await media_service.resolve_urls(
        session,
        entity_type="user",
        entity_ids=author_ids,
        role="avatar",
    )
    media = await _media(session, [review.id for review in reviews])
    replies = await _reply_context(session, reviews)
    return [
        PlaceReviewOut(
            id=str(review.id),
            place_id=str(review.place_id),
            author_user_id=str(review.author_user_id),
            author_display_name=(
                users[review.author_user_id].display_name
                if review.author_user_id in users
                else "Путешественник"
            ),
            author_rank_title=ranks.get(review.author_user_id, "Новичок"),
            author_avatar_url=avatars.get(review.author_user_id),
            body=review.body,
            rating=review.rating,
            status=review.status,  # type: ignore[arg-type]
            created_at=review.created_at,
            media=media.get(review.id, []),
            reply_to=replies.get(review.id),
        )
        for review in reviews
    ]


async def list_published_reviews(
    session: AsyncSession,
    *,
    place_id: UUID,
    limit: int,
    offset: int,
) -> PlaceReviewListOut:
    await _ensure_published_place(session, place_id)
    published = (PlaceReview.place_id == place_id, PlaceReview.status == "published")
    roots = (*published, PlaceReview.reply_to_review_id.is_(None))
    total = int(
        await session.scalar(select(func.count()).select_from(PlaceReview).where(*published)) or 0
    )
    rating_count = int(
        await session.scalar(select(func.count()).select_from(PlaceReview).where(*roots)) or 0
    )
    average = await session.scalar(select(func.avg(PlaceReview.rating)).where(*roots))
    rows = list(
        (
            await session.scalars(
                select(PlaceReview)
                .where(*published)
                .order_by(PlaceReview.created_at.desc(), PlaceReview.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    return PlaceReviewListOut(
        items=await _out_many(session, rows),
        total=total,
        average_rating=round(float(average), 1) if average is not None else None,
        rating_count=rating_count,
    )


async def upsert_review(
    session: AsyncSession,
    *,
    place_id: UUID,
    author_user_id: UUID,
    payload: PlaceReviewCreateIn,
) -> PlaceReviewOut:
    await _ensure_published_place(session, place_id)
    target: PlaceReview | None = None
    if payload.reply_to_review_id is not None:
        target = await session.scalar(
            select(PlaceReview).where(
                PlaceReview.id == payload.reply_to_review_id,
                PlaceReview.place_id == place_id,
                PlaceReview.status == "published",
            )
        )
        if target is None:
            raise AppError(
                code="review_reply_target_not_found",
                message="Отзыв, на который вы отвечаете, больше недоступен",
                status_code=404,
            )
    pending = await session.scalar(
        select(PlaceReview)
        .where(
            PlaceReview.place_id == place_id,
            PlaceReview.author_user_id == author_user_id,
            PlaceReview.status == "pending_review",
            PlaceReview.reply_to_review_id == payload.reply_to_review_id,
        )
        .order_by(PlaceReview.updated_at.desc(), PlaceReview.id.desc())
        .limit(1)
    )
    now = datetime.now(UTC)
    if pending is None:
        review = PlaceReview(
            id=uuid4(),
            place_id=place_id,
            author_user_id=author_user_id,
            reply_to_review_id=target.id if target else None,
            body=payload.body,
            rating=payload.rating,
            status="pending_review",
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
    return (await _out_many(session, [review]))[0]


async def ensure_own_pending_review(
    session: AsyncSession,
    *,
    place_id: UUID,
    review_id: UUID,
    author_user_id: UUID,
) -> PlaceReview:
    review = await session.scalar(
        select(PlaceReview).where(
            PlaceReview.id == review_id,
            PlaceReview.place_id == place_id,
            PlaceReview.author_user_id == author_user_id,
            PlaceReview.status == "pending_review",
        )
    )
    if review is None:
        raise AppError(
            code="review_media_locked",
            message="Фотографии опубликованного отзыва нельзя изменять",
            status_code=409,
        )
    return review


async def add_review_image(
    session: AsyncSession,
    *,
    place_id: UUID,
    review_id: UUID,
    author_user_id: UUID,
    position: int,
    saved: SavedReviewImage,
) -> PlaceReviewMediaOut:
    await ensure_own_pending_review(
        session,
        place_id=place_id,
        review_id=review_id,
        author_user_id=author_user_id,
    )
    active = (
        MediaAttachment.entity_type == "place_review",
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
        return PlaceReviewMediaOut(
            id=str(duplicate.id),
            url=duplicate.public_path,
            width=duplicate.width,
            height=duplicate.height,
            sort_order=duplicate.sort_order,
        )
    count = int(
        await session.scalar(select(func.count()).select_from(MediaAttachment).where(*active)) or 0
    )
    if count >= _MAX_IMAGES:
        delete_review_image(saved.storage_key, review_id=review_id)
        raise AppError(
            code="review_media_limit",
            message=f"К отзыву можно прикрепить не больше {_MAX_IMAGES} фото",
            status_code=409,
        )
    now = datetime.now(UTC)
    attachment = MediaAttachment(
        id=uuid4(),
        entity_type="place_review",
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
    return PlaceReviewMediaOut(
        id=str(attachment.id),
        url=attachment.public_path,
        width=attachment.width,
        height=attachment.height,
        sort_order=attachment.sort_order,
    )


async def delete_own_review(
    session: AsyncSession,
    *,
    place_id: UUID,
    review_id: UUID,
    author_user_id: UUID,
) -> None:
    review = await session.scalar(
        select(PlaceReview).where(
            PlaceReview.id == review_id,
            PlaceReview.place_id == place_id,
            PlaceReview.author_user_id == author_user_id,
            PlaceReview.status != "deleted",
        )
    )
    if review is None:
        raise AppError(code="review_not_found", message="Review not found", status_code=404)
    created = (
        review.created_at.replace(tzinfo=UTC)
        if review.created_at.tzinfo is None
        else review.created_at
    )
    if datetime.now(UTC) - created > _DELETE_WINDOW:
        raise AppError(
            code="review_delete_window_expired",
            message="Удалить отзыв можно только в течение 6 часов после публикации",
            status_code=409,
        )
    now = datetime.now(UTC)
    review.status = "deleted"
    review.updated_at = now
    attachments = list(
        (
            await session.scalars(
                select(MediaAttachment).where(
                    MediaAttachment.entity_type == "place_review",
                    MediaAttachment.entity_id == review_id,
                    MediaAttachment.status == "active",
                )
            )
        ).all()
    )
    await session.execute(
        update(MediaAttachment)
        .where(
            MediaAttachment.entity_type == "place_review",
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
) -> MyPlaceReviewListOut:
    rows = list(
        (
            await session.scalars(
                select(PlaceReview)
                .where(
                    PlaceReview.author_user_id == author_user_id,
                    PlaceReview.status != "deleted",
                )
                .order_by(PlaceReview.updated_at.desc(), PlaceReview.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    return MyPlaceReviewListOut(items=await _out_many(session, rows))


async def set_review_status(
    session: AsyncSession,
    *,
    review_ids: list[UUID],
    status: str,
) -> int:
    if status not in {"published", "rejected", "deleted"}:
        raise AppError(code="validation_error", message="Invalid status", status_code=400)
    rows = list(
        (await session.scalars(select(PlaceReview).where(PlaceReview.id.in_(review_ids)))).all()
    )
    now = datetime.now(UTC)
    changed = 0
    for review in rows:
        if status in {"published", "rejected"} and review.status != "pending_review":
            continue
        review.status = status
        review.moderated_at = now
        review.updated_at = now
        changed += 1
    return changed
