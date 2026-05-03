const statusEl = document.querySelector("#status");
const timerEl = document.querySelector("#timer");
const fleetEl = document.querySelector("#fleet");
const workEl = document.querySelector("#work");

function renderDefinitionList(node, entries) {
  node.replaceChildren();
  for (const [label, value] of entries) {
    const term = document.createElement("dt");
    const description = document.createElement("dd");
    term.textContent = label;
    description.textContent = value == null || value === "" ? "unknown" : String(value);
    node.append(term, description);
  }
}

async function fetchJson(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`${path} returned ${response.status}`);
  }
  return response.json();
}

async function refresh() {
  const [state, timer, work] = await Promise.all([
    fetchJson("/api/state"),
    fetchJson("/api/timer"),
    fetchJson("/api/work-state"),
  ]);

  renderDefinitionList(timerEl, [
    ["Mode", timer.current_mode],
    ["Activity", timer.activity],
    ["Break balance", `${Math.round((timer.break_balance_ms || 0) / 60000)} min`],
    ["Focus", timer.focus_active ? "active" : "inactive"],
  ]);

  renderDefinitionList(fleetEl, [
    ["Active instances", state.active_instances],
    ["Processing", state.processing_count],
    ["Work mode", state.work_mode],
    ["Location", state.location],
  ]);

  renderDefinitionList(workEl, [
    ["Productive", work.productivity_active ? "yes" : "no"],
    ["Reason", work.reason],
    ["Observed agents", work.observed_agent_count],
    ["Desktop", work.desktop_mode],
  ]);

  statusEl.textContent = `Updated ${new Date().toLocaleTimeString()}`;
}

refresh().catch((error) => {
  statusEl.textContent = error.message;
});

setInterval(() => {
  refresh().catch((error) => {
    statusEl.textContent = error.message;
  });
}, 2000);
