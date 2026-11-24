"""Map short phrases to activity topics using explicit names and a local text classifier."""

import math
import re
from collections import Counter

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
STOPWORDS = {
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
}


def tokens(text):
    return [word for word in re.findall(r"[a-zÀ-ž]+", text.lower()) if word not in STOPWORDS]


COUNTS = {label: Counter(tokens(" ".join(phrases))) for label, phrases in EXAMPLES.items()}
VOCAB = set().union(*COUNTS.values())


def infer_topic(text):
    """Score known words with smoothed multinomial Naive Bayes and abstain on weak evidence."""
    terms = [t for t in tokens(text) if t in VOCAB]
    if not terms:
        return None
    scores = {}
    smoothing = 0.25
    for label, counts in COUNTS.items():
        denominator = sum(counts.values()) + smoothing * len(VOCAB)
        scores[label] = sum(math.log((counts[term] + smoothing) / denominator) for term in terms)
    top, second = sorted(scores, key=scores.get, reverse=True)[:2]
    return top if scores[top] - scores[second] > 0.45 else None


KNOWN_TOPICS = frozenset(
    (
        "food",
        "walks",
        "art",
        "books",
        "music",
        "history",
        "fine dining",
        "trekking",
        "nightlife",
        "shopping",
    )
)

# Recover explicit topic names before asking the single-label classifier to
# interpret an ambiguous phrase. Preserve the order the visitor mentioned them.
TOPIC_NAMES = {
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
    **{topic: topic for topic in KNOWN_TOPICS},
    "heritage": "history",
    "walk": "walks",
    "walking": "walks",
    "bookshops": "books",
    "museums": "art",
    "museum": "art",
    "lunch": "food",
    "dinner": "food",
}
TOPIC_PATTERN = re.compile(r"\b(?:" + "|".join(map(re.escape, TOPIC_NAMES)) + r")\b", re.I)


def named_topics(text):
    """Return explicit topics in the order the visitor mentioned them."""
    return list(dict.fromkeys(TOPIC_NAMES[m[0].lower()] for m in TOPIC_PATTERN.finditer(text)))


def interest_topics(text):
    """Preserve every explicitly named topic, then try the classifier for an unnamed activity."""
    named = named_topics(text)
    if named:
        return named
    inferred = infer_topic(text)
    return [inferred] if inferred else []


def classify(text):
    """Return one topic for a restriction or short label."""
    topics = interest_topics(text)
    return topics[0] if topics else None
