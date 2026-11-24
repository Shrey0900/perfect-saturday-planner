"""Turn validated stops, costs and rejected alternatives into the public itinerary."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from constraints import Diet, Flag
from itinerary import DAYTIME_HOURS, Failure, clock, schedule
from routing import route_legs
from scoring import evidence, includes_lunch, is_lunch_time


def venue_reason(place, matches, indirect):
    """Use a guide note only when the visitor requested its documented topics."""
    for note in place.get("interest_notes", []):
        topics = matches if note["match"] == "primary" else (indirect if not matches else [])
        if set(note["topics"]).issubset(topics):
            return note["text"]
    return ""


def meal_reason(stop, brief):
    """Explain the meal using the actual arrival time, price range and verified diet."""
    place = stop["place"]
    meal_name = "lunch" if is_lunch_time(stop["start_minute"]) else "a meal"
    price = (
        f"₹{place['low']}" if place["low"] == place["high"] else f"₹{place['low']}–₹{place['high']}"
    )
    reason = f"Time for {meal_name} at {stop['start']}. Allow {price} per person."
    if brief.limits.diet:
        reason += (
            " "
            + {
                Diet.PURE_VEG: "The venue is listed as vegetarian-only.",
                Diet.VEG_FRIENDLY: "Vegetarian dishes are listed.",
                Diet.VEGAN: "The listing includes vegan options.",
            }[brief.limits.diet]
        )
    return reason


def explain_stop(stop, brief, previous):
    """Explain one stop using its matches, pace, price and preceding transfer."""
    place = stop["place"]
    matches = [tag for tag in brief.interests if evidence(place, tag) == 1]
    indirect = [tag for tag in brief.interests if 0 < evidence(place, tag) < 1]
    if place.get("food_role") == "meal":
        reason = meal_reason(stop, brief)
    elif note := venue_reason(place, matches, indirect):
        reason = note
    elif matches:
        reason = (
            f"{place['name']} gives you {place['minutes']} minutes for " + ", ".join(matches) + "."
        )
    else:
        benefits = []
        if place["high"] == 0:
            benefits.append("the visit is free")
        if Flag.CROWDS in brief.limits.flags and place.get("crowd") == "low":
            benefits.append("its estimated Saturday crowd is low")
        if previous and stop["leg"]["minutes"] <= 15:
            benefits.append(f"it is close to {previous['name']}")
        reason = (
            ("An optional stop because " + " and ".join(benefits) + ".")
            if benefits
            else (
                f"An optional {place['minutes']}-minute visit in {place['area']} within your "
                "remaining time and budget."
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
        reason += f" I used the shorter {place['minutes']}-minute visit to keep the pace gentle."
    return reason


def hours_note(place, cause):
    hours = place.get("hours")
    if not hours:
        return (
            f"{place['name']} has unconfirmed hours. The guide uses "
            f"{clock(DAYTIME_HOURS[0])}–{clock(DAYTIME_HOURS[1])} for planning."
        )
    if cause == Failure.OPENS_LATER:
        return (
            f"{place['name']} opens at {clock(hours[0])}. Try starting at {clock(hours[0])} or "
            "later."
        )
    return f"{place['name']} closes at {clock(hours[1])}. Choose an earlier window for this visit."


def alternative_note(tag, places, plan, brief, cache):
    """Find the concrete obstacle to including a requested interest in this route."""
    choices = [p for p in places if evidence(p, tag) == 1]
    if not choices:
        return (
            f"No primary {tag} venue is documented among the {len(places)} eligible places in "
            "this search."
        )
    current = tuple(s["place"] for s in plan["stops"])
    cheapest = min(choices, key=lambda p: p["high"])
    price = cheapest["high"] * brief.preferences.people
    if price > brief.preferences.budget:
        return (
            f"{cheapest['name']} is a {tag} option, but its upper estimate of ₹{price:,} exceeds "
            f"the ₹{brief.preferences.budget:,} budget."
        )
    for choice in choices:
        stops = current + (choice,)
        legs = route_legs(stops, brief, cache)
        _, failure = schedule(stops, legs, brief)
        if failure:
            cause, amount = failure
            if cause in {Failure.CLOSING, Failure.OPENS_LATER}:
                return hours_note(choice, cause)
            units = {
                Failure.BUDGET: f"₹{amount:,} over budget",
                Failure.TIME: f"{amount} minutes over your time limit",
            }
            if cause in units:
                return (
                    f"Adding {choice['name']} for {tag} after this route would put it "
                    f"{units[cause]}."
                )
    return (
        f"{cheapest['name']} is another {tag} option. Allow {cheapest['minutes']} minutes and up "
        f"to ₹{price:,} for the visit."
    )


def lower_travel_note(plan, brief, cache, candidates):
    """Find a shorter transfer from the same first stop for a tired visitor."""
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
            return (
                f"{message} A lower-travel option is {names}, with about {transfer} minutes "
                f"between stops and up to ₹{alternative['high']:,} total.{tradeoff}"
            )
    return message


def timing_notes(plan, brief, places, unmatched):
    """Explain opening waits, missed lunch and unused time in the requested window."""
    notes = []
    stops = plan["stops"]
    prefs = brief.preferences
    missing_lunch = brief.lunch and not includes_lunch(plan)
    substantial_spare_time = plan["spare"] >= max(60, brief.minutes * 0.3)
    if stops[0]["wait"]:
        notes.append(
            f"The first stop opens at {stops[0]['start']}. Its {stops[0]['wait']}-minute wait "
            "counts against your window. You can arrive at opening."
        )
    if missing_lunch and any(s["place"].get("food_role") == "meal" for s in stops):
        notes.append(
            "Your meal falls outside the lunch window. For a midday meal, try starting around noon."
        )
    elif missing_lunch:
        meals = [p for p in places if p.get("food_role") == "meal"]
        if meals:
            cheapest = min(p["high"] * prefs.people for p in meals)
            notes.append(
                "No lunch fits the selected outing. The lowest upper estimate for a full meal is "
                f"₹{cheapest:,}. Your total budget is ₹{prefs.budget:,}."
            )
        else:
            notes.append("There is no documented full meal meeting these constraints.")
    if substantial_spare_time and prefs.interests and len(brief.interests) == 1 and not unmatched:
        notes.append(
            f"This outing uses {plan['total']} of your {brief.minutes} minutes for "
            f"{brief.interests[0]}. "
            f"The remaining {plan['spare']} minutes are free."
        )
    elif substantial_spare_time:
        free = sum(p["high"] == 0 for p in places)
        notes.append(
            f"This outing uses {plan['total']} of {brief.minutes} minutes. The local search found "
            f"{free} zero-spend stops among {len(places)} eligible venues. {plan['spare']} "
            "minutes remain unscheduled."
        )
    return notes


def travel_and_venue_notes(plan, brief, cache, candidates):
    """Describe transfer trade-offs and which facts come from estimates."""
    notes = []
    stops = plan["stops"]
    prefs = brief.preferences
    if Flag.CROWDS in brief.limits.flags:
        notes.append(
            "Lower estimated Saturday crowds were favoured. Crowd labels are planning "
            "assumptions, not live readings."
        )
    if any(not stop["place"].get("hours") for stop in stops):
        notes.append(
            "Unknown-hours venues use a conservative "
            f"{clock(DAYTIME_HOURS[0])}–{clock(DAYTIME_HOURS[1])} planning window. Confirm "
            "opening before leaving."
        )
    rides = [stop["leg"] for stop in stops if stop["leg"]["mode"] == "auto"]
    if rides:
        alternative = lower_travel_note(plan, brief, cache, candidates)
        if alternative:
            notes.append(alternative)
        saved = sum(leg["walk_alternative"] - leg["minutes"] for leg in rides)
        fare = sum(leg["high"] for leg in rides)
        notes.append(
            f"The estimated auto fares add up to ₹{fare:,} at the upper end and save about "
            f"{saved} minutes against walking the same legs. Traffic and actual fares can vary."
        )
    if any("unavailable" in stop["leg"]["source"] for stop in stops):
        notes.append("Live routing was unavailable. Walking times use local distance estimates.")
    notes.append(
        f"The window starts at {prefs.start_time} at your first stop. "
        "Allow extra time to get there and to return home."
        if not brief.origin
        else "Travel from your starting point is included. Allow extra time for the return trip."
    )
    return notes


def finish_plan(plan, brief, places, cache, source, candidates):
    """Add reasons, trade-offs and the local Saturday date to the validated schedule."""
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
            "Activities-only alternative. No food stop is included because the sources cannot "
            "verify " + ", ".join(brief.limits.food_uncertain) + ".",
        )
    notes.extend(timing_notes(plan, brief, places, unmatched))
    notes.extend(travel_and_venue_notes(plan, brief, cache, candidates))
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
    """Turn the nearest failed candidates into budget, time or opening-hour advice."""
    if not closest:
        return "None of the available stops meets your required activities or travel constraints."
    phrases = []
    if Failure.BUDGET in closest:
        gap, name = closest[Failure.BUDGET]
        budget = brief.preferences.budget
        phrases.append(
            f"The route to {name} has an upper estimate of ₹{budget + gap:,}, which is ₹{gap:,} "
            f"over your ₹{budget:,} budget"
        )
    if Failure.TIME in closest:
        gap, name = closest[Failure.TIME]
        phrases.append(f"{name} needs {gap} more minutes")
    for cause in (Failure.OPENS_LATER, Failure.CLOSING):
        if cause in closest:
            _, name = closest[cause]
            place = next((p for p in places if p["name"] == name), None)
            if place:
                phrases.append(hours_note(place, cause).rstrip("."))
    if not phrases:
        return "None of the available routes meets your travel or activity requirements."
    return "No outing fits. " + ". ".join(phrases) + "."
