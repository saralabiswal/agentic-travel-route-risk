const state = {
  incidents: [],
  selected: null,
  selectedTrip: null,
  preview: null,
  refreshedAt: null,
};

const $ = (selector) => document.querySelector(selector);
const API_BASE_URL = window.ROUTESHIELD_CONFIG?.apiBaseUrl || "";
const AIRPORT_TIME_ZONES = {
  SFO: "America/Los_Angeles", DEN: "America/Denver", LAX: "America/Los_Angeles",
  JFK: "America/New_York", ORD: "America/Chicago", DFW: "America/Chicago",
  SEA: "America/Los_Angeles", ATL: "America/New_York", IAD: "America/New_York",
  LHR: "Europe/London", CDG: "Europe/Paris", FRA: "Europe/Berlin", NRT: "Asia/Tokyo",
};

function element(tag, { className, text, attributes = {} } = {}) {
  // Console data can contain provider text. Build DOM nodes with textContent rather than injecting
  // HTML so a provider response cannot become executable markup in the manager's browser.
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  Object.entries(attributes).forEach(([name, value]) => node.setAttribute(name, value));
  return node;
}

function replaceChildren(target, children) {
  target.replaceChildren(...children);
}

function titleCase(value) {
  return String(value || "unknown").replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatTime(value, airport) {
  if (!value) return "Time unavailable";
  const timeZone = AIRPORT_TIME_ZONES[airport] || "UTC";
  return new Intl.DateTimeFormat(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZone,
    timeZoneName: "short",
  }).format(new Date(value));
}

function visibleIncidents() {
  const severity = $("#severity-filter").value;
  const status = $("#status-filter").value;
  return state.incidents.filter((incident) => (
    (severity === "all" || incident.severity === severity)
    && (status === "all" || incident.status === status)
  ));
}

function updateWorkspaceSummary() {
  const tenant = $("#tenant-id").value.trim() || "No tenant";
  $("#workspace-summary").textContent = `Workspace: ${tenant} · ${titleCase($("#role").value)}`;
}

function setConnection(message, isError = false) {
  const target = $("#connection-status");
  target.textContent = message;
  target.classList.toggle("error", isError);
}

function renderMetrics() {
  $("#metric-open").textContent = String(state.incidents.length);
  $("#metric-high").textContent = String(
    state.incidents.filter((incident) => ["high", "critical"].includes(incident.severity)).length,
  );
  $("#metric-freshness").textContent = state.refreshedAt ? formatTime(state.refreshedAt) : "Not yet";
}

function contextHeaders(extra = {}) {
  // Local mode uses development headers. In deployed mode the API validates the bearer token and
  // derives these values from its claims; this console never stores a token outside this form.
  const headers = {
    "X-Tenant-Id": $("#tenant-id").value.trim(),
    "X-Actor-Id": $("#actor-id").value.trim(),
    "X-Actor-Role": $("#role").value,
    ...extra,
  };
  const token = $("#bearer-token").value.trim();
  if (token) headers.Authorization = `Bearer ${token}`;
  return headers;
}

function connectionError() {
  const location = API_BASE_URL || "this console's API";
  return `Cannot reach ${location}. Confirm the local API is running, then refresh the queue.`;
}

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      ...options,
      headers: contextHeaders(options.headers),
    });
  } catch {
    throw new Error(connectionError());
  }
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `Request failed (${response.status})`);
  }
  return response.status === 204 ? null : response.json();
}

function notice(message, error = false) {
  const target = $("#notice");
  target.textContent = message;
  target.classList.toggle("error", error);
}

function incidentCard(incident) {
  const button = element("button", {
    className: `incident-card ${state.selected?.incident_id === incident.incident_id ? "selected" : ""}`,
    attributes: { type: "button", "aria-pressed": String(state.selected?.incident_id === incident.incident_id) },
  });
  const heading = element("div", { className: "incident-card-heading" });
  heading.append(
    element("span", { className: `severity-dot ${incident.severity}`, attributes: { "aria-hidden": "true" } }),
    element("strong", { text: `${titleCase(incident.severity)} risk` }),
    element("span", { className: "status-chip", text: titleCase(incident.status) }),
  );
  button.append(
    heading,
    element("span", { className: "incident-card-time", text: `Created ${formatTime(incident.created_at)}` }),
    element("span", { className: "incident-card-action", text: "Open decision workspace →" }),
  );
  button.addEventListener("click", () => selectIncident(incident.incident_id));
  return button;
}

function renderQueue() {
  const incidents = visibleIncidents();
  $("#queue-count").textContent = String(incidents.length);
  const root = $("#incident-queue");
  if (!incidents.length) {
    const empty = element("div", { className: "queue-empty" });
    empty.append(
      element("strong", { text: state.incidents.length ? "No matching incidents" : "Your queue is clear" }),
      element("p", { text: state.incidents.length ? "Try a different severity or status filter." : "New elevated risks will appear here for review." }),
    );
    replaceChildren(root, [empty]);
    return;
  }
  replaceChildren(root, incidents.map(incidentCard));
}

async function loadQueue() {
  notice("Refreshing your priority queue…");
  setConnection("Connecting to the API");
  try {
    state.incidents = await api("/v1/incidents");
    state.refreshedAt = new Date().toISOString();
    updateWorkspaceSummary();
    renderMetrics();
    renderQueue();
    setConnection("API connected");
    notice(
      state.incidents.length
        ? `${state.incidents.length} incident(s) ready for review. Start with the highest priority item.`
        : "Your queue is clear. Refresh when a new disruption is reported.",
    );
  } catch (error) {
    setConnection("API connection needed", true);
    notice(error.message, true);
  }
}

function definition(label, value) {
  const fragment = document.createDocumentFragment();
  fragment.append(element("dt", { text: label }), element("dd", { text: value }));
  return fragment;
}

function renderDetail() {
  const incident = state.selected;
  if (!incident) return;
  $("#incident-status").textContent = titleCase(incident.status);
  $("#incident-status").className = `badge ${incident.severity}`;

  const root = $("#incident-detail");
  const summary = element("div", { className: "risk-summary" });
  const severity = element("div", { className: "severity-summary" });
  severity.append(
    element("span", { className: `severity-dot large ${incident.severity}`, attributes: { "aria-hidden": "true" } }),
    element("div", { text: `${titleCase(incident.severity)} disruption risk` }),
  );
  const recommendation = incident.recommendation;
  summary.append(
    severity,
    element("p", {
      text: recommendation?.severity_explanation || "This incident needs a manager review.",
    }),
  );

  const facts = element("dl", { className: "key-facts" });
  facts.append(
    definition("Created", formatTime(incident.created_at)),
    definition("Evidence cited", `${recommendation?.evidence_ids?.length ?? 0} source(s)`),
    definition("Approval", recommendation?.requires_human_approval ? "Required before action" : "Not required"),
  );

  const itinerary = element("section", { className: "detail-section" });
  itinerary.append(element("h3", { text: "Itinerary at risk" }));
  const itineraryList = element("ul", { className: "itinerary-list" });
  (state.selectedTrip?.segments || []).forEach((segment) => {
    itineraryList.append(element("li", {
      text: `${segment.departure_airport} ${formatTime(segment.scheduled_departure_at, segment.departure_airport)} → ${segment.arrival_airport} ${formatTime(segment.scheduled_arrival_at, segment.arrival_airport)}`,
    }));
  });
  itinerary.append(itineraryList);

  const action = element("section", { className: "detail-section recommendation" });
  action.append(element("h3", { text: "Recommended next step" }));
  action.append(element("p", { text: recommendation?.recommended_action || "Review the available evidence." }));
  if (recommendation?.manager_message) {
    action.append(element("p", { className: "manager-note", text: recommendation.manager_message }));
  }

  const evidence = incident.approval_payload?.evidence || [];
  const evidenceSection = element("section", { className: "detail-section" });
  evidenceSection.append(element("h3", { text: "Evidence reviewed" }));
  const evidenceList = element("ul", { className: "evidence-list" });
  evidence.forEach((item) => {
    const score = item.normalized_payload?.risk_score;
    const scoreText = Number.isFinite(score) ? `Risk signal ${score}/100` : "Risk signal unavailable";
    evidenceList.append(element("li", {
      text: `${titleCase(item.source_type)} · ${scoreText} · ${titleCase(item.freshness_status)}`,
    }));
  });
  if (!evidence.length) {
    evidenceList.append(element("li", { text: "No source details are available for this incident." }));
  }
  evidenceSection.append(evidenceList);

  const alternatives = incident.approval_payload?.eligible_alternatives || [];
  const alternativesSection = element("section", { className: "detail-section" });
  alternativesSection.append(element("h3", { text: "Policy-eligible recovery options" }));
  const alternativesList = element("ul", { className: "alternatives-list" });
  alternatives.forEach((item, index) => {
    const cost = Number.isFinite(item.incremental_cost) ? `$${item.incremental_cost} incremental cost` : "Cost unavailable";
    const delay = Number.isFinite(item.arrival_delay_minutes) ? `${item.arrival_delay_minutes} min later arrival` : "Arrival impact unavailable";
    alternativesList.append(element("li", { text: `Option ${index + 1}: ${delay} · ${cost}` }));
  });
  if (!alternatives.length) {
    alternativesList.append(element("li", { text: "No policy-eligible alternative is currently available." }));
  }
  alternativesSection.append(alternativesList);

  replaceChildren(root, [summary, facts, itinerary, evidenceSection, alternativesSection, action]);
  $("#decision-form").classList.toggle("hidden", incident.status !== "pending_approval");
}

async function selectIncident(id) {
  notice("Opening the incident decision workspace…");
  try {
    // Fetch the incident and itinerary separately: the API can apply least-privilege checks to
    // each resource, while the browser gets only the fields required for this workspace.
    state.selected = await api(`/v1/incidents/${id}`);
    state.selectedTrip = await api(`/v1/trips/${state.selected.trip_id}`);
    renderQueue();
    renderDetail();
    await Promise.all([loadTimeline(), loadPreview()]);
    notice("Review the evidence and recommendation, then record your decision when ready.");
  } catch (error) {
    notice(error.message, true);
  }
}

async function loadTimeline() {
  const root = $("#timeline");
  if (!state.selected) return;
  try {
    const events = await api(`/v1/runs/${state.selected.correlation_id}/events`);
    const relevant = events.filter((event) => event.correlation_id === state.selected.correlation_id);
    if (!relevant.length) {
      replaceChildren(root, [element("li", { className: "empty", text: "No timeline events are available yet." })]);
      return;
    }
    replaceChildren(root, relevant.map((event) => {
      const item = element("li");
      item.append(
        element("strong", { text: titleCase(event.event_type.replaceAll(".", " ")) }),
        element("time", { text: formatTime(event.emitted_at) }),
      );
      return item;
    }));
  } catch (error) {
    replaceChildren(root, [element("li", { className: "empty", text: error.message })]);
  }
}

async function loadPreview() {
  if (!state.selected) return;
  try {
    state.preview = await api(`/v1/incidents/${state.selected.incident_id}/notification-preview`);
    const root = $("#notification-preview");
    replaceChildren(root, [
      element("strong", { text: state.preview.subject }),
      element("p", { text: state.preview.body }),
      element("small", { text: `Channel: ${titleCase(state.preview.channel)}` }),
    ]);
    $("#queue-notification").classList.remove("hidden");
  } catch (error) {
    $("#notification-preview").textContent = error.message;
    $("#queue-notification").classList.add("hidden");
  }
}

$("#session-form").addEventListener("submit", (event) => {
  event.preventDefault();
  updateWorkspaceSummary();
  loadQueue();
  $(".workspace-settings").open = false;
});
$("#refresh-queue").addEventListener("click", loadQueue);
$("#severity-filter").addEventListener("change", renderQueue);
$("#status-filter").addEventListener("change", renderQueue);

$("#decision-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const decision = event.submitter?.dataset.decision;
  if (!decision || !state.selected) return;
  try {
    await api(`/v1/incidents/${state.selected.incident_id}/${decision}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({
        decision,
        actor_id: $("#actor-id").value.trim(),
        reason: $("#decision-reason").value.trim(),
      }),
    });
    notice(`Decision recorded: ${decision === "approve" ? "recommendation approved" : "recommendation rejected"}.`);
    $("#decision-reason").value = "";
    await selectIncident(state.selected.incident_id);
    await loadQueue();
  } catch (error) {
    notice(error.message, true);
  }
});

$("#preferences-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const traveler = $("#traveler-id").value.trim();
  try {
    const profile = await api(`/v1/travelers/${encodeURIComponent(traveler)}/preferences`);
    $("#preferences-result").textContent = JSON.stringify(profile, null, 2);
  } catch (error) {
    $("#preferences-result").textContent = error.message;
  }
});

$("#delete-memory").addEventListener("click", async () => {
  const traveler = $("#traveler-id").value.trim();
  if (!traveler || !confirm("Delete this traveler's confirmed preference memory?")) return;
  try {
    await api(`/v1/travelers/${encodeURIComponent(traveler)}/preferences?actor_id=${encodeURIComponent($("#actor-id").value.trim())}`, { method: "DELETE" });
    $("#preferences-result").textContent = "Preference memory deleted.";
  } catch (error) {
    $("#preferences-result").textContent = error.message;
  }
});

$("#queue-notification").addEventListener("click", async () => {
  if (!state.preview) return;
  try {
    const notification = await api("/v1/notifications", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(state.preview),
    });
    $("#notification-history").textContent = `Queued ${titleCase(notification.channel)} notification (${notification.status}).`;
    notice("Traveler notification queued for approved delivery.");
  } catch (error) {
    notice(error.message, true);
  }
});

updateWorkspaceSummary();
loadQueue();
