"""Find venues, using the sourced guide when live lookups fail."""

import json
from pathlib import Path
from typing import NamedTuple

from constraints import suitable
from scoring import evidence
from routing import position
from providers import PROVIDER_ERRORS, geocode, live_data_enabled, live_places


class PlaceSearch(NamedTuple):
    """Venues after filtering, the trip origin, the data source and the raw count."""

    places: list[dict]
    origin: tuple[float, float] | None
    source: str
    total_found: int


CATALOGUE = json.loads((Path(__file__).parent / "data/places.json").read_text())
FALLBACK_CITIES = sorted({place["city"] for place in CATALOGUE})


async def find_places(client, preferences, interests, limits, *, focused):
    """Merge nearby results and the city guide, then apply the visitor's hard constraints.

    Guide records win when both sources name the same place. A single explicit
    interest limits the search to matching venues and required activities."""
    local = [p for p in CATALOGUE if p["city"] == preferences.city]
    starting = preferences.starting_area.strip().casefold()
    match = next((p for p in local if starting and p["name"].casefold() == starting), None)
    origin = position(match) if match else None
    live = []
    if live_data_enabled():
        try:
            centre = origin or await geocode(client, preferences.city, preferences.starting_area)
            if starting:
                origin = centre
            if centre:
                topics = list(dict.fromkeys([*interests, *limits.required, "food"]))
                live = await live_places(client, preferences, centre, topics, limits.diet)
        except PROVIDER_ERRORS:
            pass
    merged = {p["name"].casefold(): p for p in [*live, *local]}
    records = [{"food_role": "", **place} for place in merged.values()]
    eligible = [place for place in records if suitable(place, limits)]
    if focused:
        # A sole interest defines the outing. Only an explicitly required
        # additional activity (for example, lunch on a books outing) can expand it.
        eligible = [
            place
            for place in eligible
            if any(evidence(place, topic) == 1 for topic in limits.required)
            or ("meal" in limits.required and place["food_role"] == "meal")
        ]
    source = "Sourced city guide"
    if live:
        source = "Live place search and city guide" if local else "Live place search"
    return PlaceSearch(eligible, origin, source, len(records))


def no_places_message(search, preferences, interests, limits):
    """Explain whether the city, a dietary requirement or an interest blocked all venues."""
    if not search.total_found:
        return (
            f"No place data available for {preferences.city}. The sourced guides cover "
            + ", ".join(FALLBACK_CITIES)
            + "."
        )
    if interests == ["food"] and limits.food_uncertain:
        return (
            f"I couldn't verify a food stop in {preferences.city} for "
            + ", ".join(limits.food_uncertain)
            + "."
        )
    if preferences.interests and len(interests) == 1:
        return (
            f"I couldn't find a {interests[0]} stop in the {preferences.city} results that meets "
            "your requirements."
        )
    return "No eligible venue meets the specified constraints."
