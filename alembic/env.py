from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from tourism_backend.config import get_settings
from tourism_backend.db.base import Base
from tourism_backend.modules.admin.infrastructure import models as admin_models
from tourism_backend.modules.favorites.infrastructure import models as favorites_models
from tourism_backend.modules.geography.infrastructure import models as geography_models
from tourism_backend.modules.identity.infrastructure import models as identity_models
from tourism_backend.modules.knowledge.infrastructure import models as knowledge_models
from tourism_backend.modules.media.infrastructure import models as media_models
from tourism_backend.modules.notifications.infrastructure import (
    models as notifications_models,
)
from tourism_backend.modules.places.infrastructure import models as places_models
from tourism_backend.modules.recommendations.infrastructure import (
    models as recommendations_models,
)
from tourism_backend.modules.route_builder.infrastructure import (
    models as route_builder_models,
)
from tourism_backend.modules.route_execution.infrastructure import (
    models as route_execution_models,
)
from tourism_backend.modules.routes.infrastructure import models as routes_models
from tourism_backend.modules.subscriptions.infrastructure import (
    models as subscriptions_models,
)
from tourism_backend.modules.support.infrastructure import models as support_models

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url_sync)

# Ensure model modules are imported for metadata discovery.
_ = (
    geography_models,
    places_models,
    recommendations_models,
    routes_models,
    route_execution_models,
    identity_models,
    favorites_models,
    support_models,
    media_models,
    notifications_models,
    admin_models,
    subscriptions_models,
    route_builder_models,
    knowledge_models,
)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
