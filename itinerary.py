"""Search a bounded set of itineraries; keep every time and rupee auditable."""

import re
from collections import Counter
from dataclasses import dataclass
from math import ceil

from constraints import Limits
from preferences import Preferences, duration
from providers import km

BEAM_WIDTH = 64


def clock(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def position(place):
    return place["lat"], place["lon"]


@dataclass
class Brief:
    """Planning inputs with a starting minute, time limit and derived pace and lunch preferences."""

    preferences: Preferences
    limits: Limits
    interests: list[str]
    origin: tuple[float, float] | None
    start: int
    minutes: int
    tired: bool
    lunch: bool

    @classmethod
    def create(cls, preferences, limits, interests, origin):
        hours, minutes = map(int, preferences.start_time.split(":"))
        start = 60 * hours + minutes
        duration_minutes = duration(preferences.available_time)
        lunch_overlap = min(start + duration_minutes, 14 * 60 + 30) - max(start, 12 * 60)
        lunch = (
            duration_minutes >= 180
            and lunch_overlap >= 45
            and "skip_meals" not in limits.flags
            and "food" not in limits.excluded
            and not limits.food_uncertain
            and (len(interests) != 1 or "food" in interests or "meal" in limits.required)
        )
        tired = bool(
            re.search(
                "tired|low.energy|relax|gentle|slow|exhaust|thak", preferences.mood, re.IGNORECASE
            )
        )
        return cls(preferences, limits, interests, origin, start, duration_minutes, tired, lunch)


def estimate_leg(start, end, brief):
    """Model one transfer, including group auto fares when the walk is too long."""
    distance = 0 if start is None else km(start, end) * 1.4
    walk = ceil(distance / 4.2 * 60) + 2 if distance else 0
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
        brief.preferences.budget < 350 and walk <= 40 and "no_walk" not in brief.limits.flags
    )
    if (
        not reserve_cash
        and walk
        and (walk > (20 if brief.tired else 32) or "no_walk" in brief.limits.flags)
        and "walk_only" not in brief.limits.flags
    ):
        vehicles = ceil(brief.preferences.people / 3)
        leg.update(
            mode="auto",
            walk_alternative=walk,
            minutes=ceil(distance / 16 * 60) + 7,
            low=vehicles * (35 + ceil(13 * distance)),
            high=vehicles * (65 + ceil(23 * distance)),
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


def schedule(stops, legs, brief):
    """Build a timeline or return the first feasibility failure and its numeric gap.

    Count travel, buffers, opening waits and visits against the time window.
    Budget checks use each venue’s upper price per person plus transport for the group.
    Candidate ranking and final validation both call this function.
    """
    cursor = brief.start
    low = high = 0
    timeline = []
    for place, leg in zip(stops, legs):
        travel = leg["minutes"]
        buffer = 5 if travel else 0
        if "no_walk" in brief.limits.flags and leg["mode"] == "walk" and travel:
            return None, ("walking restriction", travel)
        cursor += travel + buffer
        opening, closing = place.get("hours") or (600, 1080)
        wait = max(0, opening - cursor)
        if wait > (60 if not timeline else 30):
            return None, ("opens later", wait)
        cursor += wait
        visit = place["minutes"]
        if "min_minutes" in place:
            remaining = min(closing, brief.start + brief.minutes) - cursor
            visit = max(place["min_minutes"], min(visit, remaining))
        end = cursor + visit
        if end > closing:
            return None, ("closing time", end - closing)
        low += place["low"] * brief.preferences.people + leg["low"]
        high += place["high"] * brief.preferences.people + leg["high"]
        if high > brief.preferences.budget:
            return None, ("budget", high - brief.preferences.budget)
        if end - brief.start > brief.minutes:
            if wait and end - wait - brief.start <= brief.minutes:
                return None, ("opens later", wait)
            return None, ("time", end - brief.start - brief.minutes)
        timeline.append(
            {
                "place": place if visit == place["minutes"] else {**place, "minutes": visit},
                "start": clock(cursor),
                "end": clock(end),
                "start_minute": cursor,
                "leg": leg,
                "walk": travel if leg["mode"] == "walk" else 0,
                "buffer": buffer,
                "wait": wait,
            }
        )
        cursor = end
    elapsed = cursor - brief.start
    plan = {
        "stops": timeline,
        "low": low,
        "high": high,
        "total": elapsed,
        "spare": brief.minutes - elapsed,
        "budget_left": brief.preferences.budget - high,
        "walk": sum(s["walk"] for s in timeline),
    }
    return plan, None


def evidence(place, topic):
    """Return a primary, incidental or absent match for a venue and interest."""
    if topic in place.get("primary_tags", place["tags"]):
        return 1.0
    if topic in place["tags"]:
        return 0.28
    return 0.0


def is_lunch_time(start_minute):
    return 11 * 60 + 45 <= start_minute <= 14 * 60


def includes_lunch(plan):
    return any(
        s["place"].get("food_role") == "meal" and is_lunch_time(s["start_minute"])
        for s in plan["stops"]
    )


def priorities(plan, brief):
    """Score a feasible route on interest coverage, visit time, lunch, crowds, travel and cost."""
    places = [stop["place"] for stop in plan["stops"]]
    matches = [max(evidence(place, tag) for place in places) for tag in brief.interests]
    # A primary match should beat filling the same time with incidental stops.
    goal = sum(matches) * 25 + 65 * matches.count(1.0)
    for place in places:
        relevance = max((evidence(place, tag) for tag in brief.interests), default=0)
        goal += 4 * relevance
        if not relevance and brief.minutes < 300 and place.get("food_role") != "meal":
            goal -= 18
    visit_minutes = sum(place["minutes"] for place in places)
    target_visit_minutes = brief.minutes * (0.68 if brief.tired else 0.82)
    goal += 26 * min(visit_minutes / target_visit_minutes, 1)
    if brief.minutes >= 300:
        goal += 0.35 * min(visit_minutes, 0.65 * brief.minutes)
    goal += len({place["kind"] for place in places}) * 2
    if brief.lunch and includes_lunch(plan):
        goal += 75
    if "crowds" in brief.limits.flags:
        goal -= sum(
            {"low": 0, "medium": 2, "high": 12}.get(place.get("crowd"), 5) for place in places
        )
    travel = sum(stop["leg"]["minutes"] for stop in plan["stops"])
    goal -= 0.10 * travel + (0.11 if brief.tired else 0.04) * plan["walk"]
    goal -= 0.30 * sum(stop["wait"] for stop in plan["stops"])
    goal -= plan["high"] / max(1, brief.preferences.budget) * 2
    return goal


def meets_required(plan, brief):
    """Check that the candidate includes every mandatory activity or full meal."""
    places = [s["place"] for s in plan["stops"]]
    for requested in brief.limits.required:
        if requested == "meal":
            if not any(p.get("food_role") == "meal" for p in places):
                return False
        elif not any(evidence(p, requested) == 1 for p in places):
            return False
    return True


def record_closest_failure(closest, reason, gap, venue_name):
    """Keep the smallest gap for this reason across search and final validation."""
    if reason not in closest or gap < closest[reason][0]:
        closest[reason] = (gap, venue_name)


def build_candidates(places, brief):
    """Build routes one stop at a time and rank the feasible ones.

    Keep the best 64 distinct partial routes at each depth. Record the smallest
    time or cost gap for each rejection reason so a failed search can explain why."""
    # Beam search preserves distinct endings, so a modestly ranked stop can lead
    # into a strong lunch or music itinerary. Keep work bounded for live results.
    frontier = [()]
    options = []
    cache = {}
    rejections = Counter()
    closest = {}
    max_stops = 5 if brief.minutes >= 300 else 4 if brief.minutes >= 150 else 3
    for _ in range(max_stops):
        expanded = []
        for route in frontier:
            for place in places:
                if place in route:
                    continue
                if place["food_role"] == "meal" and any(p["food_role"] == "meal" for p in route):
                    continue
                stops = route + (place,)
                legs = route_legs(stops, brief, cache)
                proposed, failure = schedule(stops, legs, brief)
                if failure:
                    reason, amount = failure
                    rejections[reason] += 1
                    record_closest_failure(closest, reason, amount, place["name"])
                    continue
                score = priorities(proposed, brief)
                expanded.append((score, stops))
                if meets_required(proposed, brief):
                    options.append((score, stops))
        expanded.sort(key=lambda item: -item[0])
        frontier = []
        seen = set()
        for _, route in expanded:
            key = (frozenset(p["id"] for p in route), route[-1]["id"])
            if key not in seen:
                seen.add(key)
                frontier.append(route)
            if len(frontier) >= BEAM_WIDTH:
                break
        if not frontier:
            break
    options.sort(key=lambda item: -item[0])
    return options, cache, rejections, closest


def validate_plan(stops, legs, brief):
    """Rebuild the schedule using the final legs and recheck the mandatory activities."""
    plan, failure = schedule(stops, legs, brief)
    if plan and not meets_required(plan, brief):
        return None, ("required activity", 1)
    return plan, failure
