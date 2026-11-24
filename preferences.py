"""Interpret a brief into limits, interests and editable planning assumptions."""

import json
import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from constraints import NON_VEG, diet_labels, extract_constraints, normalize_diet_spelling
from interests import interest_topics

NUMBER_WORDS = {
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "half an hour": "30 minutes",
    "an hour": "1 hour",
    "ninety": "90",
}
CITIES = {
    "Bengaluru": ["bangalore", "bengaluru"],
    "Gurugram": ["gurgaon", "gurugram"],
    "Delhi": ["new delhi", "delhi"],
    "Mumbai": ["mumbai", "bombay"],
    "Pune": ["pune"],
    "Hyderabad": ["hyderabad"],
    "Chennai": ["chennai"],
    "Kolkata": ["kolkata"],
    "Jaipur": ["jaipur"],
    "Dehradun": ["dehradun"],
    "Noida": ["noida"],
}

Short = Annotated[str, Field(max_length=180)]


class Preferences(BaseModel):
    """Validated visitor input shared by the JSON form, free-text parser and optional LLM."""

    model_config = ConfigDict(extra="ignore")
    city: Short = ""
    budget: int | None = Field(None, ge=0, le=1000000)
    available_time: str | int = ""
    mood: Short = ""
    interests: list[Short] = Field(default_factory=list, max_length=15)
    constraints: list[Short] = Field(default_factory=list, max_length=15)
    start_time: str = "10:00"
    starting_area: Short = ""
    people: int = Field(1, ge=1, le=30)

    @model_validator(mode="after")
    def read_diet_from_interests(self):
        # The editable form can put "vegetarian food" in the interests field.
        for interest in self.interests:
            for diet in diet_labels(interest):
                if diet not in self.constraints:
                    self.constraints.append(diet)
        return self

    @model_validator(mode="before")
    @classmethod
    def infer_start_for_named_window(cls, value):
        if (
            isinstance(value, dict)
            and "start_time" not in value
            and re.search(
                r"\bevening\b|\bshaam\b", str(value.get("available_time", "")), re.IGNORECASE
            )
        ):
            return {**value, "start_time": "17:00"}
        return value

    @field_validator("interests", "constraints", mode="before")
    @classmethod
    def split_labels(cls, value):
        if isinstance(value, str):
            return [
                part.strip()
                for part in re.split(r"[,;]|\band\b", value, flags=re.IGNORECASE)
                if part.strip()
            ]
        return value

    @field_validator("budget", mode="before")
    @classmethod
    def read_budget(cls, value):
        if isinstance(value, str):
            normalized = re.fullmatch(
                r"(?:₹|inr|rs\.?)?\s*([\d,]+(?:\.\d+)?)\s*(k)?\s*(?:rupees?|rs|inr)?",
                value.strip(),
                re.IGNORECASE,
            )
            if normalized:
                return amount(normalized)
        return value

    @field_validator("start_time")
    @classmethod
    def check_time(cls, value):
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("Use a start time in HH:MM format.")
        return value


def normalize_numbers(text):
    for phrase, number in NUMBER_WORDS.items():
        text = re.sub(r"\b" + phrase + r"\b", number, text, flags=re.IGNORECASE)
    return text.lower()


def duration(text):
    """Convert a time expression to minutes. Bare values from 1 through 12 mean hours."""
    normalized = normalize_numbers(str(text)).strip()
    if normalized.isdigit():
        number = int(normalized)
        return number * 60 if 1 <= number <= 12 else number
    hours = re.search(r"(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|h|ghant[ae]|घंटे)\b", normalized)
    minutes = re.search(r"(\d+)\s*(?:minutes?|mins?|m)\b", normalized)
    if hours or minutes:
        return round(float(hours[1]) * 60 if hours else 0) + (int(minutes[1]) if minutes else 0)
    if re.search(r"half[ -]?(?:a )?day|aadha din", normalized):
        return 240
    if re.search(r"\bevening\b|\bshaam\b", normalized):
        return 180
    return 0


def canonical_city(city):
    normalized = city.strip().casefold()
    return next(
        (name for name, aliases in CITIES.items() if normalized in aliases), city.strip().title()
    )


def extract_clock(text):
    match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", text, re.IGNORECASE)
    if match and 1 <= int(match[1]) <= 12 and int(match[2] or 0) < 60:
        hour = int(match[1]) % 12 + (12 if match[3].lower() == "pm" else 0)
        return f"{hour:02}:{int(match[2] or 0):02}"
    match = re.search(
        r"\b(?:at|start(?:ing)?(?: at)?)\s+([01]?\d|2[0-3]):([0-5]\d)\b", text, re.IGNORECASE
    )
    if match:
        return f"{int(match[1]):02}:{match[2]}"
    return "17:00" if re.search(r"\bevening\b|\bshaam\b", text, re.IGNORECASE) else "10:00"


def amount(match):
    return round(float(match[1].replace(",", "")) * (1000 if match[2] else 1))


def extract_budget(text):
    """Read total INR from the brief while excluding time expressions next to “budget”."""
    if re.search(r"\b(?:zero budget|free only|no spending)\b", text):
        return 0
    number = r"(\d+(?:,\d{3})*(?:\.\d+)?)\s*(k)?"
    # A duration beside "budget" belongs to the time field in either word order.
    money_text = re.sub(
        r"\b\d+(?:\.\d+)?\s*(?:hours?|hrs?|h|minutes?|mins?|m|ghant[ae])\b", " ", text
    )
    patterns = [
        r"(?:₹|\brs\.?|\binr)\s*" + number + r"\b",
        number + r"\s*(?:rupees?|rs|inr|total|budget|rupaye|bucks)\b",
        r"(?:\bbudget(?:\s+(?:of|is))?|\bunder)\s*" + number + r"\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, money_text)
        if match:
            return amount(match)
    adjacent = re.search(
        r"\b" + number + r"(?=\s*(?:,|and|aur)\s*\d+\s*(?:h|hrs?|hours?|minutes?|mins?|ghante)\b)",
        text,
    )
    if adjacent:
        return amount(adjacent)
    bare = re.fullmatch(r"\s*" + number + r"\s*", money_text)
    if bare:
        return amount(bare)
    return None


def extract_city(normalized):
    return next(
        (
            name
            for name, aliases in CITIES.items()
            if any(re.search(r"\b" + alias + r"\b", normalized) for alias in aliases)
        ),
        "",
    )


def extract_interests(normalized):
    """Keep requested topics separate from explicit exclusions such as “no walks”."""
    exclusions = []
    interests = []
    # Keep the shared negation in "no museums or walks". A new sentence
    # or a positive clause such as "but I like food" starts a fresh scope.
    activity_noun = r"(?:walks?|museums?|galleries|parks?|books?|music|food)"
    scoped_text = re.sub(
        rf"\b(no|avoid|skip|dislike|hate)\s+({activity_noun})\s+(or|and)\s+(?={activity_noun}\b)",
        r"\1 \2 \3 \1 ",
        normalized,
    )
    for clause in re.split(r"[,.;!?]|\b(?:and|but|with|plus|or|aur)\b", scoped_text):
        topics = interest_topics(re.sub(NON_VEG, "", clause))
        if not topics:
            explicit = re.search(
                r"(?:interested in|i (?:love|like)|i want to (?:try|do))\s+(.+)", clause
            )
            if explicit:
                interests.append(explicit[1].strip()[:100])
        negative = (
            re.search(r"\b(?:skip|dislike|avoid|hate|no)\b", clause) and "crowd" not in clause
        )
        for topic in topics:
            if negative:
                exclusions.append("avoid " + topic)
            elif topic not in interests:
                interests.append(topic)
    return interests, exclusions


def group_size(normalized):
    """Read an explicit party size or recognise wording for a couple."""
    group = re.search(
        r"\b(?:for|group of|party of)\s+(\d+)\s*(?:people|persons|adults|of us|friends)?\b"
        r"(?!\s*(?:min|hour|hr))",
        normalized,
    )
    return (
        int(group[1])
        if group
        else (2 if re.search(r"\b(?:both of us|as a couple|my partner and i)\b", normalized) else 1)
    )


def local_parse(text):
    """Extract an editable, validated brief from free text without any network requests."""
    normalized = normalize_diet_spelling(normalize_numbers(text))
    budget = extract_budget(normalized)
    people = group_size(normalized)
    interests, exclusions = extract_interests(normalized)
    constraints = extract_constraints(normalized) + exclusions
    if people > 1 and budget is not None and re.search(r"per (?:person|head)|each\b", normalized):
        budget *= people
    area = re.search(
        r"\b(?:starting (?:from|near|in)|start from|near)\s+([a-z][\w -]{1,48}?)"
        r"(?=\s+(?:at|with|for|and|on|budget|under)\b|[,.;!?]|$)",
        normalized,
    )
    return Preferences(
        city=extract_city(normalized),
        budget=budget,
        available_time=f"{duration(normalized)} minutes",
        mood=text[:180],
        interests=interests,
        constraints=list(dict.fromkeys(constraints)),
        start_time=extract_clock(normalized),
        starting_area=area[1].strip() if area else "",
        people=people,
    )


class InputFormatError(ValueError):
    """The submitted brief cannot be read as JSON or supported plain text."""


def decode_input(value):
    """Validate a JSON object or pasted JSON; return None for ordinary free text."""
    if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise InputFormatError("Invalid JSON brief") from error
    if isinstance(value, dict):
        return Preferences.model_validate(value)
    if not isinstance(value, str) or len(value) > 3500:
        raise InputFormatError(
            "Describe the outing in up to 3,500 characters or paste the assignment JSON."
        )
    return None


def questions(preferences, value):
    """Identify missing inputs and ambiguity about whether a group budget is shared."""
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
