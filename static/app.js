/** Bind the two input forms and route streamed events to the itinerary or the trace. */
import { requestPlan } from "./planner-api.js";
import { make, showPlan } from "./plan-view.js";
const byId = (id) => document.getElementById(id);
const brief = byId("brief"),
  briefForm = byId("brief-form"),
  fieldsForm = byId("fields-form");
const output = byId("output"),
  result = byId("results"),
  trace = byId("trace-list");
const examples = {
  slow: "I'm in Bangalore with ₹2,000 and 4 hours. I'm tired but want to get out. I like food, music and walks. I'm vegetarian and want to avoid crowded places.",
  budget:
    "I'm in Bengaluru. I have ₹300 and 90 minutes for books and vegetarian food. Let's take it easy.",
  art: "I'm in Gurugram with ₹1,500 and 3 hours, starting at 1 pm. I'd love photography, art and lunch. I eat vegetarian food.",
};
const cities = {
  Bengaluru: examples.slow,
  Gurugram: examples.art,
  Delhi: "I have ₹700 and 3 hours in Delhi. I like heritage walks, books and vegetarian food.",
};
let lastPreferences = null;
let activeRequest = null;

function setSubmitting(disabled) {
  document.querySelectorAll('button[type="submit"]').forEach((button) => {
    button.disabled = disabled;
  });
}

/** Cancel an in-flight request when its brief changes to avoid painting a stale result. */
function clearPreviousPlan() {
  activeRequest?.abort();
  activeRequest = null;
  lastPreferences = null;
  output.hidden = true;
  result.replaceChildren();
  trace.replaceChildren();
  setSubmitting(false);
}
briefForm.addEventListener("input", clearPreviousPlan);
fieldsForm.addEventListener("input", clearPreviousPlan);

function showFields(useFields) {
  briefForm.hidden = useFields;
  fieldsForm.hidden = !useFields;
}

function fillForm(preferences = {}) {
  const defaults = { people: 1, start_time: "10:00" };
  for (const field of fieldsForm.elements) {
    if (!field.name) continue;
    const value = preferences[field.name] ?? defaults[field.name] ?? "";
    field.value = Array.isArray(value) ? value.join(", ") : value;
  }
}

function edit(p) {
  clearPreviousPlan();
  fillForm(p);
  showFields(true);
  byId("planner").scrollIntoView();
  fieldsForm.elements.city.focus({
    preventScroll: true,
  });
}

function sample(text) {
  clearPreviousPlan();
  showFields(false);
  brief.value = text;
  brief.focus();
  byId("planner").scrollIntoView();
}
document
  .querySelectorAll("[data-sample]")
  .forEach((b) => (b.onclick = () => sample(examples[b.dataset.sample])));
document
  .querySelectorAll("[data-city]")
  .forEach((b) => (b.onclick = () => sample(cities[b.dataset.city])));
byId("toggle-fields").onclick = () => edit(lastPreferences || {});
byId("toggle-brief").onclick = () => {
  clearPreviousPlan();
  showFields(false);
  brief.focus();
};

function action(p, label = "Edit my details") {
  const b = make("button", label, "text-button");
  b.type = "button";
  b.onclick = () => edit(p);
  return b;
}

function eventReceived(event) {
  if (event.type === "progress") {
    const item = make("li", event.message);
    const details = make("details");
    details.append(
      make("summary", event.tool + " · inputs and outputs"),
      make(
        "pre",
        JSON.stringify(
          {
            inputs: event.inputs,
            outputs: event.outputs,
          },
          null,
          2,
        ),
      ),
    );
    item.append(details);
    trace.append(item);
    byId("status").textContent = event.message;
    return;
  }
  lastPreferences = event.preferences || null;
  if (event.type === "plan") {
    showPlan(event, result, action(event.preferences, "Adjust this plan"));
    return;
  }
  byId("result-title").textContent =
    event.type === "clarify" ? "A couple of details first" : "Let’s adjust the outing";
  byId("status").textContent = event.message;
  result.append(action(lastPreferences || {}));
  if (event.type === "clarify") {
    fillForm(lastPreferences || {});
    showFields(true);
  }
}
async function submit(value) {
  if (activeRequest) return;
  const request = new AbortController();
  activeRequest = request;
  setSubmitting(true);
  output.hidden = false;
  output.focus({
    preventScroll: true,
  });
  output.scrollIntoView();
  result.replaceChildren();
  trace.replaceChildren();
  byId("trace").open = false;
  byId("result-title").textContent = "Planning your Saturday";
  byId("status").textContent = "Reading your preferences…";
  try {
    await requestPlan(value, request.signal, eventReceived);
  } catch (e) {
    if (request.signal.aborted) return;
    byId("result-title").textContent = "Couldn’t finish the plan";
    byId("status").textContent =
      e instanceof SyntaxError
        ? "The planner returned an incomplete response. Please try again."
        : e.message;
    result.append(action(lastPreferences || {}));
  } finally {
    if (activeRequest === request) {
      activeRequest = null;
      setSubmitting(false);
    }
  }
}
briefForm.addEventListener("submit", (e) => {
  e.preventDefault();
  submit(brief.value.trim());
});
fieldsForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const p = Object.fromEntries(new FormData(fieldsForm));
  p.people = Number(p.people);
  p.budget = Number(p.budget);
  for (const key of ["constraints", "interests"])
    p[key] = p[key]
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
  submit(p);
});
brief.value = examples.slow;
