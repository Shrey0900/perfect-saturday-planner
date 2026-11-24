"""Estimate transfers locally and optionally refine walking legs with Geoapify."""

import math
from math import ceil

from constraints import Flag
from providers import PROVIDER_ERRORS, live_data_enabled, walk_route

# Local travel assumptions. Fares are per vehicle, distances in kilometres, time in minutes.
ROAD_DETOUR_FACTOR = 1.4
WALK_KMPH = 4.2
WALK_START_MINUTES = 2
AUTO_KMPH = 16
AUTO_PICKUP_MINUTES = 7
AUTO_SEATS = 3
AUTO_BASE_FARE_LOW = 35
AUTO_BASE_FARE_HIGH = 65
AUTO_PER_KM_LOW = 13
AUTO_PER_KM_HIGH = 23
TIRED_WALK_LIMIT = 20
STANDARD_WALK_LIMIT = 32
LOW_BUDGET_INR = 350
LOW_BUDGET_WALK_LIMIT = 40
LIVE_ROUTE_LOOKUP_LIMIT = 3


def km(start, end):
    """Return the great-circle distance in kilometres between two latitude/longitude pairs."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*start, *end))
    haversine = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 6371 * 2 * math.asin(min(1, math.sqrt(haversine)))


def position(place):
    return place["lat"], place["lon"]


def estimate_leg(start, end, brief):
    """Model one transfer, including group auto fares when the walk is too long."""
    distance = 0 if start is None else km(start, end) * ROAD_DETOUR_FACTOR
    walk = ceil(distance / WALK_KMPH * 60) + WALK_START_MINUTES if distance else 0
    leg = {
        "mode": "walk",
        "minutes": walk,
        "low": 0,
        "high": 0,
        "distance": round(distance, 1),
        "source": "local distance estimate",
    }
    # Preserve a free walk for short, low-budget routes. Long transfers may
    # still use an auto, and the schedule will check its complete cost.
    reserve_cash = (
        brief.preferences.budget < LOW_BUDGET_INR
        and walk <= LOW_BUDGET_WALK_LIMIT
        and Flag.NO_WALK not in brief.limits.flags
    )
    if (
        not reserve_cash
        and walk
        and (
            walk > (TIRED_WALK_LIMIT if brief.tired else STANDARD_WALK_LIMIT)
            or Flag.NO_WALK in brief.limits.flags
        )
        and Flag.WALK_ONLY not in brief.limits.flags
    ):
        vehicles = ceil(brief.preferences.people / AUTO_SEATS)
        leg.update(
            mode="auto",
            walk_alternative=walk,
            minutes=ceil(distance / AUTO_KMPH * 60) + AUTO_PICKUP_MINUTES,
            low=vehicles * (AUTO_BASE_FARE_LOW + ceil(AUTO_PER_KM_LOW * distance)),
            high=vehicles * (AUTO_BASE_FARE_HIGH + ceil(AUTO_PER_KM_HIGH * distance)),
            source="modelled auto fare and time",
        )
    return leg


def route_legs(stops, brief, cache):
    """Reuse per-request transfer estimates for routes that share a pair of coordinates."""
    legs = []
    previous = brief.origin
    for place in stops:
        end = position(place)
        pair = (previous, end)
        if pair not in cache:
            cache[pair] = estimate_leg(previous, end, brief)
        legs.append(cache[pair])
        previous = end
    return legs


async def estimate_route(client, stops, brief, local_cache, lookups):
    """Copy the candidate's local estimates and refine up to three walking legs per request.

    Cache both successful and failed lookups so another candidate can reuse them."""
    legs = [dict(leg) for leg in route_legs(stops, brief, local_cache)]
    previous = brief.origin
    for stop, leg in zip(stops, legs):
        end = position(stop)
        key = (previous, end)
        if live_data_enabled() and leg["mode"] == "walk" and leg["minutes"]:
            if key not in lookups and len(lookups) < LIVE_ROUTE_LOOKUP_LIMIT:
                try:
                    result = await walk_route(client, [previous, end])
                    lookups[key] = result[0] if result else None
                except PROVIDER_ERRORS:
                    lookups[key] = None
            if lookups.get(key) is not None:
                leg.update(minutes=lookups[key], source="Geoapify walking route")
            elif key in lookups:
                leg["source"] = "local walking estimate; live route unavailable"
        previous = end
    return legs
