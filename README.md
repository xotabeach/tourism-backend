# tourism-backend

Private server repository для Crimea Travel Platform: modular monolith на
Python 3.13 и FastAPI.

## Назначение

- HTTP API для мобильного клиента и будущих интеграций.
- Domain modules: `identity`, `users`, `geography`, `places`, `routes`,
  `route_builder`, `media`.
- Прикладные миграции PostgreSQL и PostGIS через Alembic.

## Требования

- Python 3.13
- [uv](https://docs.astral.sh/uv/)
- Запущенный local stack из `tourism-platform` (`make up`)

## Быстрый старт

```bash
cp .env.example .env
uv sync --all-extras --dev
uv run alembic upgrade head
uv run tourism-backend
```

Проверки:

```bash
./scripts/validate.sh
```

## Endpoints

| Endpoint | Назначение |
| --- | --- |
| `GET /health` | Liveness probe |
| `GET /ready` | Readiness probe с проверкой PostgreSQL |

OpenAPI: `http://localhost:8000/docs`

## Структура

```text
src/tourism_backend/
├── api/              # HTTP layer
├── db/               # Database session utilities
├── modules/          # Domain modules (modular monolith boundaries)
├── config.py
└── main.py
alembic/              # Database migrations
```

## Связанные репозитории

- [`tourism-platform`](../tourism-platform) — архитектура и local Compose.
- [`tourism-mobile`](../tourism-mobile) — Flutter client.

Migrations живут в этом repository. Отдельный `tourism-database` repository не
используется.

## Лицензия

MIT — см. [LICENSE](LICENSE).
