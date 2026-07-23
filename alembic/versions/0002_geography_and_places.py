"""Geography and places catalog schema."""

from collections.abc import Sequence

import sqlalchemy as sa
from geoalchemy2 import Geography
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002_geography_and_places"
down_revision: str | Sequence[str] | None = "0001_enable_postgis"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "countries",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("code", sa.String(length=2), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("default_locale", sa.String(length=16), nullable=False, server_default="ru"),
        sa.Column("timezone", sa.String(length=64), nullable=False, server_default="UTC"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("source_name", sa.String(length=255), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("source_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "freshness_status",
            sa.String(length=32),
            nullable=False,
            server_default="unknown",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("code", name="uq_countries_code"),
        sa.UniqueConstraint("slug", name="uq_countries_slug"),
    )
    op.create_index("ix_countries_status", "countries", ["status"])

    op.create_table(
        "regions",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("country_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("administrative_code", sa.String(length=64), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("center", Geography(geometry_type="POINT", srid=4326), nullable=True),
        sa.Column("boundary", Geography(geometry_type="MULTIPOLYGON", srid=4326), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("source_name", sa.String(length=255), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("source_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "freshness_status",
            sa.String(length=32),
            nullable=False,
            server_default="unknown",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["country_id"], ["countries.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("country_id", "slug", name="uq_regions_country_slug"),
    )
    op.create_index("ix_regions_country_id", "regions", ["country_id"])
    op.create_index("ix_regions_status", "regions", ["status"])
    op.execute("CREATE INDEX ix_regions_center ON regions USING GIST (center)")
    op.execute("CREATE INDEX ix_regions_boundary ON regions USING GIST (boundary)")

    op.create_table(
        "localities",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("region_id", sa.Uuid(), nullable=False),
        sa.Column("parent_locality_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("type", sa.String(length=64), nullable=False, server_default="city"),
        sa.Column("postal_code", sa.String(length=32), nullable=True),
        sa.Column("center", Geography(geometry_type="POINT", srid=4326), nullable=True),
        sa.Column("boundary", Geography(geometry_type="MULTIPOLYGON", srid=4326), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("source_name", sa.String(length=255), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("source_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "freshness_status",
            sa.String(length=32),
            nullable=False,
            server_default="unknown",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["region_id"], ["regions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["parent_locality_id"], ["localities.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("region_id", "slug", name="uq_localities_region_slug"),
    )
    op.create_index("ix_localities_region_id", "localities", ["region_id"])
    op.create_index("ix_localities_parent_locality_id", "localities", ["parent_locality_id"])
    op.create_index("ix_localities_status", "localities", ["status"])
    op.execute("CREATE INDEX ix_localities_center ON localities USING GIST (center)")

    op.create_table(
        "categories",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("parent_category_id", sa.Uuid(), nullable=True),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("icon_key", sa.String(length=64), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["parent_category_id"], ["categories.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("code", name="uq_categories_code"),
        sa.UniqueConstraint("slug", name="uq_categories_slug"),
    )
    op.create_index("ix_categories_parent_category_id", "categories", ["parent_category_id"])
    op.create_index("ix_categories_status", "categories", ["status"])

    op.create_table(
        "places",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("region_id", sa.Uuid(), nullable=False),
        sa.Column("locality_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=150), nullable=False),
        sa.Column("short_description", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("location", Geography(geometry_type="POINT", srid=4326), nullable=False),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("contact_phone", sa.String(length=64), nullable=True),
        sa.Column("website_url", sa.Text(), nullable=True),
        sa.Column("accessibility", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("recommended_equipment", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("seasonality", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("difficulty", sa.String(length=32), nullable=True),
        sa.Column("is_paid", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("price_notes", sa.Text(), nullable=True),
        sa.Column("is_suitable_for_children", sa.Boolean(), nullable=True),
        sa.Column("safety_warnings", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("temporary_closure_status", sa.String(length=32), nullable=True),
        sa.Column("temporary_closure_reason", sa.Text(), nullable=True),
        sa.Column("closed_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "publication_status",
            sa.String(length=32),
            nullable=False,
            server_default="published",
        ),
        sa.Column("source_name", sa.String(length=255), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("source_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "freshness_status",
            sa.String(length=32),
            nullable=False,
            server_default="unknown",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["region_id"], ["regions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["locality_id"], ["localities.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("region_id", "slug", name="uq_places_region_slug"),
    )
    op.create_index("ix_places_region_id", "places", ["region_id"])
    op.create_index("ix_places_locality_id", "places", ["locality_id"])
    op.create_index("ix_places_publication_status", "places", ["publication_status"])
    op.create_index("ix_places_publication_region", "places", ["publication_status", "region_id"])
    op.create_index("ix_places_name", "places", ["name"])
    op.execute("CREATE INDEX ix_places_location ON places USING GIST (location)")

    op.create_table(
        "place_categories",
        sa.Column("place_id", sa.Uuid(), nullable=False),
        sa.Column("category_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["place_id"], ["places.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("place_id", "category_id", name="pk_place_categories"),
    )
    op.create_index("ix_place_categories_category_id", "place_categories", ["category_id"])

    op.create_table(
        "place_entrances",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("place_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("location", Geography(geometry_type="POINT", srid=4326), nullable=False),
        sa.Column("address_hint", sa.Text(), nullable=True),
        sa.Column("entrance_type", sa.String(length=64), nullable=False, server_default="main"),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("accessibility", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("vehicle_restrictions", sa.Text(), nullable=True),
        sa.Column("opening_notes", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("source_name", sa.String(length=255), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("source_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "freshness_status",
            sa.String(length=32),
            nullable=False,
            server_default="unknown",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["place_id"], ["places.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_place_entrances_place_id", "place_entrances", ["place_id"])
    op.execute(
        "CREATE UNIQUE INDEX uq_place_entrances_one_primary "
        "ON place_entrances (place_id) "
        "WHERE is_primary IS TRUE AND status = 'active'"
    )
    op.execute("CREATE INDEX ix_place_entrances_location ON place_entrances USING GIST (location)")

    op.create_table(
        "place_schedules",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("place_id", sa.Uuid(), nullable=False),
        sa.Column("place_entrance_id", sa.Uuid(), nullable=True),
        sa.Column("schedule_type", sa.String(length=32), nullable=False, server_default="regular"),
        sa.Column("valid_from", sa.Date(), nullable=True),
        sa.Column("valid_until", sa.Date(), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("weekdays", postgresql.ARRAY(sa.Integer()), nullable=True),
        sa.Column("opens_at", sa.Time(), nullable=True),
        sa.Column("closes_at", sa.Time(), nullable=True),
        sa.Column("is_closed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("exception_date", sa.Date(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("source_name", sa.String(length=255), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("source_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "freshness_status",
            sa.String(length=32),
            nullable=False,
            server_default="unknown",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["place_id"], ["places.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["place_entrance_id"], ["place_entrances.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_place_schedules_place_id", "place_schedules", ["place_id"])

    op.create_table(
        "place_images",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("place_id", sa.Uuid(), nullable=False),
        sa.Column("media_asset_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False, server_default="photo"),
        sa.Column("alt_text", sa.Text(), nullable=True),
        sa.Column("author", sa.String(length=255), nullable=True),
        sa.Column("license", sa.String(length=128), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_cover", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["place_id"], ["places.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_place_images_place_id", "place_images", ["place_id"])
    op.execute(
        "CREATE UNIQUE INDEX uq_place_images_one_cover "
        "ON place_images (place_id) "
        "WHERE is_cover IS TRUE AND status = 'active'"
    )


def downgrade() -> None:
    op.drop_table("place_images")
    op.drop_table("place_schedules")
    op.drop_table("place_entrances")
    op.drop_table("place_categories")
    op.drop_table("places")
    op.drop_table("categories")
    op.drop_table("localities")
    op.drop_table("regions")
    op.drop_table("countries")
