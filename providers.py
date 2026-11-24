"""External lookups. Values shown as facts come from a named place or routing record."""

import asyncio
import json
import logging
import math
import os

import httpx

from pydantic import ValidationError

from constraints import Diet, VENUE_DIET_CODE
from preferences import Preferences, canonical_city


class ProviderDataError(Exception):
    """The upstream response did not match the provider's expected data format."""


PROVIDER_ERRORS = (httpx.HTTPError, ProviderDataError)
LOGGER = logging.getLogger(__name__)


def live_data_enabled():
    return bool(os.getenv("GEOAPIFY_API_KEY", "").strip())


def json_object(value):
    if not isinstance(value, dict):
        raise ProviderDataError("Expected a JSON object")
    return value


def read_response(response):
    try:
        return json_object(response.json())
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ProviderDataError("Invalid JSON response") from error


def records(obj, key):
    items = obj.get(key)
    if not isinstance(items, list):
        raise ProviderDataError(f"Expected a list of {key}")
    return [json_object(item) for item in items]


def numeric(obj, key):
    value = obj.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ProviderDataError(f"Invalid numeric field: {key}")
    return value


ROOT = "https://api.geoapify.com"
LIVE_CATEGORIES = {
    "park": ("leisure.park", ["walks"], 35, 0, 100),
    "restaurant": ("catering.restaurant", ["food"], 45, 180, 550),
    "museum": ("entertainment.museum", ["art", "history"], 50, 50, 600),
    "music shop": ("commercial.hobby.music", ["music"], 35, 0, 0),
    "bookshop": ("commercial.books", ["books"], 35, 0, 0),
    "gallery": ("entertainment.culture.gallery", ["art"], 40, 0, 400),
}


async def model_parse(client, text):
    """Ask the configured OpenAI model for a brief that validates against the same input schema."""
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        return None
    schema = Preferences.model_json_schema()
    # Strict outputs require every field to be present, with null for unknown budget.
    schema["additionalProperties"] = False
    schema["required"] = list(schema["properties"])
    for field in schema["properties"].values():
        field.pop("default", None)
    response = await client.post(
        "https://api.openai.com/v1/responses",
        headers={"Authorization": "Bearer " + key},
        json={
            "model": os.getenv("OPENAI_MODEL", "gpt-5.4-mini"),
            "store": False,
            "instructions": (
                "Extract preferences for an Indian Saturday outing. Use the visitor's hard "
                "restrictions verbatim, including unhandled ones. "
                "Use total INR budget across people, null if missing. If group budget basis is "
                "unclear add 'clarify group budget' to constraints. "
                "Use explicit duration strings with units, e.g. 4 minutes or 4 hours (empty "
                "string if missing), HH:MM local start_time (default 10:00), people default 1. "
                "City and starting_area default empty. Preserve names, exclusions, diet and "
                "must-have activities. Interests are short categories. Never invent facts."
            ),
            "input": text,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "preferences",
                    "strict": True,
                    "schema": schema,
                }
            },
        },
    )
    response.raise_for_status()
    for item in records(read_response(response), "output"):
        if "content" not in item:
            continue
        for part in records(item, "content"):
            if part.get("type") == "output_text":
                if not isinstance(part.get("text"), str):
                    raise ProviderDataError("Missing model output text")
                try:
                    return Preferences.model_validate_json(part["text"])
                except ValidationError as error:
                    raise ProviderDataError("Invalid preference fields from model") from error
    return None


async def geo(client, path, params):
    key = os.environ["GEOAPIFY_API_KEY"].strip()
    response = await client.get(ROOT + path, params={**params, "apiKey": key}, timeout=6)
    response.raise_for_status()
    return read_response(response)


async def geocode(client, city, area=""):
    """Resolve the city or starting area in India and reject ambiguous city-only area matches."""
    query = ", ".join(x for x in [area, city, "India"] if x)
    data = await geo(
        client,
        "/v1/geocode/search",
        {"text": query, "filter": "countrycode:in", "format": "json", "limit": 3},
    )
    for row in records(data, "results"):
        names = [row.get(k) or "" for k in ("city", "county", "district", "name")]
        if not any(isinstance(v, str) and canonical_city(v) == canonical_city(city) for v in names):
            continue
        # A city-level hit must never be presented as a resolved starting neighbourhood.
        if area and row.get("result_type") in ("city", "state", "country", "county"):
            continue
        return (numeric(row, "lat"), numeric(row, "lon"))
    return None


async def nearby(client, preferences, centre, kind, diet):
    """Translate Geoapify records for one category into the guide's common venue format."""
    category, tags, minutes, low, high = LIVE_CATEGORIES[kind]
    lat, lon = centre
    params = {
        "categories": category,
        "filter": f"circle:{lon},{lat},3500",
        "bias": f"proximity:{lon},{lat}",
        "limit": 5,
        "conditions": "named",
    }
    if kind == "restaurant" and diet:
        params["conditions"] += (
            ","
            + {
                Diet.PURE_VEG: "vegetarian.only",
                Diet.VEG_FRIENDLY: "vegetarian",
                Diet.VEGAN: "vegan",
            }[diet]
        )
    data = await geo(client, "/v2/places", params)
    found = []
    for feature in records(data, "features"):
        properties = json_object(feature.get("properties"))
        if not isinstance(properties.get("name"), str) or not properties["name"]:
            continue
        if not isinstance(properties.get("place_id"), str):
            raise ProviderDataError("Place has no identifier")
        latitude = numeric(properties, "lat")
        longitude = numeric(properties, "lon")
        raw = json_object(json_object(properties.get("datasource") or {}).get("raw") or {})
        source = raw.get("website") or properties.get("website")
        if not isinstance(source, str) or not source.startswith(("http://", "https://")):
            source = (
                f"https://www.openstreetmap.org/?mlat={latitude}&mlon={longitude}"
                f"#map=18/{latitude}/{longitude}"
            )
        found.append(
            dict(
                id=properties["place_id"],
                name=properties["name"],
                city=preferences.city,
                area=properties.get("suburb") or properties.get("district") or preferences.city,
                lat=latitude,
                lon=longitude,
                tags=tags,
                kind=kind,
                minutes=minutes,
                low=low,
                high=high,
                veg=VENUE_DIET_CODE[diet] if kind == "restaurant" and diet else "none",
                primary_tags=tags,
                food_role="meal" if kind == "restaurant" else "",
                price_basis="Category-based spending estimate",
                source=source,
                note=(
                    "Browsing visit; purchases are extra."
                    if kind in ("bookshop", "music shop")
                    else (
                        "Category-based spending estimate. Confirm prices and opening hours "
                        "before leaving."
                    )
                ),
                accessible=raw.get("wheelchair") == "yes",
                origin="Live place search",
            )
        )
    return found


async def live_places(client, preferences, centre, interests, diet):
    """Search up to four categories concurrently and keep results from successful calls."""
    requested = set(interests)
    kinds = [
        kind for kind, (_, tags, *_) in LIVE_CATEGORIES.items() if requested.intersection(tags)
    ] or [
        "park",
        "restaurant",
        "museum",
    ]

    async def search_category(kind):
        try:
            return await nearby(client, preferences, centre, kind, diet)
        except PROVIDER_ERRORS as error:
            # HTTP error strings may contain the API key. Log only the failure class.
            LOGGER.warning("Geoapify category %s unavailable (%s)", kind, type(error).__name__)
            return []

    groups = await asyncio.gather(*(search_category(kind) for kind in kinds[:4]))
    return [place for group in groups for place in group]


async def walk_route(client, points):
    """Return whole walking minutes per leg when Geoapify supplies a complete route."""
    if len(points) < 2:
        return []
    data = await geo(
        client,
        "/v1/routing",
        {"waypoints": "|".join(f"{a},{b}" for a, b in points), "mode": "walk", "format": "json"},
    )
    routes = records(data, "results")
    if not routes:
        return None
    legs = records(routes[0], "legs")
    if len(legs) != len(points) - 1:
        return None
    return [max(1, math.ceil(numeric(leg, "time") / 60)) for leg in legs]
