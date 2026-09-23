"""NOAA solar equations, with no external runtime dependency.

Reference: https://gml.noaa.gov/grad/solcalc/solareqns.PDF
"""

from calendar import isleap
from datetime import UTC, date, datetime, time, timedelta
from math import acos, cos, degrees, pi, radians, sin, tan


def sunrise_sunset(day: date, lat: float, lng: float) -> tuple[datetime, datetime] | None:
    """Return UTC instants, or None when the sun does not cross the horizon."""
    if not -90 < lat < 90 or not -180 <= lng <= 180:
        return None
    gamma = 2 * pi / (366 if isleap(day.year) else 365) * (day.timetuple().tm_yday - 1)
    eqtime = 229.18 * (
        0.000075
        + 0.001868 * cos(gamma)
        - 0.032077 * sin(gamma)
        - 0.014615 * cos(2 * gamma)
        - 0.040849 * sin(2 * gamma)
    )
    decl = (
        0.006918
        - 0.399912 * cos(gamma)
        + 0.070257 * sin(gamma)
        - 0.006758 * cos(2 * gamma)
        + 0.000907 * sin(2 * gamma)
        - 0.002697 * cos(3 * gamma)
        + 0.00148 * sin(3 * gamma)
    )
    latitude = radians(lat)
    angle = cos(radians(90.833)) / (cos(latitude) * cos(decl)) - tan(latitude) * tan(decl)
    if not -1 <= angle <= 1:
        return None
    hour_angle = degrees(acos(angle))
    midnight = datetime.combine(day, time(), tzinfo=UTC)
    noon = 720 - 4 * lng - eqtime
    return (
        midnight + timedelta(minutes=noon - 4 * hour_angle),
        midnight + timedelta(minutes=noon + 4 * hour_angle),
    )
