"""Coordinate the planning tools and stream the evidence behind each decision."""

import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from pydantic import ValidationError

from itinerary import (
    Brief,
    build_candidates,
    evidence,
    includes_lunch,
    interpret_constraints,
    position,
    route_legs,
    schedule,
    suitable,
    validate_plan,
)
from preferences import canonical_city, classify, decode_input, duration, local_parse
from providers import geocode, live_places, model_parse, walk_route

CATALOGUE = json.loads((Path(__file__).parent / "data/places.json").read_text())
FALLBACK_CITIES = sorted({place["city"] for place in CATALOGUE})
BANGALORE_EXAMPLE = "I'm in Bangalore with ₹2,000 and 4 hours. I'm tired but want to get out. I like food, music and walks. I'm vegetarian and want to avoid crowded places."
PROVIDER_ERRORS = (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError)


def event(tool, inputs, outputs, message):
    return {
        "type": "progress",
        "tool": tool,
        "inputs": inputs,
        "outputs": outputs,
        "message": message,
    }


async def parse_preferences(client, value):
    preferences = decode_input(value)
    if preferences is not None:
        return preferences, "Structured input"
    try:
        preferences = await model_parse(client, value)
    except PROVIDER_ERRORS:
        preferences = None
    return (
        (preferences, "Language model")
        if preferences
        else (local_parse(value), "Local interest classifier")
    )


def questions(preferences, value):
    missing = []
    if not preferences.city:
        missing.append("Which Indian city should I plan for?")
    if preferences.budget is None:
        missing.append("What is your total budget in rupees?")
    minutes = duration(preferences.available_time)
    if not 0 < minutes <= 1440:
        missing.append("How much time do you have on Saturday? Try “4 hours” or “90 minutes”.")
    if "clarify group budget" in preferences.constraints or (
        isinstance(value, str)
        and not value.lstrip().startswith("{")
        and preferences.people > 1
        and not re.search(
            r"\b(total|together|each|per (person|head)|shared|between us)\b", value, re.IGNORECASE
        )
    ):
        if preferences.budget is not None:
            missing.append("What is the total budget for the whole group?")
    return missing


async def find_places(client, preferences, interests, limits):
    local = [p for p in CATALOGUE if p["city"] == preferences.city]
    starting = preferences.starting_area.strip().casefold()
    match = next((p for p in local if starting and p["name"].casefold() == starting), None)
    origin = position(match) if match else None
    live = []
    if os.getenv("GEOAPIFY_API_KEY"):
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
    source = (
        "Live place search and city guide"
        if live and local
        else "Live place search"
        if live
        else "Sourced city guide"
    )
    return eligible, origin, source, len(records)


async def estimate_route(client, stops, brief, local_cache, lookups):
    legs = [dict(leg) for leg in route_legs(stops, brief, local_cache)]
    previous = brief.origin
    for stop, leg in zip(stops, legs):
        end = position(stop)
        key = (previous, end)
        if os.getenv("GEOAPIFY_API_KEY") and leg["mode"] == "walk" and leg["minutes"]:
            if key not in lookups and len(lookups) < 3:
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


def explain_stop(stop, brief, previous):
    place = stop["place"]
    matches = [tag for tag in brief.interests if evidence(place, tag) == 1]
    indirect = [tag for tag in brief.interests if 0 < evidence(place, tag) < 1]
    if place.get("food_role") == "meal":
        meal_name = "lunch" if 705 <= stop["start_minute"] <= 840 else "a sit-down meal"
        reason = f"Time for {meal_name} at {stop['start']}. Allow ₹{place['low']}–₹{place['high']} per person."
        if brief.limits.diet:
            reason += (
                " "
                + {
                    "only": "The venue is listed as vegetarian-only.",
                    "options": "Vegetarian dishes are listed.",
                    "vegan": "The listing includes vegan options.",
                }[brief.limits.diet]
            )
    elif indirect and not matches and "music" in indirect and place["kind"] == "bookshop":
        reason = "A nearby vinyl-browsing option for your music interest. Stock varies and there is no confirmed performance."
    elif matches:
        reason = (
            f"{place['name']} gives you {place['minutes']} minutes for " + ", ".join(matches) + "."
        )
    else:
        reason = f"This {place['kind']} is an alternative in {place['area']}, with {place['minutes']} minutes to explore."
    if previous:
        leg = stop["leg"]
        reason += f" It is about {leg['minutes']} minutes from {previous['name']} by " + (
            "auto." if leg["mode"] == "auto" else "foot."
        )
    elif brief.tired and place["kind"] == "park":
        reason += " Start with a short loop and turn back whenever you feel ready."
    return reason


def alternative_note(tag, places, plan, brief, cache):
    choices = [p for p in places if evidence(p, tag) == 1]
    if not choices:
        return f"No primary {tag} venue is documented among the {len(places)} eligible places in this search."
    current = tuple(s["place"] for s in plan["stops"])
    cheapest = min(choices, key=lambda p: p["high"])
    price = cheapest["high"] * brief.preferences.people
    if price > brief.preferences.budget:
        return f"{cheapest['name']} is a {tag} option, but its upper estimate of ₹{price:,} exceeds the ₹{brief.preferences.budget:,} budget."
    for choice in choices:
        stops = current + (choice,)
        legs = route_legs(stops, brief, cache)
        _, failure = schedule(stops, legs, brief)
        if failure:
            cause, amount = failure
            units = {
                "budget": f"₹{amount:,} over budget",
                "time": f"{amount} minutes over your time limit",
                "closing time": f"{amount} minutes past closing",
                "opens later": f"{amount} minutes before opening",
            }
            if cause in units:
                return f"Adding {choice['name']} for {tag} after this route would put it {units[cause]}."
    return f"{cheapest['name']} is another {tag} option. Allow {cheapest['minutes']} minutes and up to ₹{price:,} for the visit."


def finish_plan(plan, brief, places, cache, source, candidates):
    prefs = brief.preferences
    notes = []
    stops = plan["stops"]
    for index, stop in enumerate(stops):
        stop["why"] = explain_stop(stop, brief, stops[index - 1]["place"] if index else None)
    unmatched = [
        topic
        for topic in brief.interests
        if not any(evidence(s["place"], topic) == 1 for s in stops)
    ]
    for topic in unmatched:
        notes.append(alternative_note(topic, places, plan, brief, cache))
    if brief.limits.food_uncertain:
        notes.insert(
            0,
            "Activities-only alternative. No food stop is included because the sources cannot verify "
            + ", ".join(brief.limits.food_uncertain)
            + ".",
        )
    if brief.lunch and not includes_lunch(plan):
        meals = [p for p in places if p.get("food_role") == "meal"]
        if meals:
            cheapest = min(p["high"] * prefs.people for p in meals)
            notes.append(
                f"No lunch fits the selected outing. The lowest upper estimate for a full meal is ₹{cheapest:,}; your total budget is ₹{prefs.budget:,}."
            )
        else:
            notes.append("There is no documented full meal meeting these constraints.")
    if plan["spare"] >= max(60, brief.minutes * 0.3):
        free = sum(p["high"] == 0 for p in places)
        notes.append(
            f"This outing uses {plan['total']} of {brief.minutes} minutes. The local search found {free} zero-spend stops among {len(places)} eligible venues; {plan['spare']} minutes remain unscheduled."
        )
        for _, longer in candidates:
            extra = [p for p in longer if p["id"] not in {s["place"]["id"] for s in stops}]
            if not extra or not all(p["high"] == 0 for p in extra):
                continue
            alternative, _ = schedule(longer, route_legs(longer, brief, cache), brief)
            if alternative and alternative["total"] > plan["total"] + 20:
                addition = extra[0]
                reason = (
                    f" Its Saturday crowd level is assumed {addition.get('crowd', 'unknown')}."
                    if "crowds" in brief.limits.flags
                    else ""
                )
                notes.append(
                    f"A longer option includes {addition['name']} and uses {alternative['total']} minutes in total.{reason}"
                )
                break
    if "crowds" in brief.limits.flags:
        notes.append(
            "Lower estimated Saturday crowds were favoured. Crowd labels are planning assumptions, not live readings."
        )
    if any(not stop["place"].get("hours") for stop in stops):
        notes.append(
            "Unknown-hours venues use a conservative 10:00–18:00 planning window. Confirm opening before leaving."
        )
    if any(stop["leg"]["mode"] == "auto" for stop in stops):
        rides = [stop["leg"] for stop in stops if stop["leg"]["mode"] == "auto"]
        saved = sum(leg["walk_alternative"] - leg["minutes"] for leg in rides)
        fare = sum(leg["high"] for leg in rides)
        notes.append(
            f"The estimated auto fares add up to ₹{fare:,} at the upper end and save about {saved} minutes against walking the same legs. Traffic and actual fares can vary."
        )
    if any("unavailable" in stop["leg"]["source"] for stop in stops):
        notes.append("Live routing was unavailable. Walking times use local distance estimates.")
    notes.append(
        "The return trip is extra."
        + (
            " The clock starts at the first stop."
            if not brief.origin
            else " Travel from the specified starting point is included."
        )
    )
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    saturday = today + timedelta(days=(5 - today.weekday()) % 7)
    no_primary = bool(brief.interests) and len(unmatched) == len(brief.interests)
    return dict(
        type="plan",
        **plan,
        preferences=prefs.model_dump(),
        date=saturday.isoformat(),
        source=source,
        notes=notes,
        partial=bool(brief.limits.food_uncertain or no_primary),
        unmatched=unmatched,
    )


def explain_failure(closest):
    if not closest:
        return "None of the available stops meets your required activities or travel constraints."
    phrases = []
    if "budget" in closest:
        gap, name = closest["budget"]
        phrases.append(f"{name} would take at least ₹{gap:,} more in the estimate")
    if "time" in closest:
        gap, name = closest["time"]
        phrases.append(f"{name} needs {gap} more minutes")
    if not phrases:
        return "The available venues are closed or unconfirmed at this time. Try an earlier start."
    return "No outing fits. " + "; ".join(phrases) + "."


async def run_plan(value):
    async with httpx.AsyncClient(timeout=12, trust_env=False) as client:
        try:
            prefs, method = await parse_preferences(client, value)
        except ValidationError as error:
            detail = error.errors()[0]
            field = ".".join(map(str, detail["loc"]))
            yield {
                "type": "clarify",
                "message": f"Please check {field}: {detail['msg']}.",
                "preferences": {},
            }
            return
        except (ValueError, TypeError):
            yield {
                "type": "clarify",
                "message": "Please use valid JSON or describe your Saturday in plain text.",
                "preferences": {},
            }
            return
        prefs.city = canonical_city(prefs.city)
        yield event(
            "parse_preferences",
            {"format": method},
            {"city": prefs.city, "budget": prefs.budget, "minutes": duration(prefs.available_time)},
            method + " interpreted your request.",
        )

        def terminal(kind, message):
            return {"type": kind, "message": message, "preferences": prefs.model_dump()}

        missing = questions(prefs, value)
        if missing:
            yield terminal("clarify", " ".join(missing[:2]))
            return
        limits = interpret_constraints(prefs)
        if limits.unsupported:
            yield terminal(
                "empty",
                "The venue records cannot verify "
                + ", ".join(limits.unsupported)
                + ". Edit this requirement if it is flexible.",
            )
            return
        tags = list(
            dict.fromkeys(classify(tag) or tag.lower().strip() for tag in prefs.interests)
        ) or ["walks", "food", "art"]
        tags = [tag for tag in tags if tag not in limits.excluded]
        places, origin, source, total_found = await find_places(client, prefs, tags, limits)
        yield event(
            "find_places",
            {"city": prefs.city, "interests": tags, "constraints": prefs.constraints},
            {"found": total_found, "eligible": len(places), "source": source},
            f"{source}: {len(places)} of {total_found} places passed the filters.",
        )
        if prefs.starting_area and not origin:
            yield terminal(
                "clarify",
                "I couldn’t locate that starting point. Enter a named venue or leave it blank to start at your first stop.",
            )
            return
        if not places:
            msg = (
                "No eligible venue meets the specified constraints."
                if total_found
                else f"No place data available for {prefs.city}. The sourced guides cover "
                + ", ".join(FALLBACK_CITIES)
                + "."
            )
            yield terminal("empty", msg)
            return
        if limits.food_uncertain:
            limits.required.discard("meal")
        brief = Brief.create(prefs, limits, tags, origin)
        if brief.tired:
            places = [
                {**place, "minutes": place.get("short_minutes", place["minutes"])}
                for place in places
            ]
        candidates, cache, rejections, closest = build_candidates(places, brief)
        yield event(
            "build_candidates",
            {"venues": len(places), "minutes": brief.minutes, "budget": prefs.budget},
            {"feasible_previews": len(candidates), "rejected": dict(rejections)},
            f"Ranked {len(candidates)} feasible provisional itineraries.",
        )
        lookups = {}
        for _, stops in candidates:
            legs = await estimate_route(client, stops, brief, cache, lookups)
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
                if reason not in closest or gap < closest[reason][0]:
                    closest[reason] = (gap, names[-1])
                continue
            yield event(
                "validate_plan",
                {"stops": names, "time_limit": brief.minutes, "budget_limit": prefs.budget},
                {"passed": True, "minutes": plan["total"], "upper_inr": plan["high"]},
                f"Verified {plan['total']} minutes and up to ₹{plan['high']:,}, including transfers.",
            )
            yield finish_plan(plan, brief, places, cache, source, candidates)
            return
        yield terminal("empty", explain_failure(closest))
