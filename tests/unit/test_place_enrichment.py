"""Unit tests for OSM fact enrichment and content drafts."""

from uuid import uuid4

from tourism_backend.modules.places.application.content_enrichment import (
    heuristic_content_draft,
    slugify_name,
    stable_slug_with_suffix,
)
from tourism_backend.modules.places.application.fact_enrichment import (
    facts_from_osm_tags,
    merge_fact_patch,
)


def test_facts_from_osm_tags_extract_price_and_transport() -> None:
    patch = facts_from_osm_tags(
        {
            "name": "Музей",
            "tourism": "museum",
            "fee": "yes",
            "charge": "300 RUB",
            "foot": "yes",
            "motor_vehicle": "yes",
            "dog": "no",
            "wheelchair": "limited",
            "parking": "yes",
        }
    )
    assert patch.payment_status == "paid"
    assert patch.price_min_amount == 300
    assert patch.price_max_amount == 300
    assert patch.access_transport is not None
    assert "walk" in patch.access_transport
    assert "car" in patch.access_transport
    assert patch.is_suitable_for_pets is False
    assert patch.parking_available is True
    assert patch.typical_crowding == "medium"
    assert patch.recommended_visit_minutes == 90


def test_merge_fact_patch_keeps_existing_unless_unknown() -> None:
    updates = merge_fact_patch(
        current={
            "typical_crowding": "low",
            "price_min_amount": 100,
            "payment_status": "unknown",
            "accessibility": None,
        },
        patch=facts_from_osm_tags({"fee": "yes", "tourism": "viewpoint", "charge": "50"}),
    )
    assert "typical_crowding" not in updates  # keep existing low
    assert "price_min_amount" not in updates  # keep existing amount
    assert updates["payment_status"] == "paid"
    assert updates["is_paid"] is True


def test_slugify_and_content_draft() -> None:
    assert slugify_name("Ласточкино гнездо") == "lastochkino-gnezdo"
    slug = stable_slug_with_suffix("Ялта парк", "node/12345678")
    assert slug.startswith("yalta-park-")
    draft = heuristic_content_draft(
        place_id=uuid4(),
        name="Ласточкино гнездо",
        source_external_id="node/1",
        category_names=["Смотровая"],
        city_hint="Ялта",
    )
    assert draft.status == "generated_draft"
    assert "Ялта" in draft.short_description
    assert draft.provenance["provider"] == "heuristic"
