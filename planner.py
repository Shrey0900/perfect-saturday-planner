"""Finite itinerary search. Prices, distance and time are computed outside the model."""
import itertools
import json
import math
import os
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import httpx
from pydantic import ValidationError
from preferences import Preferences, canonical_city, classify, decode_input, duration, local_parse
from providers import geocode, km, live_places, model_parse, walk_route

CATALOGUE=json.loads((Path(__file__).parent/"data/places.json").read_text())
BANGALORE_EXAMPLE="I'm in Bangalore with ₹2,000 and 4 hours. I'm tired but want to get out. I like food, music and walks. I'm vegetarian and want to avoid crowded places."
DEFAULT_TAGS=["walks","food","art"]
FALLBACK_CITIES=sorted({r["city"] for r in CATALOGUE})

def progress(tool, message):
    return {"type":"progress","tool":tool,"message":message}

def restrictions(p):
    s=" | ".join(p.constraints).lower()
    diet="vegan" if "vegan" in s else "only" if re.search(r"pure.?veg|vegetarian.?only|only vegetarian",s) else "options" if re.search(r"\bveg(etarian)?\b",s) else ""
    excluded=set()
    required=set()
    flags=set()
    unknown=[]
    for item in p.constraints:
        t=item.lower().strip()
        if not t: continue
        if re.search("wheelchair|step.free|accessible entrance",t): flags.add("accessible")
        elif re.search("no walking|cannot walk|can't walk|no walks",t): flags.add("no_walk")
        elif re.search("live (music|performance|show|gig)",t): flags.add("live")
        elif re.search("crowd|quiet|peaceful",t):
            flags.add("crowds")
            if re.search("must|only|guarantee|strictly",t): flags.add("confirmed_quiet")
        elif re.search("indoor|avoid (?:the )?(?:heat|sun|outdoor)",t): flags.add("indoor")
        elif re.search("outdoor",t): flags.add("outdoor")
        elif re.search(r"no (?:stairs|steps)",t): flags.add("accessible")
        elif re.search(r"(?:no|avoid|skip|dislike)\b",t) and classify(t):
            excluded.add(classify(t))
        elif re.search(r"meal required|must|need|include|mandatory|required",t) and classify(t):
            required.add(classify(t))
        elif re.search(r"\b(?:veg|vegetarian|vegan)\b|pure.veg|^none$|no constraints|no restrictions",t): pass
        else: unknown.append(item)
    return diet,excluded,required,flags,unknown

def qualifies(place, diet, excluded, flags):
    tags=set(place["tags"])
    if tags & excluded: return False
    if "accessible" in flags and not place.get("accessible"): return False
    if "indoor" in flags and not ("indoors" in tags or place["kind"] in ("restaurant","museum","gallery","bookshop") and "outdoors" not in tags): return False
    if "outdoor" in flags and not ("outdoors" in tags or place["kind"]=="park"): return False
    if "no_walk" in flags and ("walks" in tags or place["kind"]=="park"): return False
    if diet and place["kind"]=="restaurant":
        ok={"only":{"only"},"options":{"only","options","vegan"},"vegan":{"vegan"}}
        return place["veg"] in ok[diet]
    return True

def clock(minute):
    return f"{minute//60:02d}:{minute%60:02d}"

def position(row): return (row["lat"],row["lon"])

def approximate_legs(points):
    # Local estimate includes a street detour and time at crossings.
    return [max(2,math.ceil(km(a,b)*1.4/4.2*60)+2) for a,b in zip(points,points[1:])]

def build_candidates(rows,p,tags,required,flags,origin):
    tired=bool(re.search(r"tired|low.energy|relax|gentle|slow|exhaust|laid.back",p.mood,re.I))
    scored=[]
    for length in range(1,min(3,len(rows))+1):
        for stops in itertools.permutations(rows,length):
            kinds=[r["kind"] for r in stops]
            if len(set(kinds))<length: continue
            covered=set().union(*(r["tags"] for r in stops))
            if not required <= covered: continue
            points=([origin] if origin else [])+[position(r) for r in stops]
            legs=approximate_legs(points)
            if any(m>(25 if tired else 40) for m in legs) or ("no_walk" in flags and legs): continue
            value=16*len(set(tags)&covered)+2*length-0.20*sum(legs)
            if tired: value-=2*(length-1)+0.09*sum(legs)
            if "crowds" in flags: value-=4*kinds.count("market")
            # Prefer a meal as the final anchor when the fit is otherwise comparable.
            if length>1 and kinds[-1]=="restaurant": value+=1
            value-=sum(r["high"] for r in stops)/max(100,p.budget+1)
            scored.append((value,stops,points,legs))
    return sorted(scored,key=lambda item:-item[0])

def validate_plan(stops,legs,p,total_minutes):
    start_h,start_m=map(int,p.start_time.split(":"))
    start=start_h*60+start_m
    at=start
    low=high=0
    timeline=[]
    walk_offset=len(legs)-len(stops)+1
    for i,stop in enumerate(stops):
        leg_idx=i-1+walk_offset
        walk=legs[leg_idx] if leg_idx>=0 else 0
        buffer=5 if walk else 0
        at+=walk+buffer
        end=at+stop["minutes"]
        opening=stop.get("hours")
        if opening and not (opening[0]<=at and end<=opening[1]): return None,"opening hours"
        if end>24*60: return None,"Saturday ends"
        low+=stop["low"]*p.people
        high+=stop["high"]*p.people
        timeline.append({"place":stop,"start":clock(at),"end":clock(end),"walk":walk,"buffer":buffer})
        at=end
    if high>p.budget: return None,"budget"
    if at-start>total_minutes: return None,"available time"
    return {"stops":timeline,"low":low,"high":high,"total":at-start,"walk":sum(legs),
            "spare":total_minutes-(at-start),"budget_left":p.budget-high},""

def explain(stop,p,tags,diet):
    r=stop["place"]
    matches=[t for t in tags if t in r["tags"]]
    reasons=["Matches your interest in "+", ".join(matches)+"."] if matches else []
    if r["kind"]=="restaurant" and diet:
        reasons.append({"only":"The source identifies this as a vegetarian-only venue.",
            "options":"The source lists vegetarian food here.","vegan":"The listing includes vegan food."}[diet])
    if re.search(r"tired|gentle|relax|low.energy",p.mood,re.I):
        reasons.append(f"A {r['minutes']}-minute stop keeps this part of the day manageable.")
    return " ".join(reasons) or f"A {r['minutes']}-minute visit fits your available time."

async def run_plan(value):
    try:
        p=decode_input(value)
        async with httpx.AsyncClient(timeout=12,trust_env=False) as client:
            if p is None:
                try: p=await model_parse(client,value)
                except (httpx.HTTPError,ValueError,KeyError,TypeError): p=None
                method="Language model" if p else "Local interest classifier"
                if not p: p=local_parse(value)
            else: method="Structured input"
            p.city=canonical_city(p.city)
            total_minutes=duration(p.available_time)
            yield progress("parse_preferences",method+" interpreted the request.")
            missing=[]
            if not p.city: missing.append("Which Indian city are you starting in?")
            if p.budget is None or not total_minutes: missing.append("What are your total budget in rupees and available time?")
            if "clarify group budget" in p.constraints or (isinstance(value,str) and p.people>1 and not re.search(r"\b(total|together|each|per (person|head)|shared|between us)\b",value,re.I)):
                missing.append("What is the total budget for your group?")
            if missing or total_minutes>1440:
                yield {"type":"clarify","message":" ".join(missing[:2]) or "How much time do you have on Saturday, up to 24 hours?",
                       "preferences":p.model_dump()};return
            diet,excluded,required,flags,unknown=restrictions(p)
            if unknown or flags.intersection({"live","confirmed_quiet"}):
                message=("I can’t verify this requirement from the available place records: "+", ".join(unknown)+".") if unknown else (
                    "I don’t have Saturday-specific performance listings." if "live" in flags else "I don’t have confirmed live crowd levels.")
                yield {"type":"empty","message":message+" Edit that requirement if it is flexible.","preferences":p.model_dump()};return
            tags=list(dict.fromkeys(classify(t) or t.lower() for t in p.interests)) or DEFAULT_TAGS
            tags=[t for t in tags if t not in excluded]
            local=[r for r in CATALOGUE if r["city"]==p.city]
            origin=None
            if p.starting_area:
                match=next((r for r in local if p.starting_area.casefold()==r["name"].casefold()),None)
                if match: origin=position(match)
            live=[]
            have_geo=bool(os.getenv("GEOAPIFY_API_KEY"))
            if have_geo:
                try:
                    centre=origin or await geocode(client,p.city,p.starting_area)
                    if p.starting_area: origin=centre
                    if centre: live=await live_places(client,p,centre,tags+list(required),diet)
                except (httpx.HTTPError,ValueError,KeyError,TypeError): pass
            if p.starting_area and not origin:
                yield {"type":"clarify","message":"I couldn’t locate that starting point. Enter a listed venue as the starting point, or leave it blank to start the clock at the first stop.","preferences":p.model_dump()};return
            records={r["name"].casefold():r for r in live}
            records.update({r["name"].casefold():r for r in local})
            rows=[r for r in records.values() if qualifies(r,diet,excluded,flags)]
            source=("Live place search and city guide" if live and local else "Live place search" if live else "Sourced city guide")
            yield progress("find_places",f"{source}: {len(records)} named places found; {len(rows)} passed the filters.")
            if not rows:
                message=(f"I have no usable place data for {p.city} right now. The city guide covers "+", ".join(FALLBACK_CITIES)+".") if not records else "No venues have evidence for all of those constraints. Try another area, or edit a flexible preference."
                yield {"type":"empty","message":message,"preferences":p.model_dump()};return
            ranked=build_candidates(rows,p,tags,required,flags,origin)
            yield progress("build_candidates",f"Compared {len(ranked)} nearby outings against your interests and energy.")
            result=None
            rejects=Counter()
            route_calls=0
            for _,stops,points,legs in ranked:
                candidate,reason=validate_plan(stops,legs,p,total_minutes)
                if not candidate and reason!="available time":
                    rejects[reason]+=1;continue
                route_mode="Walking times are neighbourhood estimates."
                if have_geo and legs:
                    if route_calls>=3: continue
                    route_calls+=1
                    try: routed=await walk_route(client,points)
                    except (httpx.HTTPError,KeyError,IndexError,TypeError,ValueError): routed=None
                    if routed is None: rejects["route unavailable"]+=1;continue
                    candidate,reason=validate_plan(stops,routed,p,total_minutes)
                    route_mode="Walking times are route estimates from Geoapify."
                if not candidate: rejects[reason]+=1;continue
                result=candidate;break
            yield progress("estimate_route",("Checked walking time and included a five-minute allowance per transfer." if result and result["walk"] else "Checked whether the outing requires any transfers."))
            if not result:
                blockers=", ".join(rejects) or "required activities or walking limits"
                yield progress("validate_plan","No candidate passed all checks.")
                yield {"type":"empty","message":f"No outing fits these limits ({blockers}). Try changing your start time, budget, or duration, or choose another area.","preferences":p.model_dump()};return
            for stop in result["stops"]: stop["why"]=explain(stop,p,tags,diet)
            notes=[]
            if result["walk"]: notes.append(route_mode)
            if not p.starting_area: notes.append("The clock starts at the first stop. Travel from home and the return trip are extra.")
            elif p.starting_area: notes.append("Travel from your starting point is included. The return trip is extra.")
            notes.append("Spending is an estimate for the whole party. Optional purchases are extra.")
            if "crowds" in flags: notes.append("Crowd levels are unverified. Saturday afternoons can vary.")
            if any(not s["place"].get("hours") for s in result["stops"]): notes.append("Some Saturday hours are unconfirmed. Check the linked venue before leaving.")
            covered=set().union(*(s["place"]["tags"] for s in result["stops"]))
            missed=[t for t in tags if t not in covered]
            if missed: notes.append("This route leaves out "+", ".join(missed)+" to fit the available places, time and budget.")
            today=datetime.now(ZoneInfo("Asia/Kolkata")).date()
            sat=today+timedelta(days=(5-today.weekday())%7)
            yield progress("validate_plan",f"Verified {result['total']} minutes and an upper estimate of ₹{result['high']:,} against your limits.")
            yield dict(type="plan",**result,preferences=p.model_dump(),date=sat.isoformat(),source=source,notes=notes)
    except (ValidationError,ValueError,TypeError):
        yield {"type":"clarify","message":"Please check the input. Use a city, non-negative budget, duration such as ‘90 minutes’, and a start time in HH:MM format.","preferences":{}}
