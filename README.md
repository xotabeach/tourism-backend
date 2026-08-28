# tourism-backend

Private server repository для Crimea Travel Platform: modular monolith на
Python 3.13 и FastAPI.

Стек целиком (local / test / Gemma 4 home lab):
`tourism-platform/docs/stack.md`.

## Назначение

- HTTP API (`/api/v1`) для мобильного клиента.
- Domain modules с API: `identity`, `geography`, `places`, `routes`,
  `favorites`, `support`, `notifications`, `admin`, `media`.
- API-модули: `route_builder` (match/generate/AI sessions),
  `route_execution` (start/check/complete/cancel), `subscriptions` (Travel+
  entitlements; self-serve checkout остаётся mock до подключения billing).
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

OSM/Overpass import foundation (все внешние места создаются только как
`draft`; dry-run по умолчанию):

```bash
uv run python scripts/import_osm_crimea.py \
  --fetch --limit 1000 --output /tmp/crimea-osm-report.json
uv run python scripts/import_osm_crimea.py --fetch --limit 1000 --apply
```

Скрипт хранит source identity/payload/license, честный
`payment_status=unknown|free|paid`, кеширует успешные сетевые батчи и
идемпотентно обновляет OSM records. Публикация требует отдельного
boundary/dedup/editorial quality gate.

В режиме экономии GitLab minutes push pipeline отключён. Production собирается
и разворачивается с доверенного рабочего компьютера отдельной командой:

```bash
./scripts/deploy-production-local.sh
```

Скрипт требует registry/SSH variables из локального окружения, собирает
`linux/amd64`, публикует immutable SHA + `production`, затем запускает миграции
на сервере через pinned host key. Импорт 1000 OSM-кандидатов остаётся явной
опцией `--import-osm-crimea`. В GitLab сохранён только ручной registry build,
запускаемый через **Run pipeline**; автоматических pipeline на push нет.

## Endpoints (срез)

OpenAPI: `http://localhost:8000/docs`

| Область | Примеры |
| --- | --- |
| Health | `GET /health/live`, `GET /health/ready` |
| Geography / places | `/api/v1/geography/*`, `/categories`, `/places`, отдельные place reviews + media |
| Auth / me | `/auth/otp/*`, `/auth/refresh`, `/me`, `/me/preferences` |
| Users | `/users/search`, `/users/leaderboard`, `/users/{id}`, `/users/{id}/achievements` |
| Routes | catalog (в т.ч. `place_id`), `/routes/mine`, drafts, submit, reviews + reply context + media |
| Favorites | `/favorites/places/{id}`, `/favorites/routes/{id}` |
| Support | `/support/tickets`, `/support/tickets/{id}/attachments` (до 3 фото) |
| Inbox / FCM | `/me/notifications`, `/me/device-tokens` |
| Admin | `/admin` (cookie session; review photos, collapsed filters, expert actions) |

AI planning sessions и deterministic Route Builder уже подключены в Phase 8B;
OpenAI-compatible LM Studio transport и безопасный smoke probe также доступны.
2ГИС HTTP Routing API проверяется ручным sanitized smoke (ключ не печатается):

```bash
uv run python scripts/check_two_gis_routing.py --configured-only
uv run python scripts/check_two_gis_routing.py
```

``ROUTING_PROVIDER`` может оставаться ``stub``: скрипт вызывает адаптер напрямую.
Ключ — только в env (``TWO_GIS_HTTP_API_KEY`` или алиас ``TWO_GIS_API_KEY``), не в CI.
Каталог мест сверяется dry-run'ом с Places API (без публикации, квота
бережётся ``--max-requests``). ``--apply`` только после ручной проверки отчёта
и всё равно не меняет ``publication_status`` / координаты / название:

```bash
uv run python scripts/enrich_places_2gis.py --limit 20 --output /tmp/2gis-places.json
```
Пользовательская генерация проходит через domain validation. Проверка transport:

```bash
LM_STUDIO_BASE_URL=http://100.x.y.z:1234/v1 \
LM_STUDIO_MODEL='<точный id из /v1/models>' \
LM_STUDIO_API_KEY='<локальный token>' \
uv run python scripts/check_lm_studio.py
```

Токен не коммитить. См. stack.md и `ai-lm-studio-windows-gemma4.md`.

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
