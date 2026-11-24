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
    async def test_budget_preset_visits_bookshop_after_opening(self):
        plan = (
            await resolve(
                "I'm in Bengaluru. I have ₹300 and 90 minutes for books and vegetarian food. Let's take it easy."
            )
        )[-1]
        stops = plan["stops"]
        self.assertEqual([s["place"]["kind"] for s in stops], ["restaurant", "bookshop"])
        self.assertEqual(stops[1]["start"], "11:00")
        self.assertEqual(plan["total"], 90)
        self.assertLessEqual(plan["high"], 300)
        self.assertEqual(
            sum(
                s["place"]["minutes"] + s["leg"]["minutes"] + s["wait"] + s["buffer"] for s in stops
            ),
            90,
        )

    async def test_budget_position_and_opening_time(self):
        events = await resolve("bangalore 2000 budget 4 hours i like music")
        self.assertEqual(events[0]["outputs"]["budget"], 2000)
        stop = events[-1]["stops"][0]
        self.assertEqual(
            (stop["place"]["id"], stop["start"], stop["wait"]), ("blr-ime", "11:00", 60)
        )
        self.assertEqual(events[-1]["total"], 150)
        early = (await resolve(dict(SAMPLE, interests=["art"], start_time="08:00")))[-1]
        self.assertIn("opens at 10:00", early["message"])
        late = (await resolve(dict(SAMPLE, start_time="21:00")))[-1]
        self.assertTrue(any("Cubbon Park closes at 18:00" in n for n in late["notes"]))

    async def test_compound_and_adjacent_topics_visible_in_trace(self):
        events = await resolve(
            "I have ₹700 and 3 hours in Delhi. I like heritage walks, books and vegetarian food."
        )
        self.assertEqual(events[0]["outputs"]["interests"], ["history", "walks", "books", "food"])
        self.assertIn("vegetarian", events[0]["outputs"]["constraints"])
        self.assertEqual(events[-1]["unmatched"], [])
        self.assertTrue(any("heritage walk" in s["why"] for s in events[-1]["stops"]))
        events = await resolve("Bangalore 2000 budget 4 hours I like food music and walks")
        self.assertEqual(events[0]["outputs"]["interests"], ["food", "music", "walks"])
        self.assertEqual(events[-1]["unmatched"], [])

    async def test_tired_and_low_cost_tradeoffs(self):
        sample = (await resolve(BANGALORE_EXAMPLE))[-1]
        self.assertTrue(
            any("75 minutes in autos" in n and "lower-travel option" in n for n in sample["notes"])
        )
        cheap = (await resolve(dict(SAMPLE, budget=100, available_time="8 hours")))[-1]
        self.assertGreaterEqual(cheap["total"], 164)
        self.assertEqual(cheap["high"], 0)
        self.assertTrue(any("the visit is free" in s["why"] for s in cheap["stops"]))

    async def test_edited_delhi_brief_keeps_every_stop_about_food(self):
        for text in (
            "I like vegetarian food.",
            "I like only vegitatrian food.",
            "No museums or walks. Just veg food.",
        ):
            with self.subTest(text=text):
                plan = (await resolve("I have Rs 100 and 3 hours in Delhi. " + text))[-1]
                self.assertEqual(plan["type"], "plan")
                self.assertEqual(plan["preferences"]["interests"], ["food"])
                self.assertIn("vegetarian", plan["preferences"]["constraints"])
                self.assertEqual([s["place"]["id"] for s in plan["stops"]], ["del-jain-chawal"])
                self.assertLessEqual(plan["high"], 100)
                self.assertEqual(plan["spare"], 150)

    async def test_one_interest_never_pads_or_substitutes(self):
        data = dict(city="Delhi", available_time="3 hours", budget=700, interests="vegetarian food")
        food = (await resolve(data))[-1]
        self.assertEqual(food["type"], "plan")
        self.assertIn("vegetarian", food["preferences"]["constraints"])
        self.assertTrue(all(stop["place"]["kind"] == "restaurant" for stop in food["stops"]))
        for changes, blocker in (
            ({"budget": 50}, "₹75"),
            ({"budget": 100, "people": 2}, "₹150"),
            ({"constraints": ["gluten free"]}, "gluten free"),
            ({"interests": ["music"]}, "music"),
        ):
            response = (await resolve({**data, **changes}))[-1]
            self.assertEqual(response["type"], "empty")
            self.assertIn(blocker, response["message"])
        books = (await resolve({**data, "interests": ["books"]}))[-1]
        self.assertEqual([s["place"]["kind"] for s in books["stops"]], ["bookshop"])
        lunch = (await resolve({**data, "interests": ["books"], "constraints": ["meal required"]}))[
            -1
        ]
        self.assertEqual({s["place"]["kind"] for s in lunch["stops"]}, {"bookshop", "restaurant"})

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
