"""Numbers behind popularity (spec 19, D43..D45)."""

from __future__ import annotations

#: Only actions this recent count, and the newer ones weigh more (D44).
WINDOW_DAYS = 90
DECAY_HALF_LIFE_DAYS = 45.0

#: A new account's actions do not count yet (D45).
MIN_ACCOUNT_AGE_DAYS = 7

#: Routes: an honest run and a published review weigh more than a favourite
#: or a start (D45).
ROUTE_WEIGHT_COMPLETED = 1.0
ROUTE_WEIGHT_REVIEW = 1.0
ROUTE_WEIGHT_FAVORITE = 0.3
ROUTE_WEIGHT_STARTED = 0.3

#: Places: «был здесь» on a run, a favourite, an opened card.
PLACE_WEIGHT_VISITED = 1.0
PLACE_WEIGHT_FAVORITE = 0.5
PLACE_WEIGHT_VIEWED = 0.2

#: The badge goes to the top tenth, and never to a route fewer than ten
#: different people touched in the window (D43).
BADGE_TOP_SHARE = 0.10
BADGE_MIN_PEOPLE = 10

#: How fast what people do in the app outweighs outside fame for a place:
#: with this many people the two count equally.
PLACE_APP_HALF_PEOPLE = 20
