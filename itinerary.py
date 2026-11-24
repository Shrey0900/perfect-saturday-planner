"""Search a bounded set of itineraries; keep every time and rupee auditable."""

import re
from collections import Counter
from dataclasses import dataclass, field
from math import ceil

from preferences import DIETARY_UNKNOWN, NON_VEG, classify, duration, normalize_diet_spelling
from providers import km


def clock(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def position(place):
    return place["lat"], place["lon"]


@dataclass
class Limits:
    diet: str = ""
    excluded: set = field(default_factory=set)
    required: set = field(default_factory=set)
    flags: set = field(default_factory=set)
    food_uncertain: list = field(default_factory=list)
    unsupported: list = field(default_factory=list)


def interpret_constraints(preferences):
    limits = Limits()
    for raw in preferences.constraints:
        item = normalize_diet_spelling(raw.lower().strip())
        if not item or item in {"none", "no restrictions", "no constraints"}:
            continue
        if re.search(DIETARY_UNKNOWN, item):
            limits.food_uncertain.append(raw)
            continue
        if re.search(NON_VEG, item) or item == "meat required":
            if re.search(r"\bonly\b|\bmust\b|\brequired?\b", item):
                limits.food_uncertain.append("a confirmed non-vegetarian meal")
            item = re.sub(NON_VEG, "", item)
            if not re.search(r"\bveg(?:etarian)?\b", item):
                continue
        if "vegan" in item:
            limits.diet = "vegan"
        elif re.search(r"pure[ -]?veg|veg(?:etarian)?[ -]?only|only vegetarian", item):
            if limits.diet != "vegan":
                limits.diet = "only"
        elif re.search(r"\bveg(?:etarian)?\b", item):
            if not limits.diet:
                limits.diet = "options"
        elif re.search("wheelchair|step.free|accessible entrance|no stairs", item):
            limits.flags.add("accessible")
        elif re.search("no walking|cannot walk|can't walk|no walks", item):
            limits.flags.add("no_walk")
        elif re.search("walking only|walk only|no (cabs?|autos?|taxis?)", item):
            limits.flags.add("walk_only")
        elif re.search("crowd|quiet|peaceful", item):
            limits.flags.add("crowds")
            if re.search("must|only|guarantee|strictly", item):
                limits.unsupported.append("confirmed quiet conditions")
        elif re.search("live (music|performance|show|gig)", item):
            limits.unsupported.append("a confirmed performance this Saturday")
        elif "skip meals" in item or re.search("no (lunch|meals?|food)", item):
            limits.flags.add("skip_meals")
        elif re.search("indoors?|avoid (?:the )?(?:heat|sun|outdoors?)", item):
            limits.flags.add("indoors")
        elif "outdoor" in item:
            limits.flags.add("outdoors")
        elif re.search(r"\b(no|avoid|skip|dislike)\b", item) and classify(item):
            limits.excluded.add(classify(item))
        elif re.search(
            r"meal required|\b(must|need|include|mandatory|required)\b", item
        ) and classify(item):
            target = classify(item)
            limits.required.add("meal" if target == "food" else target)
        else:
            limits.unsupported.append(raw)
    return limits


def suitable(place, limits):
    tags = set(place["tags"])
    if limits.excluded.intersection(tags):
        return False
    if "accessible" in limits.flags and not (
        place.get("accessible") or place.get("wheelchair") is True
    ):
        return False
    if "indoors" in limits.flags and ("outdoors" in tags or place["kind"] == "park"):
        return False
    if "outdoors" in limits.flags and "outdoors" not in tags and place["kind"] != "park":
        return False
    if "no_walk" in limits.flags and ("walks" in tags or place["kind"] == "park"):
        return False
    if place["kind"] == "restaurant":
        if limits.food_uncertain or "skip_meals" in limits.flags:
            return False
        allowed = {"vegan": {"vegan"}, "only": {"only"}, "options": {"only", "options", "vegan"}}
        return not limits.diet or place["veg"] in allowed[limits.diet]
    return True


@dataclass
class Brief:
    preferences: object
    limits: Limits
    interests: list
    origin: tuple | None
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
    """Provisional and final schedules use identical arithmetic."""
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
    if topic in place.get("primary_tags", place["tags"]):
        return 1.0
    if topic in place["tags"]:
        return 0.28
    return 0.0


def includes_lunch(plan):
    return any(
        s["place"].get("food_role") == "meal" and 705 <= s["start_minute"] <= 840
        for s in plan["stops"]
    )


def priorities(plan, brief):
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
    goal += 26 * min(visit_minutes / (brief.minutes * (0.68 if brief.tired else 0.82)), 1)
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
    places = [s["place"] for s in plan["stops"]]
    for requested in brief.limits.required:
        if requested == "meal":
            if not any(p.get("food_role") == "meal" for p in places):
                return False
        elif not any(evidence(p, requested) == 1 for p in places):
            return False
    return True


def build_candidates(places, brief):
    # Beam search preserves distinct endings, so a modestly ranked stop can lead
    # into a strong lunch or music itinerary. Keep work bounded for live results.
    frontier = [()]
    options = []
    cache = {}
    rejections = Counter()
    closest = {}
    max_stops = 5 if brief.minutes >= 300 else 4 if brief.minutes >= 150 else 3
    for depth in range(max_stops):
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
                    if reason not in closest or amount < closest[reason][0]:
                        closest[reason] = (amount, place["name"])
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
            if len(frontier) >= 64:
                break
        if not frontier:
            break
    options.sort(key=lambda item: -item[0])
    return options, cache, rejections, closest


def validate_plan(stops, legs, brief):
    plan, failure = schedule(stops, legs, brief)
    if plan and not meets_required(plan, brief):
        return None, ("required activity", 1)
    return plan, failure
