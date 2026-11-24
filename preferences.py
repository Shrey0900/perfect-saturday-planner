"""Interpret a brief into limits, interests and editable planning assumptions."""

import json
import math
import re
from collections import Counter
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

WORDS = {
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
# Tiny supervised text classifier. Train a multinomial naive Bayes model on
# labelled phrases and abstain on unfamiliar vocabulary or ambiguous clauses.
EXAMPLES = {
    "food": [
        "food eats meals lunch dinner breakfast snack snacks",
        "eat a vegetarian meal",
        "sip coffee tea café cafe",
        "taste local cuisine dishes",
        "hungry restaurant dosa",
    ],
    "walks": [
        "walk walks walking stroll wander outdoors",
        "fresh air greenery gardens nature",
        "park trees quiet picnic",
        "stretch my legs in a garden",
    ],
    "art": [
        "art paintings gallery galleries",
        "museum museums exhibitions design",
        "photography photographs sculpture",
        "draw and sketch artwork",
    ],
    "books": [
        "books bookshop bookstore bookshops",
        "browse novels and poetry",
        "read literature book browsing",
        "reading fiction shelves",
    ],
    "music": [
        "music listen acoustic gig concert",
        "live performance jazz singing",
        "vinyl records tunes bands",
    ],
    "history": [
        "history heritage monuments architecture",
        "historic buildings old city",
        "historical landmarks fort palace",
    ],
    "fine dining": ["fine dining tasting menu upscale gourmet degustation"],
    "trekking": ["trek trekking hiking mountain trail trek"],
    "nightlife": ["nightlife clubbing nightclub dancefloor"],
    "shopping": [
        "shopping markets browse stalls",
        "shop for gifts clothes handicrafts",
        "souvenirs local crafts boutiques",
    ],
}
STOPWORDS = set(
    [
        "a",
        "an",
        "the",
        "to",
        "of",
        "in",
        "on",
        "at",
        "from",
        "for",
        "and",
        "or",
        "with",
        "my",
        "me",
        "i",
        "we",
        "want",
        "would",
        "like",
        "our",
        "but",
        "something",
        "some",
        "by",
    ]
)


def tokens(text):
    return [word for word in re.findall(r"[a-zÀ-ž]+", text.lower()) if word not in STOPWORDS]


COUNTS = {label: Counter(tokens(" ".join(phrases))) for label, phrases in EXAMPLES.items()}
VOCAB = set().union(*COUNTS.values())


def classify(text):
    aliases = {
        "fine dining": "fine dining",
        "night life": "nightlife",
        "ghoomna": "walks",
        "tehelna": "walks",
        "sair": "walks",
        "khana": "food",
        "khaana": "food",
        "gaane": "music",
        "sangeet": "music",
        "kitaab": "books",
        "kitabein": "books",
        "किताब": "books",
        "खाना": "food",
        "संगीत": "music",
    }
    for phrase, topic in aliases.items():
        if re.search(r"\b" + phrase + r"\b", text, re.IGNORECASE):
            return topic
    terms = [t for t in tokens(text) if t in VOCAB]
    if not terms:
        return None
    scores = {}
    for label, counts in COUNTS.items():
        scores[label] = sum(
            math.log((counts[t] + 0.25) / (sum(counts.values()) + 0.25 * len(VOCAB))) for t in terms
        )
    top, second = sorted(scores, key=scores.get, reverse=True)[:2]
    return top if scores[top] - scores[second] > 0.45 else None


Short = Annotated[str, Field(max_length=180)]


class Preferences(BaseModel):
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
    for phrase, number in WORDS.items():
        text = re.sub(r"\b" + phrase + r"\b", number, text, flags=re.IGNORECASE)
    return text.lower()


def duration(text):
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
        return f"{(int(match[1]) % 12 + (12 if match[3].lower() == 'pm' else 0)):02}:{int(match[2] or 0):02}"
    match = re.search(
        r"\b(?:at|start(?:ing)?(?: at)?)\s+([01]?\d|2[0-3]):([0-5]\d)\b", text, re.IGNORECASE
    )
    if match:
        return f"{int(match[1]):02}:{match[2]}"
    return "17:00" if re.search(r"\bevening\b|\bshaam\b", text, re.IGNORECASE) else "10:00"


NON_VEG = r"\b(?:non[ -]?(?:veg(?:etarian)?)(?:s)?|not (?:a )?vegetarian)\b"
DIETARY_UNKNOWN = r"allerg|gluten|nut[ -]?free|halal|kosher|lactose|jain"


def amount(match):
    return round(float(match[1].replace(",", "")) * (1000 if match[2] else 1))


def extract_budget(text):
    if re.search(r"\b(?:zero budget|free only|no spending)\b", text):
        return 0
    number = r"(\d+(?:,\d{3})*(?:\.\d+)?)\s*(k)?"
    patterns = [
        r"(?:₹|\brs\.?|\binr|\bbudget(?:\s+(?:of|is))?|\bunder)\s*" + number + r"\b",
        number + r"\s*(?:rupees?|rs|inr|total|budget|rupaye|bucks)\b",
        r"\b" + number + r"(?=\s*(?:,|and|aur)\s*\d+\s*(?:hrs?|hours?|minutes?|mins?|ghante)\b)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return amount(match)
    return None


def local_parse(text):
    normalized = normalize_numbers(text)
    city = next(
        (
            name
            for name, aliases in CITIES.items()
            if any(re.search(r"\b" + alias + r"\b", normalized) for alias in aliases)
        ),
        "",
    )
    budget = extract_budget(normalized)
    constraints = []
    rules = [
        (r"pure[\s-]*veg|veg(?:etarian)?[\s-]*only", "pure vegetarian"),
        (r"\bvegan\b", "vegan"),
        (r"\b(?:vegetarian|veg)\b", "vegetarian"),
        (
            r"(?:avoid|hate|no|away from|skip) crowds?|\b(?:uncrowded|crowded places|quiet places)\b",
            "avoid crowds",
        ),
        (r"(?:indoors? only|stay indoors|no outdoors?)", "indoors only"),
        (r"(?:outdoors? only|no indoors?)", "outdoors only"),
        (r"wheelchair|step[\s-]*free", "wheelchair access"),
        (r"(?:no walking|can't walk|cannot walk)", "no walking"),
        (
            r"(?:must|need|want|have to)(?: to)? (?:have|get|eat|include|grab)?\s*(?:a |some )?(?:meal|lunch|dinner|breakfast|food)\b",
            "meal required",
        ),
        (
            r"(?:live (?:music|show|gig) (?:is a must|only|required)|must (?:have|include) live music)",
            "live performance required",
        ),
    ]
    diet_text = re.sub(NON_VEG, "", normalized)
    if re.search(r"\b(?:must|only|require)\b.{0,14}non[ -]?veg|non[ -]?veg only", normalized):
        constraints.append("meat required")
    if re.search(r"\b(?:walk(?:ing)? only|no (?:cabs?|autos?|taxis?))\b", normalized):
        constraints.append("walking only")
    if re.search(r"\b(?:skip|no|without) (?:lunch|meals?|food)\b", normalized):
        constraints.append("skip meals")
    for pattern, label in rules:
        if re.search(
            pattern,
            diet_text if label in ("vegetarian", "pure vegetarian", "vegan") else normalized,
        ):
            constraints.append(label)
    for requirement in re.split(r"[,.;!?]|\b(?:and|but)\b", normalized):
        requirement = requirement.strip()
        if re.search(
            r"allerg|gluten|nut.free|halal|kosher|lactose|jain|pet.friendly|dog.friendly|stroller",
            requirement,
        ):
            constraints.append(requirement[:160])
        if re.search(r"only quiet|must (?:be |avoid )?(?:quiet|crowds?)", requirement):
            constraints.append("strictly avoid crowds")
    interests = []
    for clause in re.split(r"[,.;!?]|\b(?:and|but|with|plus|or|aur)\b", normalized):
        topic = classify(re.sub(NON_VEG, "", clause))
        if not topic:
            explicit = re.search(
                r"(?:interested in|i (?:love|like)|i want to (?:try|do))\s+(.+)", clause
            )
            if explicit:
                interests.append(explicit[1].strip()[:100])
        if (
            topic
            and re.search(r"\b(?:skip|dislike|avoid|hate|no)\b", clause)
            and not re.search("crowd", clause)
        ):
            constraints.append("avoid " + topic)
        elif topic and topic not in interests:
            interests.append(topic)
    group = re.search(
        r"\b(?:for|group of|party of)\s+(\d+)\s*(?:people|persons|adults|of us|friends)?\b(?!\s*(?:min|hour|hr))",
        normalized,
    )
    people = (
        int(group[1])
        if group
        else (2 if re.search(r"\b(?:both of us|as a couple|my partner and i)\b", normalized) else 1)
    )
    if people > 1 and budget is not None and re.search(r"per (?:person|head)|each\b", normalized):
        budget *= people
    area = re.search(
        r"\b(?:starting (?:from|near|in)|start from|near)\s+([a-z][\w -]{1,48}?)(?=\s+(?:at|with|for|and|on|budget|under)\b|[,.;!?]|$)",
        normalized,
    )
    return Preferences(
        city=city,
        budget=budget,
        available_time=f"{duration(normalized)} minutes",
        mood=text[:180],
        interests=interests,
        constraints=list(dict.fromkeys(constraints)),
        start_time=extract_clock(normalized),
        starting_area=area[1].strip() if area else "",
        people=people,
    )


def decode_input(value):
    if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        value = json.loads(value)
    if isinstance(value, dict):
        return Preferences.model_validate(value)
    if not isinstance(value, str) or len(value) > 3500:
        raise ValueError(
            "Describe the outing in up to 3,500 characters or paste the assignment JSON."
        )
    return None
