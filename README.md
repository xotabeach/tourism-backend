# tourism-backend

Private server repository для Crimea Travel Platform: modular monolith на
Python 3.13 и FastAPI.

## Назначение

- HTTP API для мобильного клиента и будущих интеграций.
- Domain modules: `identity`, `users`, `geography`, `places`, `routes`,
  `route_builder`, `route_execution`, `favorites`, `subscriptions`, `media`.
- Прикладные миграции PostgreSQL и PostGIS через Alembic.

## Требования

- Python 3.13
- [uv](https://docs.astral.sh/uv/)
- Запущенный local stack из `tourism-platform` (`make up`)

## Быстрый старт

```bash
# в tourism-platform
make init && make up

# в tourism-backend
cp .env.example .env
uv sync --all-extras --dev
uv run alembic upgrade head
uv run python scripts/seed_crimea.py
uv run tourism-backend
```

По умолчанию local ports: Postgres `5433`, Redis `6380` (см. `.env.example`).

Проверки:

```bash
./scripts/validate.sh
```

Стиль и DX: см. `tourism-platform/docs/development-environment.md`,
`python-code-style.md`, `python-testing-guide.md`.

Bulk import позже:

```bash
uv run python scripts/seed_crimea.py --file data/extra_places.json --places-only
```

## Endpoints

| Endpoint | Назначение |
| --- | --- |
| `GET /health/live` | Liveness probe (`/health` — alias) |
| `GET /health/ready` | Readiness: PostgreSQL + Redis (`/ready` — alias) |
| `GET /api/v1` | Versioned API root |
| `GET /api/v1/geography/countries` | Страны |
| `GET /api/v1/geography/regions` | Регионы (`country_code`) |
| `GET /api/v1/geography/localities` | Localities (`region_slug`) |
| `GET /api/v1/categories` | Категории мест |
| `GET /api/v1/places` | Каталог мест (фильтры region/locality/category/q) |
| `GET /api/v1/places/{id}` | Карточка места |

OpenAPI: `http://localhost:8000/docs`

## Структура

```text
src/tourism_backend/
├── api/                 # HTTP layer, errors, /api/v1
├── db/                  # Base, session, Redis
├── modules/             # Domain boundaries (no cross-ORM imports)
├── config.py
├── logging_config.py    # JSON logs
└── main.py
alembic/                 # Database migrations
data/crimea_seed.json    # Representative Crimea seed
scripts/seed_crimea.py   # Idempotent seed / bulk import
Dockerfile
```

## Связанные репозитории

- [`tourism-platform`](../tourism-platform) — архитектура и local Compose.
- [`tourism-mobile`](../tourism-mobile) — Flutter client.

## Лицензия

MIT — см. [LICENSE](LICENSE).
