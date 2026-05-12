const els = {
  dot: document.querySelector("#connection-dot"),
  lastUpdated: document.querySelector("#last-updated"),
  topbarCompact: document.querySelector("#topbar-compact"),
  timerMode: document.querySelector("#timer-mode"),
  timerCard: document.querySelector(".metric--timer"),
  workState: document.querySelector("#work-state"),
  workCard: document.querySelector(".metric--work"),
  fleetCount: document.querySelector("#fleet-count"),
  fleetCard: document.querySelector(".metric--fleet"),
  attentionState: document.querySelector("#attention-state"),
  attentionCard: document.querySelector(".metric--attention"),
  fleetMix: document.querySelector("#fleet-mix"),
  instanceBreakdown: document.querySelector("#instance-breakdown"),
  instances: document.querySelector("#instances"),
  selectionPanel: document.querySelector("#selection-panel"),
  selectionStatus: document.querySelector("#selection-status"),
  selectionTitle: document.querySelector("#selection-title"),
  selectionMeta: document.querySelector("#selection-meta"),
  selectionFields: document.querySelector("#selection-fields"),
  docCount: document.querySelector("#doc-count"),
  sessionDocs: document.querySelector("#session-docs"),
  events: document.querySelector("#events"),
};

const STALE_AGE_MIN = 60;

const state = {
  selectedIndex: 0,
  selectedId: null,
  publishedSelectionId: null,
  instances: [],
};

function text(value, fallback = "--") {
  if (value === null || value === undefined || value === "") return fallback;
  return String(value);
}

function minutesLabel(value) {
  if (value === null || value === undefined) return "—";
  if (value < 1) return "now";
  if (value === 1) return "1m";
  if (value < 90) return `${value}m`;
  return `${Math.round(value / 60)}h`;
}

function relativeUpdated(seconds) {
  if (seconds < 2) return "updated now";
  if (seconds < 60) return `updated ${seconds}s ago`;
  const m = Math.round(seconds / 60);
  return `updated ${m}m ago`;
}

function compactPath(path) {
  if (!path) return "";
  const parts = String(path).split("/");
  return parts.slice(-3).join("/");
}

function toneName(value) {
  return text(value, "unknown").toLowerCase().replace(/[^a-z0-9_-]/g, "-");
}

function deriveStatusTone(inst) {
  const status = (inst?.status || "unknown").toLowerCase();
  if (status === "processing") return "processing";
  if (status === "blocked") return "blocked";
  if (status === "stopped") return "stopped";
  if (status === "idle") {
    if ((inst.age_minutes ?? 0) >= STALE_AGE_MIN) return "stale";
    return "idle";
  }
  return "unknown";
}

function hasPane(inst) {
  return Boolean(inst?.pane_label || inst?.tmux_pane || inst?.dispatch_window);
}

function hasDoc(inst) {
  return Boolean(inst?.session_doc_title || inst?.session_doc_id);
}

function makeChip(label, tone = "") {
  const node = document.createElement("span");
  node.className = `chip ${tone ? `chip--${toneName(tone)}` : ""}`;
  node.textContent = label;
  return node;
}

function makeBadge(label, tone) {
  const node = document.createElement("span");
  node.className = `badge badge--${toneName(tone)}`;
  node.textContent = label;
  return node;
}

function makeField(label, value, fallback = "--") {
  const term = document.createElement("dt");
  const description = document.createElement("dd");
  term.textContent = label;
  description.textContent = text(value, fallback);
  return [term, description];
}

function formatTimestamp(value) {
  if (!value) return "--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function setConnection(ok, message) {
  els.dot.className = `dot ${ok ? "dot--ok" : "dot--error"}`;
  els.lastUpdated.textContent = message;
}

async function fetchJson(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) throw new Error(`${path} returned ${response.status}`);
  return response.json();
}

function renderMetrics(data) {
  const timer = data.timer || {};
  const work = data.work_state || {};
  const instances = data.instances || {};
  const active = instances.active || [];
  const processing = instances.status_counts?.processing || 0;
  const idle = instances.status_counts?.idle || 0;
  const backlogMinutes = Math.round((timer.break_backlog_ms || 0) / 60000);

  els.timerMode.textContent = text(timer.current_mode);
  const timerDetail = backlogMinutes > 0
    ? `${backlogMinutes}m backlog · ${text(timer.activity)} activity`
    : `${text(timer.activity)} activity · ${text(timer.work_mode)}`;
  els.timerCard.title = timerDetail;

  els.workState.textContent = work.productivity_active ? "productive" : "unverified";
  els.workCard.title = text(work.reason, "no reason given");

  els.fleetCount.textContent = `${active.length} live`;
  els.fleetCard.title = `${processing} processing · ${idle} idle`;

  const phoneApp = timer.phone_app;
  const attentionPrimary = timer.focus_active
    ? "focus"
    : phoneApp
      ? `phone:${phoneApp}`
      : text(timer.desktop_mode);
  els.attentionState.textContent = attentionPrimary;
  els.attentionCard.title = phoneApp
    ? `phone: ${phoneApp} · mode: ${text(timer.work_mode)}`
    : `mode: ${text(timer.work_mode)} · desktop: ${text(timer.desktop_mode)}`;

  els.topbarCompact.textContent = `${active.length} live · ${processing} proc`;
}

function renderFleetMix(instances) {
  els.fleetMix.replaceChildren();
  const engineEntries = Object.entries(instances?.engine_counts || {});
  const legionEntries = Object.entries(instances?.legion_counts || {});
  const entries = [...engineEntries.slice(0, 3), ...legionEntries.slice(0, 4)];

  if (!entries.length) {
    els.fleetMix.append(makeChip("no fleet"));
    return;
  }

  for (const [label, count] of entries) {
    els.fleetMix.append(makeChip(`${label} ${count}`, label));
  }
}

function renderBreakdown(counts) {
  els.instanceBreakdown.replaceChildren();
  const entries = Object.entries(counts || {});
  if (!entries.length) {
    els.instanceBreakdown.append(makeChip("clear"));
    return;
  }
  for (const [label, count] of entries) {
    els.instanceBreakdown.append(makeChip(`${label} ${count}`, label));
  }
}

function selectedInstance() {
  return state.instances[state.selectedIndex] || null;
}

function setSelected(index) {
  state.selectedIndex = Math.min(Math.max(index, 0), Math.max(state.instances.length - 1, 0));
  state.selectedId = selectedInstance()?.id || null;
}

function publishSelection() {
  if (state.selectedId === state.publishedSelectionId) return;
  state.publishedSelectionId = state.selectedId;
  fetch("/api/ui/somnium/selection", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      active_table: "instances",
      selected_instance_id: state.selectedId,
      selected_cron_job_id: null,
    }),
  }).catch(() => {
    state.publishedSelectionId = null;
  });
}

function renderSelection() {
  const inst = selectedInstance();
  els.selectionMeta.replaceChildren();
  els.selectionFields.replaceChildren();

  if (!inst) {
    els.selectionPanel.dataset.tone = "unknown";
    els.selectionStatus.textContent = "none";
    els.selectionTitle.textContent = "No instance selected";
    return;
  }

  const tone = deriveStatusTone(inst);
  const pane = inst.pane_label || inst.tmux_pane || inst.dispatch_window || "no pane";
  els.selectionPanel.dataset.tone = tone;
  els.selectionStatus.textContent = text(inst.status);
  els.selectionTitle.textContent = text(inst.display_name);

  els.selectionMeta.append(
    makeChip(text(inst.status), inst.status),
    makeChip(text(inst.engine, "claude"), inst.engine),
    makeChip(text(inst.legion || inst.instance_type, "unassigned"), inst.legion || inst.instance_type),
  );
  if (!hasPane(inst)) els.selectionMeta.append(makeBadge("no-pane", "red"));
  if (!hasDoc(inst)) els.selectionMeta.append(makeBadge("no-doc", "muted"));
  if (tone === "stale") els.selectionMeta.append(makeBadge("stale", "amber"));

  els.selectionFields.append(
    ...makeField("Pane", pane),
    ...makeField("Device", inst.device_id),
    ...makeField("Project", inst.session_doc_project),
    ...makeField("Doc", inst.session_doc_title || "No session doc"),
    ...makeField("Workflow", inst.workflow_state),
    ...makeField("Action", inst.next_required_action),
    ...makeField("Input", inst.input_lock ? "locked" : "open"),
    ...makeField("Age", minutesLabel(inst.age_minutes)),
    ...makeField("Path", compactPath(inst.working_dir || inst.session_doc_path)),
  );
}

function renderInstances(instances) {
  state.instances = instances;
  const selectedIndex = instances.findIndex((inst) => inst.id && inst.id === state.selectedId);
  if (selectedIndex >= 0) {
    state.selectedIndex = selectedIndex;
  } else if (state.selectedIndex >= instances.length) {
    state.selectedIndex = Math.max(0, instances.length - 1);
  }
  state.selectedId = selectedInstance()?.id || null;

  const savedScroll = els.instances.scrollTop;
  els.instances.replaceChildren();

  if (!instances.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "No live instances";
    els.instances.append(empty);
    renderSelection();
    return;
  }

  instances.forEach((inst, index) => {
    const tone = deriveStatusTone(inst);
    const noPane = !hasPane(inst);
    const noDoc = !hasDoc(inst);
    const isSelected = index === state.selectedIndex;

    const row = document.createElement("article");
    row.className = `instance ${isSelected ? "is-selected" : ""}`;
    row.dataset.index = String(index);
    row.dataset.status = tone;
    row.dataset.stale = String(tone === "stale");
    row.dataset.noPane = String(noPane);
    row.dataset.noDoc = String(noDoc);

    const rail = document.createElement("span");
    rail.className = "instance__rail";
    row.append(rail);

    const body = document.createElement("div");
    body.className = "instance__body";

    const titleLine = document.createElement("div");
    titleLine.className = "instance__title";
    const caret = document.createElement("span");
    caret.className = "instance__caret";
    caret.textContent = isSelected ? "▸ " : "";
    const name = document.createElement("strong");
    name.textContent = text(inst.display_name);
    const meta = document.createElement("div");
    meta.className = "instance__meta";
    meta.append(
      makeChip(text(inst.status), inst.status),
      makeChip(text(inst.engine, "claude"), inst.engine),
      makeChip(text(inst.legion || inst.instance_type, "unassigned"), inst.legion || inst.instance_type),
    );
    if (noPane) meta.append(makeBadge("no-pane", "red"));
    if (noDoc) meta.append(makeBadge("no-doc", "muted"));
    titleLine.append(caret, name, meta);

    const detail = document.createElement("p");
    detail.className = "instance__detail";
    const docLabel = inst.session_doc_title || "no doc";
    const paneLabel = inst.pane_label || inst.tmux_pane || inst.dispatch_window || "no pane";
    detail.textContent = `${docLabel} · ${paneLabel}`;

    const path = document.createElement("small");
    path.className = "instance__path";
    path.textContent = compactPath(inst.working_dir || inst.session_doc_path);

    body.append(titleLine, detail, path);

    const aside = document.createElement("div");
    aside.className = "instance__aside";
    const age = document.createElement("span");
    age.className = "instance__age";
    age.textContent = minutesLabel(inst.age_minutes);
    const device = document.createElement("span");
    device.className = "instance__device";
    device.textContent = text(inst.device_id, "");
    aside.append(age, device);

    row.append(body, aside);
    row.addEventListener("click", () => {
      setSelected(index);
      publishSelection();
      renderInstances(state.instances);
    });
    els.instances.append(row);
  });

  els.instances.scrollTop = savedScroll;
  publishSelection();
  renderSelection();
}

function docTone(doc) {
  const live = doc.live_instances || 0;
  if (live > 0) return "hot";
  if ((doc.age_minutes ?? 0) >= STALE_AGE_MIN) return "stale";
  return "cold";
}

function renderSessionDocs(lanes) {
  els.sessionDocs.replaceChildren();
  const laneNames = ["active", "held", "completed", "deployment"];
  let total = 0;

  for (const laneName of laneNames) {
    const docs = lanes?.[laneName] || [];
    total += docs.length;
    const hot = docs.filter((d) => (d.live_instances || 0) > 0).length;

    const lane = document.createElement("section");
    lane.className = "lane";

    const heading = document.createElement("h3");
    heading.textContent = `${laneName} ${docs.length}${hot > 0 ? ` · ${hot} hot` : ""}`;
    lane.append(heading);

    for (const doc of docs.slice(0, 8)) {
      const item = document.createElement("article");
      item.className = "doc";
      item.dataset.tone = docTone(doc);

      const dot = document.createElement("span");
      dot.className = `doc__dot doc__dot--${docTone(doc)}`;
      const title = document.createElement("strong");
      title.textContent = text(doc.title, `Session ${doc.id}`);
      const detail = document.createElement("span");
      detail.className = "doc__detail";
      detail.textContent = `${text(doc.project, "no project")} · ${doc.live_instances || 0} live · ${minutesLabel(doc.age_minutes)}`;

      const head = document.createElement("div");
      head.className = "doc__head";
      head.append(dot, title);
      item.append(head, detail);
      lane.append(item);
    }

    els.sessionDocs.append(lane);
  }

  els.docCount.textContent = String(total);
}

function renderEvents(events) {
  els.events.replaceChildren();
  for (const event of (events || []).slice(0, 12)) {
    const row = document.createElement("p");
    row.className = "event";
    const shortId = text(event.instance_id, "system").slice(0, 12);
    row.textContent = `${text(event.event_type)} · ${shortId} · ${formatTimestamp(event.created_at)}`;
    els.events.append(row);
  }
}

async function refresh() {
  const data = await fetchJson("/api/ui/somnium/state");
  renderMetrics(data);
  renderFleetMix(data.instances);
  renderBreakdown(data.instances?.status_counts);
  renderInstances(data.instances?.active || []);
  renderSessionDocs(data.session_docs);
  renderEvents(data.events);
  const generated = new Date(data.generated_at);
  const age = Math.max(0, Math.round((Date.now() - generated.getTime()) / 1000));
  setConnection(true, relativeUpdated(age));
}

document.addEventListener("keydown", (event) => {
  if (!state.instances.length) return;
  if (event.key === "j" || event.key === "ArrowDown") {
    setSelected(state.selectedIndex + 1);
    publishSelection();
    renderInstances(state.instances);
  }
  if (event.key === "k" || event.key === "ArrowUp") {
    setSelected(state.selectedIndex - 1);
    publishSelection();
    renderInstances(state.instances);
  }
});

refresh().catch((error) => setConnection(false, error.message));
setInterval(() => {
  refresh().catch((error) => setConnection(false, error.message));
}, 2000);
