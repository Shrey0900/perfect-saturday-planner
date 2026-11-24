import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from interests import classify
from main import app
from planner import run_plan
from preferences import duration, local_parse

BANGALORE_EXAMPLE = (
    "I'm in Bangalore with ₹2,000 and 4 hours. I'm tired but want to get out. I like food, music "
    "and walks. I'm vegetarian and want to avoid crowded places."
)

SAMPLE = {
    "city": "Bangalore",
    "budget": 2000,
    "available_time": "4 hours",
    "mood": "tired",
    "interests": ["food", "music", "walks"],
    "constraints": ["vegetarian", "avoid crowded places"],
}


class PlannerTests(unittest.IsolatedAsyncioTestCase):
    async def events(self, value):
        with patch.dict("os.environ", {"GEOAPIFY_API_KEY": "", "OPENAI_API_KEY": ""}):
            return [e async for e in run_plan(value)]

    async def test_example_and_trace(self):
        events = await self.events(BANGALORE_EXAMPLE)
        self.assertEqual(
            [e["tool"] for e in events[:-1]],
            [
                "parse_preferences",
                "find_places",
                "build_candidates",
                "estimate_route",
                "validate_plan",
            ],
        )
        plan = events[-1]
        self.assertEqual(plan["type"], "plan")
        self.assertGreater(len(plan["stops"]), 1)
        self.assertTrue(all(s["why"] and s["place"]["source"] for s in plan["stops"]))
        self.assertLessEqual(plan["total"], 240)
        self.assertLessEqual(plan["high"], 2000)

    async def test_costs_time_and_alternatives(self):
        p = (await self.events(dict(SAMPLE, budget=300, available_time="90 minutes")))[-1]
        self.assertEqual(p["type"], "plan")
        self.assertLessEqual(p["total"], 90)
        self.assertLessEqual(p["high"], 300)
        self.assertEqual(
            p["total"],
            sum(
                s["leg"]["minutes"] + s["wait"] + s["buffer"] + s["place"]["minutes"]
                for s in p["stops"]
            ),
        )
        self.assertEqual(p["high"], sum(s["place"]["high"] + s["leg"]["high"] for s in p["stops"]))

    async def test_explicit_vegetarian_meal(self):
        p = (
            await self.events(
                dict(SAMPLE, constraints=["pure vegetarian", "meal required"], people=2)
            )
        )[-1]
        food = [s["place"] for s in p["stops"] if s["place"]["kind"] == "restaurant"]
        self.assertTrue(food and all(s["veg"] == "only" for s in food))
        self.assertEqual(
            p["high"], sum(2 * s["place"]["high"] + s["leg"]["high"] for s in p["stops"])
        )

    async def test_no_match_and_clarification(self):
        for v, kind in [
            ("Tired and bored", "clarify"),
            (dict(SAMPLE, city="Pune"), "empty"),
            (dict(SAMPLE, budget=-1), "clarify"),
            (dict(SAMPLE, available_time="1 minute"), "empty"),
            (dict(SAMPLE, constraints=["wheelchair access"]), "plan"),
            (dict(SAMPLE, constraints=["live performance required"]), "empty"),
            (dict(SAMPLE, starting_area="Unresolved corner"), "clarify"),
            ("In Delhi with ₹500 for 3 hours. I have a peanut allergy.", "plan"),
        ]:
            with self.subTest(v=v):
                self.assertEqual((await self.events(v))[-1]["type"], kind)

    async def test_editable_city_guides_and_json(self):
        for city in ("Gurgaon", "Delhi", "Bangalore"):
            with self.subTest(city=city):
                p = (await self.events(json.dumps(dict(SAMPLE, city=city))))[-1]
                self.assertEqual(p["type"], "plan")
        self.assertEqual((await self.events("{ broken"))[-1]["type"], "clarify")

    async def test_starting_venue_and_no_walking(self):
        p = (await self.events(dict(SAMPLE, starting_area="Cubbon Park")))[-1]
        self.assertEqual(p["type"], "plan")
        p = (await self.events(dict(SAMPLE, constraints=["no walking"])))[-1]
        self.assertEqual(p["walk"], 0)
        self.assertFalse(any("walks" in s["place"]["tags"] for s in p["stops"]))

    async def test_provider_outage_preserves_local_venues(self):
        import httpx

        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "test", "GEOAPIFY_API_KEY": "test"}),
            patch("planner.model_parse", new=AsyncMock(side_effect=httpx.ReadTimeout("timeout"))),
            patch("places.geocode", new=AsyncMock(side_effect=httpx.ReadTimeout("timeout"))),
            patch("routing.walk_route", new=AsyncMock(side_effect=httpx.ReadTimeout("timeout"))),
        ):
            events = [
                e async for e in run_plan(dict(SAMPLE, constraints=["vegetarian", "walking only"]))
            ]
        self.assertEqual(events[-1]["type"], "plan")
        self.assertGreater(len(events[-1]["stops"]), 1)
        self.assertTrue(any("unavailable" in s["leg"]["source"] for s in events[-1]["stops"]))

    def test_normalization(self):
        self.assertEqual(classify("I want a museum"), "art")
        self.assertIsNone(classify("I am in Delhi"))
        self.assertEqual(duration("1 hour 30 minutes"), 90)
        self.assertEqual(local_parse("Delhi, ₹1,500 and two hours at 3 pm").start_time, "15:00")
        self.assertEqual(local_parse("Delhi, ₹1.5k, 2 hours").budget, 1500)


class ApiTests(unittest.TestCase):
    def test_normal_entry_points(self):
        with TestClient(app) as client:
            self.assertEqual(client.get("/").status_code, 200)
            self.assertEqual(client.get("/health").json(), {"status": "ok"})
            self.assertEqual(
                client.get("/static/saturday.webp").headers["content-type"], "image/webp"
            )
            with patch.dict("os.environ", {"OPENAI_API_KEY": "", "GEOAPIFY_API_KEY": ""}):
                r = client.post("/api/plan", json=SAMPLE)
            self.assertIn("application/x-ndjson", r.headers["content-type"])
            self.assertEqual(json.loads(r.text.splitlines()[-1])["type"], "plan")


if __name__ == "__main__":
    unittest.main()
