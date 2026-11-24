# Saturday Club · Perfect Saturday Planner

Describe a Saturday outing or paste the assignment JSON. The planner returns named places, a stop-by-stop timeline, walking estimates, spending for the whole group, reasons for each stop, and a trace of the tools it used.

[Open the live planner](https://perfect-saturday-planner-exz7.onrender.com). The free Render instance sleeps after inactivity. The first visit can take about a minute to wake.

## Run locally

Use Python 3.12. From the project directory:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```

Open http://127.0.0.1:8000. Run the checks with `python -m unittest -v`. Windows users can activate the environment with `.venv\Scripts\activate`.

## Try it

- Start with the Bangalore brief in the text box.
- Edit the Delhi brief to “I have ₹100 and 3 hours in Delhi. I like vegetarian food.” Every stop must match that focus. If none fits the budget, the planner explains the limit.
- Pick **Under ₹300** or edit a previous plan to 90 minutes. Compare the verified totals and selected places.
- Try “I’m tired and want to go out.” The planner asks for information and opens an editable form.
- Try a required live performance or an unverified dietary restriction. The planner explains missing evidence. For mixed-interest requests an unsupported diet can yield an explicitly labelled activities-only alternative. A food-only request explains which diet could not be verified.
- Paste the assignment JSON as written. All six required fields are also available in the form.

## Planning workflow

| Tool | Responsibility |
| --- | --- |
| `parse_preferences` | Validate structured input. For free text, optionally use the OpenAI Responses API. The key-free path uses a small supervised multinomial Naive Bayes classifier trained on the labelled phrases in preferences.py, with rules for budget, duration, dietary restrictions and city aliases. |
| `find_places` | Search real nearby places through Geoapify when enabled. Supplement results with a versioned guide containing venue links, evidence, coordinates and the date checked. |
| `build_candidates` | Explore up to five stops using bounded beam search. Rank primary interest coverage, useful visit time, pace, crowd assumptions and a full lunch when the window covers midday. Keep the budget as an upper limit. |
| `estimate_route` | Estimate each transfer. Use free walking for short trips and a modelled auto for longer hops. Allow up to three live walking lookups per request. Keep the local estimate if a lookup fails. |
| `validate_plan` | Recheck each chosen route with its final transfer estimates, buffers, waiting and visit time. Apply upper fares and charges for the entire party. Return numeric timing or spending blockers when available. |

FastAPI serves the frontend and a streaming `POST /api/plan` endpoint. Each newline-delimited JSON event is a completed tool action or a terminal plan, clarification or no-match result. `GET /health` reports readiness.

### Why a fixed workflow?

The planner is a bounded hybrid agent. Its coordinator chooses whether to ask a question, use live data, try a shorter outing, or return a partial plan. The five functions are called in an explicit order so that every displayed route goes through the same final feasibility check. A compact local classifier handles interest inference without a key. With an OpenAI key, only interpretation uses the LLM. Explanations are composed from accepted venue facts, arrival times, cost bounds and transfers. Tool events expose the actual inputs and outputs, including failed validation attempts.

## Data, assumptions and optional integrations

The included guide covers **18 places** in Bengaluru, Gurugram and Delhi. Venue records in `data/places.json` link to their sources. For venues without a published price the displayed range is an estimate. Book browsing excludes purchases. A venue with vegetarian dishes is distinct from an exclusively vegetarian restaurant. Estimated crowd levels are qualitative Saturday planning assumptions. No live attendance or show schedule is inferred. Unknown venue hours are restricted to 10:00–18:00, with a reminder to check. The Blossom listings disagree, so the guide uses their overlapping 11:00–21:00 window.

Auto fares and journey times are illustrative. The model assumes 1.4× straight-line distance, walking speed of 4.2 km/h, autos at 16 km/h plus pickup time, and at most three passengers per auto. Fares use a range of ₹35 + ₹13/km to ₹65 + ₹23/km per vehicle. All transfer and wait minutes count against the window. Riders should confirm actual fares.

Plans use INR and Saturday in India. An unlabelled duration from 1 through 12 is interpreted as hours. Larger numbers mean minutes. “Half day” means four hours and “evening” means three hours with a 17:00 default start. The form allows an explicit start time. The form defaults to one person at 10:00 IST. Enter the total shared budget. Travel starts at the first stop unless a starting point resolves. The return trip is additional. In guide mode use a named venue as the starting point. A free-text request with an unsupported city or unrecognised intent can be refined in the form.

The initial demo works with its local classifier and city guide. To enable the optional services, set these environment variables on the server and restart:

| Variable | Purpose |
| --- | --- |
| OPENAI_API_KEY | Structured free-text interpretation through the OpenAI Responses API |
| OPENAI_MODEL | Optional model override. The default is gpt-5.4-mini. |
| GEOAPIFY_API_KEY | Indian city geocoding, real-time place lookup and walking-route estimates |

The app uses bounded external requests and a per-process rate limit. On Render’s public ingress it uses its edge-supplied CF-Connecting-IP as the visitor key. Other deployments use the socket peer. It keeps no account or itinerary database. A successful geocoding match and data coverage are required for additional cities. Keys stay on the server.

## Deploy

Create a Render Web Service connected to this repository, or use the supplied Render Blueprint. Select Python and the Free instance. Build with `pip install -r requirements.txt`, start with `uvicorn main:app --host 0.0.0.0 --port $PORT`, and use `/health` as the health check.

## Reviewer handoff

The GitHub repository is private. Attach the source ZIP itself to the submission or grant the reviewer access to the repository. The public app works without a login. Open it immediately before the demo to wake the free instance.

## AI tools used

OpenAI Codex helped design and implement the preference parser, itinerary search, interface and regression tests. AI-assisted research collected linked venue sources, and an AI image tool created the original Bengaluru travel-poster illustration.
