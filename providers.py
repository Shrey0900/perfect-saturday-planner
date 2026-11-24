"""External lookups. Values shown as facts come from a named place or routing record."""

import asyncio
import math
import os

from preferences import Preferences, canonical_city

ROOT = "https://api.geoapify.com"
SETTINGS = {
    "park": ("leisure.park", ["walks"], 35, 0, 100),
    "restaurant": ("catering.restaurant", ["food"], 45, 180, 550),
    "museum": ("entertainment.museum", ["art", "history"], 50, 50, 600),
    "music shop": ("commercial.hobby.music", ["music"], 35, 0, 0),
    "bookshop": ("commercial.books", ["books"], 35, 0, 0),
    "gallery": ("entertainment.culture.gallery", ["art"], 40, 0, 400),
}


async def model_parse(client, text):
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
                "Extract preferences for an Indian Saturday outing. Use the visitor's hard restrictions verbatim, including unhandled ones. "
                "Use total INR budget across people, null if missing. If group budget basis is unclear add 'clarify group budget' to constraints. "
                "Use explicit duration strings with units, e.g. 4 minutes or 4 hours (empty string if missing), HH:MM local start_time (default 10:00), people default 1. "
                "City and starting_area default empty. Preserve names, exclusions, diet and must-have activities. Interests are short categories. Never invent facts."
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
    for item in response.json().get("output", []):
        for part in item.get("content", []):
            if part.get("type") == "output_text":
                return Preferences.model_validate_json(part["text"])
    return None


async def geo(client, path, params):
    key = os.environ.get("GEOAPIFY_API_KEY")
    if not key:
        return None
    response = await client.get(ROOT + path, params={**params, "apiKey": key}, timeout=6)
    response.raise_for_status()
    return response.json()


async def geocode(client, city, area=""):
    query = ", ".join(x for x in [area, city, "India"] if x)
    data = await geo(
        client,
        "/v1/geocode/search",
        {"text": query, "filter": "countrycode:in", "format": "json", "limit": 3},
    )
    for row in (data or {}).get("results", []):
        names = [row.get(k) or "" for k in ("city", "county", "district", "name")]
        if not any(canonical_city(v) == canonical_city(city) for v in names):
            continue
        # A city-level hit must never be presented as a resolved starting neighbourhood.
        if area and row.get("result_type") in ("city", "state", "country", "county"):
            continue
        return (row["lat"], row["lon"])
    return None


def km(start, end):
    lat1, lon1, lat2, lon2 = map(math.radians, (*start, *end))
    haversine = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 6371 * 2 * math.asin(min(1, math.sqrt(haversine)))


async def nearby(client, preferences, centre, kind, diet):
    category, tags, minutes, low, high = SETTINGS[kind]
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
            "," + {"only": "vegetarian.only", "options": "vegetarian", "vegan": "vegan"}[diet]
        )
    data = await geo(client, "/v2/places", params)
    found = []
    for feature in (data or {}).get("features", []):
        properties = feature["properties"]
        if not properties.get("name"):
            continue
        raw = (properties.get("datasource") or {}).get("raw") or {}
        source = raw.get("website") or properties.get("website")
        if not isinstance(source, str) or not source.startswith(("http://", "https://")):
            source = (
                "https://www.openstreetmap.org/?mlat="
                + str(properties["lat"])
                + "&mlon="
                + str(properties["lon"])
                + "#map=18/"
                + str(properties["lat"])
                + "/"
                + str(properties["lon"])
            )
        found.append(
            dict(
                id=properties["place_id"],
                name=properties["name"],
                city=preferences.city,
                area=properties.get("suburb") or properties.get("district") or preferences.city,
                lat=properties["lat"],
                lon=properties["lon"],
                tags=tags,
                kind=kind,
                minutes=minutes,
                low=low,
                high=high,
                veg=diet if kind == "restaurant" and diet else "none",
                primary_tags=tags,
                food_role="meal" if kind == "restaurant" else "",
                price_basis="Category-based spending estimate",
                source=source,
                note=(
                    "Browsing visit; purchases are extra."
                    if kind in ("bookshop", "music shop")
                    else "Category-based spending estimate. Confirm prices and opening hours before leaving."
                ),
                accessible=raw.get("wheelchair") == "yes",
                origin="Live place search",
            )
        )
    return found


async def live_places(client, preferences, centre, interests, diet):
    kinds = [
        kind for kind, (_, tags, *_) in SETTINGS.items() if set(interests).intersection(tags)
    ] or [
        "park",
        "restaurant",
        "museum",
    ]
    responses = await asyncio.gather(
        *(nearby(client, preferences, centre, kind, diet) for kind in kinds[:4]),
        return_exceptions=True,
    )
    return [row for group in responses if isinstance(group, list) for row in group]


async def walk_route(client, points):
    if len(points) < 2:
        return []
    data = await geo(
        client,
        "/v1/routing",
        {"waypoints": "|".join(f"{a},{b}" for a, b in points), "mode": "walk", "format": "json"},
    )
    if not data:
        return None
    legs = data["results"][0]["legs"]
    if len(legs) != len(points) - 1:
        return None
    return [max(1, math.ceil(float(leg["time"]) / 60)) for leg in legs]
