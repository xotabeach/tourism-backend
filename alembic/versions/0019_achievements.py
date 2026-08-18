"""Achievements catalog, starter grants, and achievement_unlocked notifications."""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

import sqlalchemy as sa
from alembic import op

revision: str = "0019_achievements"
down_revision: str | Sequence[str] | None = "0018_reviews_profile_like"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW_KINDS = (
    "kind IN ("
    "'route_review', "
    "'route_published', "
    "'route_rejected', "
    "'review_published', "
    "'review_rejected', "
    "'profile_like', "
    "'achievement_unlocked'"
    ")"
)
_OLD_KINDS = (
    "kind IN ("
    "'route_review', "
    "'route_published', "
    "'route_rejected', "
    "'review_published', "
    "'review_rejected', "
    "'profile_like'"
    ")"
)
_NEW_TARGET = "target_type IS NULL OR target_type IN ('route', 'user', 'achievement')"
_OLD_TARGET = "target_type IS NULL OR target_type IN ('route', 'user')"


def _aid(index: int) -> UUID:
    return UUID(f"00000000-0000-0000-0000-0000000003{index:02d}")


ACHIEVEMENTS: tuple[tuple[UUID, str, str, str, int], ...] = (
    (_aid(1), "marathoner", "Марафонец", "Пройти суммарно 48 км за неделю", 1),
    (_aid(2), "same-way", "Ты норм?", "Вернуться по тому же маршруту", 2),
    (_aid(3), "berlin", "Ура Советам", "Дойти пешком до Берлина", 3),
    (_aid(4), "sunrise", "Ранняя пташка", "Встретить рассвет на маршруте", 4),
    (_aid(5), "water", "К воде", "Пройти 3 приморских маршрута", 5),
    (_aid(6), "caves", "Подземный гость", "Посетить пещерный город", 6),
    (_aid(7), "photo", "Кадр дня", "Добавить 10 фото к остановкам", 7),
    (_aid(8), "night", "Ночной дозор", "Завершить маршрут после заката", 8),
    (_aid(9), "group", "Компания", "Пройти маршрут с друзьями", 9),
    (_aid(10), "season", "Все сезоны", "Пройти маршруты зимой и летом", 10),
    (_aid(11), "local", "Местный", "Отметить 20 мест Крыма", 11),
    (_aid(12), "guide", "Свой гид", "Прослушать 5 аудиогидов", 12),
    (_aid(13), "distance", "Сто км", "Набрать 100 км суммарно", 13),
    (_aid(14), "favorite", "Коллекционер", "Сохранить 15 маршрутов", 14),
    (_aid(15), "review", "Отзывчивый", "Оставить 5 отзывов", 15),
    (_aid(16), "swallow", "У гнезда", "Посетить Ласточкино гнездо", 16),
    (_aid(17), "fiolent", "На краю", "Дойти до мыса Фиолент", 17),
    (_aid(18), "ai-petri", "Выше облаков", "Подняться на Ай-Петри", 18),
    (_aid(19), "first-step", "Первые шаги", "Зарегистрироваться в КрымТрип", 19),
    (_aid(20), "social", "Душа компании", "Подписаться на 10 путешественников", 20),
    (_aid(21), "author", "Автор тропы", "Опубликовать свой маршрут", 21),
    (_aid(22), "bakhchisaray", "Ханский гость", "Побывать в Бахчисарае", 22),
    (_aid(23), "winter", "Зимний Крым", "Пройти маршрут в январе", 23),
    (_aid(24), "sea-breeze", "Морской бриз", "Пройти 5 маршрутов у моря", 24),
    (_aid(25), "photographer", "Летописец", "Сделать обложку маршрута", 25),
    (_aid(26), "yalta-lights", "Огни Ялты", "Завершить день в Ялте", 26),
    (_aid(27), "legend-path", "По следам легенд", "Пройти 3 сложных маршрута", 27),
    (_aid(28), "new-svet", "Новый Свет", "Пройти тропу к Царскому пляжу", 28),
)


def upgrade() -> None:
    achievements = op.create_table(
        "achievements",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=240), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_achievements"),
        sa.UniqueConstraint("slug", name="uq_achievements_slug"),
        sa.UniqueConstraint("sort_order", name="uq_achievements_sort_order"),
    )
    op.create_table(
        "user_achievements",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("achievement_id", sa.Uuid(), nullable=False),
        sa.Column("unlocked_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_user_achievements_user_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["achievement_id"],
            ["achievements.id"],
            name="fk_user_achievements_achievement_id_achievements",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "achievement_id", name="pk_user_achievements"),
    )
    op.create_index(
        "ix_user_achievements_achievement_id",
        "user_achievements",
        ["achievement_id"],
        unique=False,
    )
    op.bulk_insert(
        achievements,
        [
            {
                "id": achievement_id,
                "slug": slug,
                "title": title,
                "description": description,
                "sort_order": sort_order,
            }
            for achievement_id, slug, title, description, sort_order in ACHIEVEMENTS
        ],
    )

    op.drop_constraint("kind", "notifications", type_="check")
    op.create_check_constraint("kind", "notifications", _NEW_KINDS)
    op.drop_constraint("target_type", "notifications", type_="check")
    op.create_check_constraint("target_type", "notifications", _NEW_TARGET)

    _grant_starter_badges()
    _assign_default_profile_media()


def _grant_starter_badges() -> None:
    import random

    conn = op.get_bind()
    users = list(conn.execute(sa.text("SELECT id FROM users")).fetchall())
    achievement_ids = [row[0] for row in ACHIEVEMENTS]
    now = datetime.now(UTC)
    for (user_id,) in users:
        rng = random.Random(UUID(str(user_id)).int)
        count = rng.randint(5, min(15, len(achievement_ids)))
        picked = rng.sample(achievement_ids, count)
        for achievement_id in picked:
            conn.execute(
                sa.text(
                    "INSERT INTO user_achievements "
                    "(user_id, achievement_id, unlocked_at) "
                    "VALUES (:user_id, :achievement_id, :unlocked_at)"
                ),
                {
                    "user_id": user_id,
                    "achievement_id": achievement_id,
                    "unlocked_at": now,
                },
            )


def _assign_default_profile_media() -> None:
    conn = op.get_bind()
    covers = list(
        conn.execute(
            sa.text(
                "SELECT storage_key, public_path, content_type "
                "FROM media_attachments "
                "WHERE status = 'active' AND role = 'cover' "
                "AND entity_type IN ('route', 'place') "
                "LIMIT 40"
            )
        ).fetchall()
    )
    if not covers:
        return
    users = list(conn.execute(sa.text("SELECT id FROM users")).fetchall())
    now = datetime.now(UTC)
    for (user_id,) in users:
        uid = UUID(str(user_id))
        cover = covers[uid.int % len(covers)]
        avatar = covers[(uid.int // 7) % len(covers)]
        for role, source in (("cover", cover), ("avatar", avatar)):
            exists = conn.execute(
                sa.text(
                    "SELECT 1 FROM media_attachments "
                    "WHERE entity_type = 'user' AND entity_id = :user_id "
                    "AND role = :role AND status = 'active' LIMIT 1"
                ),
                {"user_id": user_id, "role": role},
            ).first()
            if exists is not None:
                continue
            conn.execute(
                sa.text(
                    "INSERT INTO media_attachments ("
                    "id, entity_type, entity_id, role, storage_key, public_path, "
                    "content_type, status, sort_order, created_at, updated_at, alt_text"
                    ") VALUES ("
                    ":id, 'user', :user_id, :role, :storage_key, :public_path, "
                    ":content_type, 'active', 0, :created_at, :updated_at, :alt_text"
                    ")"
                ),
                {
                    "id": uuid4(),
                    "user_id": user_id,
                    "role": role,
                    "storage_key": source[0],
                    "public_path": source[1],
                    "content_type": source[2],
                    "created_at": now,
                    "updated_at": now,
                    "alt_text": "Default profile cover"
                    if role == "cover"
                    else "Default profile avatar",
                },
            )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM notifications WHERE kind = 'achievement_unlocked'"))
    op.execute(
        sa.text(
            "UPDATE notifications SET target_type = NULL, target_id = NULL "
            "WHERE target_type = 'achievement'"
        )
    )
    op.drop_constraint("target_type", "notifications", type_="check")
    op.create_check_constraint("target_type", "notifications", _OLD_TARGET)
    op.drop_constraint("kind", "notifications", type_="check")
    op.create_check_constraint("kind", "notifications", _OLD_KINDS)

    op.execute(
        sa.text(
            "DELETE FROM media_attachments WHERE entity_type = 'user' "
            "AND alt_text IN ('Default profile cover', 'Default profile avatar')"
        )
    )
    op.drop_index("ix_user_achievements_achievement_id", table_name="user_achievements")
    op.drop_table("user_achievements")
    op.drop_table("achievements")
