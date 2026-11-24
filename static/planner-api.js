/** POST a brief and deliver each complete trace or result event to the UI. */
export async function requestPlan(value, signal, onEvent) {
  const response = await fetch("/api/plan", {
    method: "POST",
    signal,
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      input: value,
    }),
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
  if (!contentType.includes("application/x-ndjson"))
    throw new Error("The planner is waking up. Please try again in a moment.");
  await readPlanStream(response.body, signal, onEvent);
}

/** Buffer partial lines because a network chunk can contain half an event or several events. */
async function readPlanStream(body, signal, onEvent) {
  const reader = body.getReader(),
    decoder = new TextDecoder();
  let buffer = "",
    finished = false;

  function receive(line) {
    if (signal.aborted || !line.trim()) return;
    const event = JSON.parse(line);
    onEvent(event);
    if (event.type !== "progress") finished = true;
  }
  while (true) {
    const chunk = await reader.read();
    if (chunk.done) break;
    buffer += decoder.decode(chunk.value, {
      stream: true,
    });
    const lines = buffer.split("\n");
    buffer = lines.pop();
    for (const line of lines) receive(line);
  }
  receive(buffer + decoder.decode());
  if (!finished) throw new Error("The connection ended early. Please try again.");
}
