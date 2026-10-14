"""The editors' list of place prices (BACKEND-55, spec 33 D6/D15).

Places of published routes whose price does not count yet, those in most
routes first, each with an inline form: free, paid with a price, or «не
знаю». Saving marks the price as the editor's, checked today. This is the
one place in the admin where a price is written.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar, Literal
from uuid import UUID

from sqladmin import BaseView, expose
from sqladmin.flash import Flash
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from tourism_backend.modules.admin.application.audit import record_audit
from tourism_backend.modules.admin.presentation.auth import (
    require_permission,
    session_principal_id,
)
from tourism_backend.modules.places.application.pricing import (
    PRICE_RECHECK_AFTER,
    PriceInputError,
    apply_price,
    parse_price_input,
    price_queue,
)
from tourism_backend.modules.places.infrastructure.models import Place

#: Rows on one page; the list is worked from the top.
PAGE_ROWS = 60
_SHOWS: dict[str, str] = {
    "todo": "Нужна цена",
    "stale": "Старше года",
    "all": "Все места маршрутов",
}
STATUS_LABELS = {"free": "Бесплатно", "paid": "Платно", "unknown": "Не знаю"}


def hint_text(hint: dict[str, Any] | None) -> str:
    """«OSM: 300–500 RUB (300-500 rub)» from ``places.price_hint``."""

    if not hint:
        return ""
    source = {"osm": "OSM", "seed": "Сид"}.get(str(hint.get("source")), str(hint.get("source")))
    low, high = hint.get("min"), hint.get("max")
    amount = ""
    if low is not None:
        amount = f"{low}" if high in (None, low) else f"{low}–{high}"
        amount += f" {hint.get('currency') or 'RUB'}"
    notes = hint.get("notes")
    body = " ".join(part for part in (amount, f"({notes})" if notes else "") if part)
    return f"{source}: {body}" if body else ""


class PlacePricesAdmin(BaseView):
    name = "Цены мест"
    category = "Места"
    icon = "fa-solid fa-ruble-sign"
    session_maker: ClassVar[Any]

    def is_accessible(self, request: Request) -> bool:
        return require_permission(request, "places.read")

    def is_visible(self, request: Request) -> bool:
        return self.is_accessible(request)

    @expose("/place-prices", methods=["GET"], identity="place-prices")
    async def show(self, request: Request) -> Response:
        if not require_permission(request, "places.read"):
            return Response(status_code=403)
        raw_show = request.query_params.get("show", "todo")
        show: Literal["todo", "stale", "all"] = raw_show if raw_show in _SHOWS else "todo"  # type: ignore[assignment]
        search = request.query_params.get("q", "").strip()
        now = datetime.now(UTC)
        async with self.session_maker() as session:
            queue = await price_queue(session, now=now, show=show, search=search)
        rows = [
            {
                "row": row,
                "status_label": STATUS_LABELS.get(row.payment_status, row.payment_status),
                "hint": hint_text(row.hint),
                "checked": row.checked_at.strftime("%d.%m.%Y") if row.checked_at else "",
                "by_import": row.price_source == "import",
            }
            for row in queue.rows[:PAGE_ROWS]
        ]
        return await self.templates.TemplateResponse(
            request,
            "sqladmin/place_prices.html",
            {
                "queue": queue,
                "rows": rows,
                "more": max(0, len(queue.rows) - PAGE_ROWS),
                "show": show,
                "shows": _SHOWS,
                "search": search,
                "recheck_months": PRICE_RECHECK_AFTER.days // 30,
                "can_write": require_permission(request, "places.write"),
            },
        )

    @expose("/place-prices/save", methods=["POST"])
    async def save(self, request: Request) -> Response:
        form = await request.form()
        back = str(request.url_for("admin:view-place-prices"))
        query = str(form.get("back_query") or "")
        if query:
            back = f"{back}?{query}"
        if not require_permission(request, "places.write"):
            Flash.error(request, "Нет права менять места.")
            return RedirectResponse(back, status_code=303)
        try:
            place_id = UUID(str(form.get("place_id") or ""))
        except ValueError:
            Flash.error(request, "Место не найдено.")
            return RedirectResponse(back, status_code=303)
        try:
            price = parse_price_input(
                payment_status=_text(form.get("payment_status")),
                min_amount=_text(form.get("min_amount")),
                max_amount=_text(form.get("max_amount")),
                notes=_text(form.get("notes")),
            )
        except PriceInputError as exc:
            Flash.error(request, str(exc))
            return RedirectResponse(back, status_code=303)
        async with self.session_maker(expire_on_commit=False) as session:
            place = await session.get(Place, place_id)
            if place is None:
                Flash.error(request, "Место не найдено.")
                return RedirectResponse(back, status_code=303)
            before = {
                "payment_status": place.payment_status,
                "price_min_amount": place.price_min_amount,
                "price_max_amount": place.price_max_amount,
                "price_source": place.price_source,
            }
            apply_price(place, price, now=datetime.now(UTC))
            await record_audit(
                session,
                actor_id=session_principal_id(request),
                action="admin.place_price",
                entity_type="place",
                entity_id=str(place.id),
                ip=request.client.host if request.client else None,
                metadata={
                    "before": before,
                    "after": {
                        "payment_status": price.payment_status,
                        "price_min_amount": price.min_amount,
                        "price_max_amount": price.max_amount,
                    },
                },
            )
            await session.commit()
            Flash.success(request, f"«{place.name}»: сохранено.")
        return RedirectResponse(back, status_code=303)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None
