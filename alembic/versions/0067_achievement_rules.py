"""Achievement grant provenance, celebration and operator audit (BACKEND-8)."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0067_achievement_rules"
down_revision: str | Sequence[str] | None = "0066_uncomplete_stop"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "user_achievements",
        sa.Column("source", sa.String(16), nullable=False, server_default="rule"),
    )
    op.add_column("user_achievements", sa.Column("reason", sa.Text(), nullable=True))
    op.add_column("user_achievements", sa.Column("granted_by_admin_id", sa.UUID(), nullable=True))
    op.add_column(
        "user_achievements", sa.Column("celebrated_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_foreign_key(
        None,
        "user_achievements",
        "admin_principals",
        ["granted_by_admin_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "source", "user_achievements", "source IN ('rule', 'backfill', 'operator')"
    )
    op.create_check_constraint(
        "operator_reason",
        "user_achievements",
        "source <> 'operator' OR (reason IS NOT NULL AND length(trim(reason)) > 0)",
    )
    op.add_column(
        "route_execution_stops", sa.Column("device_distance_m", sa.Integer(), nullable=True)
    )
    op.create_table(
        "achievement_admin_actions",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "achievement_id",
            sa.UUID(),
            sa.ForeignKey("achievements.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "admin_id",
            sa.UUID(),
            sa.ForeignKey("admin_principals.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("action IN ('grant', 'revoke')", name="action"),
        sa.CheckConstraint("length(trim(reason)) > 0", name="reason_required"),
    )
    op.create_index(
        "ix_achievement_admin_actions_user_id", "achievement_admin_actions", ["user_id"]
    )
    # Existing random grants are removed only by the separately approved backfill.
    # Mark them seen so deploying this schema does not celebrate old random badges.
    op.execute("UPDATE user_achievements SET celebrated_at = unlocked_at")

    connection = op.get_bind()
    connection.execute(
        sa.text("UPDATE achievements SET how_to_earn = :text WHERE slug = :slug"),
        {"text": "Пройти 48 км за 7 суток", "slug": "marathoner"},
    )
    connection.execute(
        sa.text("UPDATE achievements SET how_to_earn = :text WHERE slug = :slug"),
        {"text": "Дважды пройти один и тот же маршрут", "slug": "same-way"},
    )
    connection.execute(
        sa.text("UPDATE achievements SET how_to_earn = :text WHERE slug = :slug"),
        {"text": "Пройти суммарно 2 000 км", "slug": "berlin"},
    )
    connection.execute(
        sa.text("UPDATE achievements SET how_to_earn = :text WHERE slug = :slug"),
        {"text": "Отметить точку рядом с собой в первый час после восхода", "slug": "sunrise"},
    )
    connection.execute(
        sa.text("UPDATE achievements SET how_to_earn = :text WHERE slug = :slug"),
        {"text": "Пройти 3 маршрута с точкой категории «Пляж»", "slug": "water"},
    )
    connection.execute(
        sa.text("UPDATE achievements SET how_to_earn = :text WHERE slug = :slug"),
        {
            "text": "Отметить точку у Чуфут-Кале, Мангупа, Эски-Кермена, Тепе-Кермена или Баклы, находясь рядом",
            "slug": "caves",
        },
    )
    connection.execute(
        sa.text("UPDATE achievements SET how_to_earn = :text WHERE slug = :slug"),
        {"text": "Опубликовать 10 фото в своих отзывах о местах", "slug": "photo"},
    )
    connection.execute(
        sa.text("UPDATE achievements SET how_to_earn = :text WHERE slug = :slug"),
        {"text": "Завершить маршрут после заката у последней отмеченной точки", "slug": "night"},
    )
    connection.execute(
        sa.text("UPDATE achievements SET how_to_earn = :text WHERE slug = :slug"),
        {"text": "Совместные прохождения появятся позже", "slug": "group"},
    )
    connection.execute(
        sa.text("UPDATE achievements SET how_to_earn = :text WHERE slug = :slug"),
        {"text": "Пройти маршруты зимой и летом", "slug": "season"},
    )
    connection.execute(
        sa.text("UPDATE achievements SET how_to_earn = :text WHERE slug = :slug"),
        {"text": "Отметить рядом с собой 20 разных мест в завершённых маршрутах", "slug": "local"},
    )
    connection.execute(
        sa.text("UPDATE achievements SET how_to_earn = :text WHERE slug = :slug"),
        {"text": "Учёт прослушивания аудиогидов появится позже", "slug": "guide"},
    )
    connection.execute(
        sa.text("UPDATE achievements SET how_to_earn = :text WHERE slug = :slug"),
        {"text": "Пройти суммарно 100 км", "slug": "distance"},
    )
    connection.execute(
        sa.text("UPDATE achievements SET how_to_earn = :text WHERE slug = :slug"),
        {"text": "Сохранить одновременно 15 маршрутов в избранном", "slug": "favorite"},
    )
    connection.execute(
        sa.text("UPDATE achievements SET how_to_earn = :text WHERE slug = :slug"),
        {"text": "Опубликовать 5 отзывов о маршрутах и местах", "slug": "review"},
    )
    connection.execute(
        sa.text("UPDATE achievements SET how_to_earn = :text WHERE slug = :slug"),
        {"text": "Отметить точку у Ласточкина гнезда, находясь рядом", "slug": "swallow"},
    )
    connection.execute(
        sa.text("UPDATE achievements SET how_to_earn = :text WHERE slug = :slug"),
        {"text": "Отметить точку у мыса Фиолент, находясь рядом", "slug": "fiolent"},
    )
    connection.execute(
        sa.text("UPDATE achievements SET how_to_earn = :text WHERE slug = :slug"),
        {"text": "Отметить точку у Ай-Петри, находясь рядом", "slug": "ai-petri"},
    )
    connection.execute(
        sa.text("UPDATE achievements SET how_to_earn = :text WHERE slug = :slug"),
        {"text": "Завершить первое прохождение маршрута", "slug": "first-step"},
    )
    connection.execute(
        sa.text("UPDATE achievements SET how_to_earn = :text WHERE slug = :slug"),
        {"text": "Подписаться на 10 путешественников", "slug": "social"},
    )
    connection.execute(
        sa.text("UPDATE achievements SET how_to_earn = :text WHERE slug = :slug"),
        {"text": "Опубликовать свой общедоступный маршрут после модерации", "slug": "author"},
    )
    connection.execute(
        sa.text("UPDATE achievements SET how_to_earn = :text WHERE slug = :slug"),
        {"text": "Отметить место в Бахчисарае, находясь рядом", "slug": "bakhchisaray"},
    )
    connection.execute(
        sa.text("UPDATE achievements SET how_to_earn = :text WHERE slug = :slug"),
        {"text": "Завершить маршрут в январе", "slug": "winter"},
    )
    connection.execute(
        sa.text("UPDATE achievements SET how_to_earn = :text WHERE slug = :slug"),
        {"text": "Пройти 5 маршрутов с точкой категории «Пляж»", "slug": "sea-breeze"},
    )
    connection.execute(
        sa.text("UPDATE achievements SET how_to_earn = :text WHERE slug = :slug"),
        {
            "text": "Опубликовать свой маршрут с собственной загруженной обложкой",
            "slug": "photographer",
        },
    )
    connection.execute(
        sa.text("UPDATE achievements SET how_to_earn = :text WHERE slug = :slug"),
        {
            "text": "Завершить маршрут после 18:00, отметив последнюю точку рядом с собой в Ялте",
            "slug": "yalta-lights",
        },
    )
    connection.execute(
        sa.text("UPDATE achievements SET how_to_earn = :text WHERE slug = :slug"),
        {"text": "Пройти маршрут сложности «очень сложный»", "slug": "legend-path"},
    )
    connection.execute(
        sa.text("UPDATE achievements SET how_to_earn = :text WHERE slug = :slug"),
        {
            "text": "Отметить точку у Тропы Голицына или Царского пляжа, находясь рядом",
            "slug": "new-svet",
        },
    )
    connection.execute(
        sa.text(
            "INSERT INTO achievements (id, slug, title, description, how_to_earn, icon_slug, sort_order) VALUES (:id, :slug, :title, :description, :how_to_earn, :icon_slug, :sort_order) ON CONFLICT (slug) DO NOTHING"
        ),
        {
            "id": "7bf159e3-c720-5979-8742-438aa290fb72",
            "slug": "veteran",
            "title": "Бывалый",
            "description": "Завершить 10 прохождений",
            "how_to_earn": "Завершить 10 прохождений",
            "icon_slug": "veteran",
            "sort_order": 29,
        },
    )
    connection.execute(
        sa.text(
            "INSERT INTO achievements (id, slug, title, description, how_to_earn, icon_slug, sort_order) VALUES (:id, :slug, :title, :description, :how_to_earn, :icon_slug, :sort_order) ON CONFLICT (slug) DO NOTHING"
        ),
        {
            "id": "4c99d6e5-8636-56a8-bcda-d26aaefef7f1",
            "slug": "pen",
            "title": "Перо",
            "description": "Опубликовать свою статью",
            "how_to_earn": "Опубликовать свою статью",
            "icon_slug": "pen",
            "sort_order": 30,
        },
    )
    connection.execute(
        sa.text(
            "INSERT INTO achievements (id, slug, title, description, how_to_earn, icon_slug, sort_order) VALUES (:id, :slug, :title, :description, :how_to_earn, :icon_slug, :sort_order) ON CONFLICT (slug) DO NOTHING"
        ),
        {
            "id": "b8310407-b5e3-5ca4-b0d5-3d7edafdf0eb",
            "slug": "people-author",
            "title": "Народный автор",
            "description": "Получить суммарно 50 лайков на своих опубликованных статьях",
            "how_to_earn": "Получить суммарно 50 лайков на своих опубликованных статьях",
            "icon_slug": "people-author",
            "sort_order": 31,
        },
    )
    connection.execute(
        sa.text(
            "INSERT INTO achievements (id, slug, title, description, how_to_earn, icon_slug, sort_order) VALUES (:id, :slug, :title, :description, :how_to_earn, :icon_slug, :sort_order) ON CONFLICT (slug) DO NOTHING"
        ),
        {
            "id": "475b09ae-8728-5b68-b7e7-ea6b0e7787df",
            "slug": "navigator",
            "title": "Штурман",
            "description": "Пройти маршрут, собранный ИИ-помощником для вас",
            "how_to_earn": "Пройти маршрут, собранный ИИ-помощником для вас",
            "icon_slug": "navigator",
            "sort_order": 32,
        },
    )


def downgrade() -> None:
    op.drop_table("achievement_admin_actions")
    op.drop_column("route_execution_stops", "device_distance_m")
    op.drop_constraint("ck_user_achievements_operator_reason", "user_achievements", type_="check")
    op.drop_constraint("ck_user_achievements_source", "user_achievements", type_="check")
    op.drop_constraint(
        "fk_user_achievements_granted_by_admin_id_admin_principals",
        "user_achievements",
        type_="foreignkey",
    )
    for column in ("celebrated_at", "granted_by_admin_id", "reason", "source"):
        op.drop_column("user_achievements", column)
