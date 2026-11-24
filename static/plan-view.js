/** Render a validated plan using text nodes for visitor and provider content. */
const rupees = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  maximumFractionDigits: 0,
});
const money = (value) => rupees.format(value);
function priceRange(low, high, separator = "–") {
  return low === high ? money(low) : money(low) + separator + money(high);
}
function time(minutes) {
  if (minutes < 60) return minutes + " min";
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return remainder ? hours + "h " + remainder + "m" : hours + "h";
}
export const make = (tag, value, className) => {
  const element = document.createElement(tag);
  if (value !== undefined) element.textContent = value;
  if (className) element.className = className;
  return element;
};

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

export function showPlan(plan, result, editButton) {
  const preferences = plan.preferences;
  const date = new Date(plan.date + "T12:00:00+05:30").toLocaleDateString("en-IN", {
    weekday: "long",
    day: "numeric",
    month: "long",
    timeZone: "Asia/Kolkata",
  });
  document.getElementById("result-title").textContent =
    (plan.partial ? "An alternative in " : "Your Saturday in ") + preferences.city;
  if (plan.partial || plan.unmatched?.length) {
    const message =
      plan.partial && plan.notes[0].startsWith("Activities-only")
        ? plan.notes[0]
        : "Primary matches unavailable for this route: " + plan.unmatched.join(", ") + ".";
    result.append(make("p", message, "notes"));
  }
  const party = preferences.people === 1 ? "1 person" : preferences.people + " people";
  document.getElementById("status").textContent = [
    date,
    party,
    "Start at " + preferences.start_time + " IST",
    plan.source,
  ].join(" · ");
  result.append(renderMetrics(plan));
  for (const visit of plan.stops) result.append(renderStop(visit, preferences));
  const notes = make("ul", undefined, "notes");
  plan.notes.forEach((n) => notes.append(make("li", n)));
  result.append(notes, editButton);
  if (plan.source.includes("Live")) {
    const attribution = make("p", undefined, "minor links");
    attribution.append(
      link("Powered by Geoapify", "https://www.geoapify.com/"),
      link("© OpenStreetMap contributors", "https://www.openstreetmap.org/copyright"),
    );
    result.append(attribution);
  }
}

function renderMetrics(plan) {
  const metrics = make("div", undefined, "metrics");
  for (const [label, value] of [
    ["Estimated spend", priceRange(plan.low, plan.high, " – ")],
    ["Total time", time(plan.total)],
    ["Time to spare", time(plan.spare)],
  ]) {
    const item = make("div", undefined, "metric");
    item.append(make("small", label), make("strong", value));
    metrics.append(item);
  }
  return metrics;
}

/** Display one scheduled visit with its incoming transfer, costs, reason and sources. */
function renderStop(visit, preferences) {
  const stop = make("article", undefined, "stop");
  const when = make("div", undefined, "when");
  const body = make("div");
  when.append(make("span", visit.start + " – " + visit.end));
  const venue = visit.place;
  const leg = visit.leg;
  if (leg.minutes) {
    const fare =
      leg.mode === "auto"
        ? " · estimated group fare " + money(leg.low) + "–" + money(leg.high)
        : "";
    const transfer = `${leg.minutes} min ${leg.mode} + ${visit.buffer} min buffer${fare} · ${leg.source}`;
    body.append(make("p", transfer, "minor"));
  }
  if (visit.wait) body.append(make("p", visit.wait + " min wait until opening", "minor"));
  const cost = priceRange(venue.low * preferences.people, venue.high * preferences.people);
  body.append(
    make("h3", venue.name),
    make("p", `${venue.area} · ${cost} · ${venue.minutes} min`, "minor"),
    make("p", venue.note),
    make("p", visit.why),
  );
  if (venue.price_basis) body.append(make("p", venue.price_basis, "minor"));
  if (venue.crowd && preferences.constraints.some((c) => /crowd|quiet/i.test(c)))
    body.append(make("p", "Saturday crowd estimate: " + venue.crowd + " (assumption)", "minor"));
  const links = make("div", undefined, "minor links");
  if (venue.hours_source) links.append(link("Hours listing", venue.hours_source));
  links.append(
    link("Venue source", venue.source),
    link(
      "Map",
      "https://www.google.com/maps/search/?api=1&query=" +
        encodeURIComponent(venue.name + ", " + venue.city),
    ),
  );
  body.append(links);
  stop.append(when, body);
  return stop;
}
