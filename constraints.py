"""Recognise constraints in text and enforce the same rules on structured input."""

import re
from dataclasses import dataclass, field
from enum import StrEnum

from interests import classify


class Flag(StrEnum):
    ACCESSIBLE = "accessible"
    NO_WALK = "no_walk"
    WALK_ONLY = "walk_only"
    CROWDS = "crowds"
    SKIP_MEALS = "skip_meals"
    INDOORS = "indoors"
    OUTDOORS = "outdoors"


class Diet(StrEnum):
    VEG_FRIENDLY = "veg_friendly"
    PURE_VEG = "pure_veg"
    VEGAN = "vegan"


# These short codes are the venue catalogue's published wire format.
VENUE_DIET_CODE = {Diet.VEGAN: "vegan", Diet.PURE_VEG: "only", Diet.VEG_FRIENDLY: "options"}
ALLOWED_VENUE_DIETS = {
    Diet.VEGAN: {"vegan"},
    Diet.PURE_VEG: {"only"},
    Diet.VEG_FRIENDLY: set(VENUE_DIET_CODE.values()),
}
NON_VEG = r"\b(?:non[ -]?veg(?:etarian)?s?|not (?:a )?vegetarian)\b"
UNVERIFIED_DIET = r"allerg|gluten|nut[ -]?free|halal|kosher|lactose|jain"
UNVERIFIED_ACCESS = r"pet.friendly|dog.friendly|stroller"


def normalize_diet_spelling(text):
    return re.sub(r"\bveg(?:itatrian|itarian|eterian)\b", "vegetarian", text, flags=re.I)


@dataclass(frozen=True)
class Rule:
    """Common wording plus optional shorthand used inside the explicit constraints field."""

    label: str
    pattern: str
    flag: Flag | None = None
    diet: Diet | None = None
    structured: str = ""
    unsupported: str = ""
    required: str = ""

    def matches(self, text, explicit=False):
        return bool(
            text == self.label
            or re.search(self.pattern, text)
            or (explicit and self.structured and re.search(self.structured, text))
        )


RULES = (
    Rule("vegan", r"\bvegan\b", diet=Diet.VEGAN),
    Rule(
        "pure vegetarian",
        r"pure[\s-]*veg|veg(?:etarian)?[\s-]*only",
        diet=Diet.PURE_VEG,
        structured=r"^only vegetarian$",
    ),
    Rule("vegetarian", r"\bveg(?:etarian)?\b", diet=Diet.VEG_FRIENDLY),
    Rule(
        "strictly avoid crowds",
        r"only quiet|must (?:be |avoid )?(?:quiet|crowds?)",
        structured=r"(?:must|only|guarantee|strictly).*?(?:crowd|quiet|peaceful)",
        unsupported="confirmed quiet conditions",
    ),
    Rule(
        "avoid crowds",
        r"(?:avoid|hate|no|away from|skip) crowds?|uncrowded|crowded places|quiet places",
        Flag.CROWDS,
        structured=r"crowd|quiet|peaceful",
    ),
    Rule(
        "indoors only",
        r"indoors? only|stay indoors|no outdoors?|avoid (?:the )?(?:heat|sun|outdoors?)",
        Flag.INDOORS,
        structured=r"^indoors?$",
    ),
    Rule("outdoors only", r"outdoors? only|no indoors?", Flag.OUTDOORS, structured=r"^outdoors?$"),
    Rule(
        "wheelchair access",
        r"wheelchair|step[ -]?free|accessible entrance|no stairs",
        Flag.ACCESSIBLE,
    ),
    Rule("no walking", r"no walking|can't walk|cannot walk|no walks", Flag.NO_WALK),
    Rule("walking only", r"walk(?:ing)? only|no (?:cabs?|autos?|taxis?)", Flag.WALK_ONLY),
    Rule("skip meals", r"(?:skip|no|without) (?:lunch|meals?|food)\b", Flag.SKIP_MEALS),
    Rule(
        "meal required",
        r"(?:must|need|want|have to)(?: to)? (?:have|get|eat|include|grab)?"
        r"\s*(?:a |some )?(?:meal|lunch|dinner|breakfast|food)\b",
        required="meal",
    ),
    Rule(
        "live performance required",
        r"live (?:music|performance|show|gig) (?:is a must|only|required)"
        r"|must (?:have|include) live music",
        structured=r"live (?:music|performance|show|gig)",
        unsupported="a confirmed performance this Saturday",
    ),
)
MEAT_REQUIRED = (
    r"(?:only|must|require)\b.{0,14}non[ -]?veg|non[ -]?veg(?:etarian)? (?:only|required)"
)


def diet_labels(text):
    """Find the strongest dietary preference in a phrase after masking negated mentions."""
    text = re.sub(NON_VEG, "", normalize_diet_spelling(text.lower()))
    for rule in RULES:
        if rule.diet and rule.matches(text):
            return [rule.label]
    return []


def extract_constraints(text):
    """Extract only explicit restrictions from a whole brief, keeping unverifiable wording."""
    labels = diet_labels(text)
    if re.search(MEAT_REQUIRED, text):
        labels.append("meat required")
    labels.extend(rule.label for rule in RULES if not rule.diet and rule.matches(text))
    for fragment in re.split(r"[,.;!?]|\b(?:and|but)\b", text):
        if re.search(UNVERIFIED_DIET, fragment) or re.search(UNVERIFIED_ACCESS, fragment):
            labels.append(fragment.strip()[:160])
    return list(dict.fromkeys(labels))


@dataclass
class Limits:
    """Hard venue filters and any dietary or other requirements that lack evidence."""

    diet: Diet | None = None
    excluded: set[str] = field(default_factory=set)
    required: set[str] = field(default_factory=set)
    flags: set[Flag] = field(default_factory=set)
    food_uncertain: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)


def apply_rule(rule, limits):
    if rule.diet:
        strength = (None, Diet.VEG_FRIENDLY, Diet.PURE_VEG, Diet.VEGAN)
        if strength.index(rule.diet) > strength.index(limits.diet):
            limits.diet = rule.diet
    if rule.flag:
        limits.flags.add(rule.flag)
    if rule.required:
        limits.required.add(rule.required)
    if rule.unsupported:
        limits.unsupported.append(rule.unsupported)


def apply_constraint(raw, limits):
    """Resolve one entry, preserving restrictions that the venue records cannot verify."""
    item = normalize_diet_spelling(raw.lower().strip())
    if not item or item in {"none", "no restrictions", "no constraints"}:
        return
    if re.search(UNVERIFIED_DIET, item):
        limits.food_uncertain.append(raw)
        return
    if item == "meat required" or re.search(MEAT_REQUIRED, item):
        limits.food_uncertain.append("a confirmed non-vegetarian meal")
        return
    if re.search(NON_VEG, item):
        item = re.sub(NON_VEG, "", item).strip()
        if not any(rule.matches(item, explicit=True) for rule in RULES):
            return
    rules = [rule for rule in RULES if rule.matches(item, explicit=True)]
    if rules:
        for rule in rules:
            apply_rule(rule, limits)
        return
    topic = classify(item)
    if topic and re.search(r"\b(no|avoid|skip|dislike)\b", item):
        limits.excluded.add(topic)
    elif topic and re.search(r"\b(must|need|include|mandatory|required)\b", item):
        limits.required.add("meal" if topic == "food" else topic)
    else:
        limits.unsupported.append(raw)


def interpret_constraints(preferences):
    """Apply shared rules and offer activities only when a diet cannot be verified."""
    limits = Limits()
    for raw in preferences.constraints:
        apply_constraint(raw, limits)
    if limits.food_uncertain:
        limits.required.discard("meal")
    return limits


def suitable(place, limits):
    """Check the venue before any travel or budget calculations."""
    tags = set(place["tags"])
    if limits.excluded.intersection(tags):
        return False
    if Flag.ACCESSIBLE in limits.flags and not (
        place.get("accessible") or place.get("wheelchair") is True
    ):
        return False
    outdoors = "outdoors" in tags or place["kind"] == "park"
    if Flag.INDOORS in limits.flags and outdoors:
        return False
    if Flag.OUTDOORS in limits.flags and not outdoors:
        return False
    if Flag.NO_WALK in limits.flags and ("walks" in tags or place["kind"] == "park"):
        return False
    if place["kind"] == "restaurant":
        if limits.food_uncertain or Flag.SKIP_MEALS in limits.flags:
            return False
        return not limits.diet or place["veg"] in ALLOWED_VENUE_DIETS[limits.diet]
    return True
