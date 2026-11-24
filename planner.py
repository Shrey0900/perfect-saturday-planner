"""Stream planning tools in order, ending with a plan, clarification or no-match result."""

import httpx
from pydantic import ValidationError

from constraints import interpret_constraints
from explanations import explain_failure, finish_plan
from interests import interest_topics
from itinerary import Brief, build_candidates, record_closest_failure, validate_plan
from places import find_places, no_places_message
from routing import estimate_route
from scoring import priorities
from preferences import (
    InputFormatError,
    canonical_city,
    decode_input,
    duration,
    local_parse,
    questions,
)
from providers import PROVIDER_ERRORS, model_parse


def event(tool, inputs, outputs, message):
    """Describe one completed tool call for the browser's progress trace."""
    return {
        "type": "progress",
        "tool": tool,
        "inputs": inputs,
        "outputs": outputs,
        "message": message,
    }


async def parse_preferences(client, value):
    """Parse JSON directly or fall back from the optional LLM to the local text classifier."""
    preferences = decode_input(value)
    method = "Structured input"
    if preferences is None:
        try:
            preferences = await model_parse(client, value)
        except PROVIDER_ERRORS:
            preferences = None
        if preferences is None:
            preferences = local_parse(value)
            method = "Local interest classifier"
        else:
            method = "Language model"
    preferences.city = canonical_city(preferences.city)
    preferences.interests = list(
        dict.fromkeys(
            topic
            for label in preferences.interests
            for topic in (interest_topics(label) or [label.lower().strip()])
        )
    )
    return preferences, method


def result_event(kind, message, preferences=None):
    """Carry parsed fields into the form when an outing needs clarification or changes."""
    return {
        "type": kind,
        "message": message,
        "preferences": preferences.model_dump() if preferences else {},
    }


async def run_plan(value):
    """Yield tool events for a single request and finish with exactly one user-facing result.

    Ask for missing details before searching. If places qualify, pass the resulting
    brief to select_itinerary for ranking and final route checks."""
    async with httpx.AsyncClient(timeout=12, trust_env=False) as client:
        try:
            prefs, method = await parse_preferences(client, value)
        except ValidationError as error:
            detail = error.errors()[0]
            field = ".".join(map(str, detail["loc"]))
            yield result_event("clarify", f"Please check {field}: {detail['msg']}.")
            return
        except InputFormatError:
            yield result_event(
                "clarify", "Please use valid JSON or describe your Saturday in plain text."
            )
            return
        yield event(
            "parse_preferences",
            {"format": method},
            {
                "city": prefs.city,
                "budget": prefs.budget,
                "minutes": duration(prefs.available_time),
                "interests": prefs.interests,
                "constraints": prefs.constraints,
            },
            method + " interpreted your request.",
        )

        missing = questions(prefs, value)
        if missing:
            yield result_event("clarify", " ".join(missing[:2]), prefs)
            return
        limits = interpret_constraints(prefs)
        if limits.unsupported:
            yield result_event(
                "empty",
                "The venue records cannot verify "
                + ", ".join(limits.unsupported)
                + ". Edit this requirement if it is flexible.",
                prefs,
            )
            return
        brief = Brief.create(prefs, limits)
        search = await find_places(client, prefs, brief.interests, limits, focused=brief.focused)
        yield event(
            "find_places",
            {"city": prefs.city, "interests": brief.interests, "constraints": prefs.constraints},
            {"found": search.total_found, "eligible": len(search.places), "source": search.source},
            f"{search.source}: {len(search.places)} of {search.total_found} places qualified.",
        )
        if prefs.starting_area and not search.origin:
            yield result_event(
                "clarify",
                "I couldn't locate that starting point. Enter a named venue or leave it blank to "
                "start at your first stop.",
                prefs,
            )
            return
        if not search.places:
            yield result_event(
                "empty", no_places_message(search, prefs, brief.interests, limits), prefs
            )
            return
        brief.origin = search.origin
        async for update in select_itinerary(client, brief, search.places, search.source):
            yield update


async def select_itinerary(client, brief, places, source):
    """Try feasible candidates in score order until one passes final transfer validation."""
    search = build_candidates(places, brief)
    yield event(
        "build_candidates",
        {"venues": len(places), "minutes": brief.minutes, "budget": brief.preferences.budget},
        {
            "feasible_previews": len(search.ranked),
            "rejected": dict(search.rejections),
            "top_routes": [
                {"stops": [stop["name"] for stop in stops], "score": round(score, 3)}
                for score, stops in search.ranked[:2]
            ],
        },
        f"Ranked {len(search.ranked)} feasible provisional itineraries.",
    )
    lookups = {}
    for rank_score, stops in search.ranked:
        legs = await estimate_route(client, stops, brief, search.leg_cache, lookups)
        names = [place["name"] for place in stops]
        yield event(
            "estimate_route",
            {"stops": names},
            {"legs": legs},
            f"Estimated {sum(leg['minutes'] for leg in legs)} minutes of travel.",
        )
        plan, failure = validate_plan(stops, legs, brief)
        if failure:
            reason, gap = failure
            yield event(
                "validate_plan",
                {"stops": names},
                {"passed": False, "reason": reason, "gap": gap},
                "Rechecked route failed on " + reason + ".",
            )
            record_closest_failure(search.closest, reason, gap, names[-1])
            continue
        yield event(
            "validate_plan",
            {"stops": names, "time_limit": brief.minutes, "budget_limit": brief.preferences.budget},
            {
                "passed": True,
                "minutes": plan["total"],
                "upper_inr": plan["high"],
                "provisional_rank_score": round(rank_score, 3),
                "validated_score": priorities(plan, brief).trace(),
            },
            f"Verified {plan['total']} minutes and up to ₹{plan['high']:,}, including transfers.",
        )
        yield finish_plan(plan, brief, places, search.leg_cache, source, search.ranked)
        return
    yield result_event("empty", explain_failure(search.closest, brief, places), brief.preferences)
