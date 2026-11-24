"""Rank already-feasible routes. Every contribution is in points, and higher wins."""

from dataclasses import asdict, dataclass

from constraints import Flag

# Coverage is awarded once per requested interest. Stop relevance is a smaller tie-breaker.
PRIMARY_TOPIC_POINTS = 90
INCIDENTAL_TOPIC_POINTS = 7
INCIDENTAL_RELEVANCE = 0.28
RELEVANCE_PER_STOP = 4
UNRELATED_SHORT_OUTING_PENALTY = 18
LONG_WINDOW_MINUTES = 300
VISIT_TIME_POINTS = 26
TIRED_VISIT_TARGET = 0.68
STANDARD_VISIT_TARGET = 0.82
LONG_VISIT_POINTS_PER_MINUTE = 0.35
LONG_VISIT_TARGET = 0.65
POINTS_PER_VENUE_KIND = 2
LUNCH_POINTS = 75
LUNCH_START_RANGE = (11 * 60 + 45, 14 * 60)
CROWD_PENALTIES = {"low": 0, "medium": 2, "high": 12}
UNKNOWN_CROWD_PENALTY = 5
TRAVEL_PENALTY_PER_MINUTE = 0.10
TIRED_WALK_PENALTY = 0.11
STANDARD_WALK_PENALTY = 0.04
WAIT_PENALTY_PER_MINUTE = 0.30
FULL_BUDGET_PENALTY = 2


def evidence(place, topic):
    """Return the strength of the documented link between a venue and a topic."""
    if topic in place.get("primary_tags", place["tags"]):
        return 1.0
    return INCIDENTAL_RELEVANCE if topic in place["tags"] else 0.0


def is_lunch_time(start_minute):
    return LUNCH_START_RANGE[0] <= start_minute <= LUNCH_START_RANGE[1]


def includes_lunch(plan):
    return any(
        stop["place"].get("food_role") == "meal" and is_lunch_time(stop["start_minute"])
        for stop in plan["stops"]
    )


@dataclass(frozen=True)
class ScoreBreakdown:
    coverage: float
    stop_relevance: float
    visit_time: float
    variety: float
    lunch: float
    crowds: float
    travel: float
    waiting: float
    cost: float

    @property
    def total(self):
        return (
            self.coverage
            + self.stop_relevance
            + self.visit_time
            + self.variety
            + self.lunch
            + self.crowds
            + self.travel
            + self.waiting
            + self.cost
        )

    def trace(self):
        """Show each signed contribution and the total used for ranking."""
        return {
            "components": {key: round(value, 3) for key, value in asdict(self).items()},
            "total": round(self.total, 3),
        }


def interest_points(places, brief):
    """Count each interest once, then reward how well individual stops serve the outing."""
    coverage = 0
    for topic in brief.interests:
        best_match = max(evidence(place, topic) for place in places)
        if best_match == 1:
            coverage += PRIMARY_TOPIC_POINTS
        elif best_match:
            coverage += INCIDENTAL_TOPIC_POINTS
    relevance = 0
    for place in places:
        match = max((evidence(place, tag) for tag in brief.interests), default=0)
        relevance += RELEVANCE_PER_STOP * match
        if not match and brief.minutes < LONG_WINDOW_MINUTES and place.get("food_role") != "meal":
            relevance -= UNRELATED_SHORT_OUTING_PENALTY
    return coverage, relevance


def visit_time_points(places, brief):
    minutes = sum(place["minutes"] for place in places)
    target = brief.minutes * (TIRED_VISIT_TARGET if brief.tired else STANDARD_VISIT_TARGET)
    points = VISIT_TIME_POINTS * min(minutes / target, 1)
    if brief.minutes >= LONG_WINDOW_MINUTES:
        points += LONG_VISIT_POINTS_PER_MINUTE * min(minutes, LONG_VISIT_TARGET * brief.minutes)
    return points


def crowd_points(places, brief):
    if Flag.CROWDS not in brief.limits.flags:
        return 0
    return -sum(CROWD_PENALTIES.get(p.get("crowd"), UNKNOWN_CROWD_PENALTY) for p in places)


def travel_points(plan, brief):
    transfer = sum(stop["leg"]["minutes"] for stop in plan["stops"])
    walking_rate = TIRED_WALK_PENALTY if brief.tired else STANDARD_WALK_PENALTY
    return -TRAVEL_PENALTY_PER_MINUTE * transfer - walking_rate * plan["walk"]


def priorities(plan, brief):
    """Return the component scores used to compare feasible candidates and explain the winner."""
    places = [stop["place"] for stop in plan["stops"]]
    coverage, relevance = interest_points(places, brief)
    return ScoreBreakdown(
        coverage=coverage,
        stop_relevance=relevance,
        visit_time=visit_time_points(places, brief),
        variety=POINTS_PER_VENUE_KIND * len({place["kind"] for place in places}),
        lunch=LUNCH_POINTS if brief.lunch and includes_lunch(plan) else 0,
        crowds=crowd_points(places, brief),
        travel=travel_points(plan, brief),
        waiting=-WAIT_PENALTY_PER_MINUTE * sum(stop["wait"] for stop in plan["stops"]),
        cost=-FULL_BUDGET_PENALTY * plan["high"] / max(1, brief.preferences.budget),
    )
