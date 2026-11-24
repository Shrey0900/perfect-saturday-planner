"""Search a bounded set of itineraries; keep every time and rupee auditable."""

import re
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from typing import NamedTuple

from constraints import Flag, Limits
from interests import KNOWN_TOPICS
from preferences import Preferences, duration
from routing import route_legs
from scoring import LONG_WINDOW_MINUTES, evidence, priorities

BEAM_WIDTH = 64
TRANSFER_BUFFER = 5
FIRST_STOP_WAIT_LIMIT = 60
LATER_STOP_WAIT_LIMIT = 30
DAYTIME_HOURS = (10 * 60, 18 * 60)
LUNCH_WINDOW = (12 * 60, 14 * 60 + 30)
MIN_OUTING_FOR_LUNCH = 180
MIN_LUNCH_OVERLAP = 45
MEDIUM_WINDOW_MINUTES = 150
DEFAULT_INTERESTS = ("walks", "food", "art")


class Failure(StrEnum):
    WALKING = "walking restriction"
    OPENS_LATER = "opens later"
    CLOSING = "closing time"
    BUDGET = "budget"
    TIME = "time"
    REQUIRED = "required activity"


def clock(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


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
    focused: bool

    @classmethod
    def create(cls, preferences, limits, interests=None, origin=None):
        """Derive the active interests, pace and lunch policy once for all planning tools."""
        requested = preferences.interests if interests is None else interests
        interests = [tag for tag in (requested or DEFAULT_INTERESTS) if tag not in limits.excluded]
        focused = bool(requested) and len(interests) == 1 and interests[0] in KNOWN_TOPICS
        if focused:
            limits.required.add(interests[0])
        hours, minutes = map(int, preferences.start_time.split(":"))
        start = 60 * hours + minutes
        duration_minutes = duration(preferences.available_time)
        lunch_overlap = min(start + duration_minutes, LUNCH_WINDOW[1]) - max(start, LUNCH_WINDOW[0])
        lunch = (
            duration_minutes >= MIN_OUTING_FOR_LUNCH
            and lunch_overlap >= MIN_LUNCH_OVERLAP
            and Flag.SKIP_MEALS not in limits.flags
            and "food" not in limits.excluded
            and not limits.food_uncertain
            and (len(interests) != 1 or "food" in interests or "meal" in limits.required)
        )
        tired = bool(
            re.search(
                "tired|low.energy|relax|gentle|slow|exhaust|thak", preferences.mood, re.IGNORECASE
            )
        )
        return cls(
            preferences, limits, interests, origin, start, duration_minutes, tired, lunch, focused
        )


def visit_length(place, brief, start, closing):
    """Use the gentle pace when available and trim only visits with a documented minimum."""
    preferred = place.get("short_minutes", place["minutes"]) if brief.tired else place["minutes"]
    if "min_minutes" not in place:
        return preferred
    remaining = min(closing, brief.start + brief.minutes) - start
    return max(place["min_minutes"], min(preferred, remaining))


class VisitTiming(NamedTuple):
    start: int
    wait: int
    visit: int
    end: int


def check_opening(place, brief, arrival, *, first):
    """Resolve the opening wait and visit duration before charging for this stop."""
    opening, closing = place.get("hours") or DAYTIME_HOURS
    wait = max(0, opening - arrival)
    if wait > (FIRST_STOP_WAIT_LIMIT if first else LATER_STOP_WAIT_LIMIT):
        return None, (Failure.OPENS_LATER, wait)
    start = arrival + wait
    visit = visit_length(place, brief, start, closing)
    end = start + visit
    if end > closing:
        return None, (Failure.CLOSING, end - closing)
    return VisitTiming(start, wait, visit, end), None


def schedule(stops, legs, brief):
    """Build a timeline or return the first feasibility failure and its numeric gap.

    Count travel, buffers, opening waits and visits against the time window.
    Budget checks use each venue's upper price per person plus transport for the group.
    Candidate ranking and final validation both call this function.
    """
    cursor = brief.start
    low = high = 0
    timeline = []
    for place, leg in zip(stops, legs):
        travel = leg["minutes"]
        buffer = TRANSFER_BUFFER if travel else 0
        if Flag.NO_WALK in brief.limits.flags and leg["mode"] == "walk" and travel:
            return None, (Failure.WALKING, travel)
        arrival = cursor + travel + buffer
        timing, failure = check_opening(place, brief, arrival, first=not timeline)
        if failure:
            return None, failure
        cursor, wait, visit, end = timing
        low += place["low"] * brief.preferences.people + leg["low"]
        high += place["high"] * brief.preferences.people + leg["high"]
        if high > brief.preferences.budget:
            return None, (Failure.BUDGET, high - brief.preferences.budget)
        if end - brief.start > brief.minutes:
            # If removing the wait alone would make the visit fit, suggest a later start.
            if wait and end - wait - brief.start <= brief.minutes:
                return None, (Failure.OPENS_LATER, wait)
            return None, (Failure.TIME, end - brief.start - brief.minutes)
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


class CandidateSearch(NamedTuple):
    ranked: list
    leg_cache: dict
    rejections: Counter
    closest: dict


def next_stops(route, places):
    """Never repeat a venue or schedule two full meals in the same outing."""
    has_meal = any(place["food_role"] == "meal" for place in route)
    return (
        place
        for place in places
        if place not in route and not (has_meal and place["food_role"] == "meal")
    )


def best_frontier(expanded):
    """Keep the best distinct visit sets and endpoints for the next search depth."""
    frontier = []
    seen = set()
    for _, route in sorted(expanded, key=lambda item: -item[0]):
        key = (frozenset(place["id"] for place in route), route[-1]["id"])
        if key not in seen:
            seen.add(key)
            frontier.append(route)
        if len(frontier) >= BEAM_WIDTH:
            break
    return frontier


def extend_route(route, places, brief, search):
    """Score feasible extensions and record why a rejected extension failed."""
    for place in next_stops(route, places):
        stops = route + (place,)
        legs = route_legs(stops, brief, search.leg_cache)
        proposed, failure = schedule(stops, legs, brief)
        if failure:
            reason, gap = failure
            search.rejections[reason] += 1
            record_closest_failure(search.closest, reason, gap, place["name"])
            continue
        entry = (priorities(proposed, brief).total, stops)
        if meets_required(proposed, brief):
            search.ranked.append(entry)
        yield entry


def build_candidates(places, brief):
    """Search up to three, four or five stops, keeping at most BEAM_WIDTH routes per depth."""
    search = CandidateSearch([], {}, Counter(), {})
    frontier = [()]
    max_stops = 3
    if brief.minutes >= LONG_WINDOW_MINUTES:
        max_stops = 5
    elif brief.minutes >= MEDIUM_WINDOW_MINUTES:
        max_stops = 4
    for _ in range(max_stops):
        expanded = (
            entry for route in frontier for entry in extend_route(route, places, brief, search)
        )
        frontier = best_frontier(expanded)
        if not frontier:
            break
    search.ranked.sort(key=lambda item: -item[0])
    return search


def validate_plan(stops, legs, brief):
    """Rebuild the schedule using the final legs and recheck the mandatory activities."""
    plan, failure = schedule(stops, legs, brief)
    if plan and not meets_required(plan, brief):
        return None, (Failure.REQUIRED, 1)
    return plan, failure
