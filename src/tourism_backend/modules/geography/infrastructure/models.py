from uuid import UUID

from geoalchemy2 import Geography
from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from tourism_backend.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from tourism_backend.db.mixins import EditorialSourceMixin


class Country(Base, UUIDPrimaryKeyMixin, TimestampMixin, EditorialSourceMixin):
    __tablename__ = "countries"

    code: Mapped[str] = mapped_column(String(2), unique=True, nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    default_locale: Mapped[str] = mapped_column(String(16), nullable=False, default="ru")
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")


class Region(Base, UUIDPrimaryKeyMixin, TimestampMixin, EditorialSourceMixin):
    __tablename__ = "regions"
    __table_args__ = (UniqueConstraint("country_id", "slug", name="uq_regions_country_slug"),)

    country_id: Mapped[UUID] = mapped_column(
        ForeignKey("countries.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    administrative_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    center = mapped_column(Geography(geometry_type="POINT", srid=4326), nullable=True)
    boundary = mapped_column(Geography(geometry_type="MULTIPOLYGON", srid=4326), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)


class Locality(Base, UUIDPrimaryKeyMixin, TimestampMixin, EditorialSourceMixin):
    __tablename__ = "localities"
    __table_args__ = (UniqueConstraint("region_id", "slug", name="uq_localities_region_slug"),)

    region_id: Mapped[UUID] = mapped_column(
        ForeignKey("regions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    parent_locality_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("localities.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False, default="city")
    postal_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    center = mapped_column(Geography(geometry_type="POINT", srid=4326), nullable=True)
    boundary = mapped_column(Geography(geometry_type="MULTIPOLYGON", srid=4326), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)
