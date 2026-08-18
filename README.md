# tourism-backend

Private server repository для Crimea Travel Platform: modular monolith на
Python 3.13 и FastAPI.

Стек целиком (local / test / Gemma 4 home lab):
`tourism-platform/docs/stack.md`.

## Назначение

- HTTP API (`/api/v1`) для мобильного клиента.
- Domain modules с API: `identity`, `geography`, `places`, `routes`,
  `favorites`, `support`, `notifications`, `admin`, `media`.
- Заглушки (пакеты без router): `route_builder`, `route_execution`,
  `subscriptions`.
- Миграции PostgreSQL/PostGIS через Alembic. Ops UI: SQLAdmin `/admin`.

## Требования

- Python 3.13
- [uv](https://docs.astral.sh/uv/)
- Local stack из `tourism-platform` (`make up`)

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
Admin: `http://localhost:8000/admin` (bootstrap login из `.env`).

## Окружения

`APP_ENV` принимает только `local`, `test`, `staging` или `production`.
Local/test могут использовать локальные placeholder credentials;
staging/production откажутся запускаться с ними.

Контейнерный image содержит Alembic, seed и тестовые media:

```bash
alembic upgrade head
python scripts/seed_crimea.py
```

Проверки (обязательны при lean GitLab CI):

```bash
./scripts/validate.sh
```

Стиль и DX: `tourism-platform/docs/development-environment.md`,
`python-code-style.md`, `python-testing-guide.md`.

Bulk import:

```bash
uv run python scripts/seed_crimea.py --file data/extra_places.json --places-only
```

## Endpoints (срез)

OpenAPI: `http://localhost:8000/docs`

| Область | Примеры |
| --- | --- |
| Health | `GET /health/live`, `GET /health/ready` |
| Geography / places | `/api/v1/geography/*`, `/categories`, `/places` |
| Auth / me | `/auth/otp/*`, `/auth/refresh`, `/me` |
| Users | `/users/search`, `/users/leaderboard`, `/users/{id}`, `/users/{id}/achievements` |
| Routes | catalog, `/routes/mine`, drafts, submit, reviews |
| Favorites | `/favorites/places/{id}`, `/favorites/routes/{id}` |
| Support | `/support/tickets` |
| Inbox / FCM | `/me/notifications`, `/me/device-tokens` |
| Admin | `/admin` (cookie session, не Bearer) |

AI planning endpoints ещё нет. Заготовки env: `AI_PROVIDER=mock|gemini|ollama`,
`OLLAMA_CHAT_MODEL=gemma4:12b` — см. stack.md и home-lab guide.

## Структура

```text
src/tourism_backend/
├── api/                 # HTTP layer, errors, /api/v1
├── db/                  # Base, session, Redis
├── modules/             # Domain boundaries (no cross-ORM imports)
├── config.py            # APP_ENV, JWT, admin, optional FCM
├── logging_config.py
└── main.py
alembic/
data/crimea_seed.json
scripts/seed_crimea.py
Dockerfile
```

## Связанные репозитории

- [`tourism-platform`](../tourism-platform) — архитектура, Compose, deploy.
- [`tourism-mobile`](../tourism-mobile) — Flutter client.

## Лицензия

MIT — см. [LICENSE](LICENSE).
