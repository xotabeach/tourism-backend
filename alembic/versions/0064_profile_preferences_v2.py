"""Profile preferences v2: new interest words, duration and «on transport».

The profile quiz now offers Природа, Гастрономия, История, Смотровые,
Романтика, Семейное. Stored interests from the first quiz (Море, Горы, Еда,
Лес) and from the AI chat are folded into these words; anything without a
counterpart is dropped.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0064_profile_preferences_v2"
down_revision: str | Sequence[str] | None = "0063_app_stats"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Keep in sync with LEGACY_PREFERENCE_CATEGORIES in identity models (copied:
# a migration must not change when the app code later does).
_MAPPING = {
    "природа": "Природа",
    "море": "Природа",
    "пляж": "Природа",
    "лес": "Природа",
    "леса": "Природа",
    "водопады": "Природа",
    "горы": "Смотровые",
    "смотровые": "Смотровые",
    "смотровые площадки": "Смотровые",
    "фото": "Смотровые",
    "еда": "Гастрономия",
    "вино": "Гастрономия",
    "гастрономия": "Гастрономия",
    "история": "История",
    "романтика": "Романтика",
    "семейное": "Семейное",
    "с детьми": "Семейное",
}


def upgrade() -> None:
    op.add_column("users", sa.Column("preferred_duration", sa.String(8), nullable=True))
    op.add_column("users", sa.Column("preferred_transport", sa.String(8), nullable=True))
    op.create_check_constraint(
        "ck_users_preferred_duration",
        "users",
        "preferred_duration IS NULL OR preferred_duration IN ('d1_2', 'd3_5', 'd6_7', 'd7plus')",
    )
    op.create_check_constraint(
        "ck_users_preferred_transport",
        "users",
        "preferred_transport IS NULL OR preferred_transport IN ('car')",
    )

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, preferred_categories FROM users "
            "WHERE preferred_categories IS NOT NULL AND cardinality(preferred_categories) > 0"
        )
    ).all()
    for user_id, categories in rows:
        mapped: list[str] = []
        for value in categories:
            word = _MAPPING.get(str(value).casefold().strip())
            if word and word not in mapped:
                mapped.append(word)
        if mapped != list(categories):
            bind.execute(
                sa.text("UPDATE users SET preferred_categories = :c WHERE id = :id"),
                {"c": mapped, "id": user_id},
            )


def downgrade() -> None:
    # The old words cannot be restored from the new ones; only the columns go.
    op.drop_constraint("ck_users_preferred_transport", "users", type_="check")
    op.drop_constraint("ck_users_preferred_duration", "users", type_="check")
    op.drop_column("users", "preferred_transport")
    op.drop_column("users", "preferred_duration")
