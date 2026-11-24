"""Interpret a brief into limits, interests and editable planning assumptions."""
import json
import math
import re
from collections import Counter
from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field, field_validator

WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
         "half an hour": "30 minutes", "an hour": "1 hour", "ninety": "90"}
CITIES = {"Bengaluru": ["bangalore", "bengaluru"], "Gurugram": ["gurgaon", "gurugram"],
          "Delhi": ["new delhi", "delhi"], "Mumbai": ["mumbai", "bombay"], "Pune": ["pune"],
          "Hyderabad": ["hyderabad"], "Chennai": ["chennai"], "Kolkata": ["kolkata"],
          "Jaipur": ["jaipur"], "Dehradun": ["dehradun"], "Noida": ["noida"]}
# Tiny supervised text classifier. Train a multinomial naive Bayes model on
# labelled phrases and abstain on unfamiliar vocabulary or ambiguous clauses.
EXAMPLES = {
    "food": ["food eats meals lunch dinner breakfast snack snacks", "eat a vegetarian meal", "sip coffee tea café cafe", "taste local cuisine dishes", "hungry restaurant dosa"],
    "walks": ["walk walks walking stroll wander outdoors", "fresh air greenery gardens nature", "park trees quiet picnic", "stretch my legs in a garden"],
    "art": ["art paintings gallery galleries", "museum museums exhibitions design", "photography photographs sculpture", "draw and sketch artwork"],
    "books": ["books bookshop bookstore bookshops", "browse novels and poetry", "read literature book browsing", "reading fiction shelves"],
    "music": ["music listen acoustic gig concert", "live performance jazz singing", "vinyl records tunes bands"],
    "history": ["history heritage monuments architecture", "historic buildings old city", "historical landmarks fort palace"],
    "shopping": ["shopping markets browse stalls", "shop for gifts clothes handicrafts", "souvenirs local crafts boutiques"]
}
def tokens(s):
    return re.findall(r"[a-zÀ-ž]+", s.lower())

COUNTS = {label: Counter(tokens(" ".join(phrases))) for label, phrases in EXAMPLES.items()}
VOCAB = set().union(*COUNTS.values())

def classify(text):
    terms = [t for t in tokens(text) if t in VOCAB]
    if not terms:
        return None
    scores = {}
    for label, counts in COUNTS.items():
        scores[label] = sum(math.log((counts[t] + 0.25) / (sum(counts.values()) + 0.25 * len(VOCAB))) for t in terms)
    top, second = sorted(scores, key=scores.get, reverse=True)[:2]
    return top if scores[top] - scores[second] > 0.45 else None

Short = Annotated[str, Field(max_length=180)]
class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    city: Short = ""
    budget: int | None = Field(None, ge=0, le=1000000)
    available_time: str | int = ""
    mood: Short = ""
    interests: list[Short] = Field(default_factory=list, max_length=15)
    constraints: list[Short] = Field(default_factory=list, max_length=15)
    start_time: str = "10:00"
    starting_area: Short = ""
    people: int = Field(1, ge=1, le=30)

    @field_validator("start_time")
    @classmethod
    def check_time(cls, v):
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
            raise ValueError("Use a start time in HH:MM format.")
        return v

def normalize_numbers(text):
    for k,v in WORDS.items():
        text = re.sub(r"\b"+k+r"\b", v, text, flags=re.I)
    return text.lower()

def duration(text):
    s = normalize_numbers(str(text)).strip()
    if s.isdigit():
        return int(s)
    h = re.search(r"(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|h)\b",s)
    m = re.search(r"(\d+)\s*(?:minutes?|mins?|m)\b",s)
    return round(float(h[1])*60 if h else 0) + (int(m[1]) if m else 0)

def canonical_city(city):
    s=city.strip().casefold()
    return next((name for name, aliases in CITIES.items() if s in aliases), city.strip().title())

def extract_clock(text):
    match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", text, re.I)
    if match and 1 <= int(match[1]) <= 12 and int(match[2] or 0)<60:
        return f"{(int(match[1])%12 + (12 if match[3].lower()=='pm' else 0)):02}:{int(match[2] or 0):02}"
    match = re.search(r"\b(?:at|start(?:ing)?(?: at)?)\s+([01]?\d|2[0-3]):([0-5]\d)\b",text,re.I)
    return f"{int(match[1]):02}:{match[2]}" if match else "10:00"

def local_parse(text):
    s=normalize_numbers(text)
    city=next((name for name, aliases in CITIES.items() if any(re.search(r"\b"+a+r"\b",s) for a in aliases)), "")
    money=re.search(r"(?:₹|rs\.?|inr|budget(?:\s+of)?|under)\s*([\d,]+(?:\.\d+)?)(k)?\b",s)
    if not money:
        money=re.search(r"([\d,]+(?:\.\d+)?)(k)?\s*(?:rupees|rs|inr)\b",s)
    budget=round(float(money[1].replace(",","")) * (1000 if money[2] else 1)) if money else None
    if re.search(r"\b(?:zero budget|free only|no spending)\b",s):
        budget=0
    constraints=[]
    rules=[(r"pure[\s-]*veg|vegetarian[\s-]*only","pure vegetarian"),(r"\bvegan\b","vegan"),
           (r"\b(?:vegetarian|veg)\b","vegetarian"),(r"(?:avoid|hate|no|away from|skip) crowds?|\b(?:uncrowded|crowded places|quiet places)\b","avoid crowds"),
           (r"(?:indoors? only|stay indoors|no outdoors?)","indoors only"),(r"(?:outdoors? only|no indoors?)","outdoors only"),
           (r"wheelchair|step[\s-]*free","wheelchair access"),(r"(?:no walking|can't walk|cannot walk)","no walking"),
           (r"(?:must|need|want|have to) (?:have|get|eat|include|grab)?\s*(?:a |some )?(?:meal|lunch|dinner|breakfast|food)\b","meal required"),
           (r"(?:live (?:music|show|gig) (?:is a must|only|required)|must (?:have|include) live music)","live performance required")]
    for pattern, label in rules:
        if re.search(pattern,s):
            constraints.append(label)
    for requirement in re.split(r"[,.;!?]|\b(?:and|but)\b", s):
        requirement=requirement.strip()
        if re.search(r"allerg|gluten|nut.free|halal|kosher|lactose|jain|pet.friendly|dog.friendly|stroller",requirement):
            constraints.append(requirement[:160])
        if re.search(r"only quiet|must (?:be |avoid )?(?:quiet|crowds?)",requirement):
            constraints.append("strictly avoid crowds")
    interests=[]
    for clause in re.split(r"[,.;!?]|\b(?:and|but|with|plus|or)\b",s):
        cat=classify(clause)
        if cat and re.search(r"\b(?:skip|dislike|avoid|hate|no)\b",clause) and not re.search("crowd",clause):
            constraints.append("avoid "+cat)
        elif cat and cat not in interests:
            interests.append(cat)
    group=re.search(r"\b(?:for|group of|party of)\s+(\d+)\s*(?:people|persons|adults|of us|friends)?\b(?!\s*(?:min|hour|hr))",s)
    people=int(group[1]) if group else (2 if re.search(r"\b(?:both of us|as a couple|my partner and i)\b",s) else 1)
    if people>1 and budget is not None and re.search(r"per (?:person|head)|each\b",s):
        budget*=people
    area=re.search(r"\b(?:starting (?:from|near|in)|start from|near)\s+([a-z][\w -]{1,48}?)(?=\s+(?:at|with|for|and|on|budget|under)\b|[,.;!?]|$)",s)
    return Preferences(city=city,budget=budget,available_time=duration(s),mood=text[:180],
                       interests=interests,constraints=list(dict.fromkeys(constraints)),start_time=extract_clock(s),
                       starting_area=area[1].strip() if area else "",people=people)

def decode_input(value):
    if isinstance(value,str) and value.lstrip().startswith(("{","[")):
        value=json.loads(value)
    if isinstance(value,dict):
        return Preferences.model_validate(value)
    if not isinstance(value,str) or len(value)>3500:
        raise ValueError("Describe the outing in up to 3,500 characters or paste the assignment JSON.")
    return None
