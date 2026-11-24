"use strict";
const byId = (id) => document.getElementById(id);
const brief = byId("brief"),
    briefForm = byId("brief-form"),
    fieldsForm = byId("fields-form");
const output = byId("output"),
    result = byId("results"),
    trace = byId("trace-list");
const examples = {
    slow: "I'm in Bangalore with ₹2,000 and 4 hours. I'm tired but want to get out. I like food, music and walks. I'm vegetarian and want to avoid crowded places.",
    budget: "I'm in Bengaluru. I have ₹300 and 90 minutes for books and vegetarian food. Let's take it easy.",
    art: "I'm in Gurugram with ₹1,500 and 3 hours, starting at 1 pm. I'd love photography, art and lunch. I eat vegetarian food."
};
const cities = {
    Bengaluru: examples.slow,
    Gurugram: examples.art,
    Delhi: "I have ₹700 and 3 hours in Delhi. I like heritage walks, books and vegetarian food."
};
const money = (n) => new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 0
}).format(n);
const time = (n) => n < 60 ? n + " min" : Math.floor(n / 60) + "h" + (n % 60 ? " " + n % 60 + "m" : "");
const make = (tag, value, cls) => {
    const el = document.createElement(tag);
    if (value !== undefined) el.textContent = value;
    if (cls) el.className = cls;
    return el;
};
let lastPreferences = null;
let activeRequest = null;

function clearPreviousPlan() {
    activeRequest?.abort();
    activeRequest = null;
    lastPreferences = null;
    output.hidden = true;
    result.replaceChildren();
    trace.replaceChildren();
    document.querySelectorAll('button[type="submit"]').forEach(b => b.disabled = false);
}
briefForm.addEventListener("input", clearPreviousPlan);
fieldsForm.addEventListener("input", clearPreviousPlan);

function formMode(on) {
    briefForm.hidden = on;
    fieldsForm.hidden = !on;
}

function fillForm(p = {}) {
    for (const field of fieldsForm.elements) {
        if (!field.name) continue;
        let v = p[field.name];
        field.value = Array.isArray(v) ? v.join(", ") : (v ?? (field.name === "people" ? 1 : field.name === "start_time" ? "10:00" : ""));
    }
}

function edit(p) {
    clearPreviousPlan();
    fillForm(p);
    formMode(true);
    byId("planner").scrollIntoView();
    fieldsForm.elements.city.focus({
        preventScroll: true
    });
}

function sample(text) {
    clearPreviousPlan();
    formMode(false);
    brief.value = text;
    brief.focus();
    byId("planner").scrollIntoView();
}
document.querySelectorAll("[data-sample]").forEach(b => b.onclick = () => sample(examples[b.dataset.sample]));
document.querySelectorAll("[data-city]").forEach(b => b.onclick = () => sample(cities[b.dataset.city]));
byId("toggle-fields").onclick = () => edit(lastPreferences || {});
byId("toggle-brief").onclick = () => {
    clearPreviousPlan();
    formMode(false);
    brief.focus();
};

function action(p, label = "Edit my details") {
    const b = make("button", label, "text-button");
    b.type = "button";
    b.onclick = () => edit(p);
    return b;
}

function link(text, url) {
    const a = make("a", text);
    try {
        const u = new URL(url);
        if (!["http:", "https:"].includes(u.protocol)) return make("span", text);
        a.href = u.href;
    } catch {
        return make("span", text);
    }
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    return a;
}

function showPlan(plan) {
    const preferences = plan.preferences;
    const date = new Date(plan.date + "T12:00:00+05:30").toLocaleDateString("en-IN", {
        weekday: "long",
        day: "numeric",
        month: "long",
        timeZone: "Asia/Kolkata"
    });
    byId("result-title").textContent = (plan.partial ? "An alternative in " : "Your Saturday in ") + preferences.city;
    if (plan.partial || plan.unmatched?.length) {
        const message = plan.partial && plan.notes[0].startsWith("Activities-only") ? plan.notes[0] : "Primary matches unavailable for this route: " + plan.unmatched.join(", ") + ".";
        result.append(make("p", message, "notes"));
    }
    byId("status").textContent = date + " · " + preferences.people + (preferences.people === 1 ? " person" : " people") + " · Start at " + preferences.start_time + " IST · " + plan.source;
    const metrics = make("div", undefined, "metrics");
    for (const [label, value] of [
            ["Estimated spend", money(plan.low) + (plan.high === plan.low ? "" : " – " + money(plan.high))],
            ["Total time", time(plan.total)],
            ["Time to spare", time(plan.spare)]
        ]) {
        const item = make("div", undefined, "metric");
        item.append(make("small", label), make("strong", value));
        metrics.append(item);
    }
    result.append(metrics);
    for (const visit of plan.stops) {
        const stop = make("article", undefined, "stop"),
            left = make("div", undefined, "when"),
            body = make("div");
        left.append(make("span", visit.start + " – " + visit.end));
        const venue = visit.place;
        const leg = visit.leg;
        if (leg.minutes) {
            const fare = leg.mode === "auto" ? " · estimated group fare " + money(leg.low) + "–" + money(leg.high) : "";
            body.append(make("p", leg.minutes + " min " + leg.mode + " + " + visit.buffer + " min buffer" + fare + " · " + leg.source, "minor"));
        }
        if (visit.wait) body.append(make("p", visit.wait + " min wait until opening", "minor"));
        body.append(make("h3", venue.name), make("p", venue.area + " · " + money(venue.low * plan.preferences.people) + (venue.high !== venue.low ? "–" + money(venue.high * plan.preferences.people) : "") + " · " + venue.minutes + " min", "minor"), make("p", venue.note), make("p", visit.why));
        if (venue.price_basis) body.append(make("p", venue.price_basis, "minor"));
        if (venue.crowd && plan.preferences.constraints.some(c => /crowd|quiet/i.test(c))) body.append(make("p", "Saturday crowd estimate: " + venue.crowd + " (assumption)", "minor"));
        const links = make("div", undefined, "minor links");
        if (venue.hours_source) links.append(link("Hours listing", venue.hours_source));
        links.append(link("Venue source", venue.source), link("Map", "https://www.google.com/maps/search/?api=1&query=" + encodeURIComponent(venue.name + ", " + venue.city)));
        body.append(links);
        stop.append(left, body);
        result.append(stop);
    }
    const notes = make("ul", undefined, "notes");
    plan.notes.forEach(n => notes.append(make("li", n)));
    result.append(notes, action(preferences, "Adjust this plan"));
    if (plan.source.includes("Live")) {
        const attribution = make("p", undefined, "minor links");
        attribution.append(link("Powered by Geoapify", "https://www.geoapify.com/"), link("© OpenStreetMap contributors", "https://www.openstreetmap.org/copyright"));
        result.append(attribution);
    }
}

function eventReceived(event) {
    if (event.type === "progress") {
        const item = make("li", event.message);
        const details = make("details");
        details.append(make("summary", event.tool + " · inputs and outputs"), make("pre", JSON.stringify({
            inputs: event.inputs,
            outputs: event.outputs
        }, null, 2)));
        item.append(details);
        trace.append(item);
        byId("status").textContent = event.message;
        return;
    }
    lastPreferences = event.preferences || null;
    if (event.type === "plan") {
        showPlan(event);
        return;
    }
    byId("result-title").textContent = event.type === "clarify" ? "A couple of details first" : "Let’s adjust the outing";
    byId("status").textContent = event.message;
    result.append(action(lastPreferences || {}));
    if (event.type === "clarify") {
        fillForm(lastPreferences || {});
        formMode(true);
    }
}
async function submit(value) {
    if (activeRequest) return;
    const request = new AbortController();
    activeRequest = request;
    document.querySelectorAll('button[type="submit"]').forEach(b => b.disabled = true);
    output.hidden = false;
    output.focus({
        preventScroll: true
    });
    output.scrollIntoView();
    result.replaceChildren();
    trace.replaceChildren();
    byId("trace").open = false;
    byId("result-title").textContent = "Planning your Saturday";
    byId("status").textContent = "Reading your preferences…";
    try {
        const response = await fetch("/api/plan", {
            method: "POST",
            signal: request.signal,
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                input: value
            })
        });
        const contentType = response.headers.get("content-type") || "";
        if (!response.ok) {
            let message = "The planner is temporarily unavailable. Please retry in a moment.";
            if (contentType.includes("application/json")) {
                const problem = await response.json().catch(() => null);
                if (problem?.error) message = problem.error;
            }
            throw new Error(message);
        }
        if (!contentType.includes("application/x-ndjson")) throw new Error("The planner is waking up. Please try again in a moment.");
        const reader = response.body.getReader(),
            decoder = new TextDecoder();
        let buffer = "",
            finished = false;

        function receive(line) {
            if (request.signal.aborted || !line.trim()) return;
            const e = JSON.parse(line);
            eventReceived(e);
            if (e.type !== "progress") finished = true;
        }
        while (true) {
            const chunk = await reader.read();
            if (chunk.done) break;
            buffer += decoder.decode(chunk.value, {
                stream: true
            });
            const lines = buffer.split("\n");
            buffer = lines.pop();
            for (const line of lines) receive(line);
        }
        receive(buffer + decoder.decode());
        if (!finished) throw new Error("The connection ended early. Please try again.");
    } catch (e) {
        if (request.signal.aborted) return;
        byId("result-title").textContent = "Couldn’t finish the plan";
        byId("status").textContent = e instanceof SyntaxError ? "The planner returned an incomplete response. Please try again." : e.message;
        result.append(action(lastPreferences || {}));
    } finally {
        if (activeRequest === request) {
            activeRequest = null;
            document.querySelectorAll('button[type="submit"]').forEach(b => b.disabled = false);
        }
    }
}
briefForm.addEventListener("submit", e => {
    e.preventDefault();
    submit(brief.value.trim());
});
fieldsForm.addEventListener("submit", e => {
    e.preventDefault();
    const p = Object.fromEntries(new FormData(fieldsForm));
    p.people = Number(p.people);
    p.budget = Number(p.budget);
    for (const key of ["constraints", "interests"]) p[key] = p[key].split(",").map(s => s.trim()).filter(Boolean);
    submit(p);
});
brief.value = examples.slow;
