"""Convert user restrictions into checks shared by place discovery and scheduling."""

import re
from dataclasses import dataclass, field

from interests import classify
from preferences import DIETARY_UNKNOWN, NON_VEG, normalize_diet_spelling


@dataclass
class Limits:
    """Venue filters, required activities and restrictions the guide cannot verify."""

    diet: str = ""
    excluded: set = field(default_factory=set)
    required: set = field(default_factory=set)
    flags: set = field(default_factory=set)
    food_uncertain: list = field(default_factory=list)
    unsupported: list = field(default_factory=list)


def interpret_constraints(preferences):
    """Translate visitor wording into restrictions used by venue search and final validation."""
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
        elif re.search(r"\b(no|avoid|skip|dislike)\b", item) and (topic := classify(item)):
            limits.excluded.add(topic)
        elif re.search(r"meal required|\b(must|need|include|mandatory|required)\b", item) and (
            target := classify(item)
        ):
            limits.required.add("meal" if target == "food" else target)
        else:
            limits.unsupported.append(raw)
    return limits


def suitable(place, limits):
    """Check one venue against diet, access, exclusions and indoor or outdoor restrictions."""
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
