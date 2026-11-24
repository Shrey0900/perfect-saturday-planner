"""Regression cases supplied during the assignment review."""

import unittest
from unittest.mock import patch

from starlette.requests import Request

from itinerary import Brief, interpret_constraints, priorities, schedule
from main import visitor_key
from planner import BANGALORE_EXAMPLE, run_plan
from preferences import Preferences, classify, duration, local_parse
from test_planner import SAMPLE


async def resolve(value):
    with patch.dict("os.environ", {"OPENAI_API_KEY": "", "GEOAPIFY_API_KEY": ""}):
        return [event async for event in run_plan(value)]


class InputTests(unittest.TestCase):
    def test_diet_negation_in_text_and_structured_input(self):
        for text in ("non-vegetarian", "I eat non-veg", "non veg", "not a vegetarian"):
            with self.subTest(text=text):
                self.assertEqual(interpret_constraints(local_parse(text)).diet, "")
                self.assertEqual(interpret_constraints(Preferences(constraints=[text])).diet, "")
        self.assertEqual(interpret_constraints(local_parse("pure veg only")).diet, "only")

    def test_budgets_and_durations(self):
        for text, wanted in [("2000 total", 2000), ("1500 and 3 hrs", 1500), ("2k budget", 2000)]:
            self.assertEqual(local_parse("Delhi " + text).budget, wanted)
        for value, wanted in [
            (4, 240),
            ("4 minutes", 4),
            ("90", 90),
            ("half day", 240),
            ("evening", 180),
        ]:
            self.assertEqual(duration(value), wanted)
        self.assertEqual(Preferences(available_time="evening").start_time, "17:00")
        self.assertEqual(local_parse("shaam, 2 ghante in Delhi").start_time, "17:00")

    def test_extended_interests(self):
        for query in ("fine dining", "trekking", "nightlife", "gaane", "khana", "tehelna"):
            self.assertTrue(classify(query), query)

    def test_proxy_identity(self):
        def req(headers):
            return Request({"type": "http", "headers": headers, "client": ("127.0.0.1", 5000)})

        with patch.dict("os.environ", {"RENDER": "true"}):
            self.assertNotEqual(
                visitor_key(req([(b"cf-connecting-ip", b"192.0.2.1")])),
                visitor_key(req([(b"cf-connecting-ip", b"192.0.2.2")])),
            )
            self.assertEqual(visitor_key(req([])), "127.0.0.1")
        with patch.dict("os.environ", {"RENDER": ""}):
            self.assertEqual(visitor_key(req([(b"cf-connecting-ip", b"192.0.2.1")])), "127.0.0.1")


class ReviewedPlans(unittest.IsolatedAsyncioTestCase):
    async def test_sample_uses_primary_music_and_a_lunch(self):
        events = await resolve(BANGALORE_EXAMPLE)
        plan = events[-1]
        self.assertGreaterEqual(plan["total"], 180)
        self.assertLessEqual(plan["total"], 240)
        self.assertLessEqual(plan["high"], 2000)
        self.assertTrue(any("music" in s["place"]["primary_tags"] for s in plan["stops"]))
        self.assertTrue(
            any(
                s["place"].get("food_role") == "meal" and 705 <= s["start_minute"] <= 840
                for s in plan["stops"]
            )
        )
        self.assertTrue(any(s["leg"]["mode"] == "auto" for s in plan["stops"]))
        self.assertTrue(all("inputs" in e and "outputs" in e for e in events[:-1]))
        self.assertEqual(events[-2]["outputs"]["upper_inr"], plan["high"])

    async def test_loose_json_and_precise_questions(self):
        plan = (
            await resolve(
                dict(
                    SAMPLE,
                    budget="₹2,000",
                    available_time=4,
                    interests="food, music and walks",
                    another_field=True,
                )
            )
        )[-1]
        self.assertEqual(plan["type"], "plan")
        for invalid in (0, -2):
            event = (await resolve(dict(SAMPLE, people=invalid)))[-1]
            self.assertIn("people", event["message"])
        for missing, word, other in [
            ("city", "city", "budget"),
            ("budget", "budget", "time"),
            ("available_time", "time", "budget"),
        ]:
            value = dict(SAMPLE)
            value.pop(missing)
            message = (await resolve(value))[-1]["message"]
            self.assertIn(word, message)
            self.assertNotIn(other, message)

    async def test_unverified_food_gets_non_food_alternative(self):
        for constraint in ["Jain", "gluten free", "peanut allergy"]:
            plan = (await resolve(dict(SAMPLE, constraints=[constraint, "meal required"])))[-1]
            self.assertTrue(plan["partial"])
            self.assertIn(constraint, plan["notes"][0])
            self.assertTrue(all(s["place"]["kind"] != "restaurant" for s in plan["stops"]))

    async def test_unknown_interest_and_tiny_budget(self):
        unknown = (await resolve(dict(SAMPLE, interests=["scuba diving"])))[-1]
        self.assertTrue(unknown["partial"])
        self.assertEqual(unknown["unmatched"], ["scuba diving"])
        cheap = (await resolve(dict(SAMPLE, budget=100, available_time="8 hours")))[-1]
        self.assertLessEqual(cheap["high"], 100)
        self.assertTrue(any(str(cheap["spare"]) in n for n in cheap["notes"]))

    async def test_late_opening_guard(self):
        plan = (await resolve(dict(SAMPLE, interests=["books"], start_time="21:00")))[-1]
        self.assertTrue(
            all(
                s["place"]["id"] != "blr-blossom" and s["place"].get("hours")
                for s in plan.get("stops", [])
            )
        )

    async def test_crowd_preference_changes_scoring(self):
        prefs = Preferences(**SAMPLE)
        brief = Brief.create(prefs, interpret_constraints(prefs), ["art"], None)
        venue = {
            "id": "one",
            "name": "Gallery",
            "kind": "gallery",
            "minutes": 45,
            "high": 0,
            "low": 0,
            "tags": ["art"],
            "food_role": "",
            "crowd": "low",
        }
        leg = {"mode": "walk", "minutes": 0, "low": 0, "high": 0}
        quiet, _ = schedule([venue], [leg], brief)
        busy, _ = schedule([{**venue, "crowd": "high"}], [leg], brief)
        self.assertGreater(priorities(quiet, brief), priorities(busy, brief))

    async def test_unconfirmed_hours_late_guard(self):
        prefs = Preferences(**dict(SAMPLE, start_time="20:30"))
        brief = Brief.create(prefs, interpret_constraints(prefs), ["books"], None)
        venue = {"kind": "bookshop", "minutes": 35, "low": 0, "high": 0}
        _, failure = schedule([venue], [{"mode": "walk", "minutes": 0, "low": 0, "high": 0}], brief)
        self.assertEqual(failure[0], "closing time")
