"""Pure achievement evaluation using server timestamps and verified distances."""

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from tourism_backend.modules.achievements.solar import sunrise_sunset

CRIMEA = ZoneInfo("Europe/Simferopol")
PLACE_SLUGS: dict[str, frozenset[str]] = {
    "caves": frozenset(
        {
            "chufut-kale",
            "mangup-kale",
            "eski-kermen",
            "eski-kermen-12998470",
            "tepe-kermen",
            "tepe-kermen-26266489",
            "peschernyi-gorod-tepe-kermen-44440866",
            "bakla",
        }
    ),
    "swallow": frozenset({"swallow-nest", "lastochkino-gnezdo-03635688"}),
    "fiolent": frozenset({"cape-fiolent"}),
    "ai-petri": frozenset({"ai-petri"}),
    "new-svet": frozenset({"golitsyn-trail", "tsar-beach", "novy-svet"}),
}


@dataclass(frozen=True)
class StopFact:
    place_id: UUID | None
    slug: str | None
    locality: str | None
    lat: float | None
    lng: float | None
    distance: int | None
    recorded_at: datetime | None
    position: int

    @property
    def nearby(self) -> bool:
        return self.distance is not None and 0 <= self.distance <= 500


@dataclass(frozen=True)
class RunFact:
    route_id: UUID | None
    completed_at: datetime | None
    distance_m: int
    seaside: bool
    extreme: bool
    generated_for_user: bool
    stops: tuple[StopFact, ...] = ()


@dataclass
class Facts:
    runs: list[RunFact] = field(default_factory=list)
    stops: list[StopFact] = field(default_factory=list)
    counters: dict[str, float] = field(default_factory=dict)
    soon: set[str] = field(default_factory=set)
    flagged: bool = False
    counter_times: dict[str, list[datetime]] = field(default_factory=dict)


def progress(facts: Facts, now: datetime) -> dict[str, float]:
    result = dict(facts.counters)
    runs = facts.runs
    distance = sum(run.distance_m for run in runs) / 1000
    result.update(distance=distance, berlin=distance, veteran=len(runs))
    result["first-step"] = float(bool(runs))
    result["same-way"] = float(
        any(
            count >= 2
            for count in Counter(run.route_id for run in runs if run.route_id is not None).values()
        )
    )
    result["water"] = result["sea-breeze"] = sum(run.seaside for run in runs)
    result["legend-path"] = float(any(run.extreme for run in runs))
    result["navigator"] = float(any(run.generated_for_user for run in runs))
    timed = sorted(
        ((run.completed_at, run.distance_m) for run in runs if run.completed_at is not None),
        key=lambda item: item[0],
    )
    # The award is evaluated against every historical seven-day window. The
    # current progress is separately bounded to the most recent seven days.
    result["marathoner"] = (
        sum(meters for at, meters in timed if now - timedelta(days=7) <= at <= now) / 1000
    )
    left = total = 0
    best = 0
    for right, (finished, meters) in enumerate(timed):
        total += meters
        while left <= right and finished - timed[left][0] > timedelta(days=7):
            total -= timed[left][1]
            left += 1
        best = max(best, total)
    result["_marathoner_award"] = best / 1000
    months = {at.astimezone(CRIMEA).month for at, _ in timed}
    result["winter"] = float(1 in months)
    result["season"] = int(bool(months & {12, 1, 2})) + int(bool(months & {6, 7, 8}))
    result["local"] = len(
        {
            stop.place_id
            for run in runs
            for stop in run.stops
            if stop.nearby and stop.place_id is not None
        }
    )
    for key, slugs in PLACE_SLUGS.items():
        result[key] = float(any(stop.nearby and stop.slug in slugs for stop in facts.stops))
    result["bakhchisaray"] = float(
        any(stop.nearby and stop.locality == "bakhchisaray" for stop in facts.stops)
    )
    result["sunrise"] = 0
    for stop in facts.stops:
        at = stop.recorded_at
        if not stop.nearby or at is None or stop.lat is None or stop.lng is None:
            continue
        solar = sunrise_sunset(at.astimezone(CRIMEA).date(), stop.lat, stop.lng)
        if solar is not None and solar[0] <= at <= solar[0] + timedelta(hours=1):
            result["sunrise"] = 1
    result["night"] = result["yalta-lights"] = 0
    for run in runs:
        at = run.completed_at
        if at is None or not run.stops:
            continue
        last = max(run.stops, key=lambda stop: (stop.recorded_at or at, stop.position))
        local_at = at.astimezone(CRIMEA)
        if (
            last.nearby
            and last.locality == "yalta"
            and (local_at.hour, local_at.minute, local_at.second) > (18, 0, 0)
        ):
            result["yalta-lights"] = 1
        if last.lat is not None and last.lng is not None:
            solar = sunrise_sunset(local_at.date(), last.lat, last.lng)
            if solar is not None and at > solar[1]:
                result["night"] = 1
    return result


def historical_unlock_time(facts: Facts, slug: str, target: float, fallback: datetime) -> datetime:
    """Recover the first known qualifying event; never trust phone timestamps."""
    if slug in facts.counter_times:
        times = sorted(facts.counter_times[slug])
        index = int(target) - 1
        return times[index] if len(times) > index else fallback
    moments = sorted(
        {
            at
            for at in [
                *(run.completed_at for run in facts.runs),
                *(stop.recorded_at for stop in facts.stops),
            ]
            if at is not None
        }
    )
    for moment in moments:
        partial = Facts(
            runs=[
                run
                for run in facts.runs
                if run.completed_at is not None and run.completed_at <= moment
            ],
            stops=[
                stop
                for stop in facts.stops
                if stop.recorded_at is not None and stop.recorded_at <= moment
            ],
        )
        if (
            progress(partial, moment).get("_marathoner_award" if slug == "marathoner" else slug, 0)
            >= target
        ):
            return moment
    return fallback
