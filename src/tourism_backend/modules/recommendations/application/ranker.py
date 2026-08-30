"""Deterministic recommendation ranker v1 (no embeddings, no ML)."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from tourism_backend.modules.recommendations.application.policy import (
    COMPLETED_COOLDOWN_DAYS,
    DECK_SIZE,
    EXCLUDED_QUALITY,
    EXPLORATION_SHARE,
    FRESHNESS_HALF_LIFE_DAYS,
    MAX_CATEGORY_SHARE,
    MAX_REGION_SHARE,
    SKIP_COOLDOWN_DAYS,
    WEIGHT_COMPLETION,
    WEIGHT_CONTENT,
    WEIGHT_CONTEXT,
    WEIGHT_EXPLICIT,
    WEIGHT_EXPLORATION,
    WEIGHT_FRESHNESS,
    WEIGHT_POPULARITY,
    normalize_difficulty,
)
from tourism_backend.modules.route_builder.application.scoring import categories_for_interest


@dataclass(frozen=True, slots=True)
class RecommendationCandidate:
    route_id: UUID
    region_id: UUID
    category_slugs: frozenset[str]
    difficulty: str | None
    suitable_for_children: bool | None
    pets_allowed: bool | None
    created_at: datetime | None
    favorite_count: int
    seasonality: tuple[str, ...]
    quality_status: str = "unknown"


@dataclass(frozen=True, slots=True)
class RecommendationProfile:
    preferred_categories: frozenset[str] = frozenset()
    preferred_difficulty: str | None = None
    travels_with_kids: bool = False
    travels_with_pets: bool = False
    favorite_route_ids: frozenset[UUID] = frozenset()
    favorite_category_slugs: frozenset[str] = frozenset()
    favorite_region_ids: frozenset[UUID] = frozenset()
    skipped_route_ids: frozenset[UUID] = frozenset()
    completed_route_ids: frozenset[UUID] = frozenset()


@dataclass(frozen=True, slots=True)
class ScoredRecommendation:
    candidate: RecommendationCandidate
    score: float
    explanation_code: str
    exploration: bool


def preferred_taxonomy_slugs(preferred_categories: frozenset[str]) -> frozenset[str]:
    slugs: set[str] = set()
    for category in preferred_categories:
        slugs.update(categories_for_interest(category))
    return frozenset(slugs)


def primary_category(slugs: frozenset[str]) -> str:
    if not slugs:
        return "unknown"
    return sorted(slugs)[0]


def eligible_candidates(
    candidates: Sequence[RecommendationCandidate],
    profile: RecommendationProfile,
) -> list[RecommendationCandidate]:
    """Drop routes that violate hard publication/safety/cooldown gates."""

    excluded = profile.favorite_route_ids | profile.skipped_route_ids | profile.completed_route_ids
    out: list[RecommendationCandidate] = []
    for candidate in candidates:
        if candidate.route_id in excluded:
            continue
        if candidate.quality_status in EXCLUDED_QUALITY:
            continue
        if profile.travels_with_kids and candidate.suitable_for_children is False:
            continue
        if profile.travels_with_pets and candidate.pets_allowed is False:
            continue
        out.append(candidate)
    return out


def score_candidates(
    candidates: Sequence[RecommendationCandidate],
    profile: RecommendationProfile,
    *,
    season: str,
    as_of: datetime,
) -> list[ScoredRecommendation]:
    eligible = eligible_candidates(candidates, profile)
    max_favorites = max((item.favorite_count for item in eligible), default=0)
    preferred_slugs = preferred_taxonomy_slugs(profile.preferred_categories)
    scored = [
        _score_one(
            candidate,
            profile,
            preferred_slugs=preferred_slugs,
            season=season,
            as_of=as_of,
            max_favorites=max_favorites,
        )
        for candidate in eligible
    ]
    scored.sort(key=lambda item: (-item.score, item.candidate.route_id.hex))
    return scored


def rerank_for_diversity(
    scored: Sequence[ScoredRecommendation],
    *,
    deck_size: int = DECK_SIZE,
    max_category_share: float = MAX_CATEGORY_SHARE,
    max_region_share: float = MAX_REGION_SHARE,
    exploration_share: float = EXPLORATION_SHARE,
) -> list[ScoredRecommendation]:
    """Constrained rerank: caps and exploration, never invented diversity."""

    if not scored:
        return []
    size = min(deck_size, len(scored))
    # A single-region catalogue (everything is in Crimea today) would other-
    # wise hit the region cap and truncate the deck to half its size no
    # matter how many candidates exist. The cap only means something when
    # there is more than one region to balance between.
    regions = {item.candidate.region_id for item in scored}
    apply_region_cap = len(regions) > 1
    exploration_pool = [item for item in scored if item.exploration]
    exploration_target = 0
    if size >= 5 and exploration_pool:
        exploration_target = min(len(exploration_pool), max(1, round(size * exploration_share)))

    selected: list[ScoredRecommendation] = []
    selected_ids: set[UUID] = set()

    def _try_add(item: ScoredRecommendation) -> bool:
        if item.candidate.route_id in selected_ids:
            return False
        if not _respects_caps(
            selected,
            item,
            deck_size=size,
            max_category_share=max_category_share,
            max_region_share=max_region_share if apply_region_cap else None,
        ):
            return False
        selected.append(item)
        selected_ids.add(item.candidate.route_id)
        return True

    for item in exploration_pool:
        if len([row for row in selected if row.exploration]) >= exploration_target:
            break
        _try_add(item)
    for item in scored:
        if len(selected) >= size:
            break
        _try_add(item)
    return selected


def cooldown_cutoff(*, as_of: datetime, days: int = SKIP_COOLDOWN_DAYS) -> datetime:
    aware = as_of if as_of.tzinfo is not None else as_of.replace(tzinfo=UTC)
    return aware.astimezone(UTC) - timedelta(days=days)


def completed_cutoff(*, as_of: datetime) -> datetime:
    aware = as_of if as_of.tzinfo is not None else as_of.replace(tzinfo=UTC)
    return aware.astimezone(UTC) - timedelta(days=COMPLETED_COOLDOWN_DAYS)


def _respects_caps(
    selected: Sequence[ScoredRecommendation],
    candidate: ScoredRecommendation,
    *,
    deck_size: int,
    max_category_share: float,
    max_region_share: float | None,
) -> bool:
    if deck_size < 5 or not selected:
        return True
    max_category = max(1, int(deck_size * max_category_share))
    category = primary_category(candidate.candidate.category_slugs)
    category_count = sum(
        1 for item in selected if primary_category(item.candidate.category_slugs) == category
    )
    if category_count >= max_category:
        return False
    if max_region_share is None:
        return True
    max_region = max(1, int(deck_size * max_region_share))
    region_id = candidate.candidate.region_id
    region_count = sum(1 for item in selected if item.candidate.region_id == region_id)
    return region_count < max_region


def _score_one(
    candidate: RecommendationCandidate,
    profile: RecommendationProfile,
    *,
    preferred_slugs: frozenset[str],
    season: str,
    as_of: datetime,
    max_favorites: int,
) -> ScoredRecommendation:
    explicit = _explicit_profile(candidate, profile, preferred_slugs)
    content = _content_affinity(candidate, profile)
    context = _context_fit(candidate, season)
    popularity = _popularity(candidate.favorite_count, max_favorites)
    freshness = _freshness(candidate.created_at, as_of)
    completion = _completion_likelihood(candidate, profile)
    exploration_value = _exploration_bonus(candidate, profile, preferred_slugs)
    score = (
        WEIGHT_EXPLICIT * explicit
        + WEIGHT_CONTENT * content
        + WEIGHT_CONTEXT * context
        + WEIGHT_POPULARITY * popularity
        + WEIGHT_FRESHNESS * freshness
        + WEIGHT_COMPLETION * completion
        + WEIGHT_EXPLORATION * exploration_value
    )
    bounded = min(1.0, max(0.0, score))
    exploration = exploration_value >= 0.8
    return ScoredRecommendation(
        candidate=candidate,
        score=bounded,
        explanation_code=_explanation(
            explicit=explicit,
            freshness=freshness,
            popularity=popularity,
            exploration=exploration,
            cold_start=_is_cold_start(profile),
        ),
        exploration=exploration,
    )


def _is_cold_start(profile: RecommendationProfile) -> bool:
    return not profile.preferred_categories and not profile.favorite_route_ids


def _explicit_profile(
    candidate: RecommendationCandidate,
    profile: RecommendationProfile,
    preferred_slugs: frozenset[str],
) -> float:
    parts: list[float] = []
    if preferred_slugs:
        overlap = preferred_slugs & candidate.category_slugs
        # Any taxonomy hit is a real preference match. A fractional split
        # against a multi-slug mapping (Море → beach+viewpoint) would let
        # popularity and exploration bury the thing the user asked for.
        parts.append(1.0 if overlap else 0.15)
    wanted = normalize_difficulty(profile.preferred_difficulty)
    actual = normalize_difficulty(candidate.difficulty)
    if wanted:
        if actual is None:
            parts.append(0.5)
        elif actual == wanted:
            parts.append(1.0)
        else:
            parts.append(0.25)
    if not parts:
        return 0.5
    return sum(parts) / len(parts)


def _content_affinity(
    candidate: RecommendationCandidate,
    profile: RecommendationProfile,
) -> float:
    if not profile.favorite_category_slugs and not profile.favorite_region_ids:
        return 0.5
    parts: list[float] = []
    if profile.favorite_category_slugs:
        overlap = profile.favorite_category_slugs & candidate.category_slugs
        denom = max(1, min(2, len(profile.favorite_category_slugs)))
        parts.append(min(1.0, len(overlap) / denom))
    if profile.favorite_region_ids:
        parts.append(1.0 if candidate.region_id in profile.favorite_region_ids else 0.0)
    return sum(parts) / len(parts)


def _context_fit(candidate: RecommendationCandidate, season: str) -> float:
    seasons = {item.casefold().strip() for item in candidate.seasonality if item.strip()}
    if not seasons:
        return 0.5
    needle = season.casefold()
    if needle in seasons or any(needle in item or item in needle for item in seasons):
        return 1.0
    return 0.2


def _popularity(count: int, max_count: int) -> float:
    if max_count <= 0:
        return 0.5
    return math.log1p(max(0, count)) / math.log1p(max_count)


def _freshness(created_at: datetime | None, as_of: datetime) -> float:
    if created_at is None:
        return 0.5
    created = created_at if created_at.tzinfo is not None else created_at.replace(tzinfo=UTC)
    now = as_of if as_of.tzinfo is not None else as_of.replace(tzinfo=UTC)
    age_days = max(0.0, (now - created).total_seconds() / 86_400)
    return math.exp(-age_days / FRESHNESS_HALF_LIFE_DAYS)


def _completion_likelihood(
    candidate: RecommendationCandidate,
    profile: RecommendationProfile,
) -> float:
    score = 0.5
    wanted = normalize_difficulty(profile.preferred_difficulty)
    actual = normalize_difficulty(candidate.difficulty)
    if wanted and actual == wanted:
        score = 0.8
    if profile.travels_with_kids and candidate.suitable_for_children is True:
        score = min(1.0, score + 0.15)
    if profile.travels_with_pets and candidate.pets_allowed is True:
        score = min(1.0, score + 0.15)
    return score


def _exploration_bonus(
    candidate: RecommendationCandidate,
    profile: RecommendationProfile,
    preferred_slugs: frozenset[str],
) -> float:
    known = preferred_slugs | profile.favorite_category_slugs
    if not known:
        return 0.5
    if candidate.category_slugs and not (candidate.category_slugs & known):
        return 1.0
    return 0.0


def _explanation(
    *,
    explicit: float,
    freshness: float,
    popularity: float,
    exploration: bool,
    cold_start: bool,
) -> str:
    if exploration:
        return "nearby_exploration"
    if explicit >= 0.7:
        return "matches_interest"
    if freshness >= 0.8:
        return "fresh_route"
    if popularity >= 0.7:
        return "popular_route"
    if cold_start:
        return "cold_start"
    if explicit >= 0.5:
        return "matches_interest"
    return "cold_start"
