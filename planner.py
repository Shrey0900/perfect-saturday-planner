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
    clock,
    evidence,
    includes_lunch,
    interpret_constraints,
    position,
    route_legs,
    schedule,
    suitable,
    validate_plan,
)
from preferences import (
    EXAMPLES,
    canonical_city,
    decode_input,
    duration,
    interest_topics,
    local_parse,
)
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


async def find_places(client, preferences, interests, limits, *, focused):
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
    if focused:
        # A sole interest defines the outing. Only an explicitly required
        # additional activity (for example, lunch on a books outing) can expand it.
        eligible = [
            place
            for place in eligible
            if any(evidence(place, topic) == 1 for topic in limits.required)
            or ("meal" in limits.required and place["food_role"] == "meal")
        ]
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
        meal_name = "lunch" if 705 <= stop["start_minute"] <= 840 else "a meal"
        price = (
            f"₹{place['low']}"
            if place["low"] == place["high"]
            else f"₹{place['low']}–₹{place['high']}"
        )
        reason = f"Time for {meal_name} at {stop['start']}. Allow {price} per person."
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
    elif {"history", "walks"}.issubset(matches):
        reason = f"You asked for a heritage walk. This stop gives you {place['minutes']} minutes to explore a historic site on foot."
    elif matches:
        reason = (
            f"{place['name']} gives you {place['minutes']} minutes for " + ", ".join(matches) + "."
        )
    else:
        benefits = []
        if place["high"] == 0:
            benefits.append("the visit is free")
        if "crowds" in brief.limits.flags and place.get("crowd") == "low":
            benefits.append("its estimated Saturday crowd is low")
        if previous and stop["leg"]["minutes"] <= 15:
            benefits.append(f"it is close to {previous['name']}")
        reason = (
            ("An optional stop because " + " and ".join(benefits) + ".")
            if benefits
            else (
                f"An optional {place['minutes']}-minute visit in {place['area']} within your remaining time and budget."
            )
        )
    if previous:
        leg = stop["leg"]
        reason += f" It is about {leg['minutes']} minutes from {previous['name']} by " + (
            "auto." if leg["mode"] == "auto" else "foot."
        )
    elif brief.tired and place["kind"] == "park":
        reason += " Start with a short loop and turn back whenever you feel ready."
    if brief.tired and place.get("short_minutes") == place["minutes"]:
        reason += f"I used the shorter {place['minutes']}-minute visit to keep the pace gentle."
    return reason


def hours_note(place, cause):
    hours = place.get("hours")
    if not hours:
        return f"{place['name']} has unconfirmed hours. The guide uses 10:00–18:00 for planning."
    if cause == "opens later":
        return f"{place['name']} opens at {clock(hours[0])}. Try starting at {clock(hours[0])} or later."
    return f"{place['name']} closes at {clock(hours[1])}. Choose an earlier window for this visit."


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
            if cause in {"closing time", "opens later"}:
                return hours_note(choice, cause)
            units = {
                "budget": f"₹{amount:,} over budget",
                "time": f"{amount} minutes over your time limit",
            }
            if cause in units:
                return f"Adding {choice['name']} for {tag} after this route would put it {units[cause]}."
    return f"{cheapest['name']} is another {tag} option. Allow {cheapest['minutes']} minutes and up to ₹{price:,} for the visit."


def lower_travel_note(plan, brief, cache, candidates):
    auto_minutes = sum(s["leg"]["minutes"] for s in plan["stops"] if s["leg"]["mode"] == "auto")
    if not brief.tired or auto_minutes < 45:
        return ""
    message = f"At your requested pace, this route still spends {auto_minutes} minutes in autos."
    for _, stops in candidates:
        if len(stops) < 2 or stops[0]["id"] != plan["stops"][0]["place"]["id"]:
            continue
        legs = route_legs(stops, brief, cache)
        transfer = sum(leg["minutes"] for leg in legs)
        if transfer > min(30, auto_minutes // 2):
            continue
        alternative, _ = schedule(stops, legs, brief)
        if alternative:
            names = " → ".join(p["name"] for p in stops)
            lost = [
                t
                for t in brief.interests
                if any(evidence(s["place"], t) == 1 for s in plan["stops"])
                and not any(evidence(p, t) == 1 for p in stops)
            ]
            tradeoff = " This skips the dedicated " + " and ".join(lost) + " stop." if lost else ""
            return f"{message} A lower-travel option is {names}, with about {transfer} minutes between stops and up to ₹{alternative['high']:,} total.{tradeoff}"
    return message


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
    if stops[0]["wait"]:
        notes.append(
            f"The first stop opens at {stops[0]['start']}. Its {stops[0]['wait']}-minute wait counts against your window; you can arrive at opening."
        )
    if (
        brief.lunch
        and not includes_lunch(plan)
        and any(s["place"].get("food_role") == "meal" for s in stops)
    ):
        notes.append(
            "Your meal falls outside the lunch window. For a midday meal, try starting around noon."
        )
    elif brief.lunch and not includes_lunch(plan):
        meals = [p for p in places if p.get("food_role") == "meal"]
        if meals:
            cheapest = min(p["high"] * prefs.people for p in meals)
            notes.append(
                f"No lunch fits the selected outing. The lowest upper estimate for a full meal is ₹{cheapest:,}; your total budget is ₹{prefs.budget:,}."
            )
        else:
            notes.append("There is no documented full meal meeting these constraints.")
    if (
        plan["spare"] >= max(60, brief.minutes * 0.3)
        and prefs.interests
        and len(brief.interests) == 1
        and not unmatched
    ):
        notes.append(
            f"This outing uses {plan['total']} of your {brief.minutes} minutes for {brief.interests[0]}. "
            f"The remaining {plan['spare']} minutes are free."
        )
    elif plan["spare"] >= max(60, brief.minutes * 0.3):
        free = sum(p["high"] == 0 for p in places)
        notes.append(
            f"This outing uses {plan['total']} of {brief.minutes} minutes. The local search found {free} zero-spend stops among {len(places)} eligible venues; {plan['spare']} minutes remain unscheduled."
        )
    if "crowds" in brief.limits.flags:
        notes.append(
            "Lower estimated Saturday crowds were favoured. Crowd labels are planning assumptions, not live readings."
        )
    if any(not stop["place"].get("hours") for stop in stops):
        notes.append(
            "Unknown-hours venues use a conservative 10:00–18:00 planning window. Confirm opening before leaving."
        )
    if any(stop["leg"]["mode"] == "auto" for stop in stops):
        alternative = lower_travel_note(plan, brief, cache, candidates)
        if alternative:
            notes.append(alternative)
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
            f" The window starts at {prefs.start_time}. Travel to the first stop is extra."
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


def explain_failure(closest, brief, places):
    if not closest:
        return "None of the available stops meets your required activities or travel constraints."
    phrases = []
    if "budget" in closest:
        gap, name = closest["budget"]
        budget = brief.preferences.budget
        phrases.append(
            f"The route to {name} has an upper estimate of ₹{budget + gap:,}, which is ₹{gap:,} over your ₹{budget:,} budget"
        )
    if "time" in closest:
        gap, name = closest["time"]
        phrases.append(f"{name} needs {gap} more minutes")
    for cause in ("opens later", "closing time"):
        if cause in closest:
            _, name = closest[cause]
            place = next((p for p in places if p["name"] == name), None)
            if place:
                phrases.append(hours_note(place, cause).rstrip("."))
    if not phrases:
        return "None of the available routes meets your travel or activity requirements."
    return "No outing fits. " + ". ".join(phrases) + "."


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
        prefs.interests = list(
            dict.fromkeys(
                topic
                for label in prefs.interests
                for topic in (interest_topics(label) or [label.lower().strip()])
            )
        )
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
        tags = prefs.interests or ["walks", "food", "art"]
        tags = [tag for tag in tags if tag not in limits.excluded]
        focused = bool(prefs.interests) and len(tags) == 1 and tags[0] in EXAMPLES
        if focused:
            limits.required.add(tags[0])
        places, origin, source, total_found = await find_places(
            client, prefs, tags, limits, focused=focused
        )
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
            if not total_found:
                msg = (
                    f"No place data available for {prefs.city}. The sourced guides cover "
                    + ", ".join(FALLBACK_CITIES)
                    + "."
                )
            elif tags == ["food"] and limits.food_uncertain:
                msg = (
                    f"I couldn’t verify a food stop in {prefs.city} for "
                    + ", ".join(limits.food_uncertain)
                    + "."
                )
            elif prefs.interests and len(tags) == 1:
                msg = f"I couldn’t find a {tags[0]} stop in the {prefs.city} results that meets your requirements."
            else:
                msg = "No eligible venue meets the specified constraints."
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
        yield terminal("empty", explain_failure(closest, brief, places))
