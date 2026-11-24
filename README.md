# Saturday Club · Perfect Saturday Planner

Describe a Saturday outing or paste the assignment JSON. The planner returns named places, a stop-by-stop timeline, walking estimates, spending for the whole group, reasons for each stop, and a trace of the tools it used.

[Open the live planner](https://perfect-saturday-planner-exz7.onrender.com)

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
- Pick **Under ₹300** or edit a previous plan to 90 minutes. Compare the verified totals and selected places.
- Try “I’m tired and want to go out.” The planner asks for information and opens an editable form.
- Try a required live performance or an unverified dietary restriction. The planner explains what it cannot verify.
- Paste the assignment JSON as written. All six required fields are also available in the form.

## Planning workflow

| Tool | Responsibility |
| --- | --- |
| `parse_preferences` | Validate structured input. For free text, optionally use the OpenAI Responses API. The key-free path uses a small supervised multinomial Naive Bayes classifier trained on the labelled phrases in preferences.py, with rules for budget, duration, dietary restrictions and city aliases. |
| `find_places` | Search real nearby places through Geoapify when enabled. Supplement results with a versioned guide containing venue links, evidence, coordinates and the date checked. |
| `build_candidates` | Consider distinct types of stops, interest coverage, energy, dietary requirements and travel proximity. Produce ranked one- to three-stop outings. |
| `estimate_route` | Request walking routes for up to three shortlisted combinations when enabled. In city-guide mode, estimate walking time from distance, a detour factor and crossing allowance. |
| `validate_plan` | Add walking, transfer buffers, visits and the upper spending estimate. Check the full visit against known opening windows. Offer a shorter plan or explain the blocker. |

FastAPI serves the frontend and a streaming `POST /api/plan` endpoint. Each newline-delimited JSON event is a completed tool action or a terminal plan, clarification or no-match result. `GET /health` reports readiness.

## Data, assumptions and optional integrations

The included guide covers **16 places** in Bengaluru, Gurugram and Delhi. Venue records in `data/places.json` link to their sources. For venues without a published price the displayed range is an estimate. Book browsing excludes purchases. A venue with vegetarian dishes is distinct from an exclusively vegetarian restaurant. Crowd levels and show schedules are unverified. Unknown venue hours are flagged.

Plans use INR and Saturday in India. The form defaults to one person at 10:00 IST. Enter the total shared budget. Travel starts at the first stop unless a starting point resolves. The return trip is additional. In guide mode use a named venue as the starting point. A free-text request with an unsupported city or unrecognised intent can be refined in the form.

The initial demo works with its local classifier and city guide. To enable the optional services, set these environment variables on the server and restart:

| Variable | Purpose |
| --- | --- |
| OPENAI_API_KEY | Structured free-text interpretation through the OpenAI Responses API |
| OPENAI_MODEL | Optional model override. The default is gpt-5.4-mini. |
| GEOAPIFY_API_KEY | Indian city geocoding, real-time place lookup and walking-route estimates |

The app uses bounded external requests and a per-process rate limit. It keeps no account or itinerary database. A successful geocoding match and data coverage are required for additional cities. Keys stay on the server.

## Deploy

Create a Render Web Service connected to this repository, or use the supplied Render Blueprint. Select Python and the Free instance. Build with `pip install -r requirements.txt`, start with `uvicorn main:app --host 0.0.0.0 --port $PORT`, and use `/health` as the health check.

## AI tools used

OpenAI Codex helped design and implement the preference parser, itinerary search, interface and regression tests. AI-assisted research collected linked venue sources, and an AI image tool created the original Bengaluru travel-poster illustration.
