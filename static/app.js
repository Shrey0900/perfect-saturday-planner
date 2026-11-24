"use strict";
const byId = (id) => document.getElementById(id);
const brief = byId("brief"), briefForm = byId("brief-form"), fieldsForm = byId("fields-form");
const output = byId("output"), result = byId("results"), trace = byId("trace-list");
const examples = {
  slow: "I'm in Bangalore with ₹2,000 and 4 hours. I'm tired but want to get out. I like food, music and walks. I'm vegetarian and want to avoid crowded places.",
  budget: "I'm in Bengaluru. I have ₹300 and 90 minutes for books and vegetarian food. Let's take it easy.",
  art: "I'm in Gurugram with ₹1,500 and 3 hours, starting at 1 pm. I'd love photography, art and lunch. I eat vegetarian food."
};
const cities = {Bengaluru: examples.slow, Gurugram: examples.art, Delhi:"I have ₹700 and 3 hours in Delhi. I like heritage walks, books and vegetarian food."};
const money = (n) => new Intl.NumberFormat("en-IN",{style:"currency",currency:"INR",maximumFractionDigits:0}).format(n);
const time = (n) => n < 60 ? n+" min" : Math.floor(n/60)+"h"+(n%60 ? " "+n%60+"m":"");
const make = (tag, value, cls) => {const el=document.createElement(tag); if(value!==undefined) el.textContent=value; if(cls) el.className=cls; return el;};
let lastPreferences = null;
let busy=false;
function formMode(on) {briefForm.hidden=on; fieldsForm.hidden=!on;}
function fillForm(p={}) {
  for(const field of fieldsForm.elements) {
    if(!field.name) continue;
    let v=p[field.name];
    field.value=Array.isArray(v)?v.join(", "):(v??(field.name==="people"?1:field.name==="start_time"?"10:00":""));
  }
}
function edit(p) {
  fillForm(p);formMode(true);byId("planner").scrollIntoView();fieldsForm.elements.city.focus({preventScroll:true});
}
function sample(text) {
  formMode(false);brief.value=text;lastPreferences=null;brief.focus();byId("planner").scrollIntoView();
}
document.querySelectorAll("[data-sample]").forEach(b=>b.onclick=()=>sample(examples[b.dataset.sample]));
document.querySelectorAll("[data-city]").forEach(b=>b.onclick=()=>sample(cities[b.dataset.city]));
byId("toggle-fields").onclick=()=>edit(lastPreferences||{});
byId("toggle-brief").onclick=()=>{formMode(false);brief.focus();};
function action(p,label="Edit my details") {
  const b=make("button",label,"text-button");b.type="button";b.onclick=()=>edit(p);return b;
}
function link(text, url) {
  const a=make("a",text);
  try { const u=new URL(url);if(!["http:","https:"].includes(u.protocol)) return make("span",text);a.href=u.href; }
  catch {return make("span",text);}
  a.target="_blank";a.rel="noopener noreferrer";return a;
}
function showPlan(e) {
  const p=e.preferences;const date=new Date(e.date+"T12:00:00+05:30").toLocaleDateString("en-IN",{weekday:"long",day:"numeric",month:"long",timeZone:"Asia/Kolkata"});
  byId("result-title").textContent="Your Saturday in "+p.city;
  byId("status").textContent=date+" · "+p.people+(p.people===1?" person":" people")+" · Start at "+p.start_time+" IST · "+e.source;
  const metrics=make("div",undefined,"metrics");
  for(const [label,value] of [["Estimated spend",money(e.low)+(e.high===e.low?"":" – "+money(e.high))],["Total time",time(e.total)],["Time to spare",time(e.spare)]]) {
    const item=make("div",undefined,"metric");item.append(make("small",label),make("strong",value));metrics.append(item);
  }
  result.append(metrics);
  for(const s of e.stops) {
    const stop=make("article",undefined,"stop"), left=make("div",undefined,"when"),body=make("div");
    left.append(make("span",s.start+" – "+s.end));
    const p=s.place;
    if(s.walk) body.append(make("p",s.walk+" min walk + "+s.buffer+" min buffer before this stop","minor"));
    body.append(make("h3",p.name),make("p",p.area+" · "+money(p.low*e.preferences.people)+(p.high!==p.low?"–"+money(p.high*e.preferences.people):"")+" · "+p.minutes+" min","minor"),make("p",p.note),make("p",s.why));
    const links=make("div",undefined,"minor links");
    links.append(link("Venue source",p.source),link("Map","https://www.google.com/maps/search/?api=1&query="+encodeURIComponent(p.name+", "+p.city)));
    body.append(links);stop.append(left,body);result.append(stop);
  }
  const notes=make("ul",undefined,"notes");
  e.notes.forEach(n=>notes.append(make("li",n)));result.append(notes,action(p,"Adjust this plan"));
  if(e.source.includes("Live")) {
    const attribution=make("p",undefined,"minor links");
    attribution.append(link("Powered by Geoapify","https://www.geoapify.com/"),link("© OpenStreetMap contributors","https://www.openstreetmap.org/copyright"));result.append(attribution);
  }
}
function eventReceived(e) {
  if(e.type==="progress") {
    const item=make("li",e.message);trace.append(item);byId("status").textContent=e.message;return;
  }
  lastPreferences=e.preferences||null;
  if(e.type==="plan") {showPlan(e);return;}
  byId("result-title").textContent=e.type==="clarify"?"A couple of details first":"Let’s adjust the outing";
  byId("status").textContent=e.message;result.append(action(lastPreferences||{}));
  if(e.type==="clarify") {fillForm(lastPreferences||{});formMode(true);}
}
async function submit(value) {
  if(busy) return;
  busy=true;document.querySelectorAll('button[type="submit"]').forEach(b=>b.disabled=true);
  output.hidden=false;output.focus({preventScroll:true});output.scrollIntoView();
  result.replaceChildren();trace.replaceChildren();byId("trace").open=true;byId("result-title").textContent="Planning your Saturday";byId("status").textContent="Reading your preferences…";
  try {
    const response=await fetch("/api/plan",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({input:value})});
    if(!response.ok) {const error=await response.json();throw new Error(error.error||"The server is busy. Please try again.");}
    const reader=response.body.getReader(),decoder=new TextDecoder();let buffer="",finished=false;
    function receive(line) {if(!line.trim()) return;const e=JSON.parse(line);eventReceived(e);if(e.type!=="progress") finished=true;}
    while(true) {
      const chunk=await reader.read();
      if(chunk.done) break;
      buffer+=decoder.decode(chunk.value,{stream:true});const lines=buffer.split("\n");buffer=lines.pop();for(const line of lines) receive(line);
    }
    receive(buffer+decoder.decode());
    if(!finished) throw new Error("The connection ended early. Please try again.");
  } catch(e) {byId("result-title").textContent="Couldn’t finish the plan";byId("status").textContent=e.message;result.append(action(lastPreferences||{}));}
  finally {busy=false;document.querySelectorAll('button[type="submit"]').forEach(b=>b.disabled=false);}
}
briefForm.addEventListener("submit",e=>{e.preventDefault();submit(brief.value.trim());});
fieldsForm.addEventListener("submit",e=>{
  e.preventDefault();const p=Object.fromEntries(new FormData(fieldsForm));
  p.people=Number(p.people);p.budget=Number(p.budget);
  for(const key of ["constraints","interests"]) p[key]=p[key].split(",").map(s=>s.trim()).filter(Boolean);
  submit(p);
});
brief.value=examples.slow;
