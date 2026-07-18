"use strict";

const root = document.getElementById("app");
const query = new URLSearchParams(window.location.search);
const suppliedSession = query.get("session");
if (suppliedSession) {
  window.sessionStorage.setItem("apoapsis-ui-session", suppliedSession);
  window.history.replaceState({}, "", window.location.pathname + window.location.hash);
}
const sessionToken = suppliedSession || window.sessionStorage.getItem("apoapsis-ui-session");

const store = {
  overview: null,
  task: null,
  doctor: null,
  evaluations: null,
  route: { name: "home" },
  busy: false,
  error: null,
  approvalPending: false,
};

const e = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

function titleCase(value) {
  return String(value ?? "")
    .toLowerCase()
    .replaceAll("_", " ")
    .replaceAll("-", " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

function compactNumber(value) {
  const number = Number(value || 0);
  return new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(number);
}

function formatDate(value) {
  if (!value) return "Unknown";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return String(value);
  return new Intl.DateTimeFormat("en-US", {
    month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
  }).format(date);
}

function statusClass(status) {
  const normalized = String(status || "").toLowerCase();
  if (["complete", "passed", "ok", "ready"].includes(normalized)) return "good";
  if (["failed", "error", "timed_out", "unreadable"].includes(normalized)) return "bad";
  if (["warning", "human_review_required", "skipped"].includes(normalized)) return "warn";
  return "purple";
}

function acceptanceStatusClass(status) {
  const normalized = String(status || "").toLowerCase();
  if (normalized === "proven") return "good";
  if (normalized === "failed") return "bad";
  return "warn";
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("X-Apoapsis-Session", sessionToken || "");
  if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const response = await fetch(path, { ...options, headers });
  const payload = await response.json().catch(() => ({ error: `HTTP ${response.status}` }));
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

function parseRoute() {
  const parts = (window.location.hash.replace(/^#\/?/, "") || "home").split("/").filter(Boolean);
  if (parts[0] === "task" && parts[1]) {
    return { name: "task", taskId: decodeURIComponent(parts[1]), view: parts[2] || "spec" };
  }
  const allowed = new Set(["home", "new", "evaluations", "models"]);
  return { name: allowed.has(parts[0]) ? parts[0] : "home" };
}

async function syncRoute() {
  store.route = parseRoute();
  store.error = null;
  store.approvalPending = false;
  try {
    if (store.route.name === "task") {
      if (!store.task || store.task.task.task_id !== store.route.taskId) {
        store.busy = true;
        render();
        store.task = await api(`/api/tasks/${encodeURIComponent(store.route.taskId)}`);
      }
    } else if (store.route.name === "evaluations" && store.evaluations === null) {
      store.busy = true;
      render();
      store.evaluations = await api("/api/evaluations");
    }
  } catch (error) {
    store.error = error.message;
  } finally {
    store.busy = false;
    render();
  }
}

function sidebar() {
  const route = store.route;
  const active = (name) => route.name === name ? " active" : "";
  const task = route.name === "task" ? store.task?.task : null;
  const taskLinks = task ? `
    <p class="nav-label">Current task · ${e(task.task_id)}</p>
    <div class="nav-list current-task-nav">
      ${taskNavLink("spec", "Specification")}
      ${taskNavLink("control", "Control room", '<span class="nav-badge">LIVE</span>')}
      ${taskNavLink("changes", "Changes & verification")}
      ${taskNavLink("review", "Human review")}
      ${taskNavLink("report", "Report & audit")}
    </div>` : "";
  return `
    <aside class="sidebar">
      <a class="brand" href="#/home" aria-label="Apoapsis home">
        <span class="orbit-mark" aria-hidden="true"><i></i><b></b></span>
        <span class="brand-copy"><strong>Apoapsis</strong><span>CODING HARNESS</span></span>
      </a>
      <p class="nav-label">Workspace</p>
      <nav class="nav-list" aria-label="Workspace">
        <a class="nav-link${active("home")}" href="#/home"><span class="nav-dot"></span><span>Projects</span></a>
        <a class="nav-link${active("new")}" href="#/new"><span class="nav-dot"></span><span>New task</span></a>
        <a class="nav-link${active("evaluations")}" href="#/evaluations"><span class="nav-dot"></span><span>Evaluations</span></a>
        <a class="nav-link${active("models")}" href="#/models"><span class="nav-dot"></span><span>Models & environment</span></a>
      </nav>
      ${taskLinks}
      <div class="authority-card">
        <div class="mini-rule"><span class="nav-dot"></span> DETERMINISTIC AUTHORITY</div>
        <p><strong>Models propose.</strong> Apoapsis alone controls files, commands, verification, retries, transitions, and completion.</p>
      </div>
    </aside>`;
}

function taskNavLink(view, label, extra = "") {
  const route = store.route;
  const current = route.name === "task" && route.view === view ? " active" : "";
  return `<a class="nav-link${current}" href="#/task/${encodeURIComponent(route.taskId)}/${view}"><span class="nav-dot"></span><span class="task-objective">${e(label)}</span>${extra}</a>`;
}

function topbar() {
  const overview = store.overview;
  const project = overview?.project?.name || "Local project";
  const branch = overview?.repository?.branch || "detached";
  return `
    <header class="topbar">
      <div class="crumb">Apoapsis / <strong>${e(project)}</strong> / ${e(routeTitle())}</div>
      <div class="top-status">
        <span class="pill purple">${e(branch)}</span>
        <span class="pill ${overview?.project?.initialized ? "good" : "warn"}">${overview?.project?.initialized ? "Initialized" : "Setup needed"}</span>
      </div>
    </header>`;
}

function routeTitle() {
  if (store.route.name === "task") return titleCase(store.route.view);
  return titleCase(store.route.name);
}

function taskBanner(detail) {
  const task = detail.task;
  const spec = task.specification;
  const views = ["spec", "control", "changes", "review", "report"];
  return `
    <section class="task-banner">
      <div class="task-banner-main">
        <div class="task-title">
          <p>${e(task.task_id)} · VERSION ${e(task.version)}</p>
          <h1>${e(spec.objective.text)}</h1>
          <p>${e(task.state)} · UPDATED ${e(formatDate(task.updated_at))}</p>
        </div>
        <nav class="phase-nav" aria-label="Task phases">
          ${views.map((view) => `<a class="${store.route.view === view ? "current" : ""}" href="#/task/${encodeURIComponent(task.task_id)}/${view}">${e(titleCase(view))}</a>`).join("")}
        </nav>
      </div>
    </section>`;
}

function homeView() {
  const overview = store.overview;
  const tasks = overview.tasks || [];
  const activeTasks = tasks.filter((task) => !["COMPLETE", "FAILED", "ROLLED_BACK"].includes(task.state)).length;
  const completeTasks = tasks.filter((task) => task.state === "COMPLETE").length;
  const models = overview.models.filter((model) => model.configured);
  const lifecycle = overview.last_model_lifecycle;
  return `
    <main class="content">
      <div class="page-heading">
        <div><p class="eyebrow">LOCAL-FIRST / VERIFIED BY CONSTRUCTION</p><h1>Engineering control,<br>without the guesswork.</h1><p>Apoapsis turns model proposals into inspectable tasks, bounded changes, deterministic checks, and durable evidence.</p></div>
        <a class="button primary" href="#/new">New task →</a>
      </div>
      <section class="card hero-card">
        <div>
          <span class="pill ${overview.project.initialized ? "good" : "warn"}">${overview.project.initialized ? "Project ready" : "Initialization required"}</span>
          <h2>${e(overview.project.name)}</h2>
          <p class="mono">${e(overview.project.root)}</p>
          <p>${overview.repository.is_clean === true ? "The Git worktree is clean." : `${overview.repository.changed_files?.length || 0} local path(s) currently differ from HEAD.`} Model services are shown from the last explicit lifecycle action; no model was contacted to render this page.</p>
        </div>
        <div class="stat-stack">
          <div class="stat"><span>Active tasks</span><strong>${activeTasks}</strong></div>
          <div class="stat"><span>Complete</span><strong>${completeTasks}</strong></div>
          <div class="stat"><span>Models</span><strong>${models.length}</strong></div>
          <div class="stat"><span>Eval runs</span><strong>${overview.evaluation_runs}</strong></div>
        </div>
      </section>
      <div class="grid two mt-18">
        <section class="card">
          <div class="card-header"><div><h2>Recent tasks</h2><p>Persisted workflow records, newest first.</p></div><span class="pill purple">${tasks.length} total</span></div>
          ${tasks.length ? `<div class="task-list">${tasks.slice(0, 8).map(taskRow).join("")}</div>` : emptyState("No tasks yet", "Create a task through the CLI today; natural-language task creation in this interface is the next product slice.")}
        </section>
        <section class="card">
          <div class="card-header"><div><h2>Models & execution</h2><p>Configuration, not a synthetic readiness claim.</p></div><a class="button ghost" href="#/models">Inspect →</a></div>
          <div class="card-body">
            <div class="grid two">
              <div class="metric card"><span>Route</span><strong class="metric-compact">${e(overview.execution?.route || "unconfigured")}</strong><small>${e(overview.execution?.mode || "—")}</small></div>
              <div class="metric card"><span>Sandbox</span><strong class="metric-compact">${e(overview.execution?.verification_backend || "unconfigured")}</strong><small>Doctor confirms availability</small></div>
            </div>
            <p class="section-title">Last explicit model action</p>
            ${lifecycle ? `<div class="constraint"><div class="constraint-head"><span class="constraint-id">${e(String(lifecycle.action || "recorded").toUpperCase())}</span><span class="pill ${lifecycle.action === "start" ? "good" : "purple"}">${e(lifecycle.recorded_at ? formatDate(lifecycle.recorded_at) : "Recorded")}</span></div><blockquote>${e(lifecycle.note || "Lifecycle action recorded.")}</blockquote></div>` : `<div class="notice">No model lifecycle action has been recorded yet. Use START_APOAPSIS.cmd when you want to load local models.</div>`}
          </div>
        </section>
      </div>
    </main>`;
}

function taskRow(task) {
  return `<a class="task-row" href="#/task/${encodeURIComponent(task.task_id)}/spec">
    <div class="task-main"><strong>${e(task.objective)}</strong><span>${e(task.task_id)} · v${e(task.version)}</span></div>
    <span class="pill ${statusClass(task.state)}">${e(titleCase(task.state))}</span>
    <span class="meta">${e(formatDate(task.updated_at))}</span><span class="arrow">→</span>
  </a>`;
}

function newTaskView() {
  return `<main class="content narrow">
    <div class="page-heading"><div><p class="eyebrow">NEW TASK / AUTHORITY-GATED</p><h1>Describe the outcome.</h1><p>The designed intake surface is ready to implement, but model-assisted extraction needs a resumable server-side workflow rather than a UI that pretends a blocking CLI prompt is interactive.</p></div></div>
    <section class="card">
      <div class="card-body">
        <label class="section-title" for="task-request">Natural-language request</label>
        <div class="constraint"><blockquote id="task-request" class="muted">Task creation is intentionally disabled in this first slice.</blockquote></div>
        <div class="notice mt-16"><strong>Available now:</strong> create and inspect tasks with <span class="mono orange">apoapsis task "your request"</span>, then return here to review and approve the extracted record. The next UI milestone will add resumable model-assisted intake without transferring workflow authority to the browser.</div>
        <div class="flex-end mt-18"><button class="button primary" disabled>Extract specification</button></div>
      </div>
    </section>
  </main>`;
}

function modelsView() {
  const overview = store.overview;
  const configured = overview.models || [];
  const doctor = store.doctor;
  return `<main class="content">
    <div class="page-heading"><div><p class="eyebrow">MODELS / ENVIRONMENT</p><h1>Know what is ready.</h1><p>Configured roles are separate from measured readiness. Running Doctor performs Apoapsis's existing deterministic checks and never sends a model prompt unless an explicit probe is requested from the CLI.</p></div><button class="button primary" data-action="doctor" ${store.busy ? "disabled" : ""}>${store.busy ? "Checking…" : "Run doctor"}</button></div>
    <div class="grid four">${configured.map(modelCard).join("")}</div>
    <p class="section-title">Execution limits</p>
    <div class="grid four">
      ${metric("Mode", overview.execution?.mode || "—", "Configured workflow")}
      ${metric("Route", overview.execution?.route || "—", "Deterministic routing")}
      ${metric("Turns", overview.execution?.max_turns ?? "—", "Maximum agent turns")}
      ${metric("Verify runs", overview.execution?.max_verification_runs ?? "—", "Hard ceiling")}
    </div>
    <p class="section-title">Doctor evidence</p>
    <section class="card card-pad">
      ${doctor ? doctorList(doctor) : `<div class="empty"><h2>Not run in this UI session</h2><p>Readiness is never inferred from configuration alone. Run Doctor to produce a fresh, timestamped diagnostic result.</p></div>`}
    </section>
  </main>`;
}

function modelCard(model) {
  if (!model.configured) return `<article class="card model-card"><span class="role">${e(model.role)}</span><h3>Not configured</h3><p>This role will remain unavailable; Apoapsis will not silently substitute it.</p><div class="context">FAIL CLOSED</div></article>`;
  return `<article class="card model-card"><span class="role">${e(model.role)}</span><h3>${e(model.model)}</h3><p>${e(model.provider)} · ${e(model.base_url)}</p><div class="context">${compactNumber(model.context_window_tokens)} TOKEN WINDOW</div></article>`;
}

function doctorList(doctor) {
  return `<div class="doctor-summary"><div><strong>Overall ${e(titleCase(doctor.overall_status))}</strong><div class="meta">GENERATED ${e(formatDate(doctor.generated_at))}</div></div><span class="pill ${statusClass(doctor.overall_status)}">${e(doctor.overall_status)}</span></div><div class="doctor-list">${doctor.checks.map((check) => `<div class="doctor-row"><code>${e(check.name)}</code><span class="pill ${statusClass(check.status)}">${e(check.status)}</span><p>${e(check.detail)}${check.remediation ? `<br><span class="orange">${e(check.remediation)}</span>` : ""}</p></div>`).join("")}</div>`;
}

function evaluationsView() {
  const runs = store.evaluations?.runs || [];
  return `<main class="content">
    <div class="page-heading"><div><p class="eyebrow">EVALUATIONS / EVIDENCE-LABELED</p><h1>Compare measured behavior.</h1><p>Every lane keeps its evidence type. Missing hosted data remains unmeasured—never zero, never implied.</p></div><span class="pill ${runs.length ? "good" : "warn"}">${runs.length ? `${runs.length} persisted` : "No data"}</span></div>
    <section class="card">
      ${runs.length ? `<table class="eval-table"><thead><tr><th>Run</th><th>Profile</th><th>Lanes</th><th>Evidence</th><th>Artifact</th></tr></thead><tbody>${runs.map(evalRow).join("")}</tbody></table>` : emptyState("No persisted evaluation runs", "Run apoapsis eval download-service to create comparison evidence. Hosted metrics will remain explicitly unmeasured until credentials and live runs exist.")}
    </section>
  </main>`;
}

function evalRow(run) {
  const report = run.comparison || {};
  const lanes = Array.isArray(report.lanes) ? report.lanes : [];
  const evidence = [...new Set(lanes.map((lane) => lane.evidence_kind).filter(Boolean))];
  return `<tr><td><strong>${e(report.run_id || "Unknown run")}</strong><div class="meta">${e(formatDate(report.generated_at))}</div></td><td>${e(report.context_profile || "default")}</td><td>${lanes.length}</td><td>${e(evidence.map(titleCase).join(", ") || "Unmeasured")}</td><td><code>${e(run.artifact)}</code></td></tr>`;
}

function taskView() {
  if (!store.task) return loadingView();
  const detail = store.task;
  let body;
  switch (store.route.view) {
    case "control": body = controlView(detail); break;
    case "changes": body = changesView(detail); break;
    case "review": body = reviewView(detail); break;
    case "report": body = reportView(detail); break;
    default: body = specificationView(detail);
  }
  return `${taskBanner(detail)}${body}`;
}

function specificationView(detail) {
  const task = detail.task;
  const spec = task.specification;
  const constraints = spec.hard_constraints || [];
  const criteria = spec.acceptance_criteria || [];
  const canApprove = detail.available_actions?.includes("approve_specification");
  return `<main class="content narrow">
    <div class="page-heading"><div><p class="eyebrow">SPECIFICATION / VERSION ${e(task.version)}</p><h1>Approve the record,<br>not an interpretation.</h1><p>Hard constraints retain the user's exact wording alongside deterministic interpretation and verification intent.</p></div><span class="pill ${statusClass(task.state)}">${e(titleCase(task.state))}</span></div>
    <div class="split">
      <section>
        <article class="card card-pad"><p class="section-title mt-0">Objective</p><p class="objective">${e(spec.objective.text)}</p><div class="meta mt-14">SOURCE ${e(spec.objective.source)} · ${e(spec.objective.source_reference)}</div></article>
        <p class="section-title">Verbatim hard constraints · ${constraints.length}</p>
        ${constraints.length ? constraints.map(constraintCard).join("") : `<div class="notice">No hard constraints are recorded for this specification.</div>`}
      </section>
      <aside>
        <section class="card"><div class="card-header"><div><h3>Acceptance criteria</h3><p>${criteria.length} recorded</p></div></div><div class="card-body">${criteria.length ? criteria.map((criterion) => `<div class="constraint"><div class="constraint-head"><span class="constraint-id">${e(criterion.id)}</span><span class="pill purple">${e(criterion.status)}</span></div><blockquote>${e(criterion.text)}</blockquote><div class="meta mt-14">PROPOSED CHECK: ${criterion.verification_method ? `<code>${e(criterion.verification_method)}</code>` : "none -- unproven under the strict completion policy until mapped"}</div></div>`).join("") : `<p class="muted">No explicit criteria recorded.</p>`}</div></section>
        <p class="section-title">Risk & output</p>
        <section class="card card-pad"><div class="file-item"><span>Risk</span><strong>${e(titleCase(spec.risk_level))}</strong></div><div class="file-item"><span>Requested output</span><strong>${e(titleCase(spec.requested_output))}</strong></div></section>
      </aside>
    </div>
    ${canApprove ? `<div class="approval-bar"><div><strong>${store.approvalPending ? "Confirm this version" : "Human approval required"}</strong><span>${store.approvalPending ? `Approve version ${e(task.version)} for ${e(task.task_id)}. This records a user-authorized workflow transition.` : "This exact version will become the active specification."}</span></div><div class="approval-actions">${store.approvalPending ? `<button class="button ghost" data-action="approve-cancel">Cancel</button><button class="button primary" data-action="approve-confirm" data-task-id="${e(task.task_id)}" data-version="${e(task.version)}" ${store.busy ? "disabled" : ""}>Confirm approval →</button>` : `<button class="button primary" data-action="approve-intent">Approve specification →</button>`}</div></div>` : ""}
  </main>`;
}

function constraintCard(constraint) {
  return `<article class="constraint"><div class="constraint-head"><span class="constraint-id">${e(constraint.id)} · VERBATIM</span><span class="pill ${constraint.status === "active" ? "good" : "warn"}">${e(constraint.status)}</span></div><blockquote>“${e(constraint.verbatim_source)}”</blockquote><div class="interpretation"><strong>Deterministic interpretation</strong><br>${e(constraint.interpreted_meaning)}<br><span class="meta">VERIFY: ${e(constraint.verification_method)}</span></div></article>`;
}

function controlView(detail) {
  const report = detail.report;
  const events = detail.events || [];
  return `<main class="content">
    <div class="page-heading"><div><p class="eyebrow">CONTROL ROOM / EVENT RECORD</p><h1>Every transition is evidence.</h1><p>The timeline comes from persisted workflow events. Models cannot append, remove, or advance these states.</p></div><span class="pill ${statusClass(detail.task.state)}">${e(detail.task.state)}</span></div>
    <div class="split">
      <section class="card"><div class="card-header"><div><h2>Workflow timeline</h2><p>${events.length} deterministic events</p></div></div><div class="card-body"><div class="timeline">${events.map(eventCard).join("")}</div></div></section>
      <aside class="grid">
        <section class="card card-pad"><p class="section-title mt-0">Bounded execution</p><div class="grid two">${metric("Turns", report?.agent_turns ?? 0, "Recorded")}${metric("Patch attempts", report?.agent_patch_attempts ?? 0, "Recorded")}${metric("Verify runs", report?.agent_verification_runs ?? 0, "Recorded")}${metric("Calls", report?.number_of_calls ?? 0, "Provider telemetry")}</div></section>
        <section class="card card-pad"><p class="section-title mt-0">Authority boundary</p><div class="notice">The UI observes workflow events. It does not run shell commands, apply patches, decide completion, or extend retry ceilings.</div></section>
      </aside>
    </div>
  </main>`;
}

function eventCard(event) {
  const transition = event.from_state ? `${event.from_state} → ${event.to_state}` : event.to_state;
  return `<article class="event"><strong>${e(titleCase(event.event_type))}</strong><p>${e(transition)}</p><div class="meta">${e(event.actor)} · ${e(formatDate(event.created_at))} · #${e(event.sequence)}</div></article>`;
}

function changesView(detail) {
  const report = detail.report;
  const files = report?.files_changed || [];
  const coverage = report?.constraint_coverage || [];
  const verifications = report?.verification_results || [];
  const acceptanceCoverage = report?.acceptance_coverage || [];
  const completionPolicy = report?.completion_policy || "baseline";
  return `<main class="content">
    <div class="page-heading"><div><p class="eyebrow">CHANGES / VERIFICATION</p><h1>Proposal versus proof.</h1><p>Changed paths, constraint dispositions, and command results are rendered from the final report. An absent report remains pending.</p></div><span class="pill ${report ? statusClass(report.outcome) : "warn"}">${report ? e(report.outcome) : "Pending"}</span></div>
    ${report ? `<div class="grid four">${metric("Files changed", files.length, "Validated paths")}${metric("Transmitted files", report.transmitted_files, "Provider context")}${metric("Transmitted lines", report.transmitted_lines, "Provider context")}${metric("Verify runs", verifications.length, "Recorded results")}</div>` : `<div class="notice">No final report is present for this task yet.</div>`}
    <div class="grid two mt-22">
      <section class="card"><div class="card-header"><div><h2>Files changed</h2><p>Accepted report paths</p></div></div><div class="card-body">${files.length ? `<div class="file-list">${files.map((file) => `<div class="file-item"><code>${e(file)}</code><span class="pill purple">changed</span></div>`).join("")}</div>` : `<p class="muted">No changed files are recorded.</p>`}</div></section>
      <section class="card"><div class="card-header"><div><h2>Constraint coverage</h2><p>Model disposition, not independent proof</p></div></div><div class="card-body">${coverage.length ? coverage.map((item) => `<div class="constraint"><div class="constraint-head"><span class="constraint-id">${e(item.constraint_id)}</span><span class="pill ${item.disposition === "included" ? "good" : "warn"}">${e(item.disposition)}</span></div><blockquote>${e(item.reason)}</blockquote></div>`).join("") : `<p class="muted">No coverage entries are recorded.</p>`}</div></section>
    </div>
    <p class="section-title">Acceptance coverage · ${e(titleCase(completionPolicy))} completion policy</p>
    <section class="card card-pad">${acceptanceCoverage.length ? `<div class="verification-list">${acceptanceCoverage.map(acceptanceCoverageItem).join("")}</div>` : `<p class="muted">${completionPolicy === "strict" ? "No active acceptance criteria are configured for this task." : "The baseline completion policy does not gate on acceptance coverage; this task recorded none."}</p>`}</section>
    <p class="section-title">Verification results</p>
    <section class="card card-pad">${verifications.length ? `<div class="verification-list">${verifications.flatMap((result) => (result.commands || []).map((command) => verificationItem(command, result))).join("")}</div>` : `<p class="muted">No verification results are recorded.</p>`}</section>
  </main>`;
}

function acceptanceCoverageItem(item) {
  return `<div class="verification-item"><div><strong>${e(item.criterion_id)}</strong><p>${e(item.reason)}</p>${item.evidence_reference ? `<p class="mono">${e(item.evidence_reference)}</p>` : ""}</div><span class="pill ${acceptanceStatusClass(item.status)}">${e(titleCase(item.status))}</span></div>`;
}

function verificationItem(command, aggregate) {
  return `<div class="verification-item"><div><strong>${e(command.name)}</strong><p class="mono">${e((command.argv || []).join(" "))}</p><p>${e(command.backend)} · ${Number(command.duration_seconds || 0).toFixed(2)}s</p></div><span class="pill ${statusClass(command.status || aggregate.status)}">${e(command.status || aggregate.status)}</span></div>`;
}

function reviewView(detail) {
  const required = detail.task.state === "HUMAN_REVIEW_REQUIRED";
  return `<main class="content narrow">
    <div class="page-heading"><div><p class="eyebrow">HUMAN REVIEW / EXPLICIT CONTROL</p><h1>${required ? "A person must decide." : "No review stop is active."}</h1><p>Resume choices must come from workflow state and deterministic policy. This first interface slice does not invent generic approve or retry buttons.</p></div><span class="pill ${required ? "warn" : "good"}">${required ? "Review required" : "No active stop"}</span></div>
    <section class="card result-hero"><div class="result-outcome"><span class="result-orb ${required ? "failed" : ""}"></span><div><h2>${required ? "Paused" : "Clear"}</h2><p>${required ? "Inspect the persisted event timeline and audit artifacts before choosing a supported resume path." : "The deterministic workflow has not requested human intervention."}</p></div></div></section>
    <div class="grid three mt-18">${reviewOption("Resume local", "Only when the workflow exposes this transition.")}${reviewOption("Escalate frontier", "Only through deterministic routing and configured ceilings.")}${reviewOption("Roll back", "Recoverable worktree cleanup remains a CLI action today.")}</div>
  </main>`;
}

function reviewOption(title, description) {
  return `<article class="card card-pad"><span class="pill purple">Planned action</span><h3>${e(title)}</h3><p class="muted">${e(description)}</p><button class="button" disabled>Unavailable</button></article>`;
}

function reportView(detail) {
  const report = detail.report;
  if (!report) return `<main class="content narrow">${emptyState("Report pending", "Apoapsis has not written a final task report. Pending is distinct from success.")}</main>`;
  const complete = report.outcome === "complete";
  const artifacts = detail.artifacts || [];
  const models = report.models_used || [];
  return `<main class="content">
    <section class="card result-hero"><div class="result-outcome"><span class="result-orb ${complete ? "" : "failed"}"></span><div><h2>${e(report.outcome)}</h2><p>${e(report.error || (complete ? "Deterministic verification and reporting completed." : "The task did not reach verified completion."))}</p></div></div><span class="pill ${statusClass(report.outcome)}">${complete ? "Verified" : "Recorded"}</span></section>
    <p class="section-title">Usage & telemetry</p>
    <div class="grid four">${metric("Calls", report.number_of_calls, "Provider invocations")}${metric("Input tokens", compactNumber(report.input_tokens), `${compactNumber(report.cached_input_tokens)} cached`)}${metric("Output tokens", compactNumber(report.output_tokens), "Generated")}${metric("Estimated cost", `$${Number(report.estimated_cost_usd || 0).toFixed(4)}`, `${Number(report.latency_seconds || 0).toFixed(1)}s latency`)}</div>
    <p class="section-title">Bounded agent budget · ${e(titleCase(report.completion_policy || "baseline"))} completion policy</p>
    <div class="grid two mt-22">
      <section class="card card-pad"><p class="section-title mt-0">Local agent</p><div class="grid two">${metric("Turns", `${report.local_agent_turns ?? 0} / ${report.local_agent_budget?.max_turns ?? "—"}`, "Used / configured ceiling")}${metric("Patch attempts", `${report.agent_patch_attempts ?? 0} / ${report.local_agent_budget?.max_patch_attempts ?? "—"}`, "Used / configured ceiling")}${metric("Verify runs", `${report.agent_verification_runs ?? 0} / ${report.local_agent_budget?.max_verification_runs ?? "—"}`, "Used / configured ceiling")}${metric("Rejected requests", report.rejected_tool_requests ?? 0, "Tool actions the harness refused")}</div></section>
      <section class="card card-pad"><p class="section-title mt-0">Frontier escalation</p><div class="grid two">${metric("Available", report.frontier_available ? "Yes" : "No", "Configured frontier coder")}${metric("Escalated", report.escalation_triggered ? "Yes" : "No", report.escalation_reason || "Not triggered")}${metric("Turns", `${report.frontier_agent_turns ?? 0} / ${report.frontier_agent_budget?.max_turns ?? "—"}`, "Used / configured ceiling")}${metric("Verify runs", `${report.frontier_agent_verification_runs ?? 0} / ${report.frontier_agent_budget?.max_verification_runs ?? "—"}`, "Used / configured ceiling")}</div></section>
    </div>
    <div class="grid two mt-22">
      <section class="card"><div class="card-header"><div><h2>Models & roles used</h2><p>Exact report identities</p></div></div><div class="card-body">${models.length ? models.map((model) => `<div class="file-item"><span>${e(model.provider)}</span><code>${e(model.model)}</code></div>`).join("") : `<p class="muted">No model calls were recorded.</p>`}</div></section>
      <section class="card"><div class="card-header"><div><h2>Audit artifacts</h2><p>${artifacts.length} files in the task record</p></div></div><div class="card-body">${artifacts.length ? `<div class="artifact-list">${artifacts.map((artifact) => `<div class="artifact-item"><code>${e(artifact)}</code></div>`).join("")}</div>` : `<p class="muted">No task artifacts were discovered.</p>`}</div></section>
    </div>
  </main>`;
}

function metric(label, value, note) {
  return `<article class="card metric"><span>${e(label)}</span><strong>${e(value)}</strong><small>${e(note)}</small></article>`;
}

function emptyState(title, description) {
  return `<div class="empty"><span class="orbit-mark" aria-hidden="true"><i></i><b></b></span><h2>${e(title)}</h2><p>${e(description)}</p></div>`;
}

function loadingView() {
  return `<main class="content narrow">${emptyState("Reading the deterministic record…", "Apoapsis is loading persisted state from this project.")}</main>`;
}

function render() {
  if (!sessionToken) {
    root.innerHTML = `<div class="boot-screen"><span class="orbit-mark"><i></i><b></b></span><p class="eyebrow">SESSION REQUIRED</p><h1>Launch with <span class="orange mono">apoapsis ui</span></h1><p class="muted">The local API requires a fresh capability token and does not expose project data from a manually entered URL.</p></div>`;
    return;
  }
  if (!store.overview) {
    root.innerHTML = `${loadingView()}${store.error ? `<div class="error-banner" role="alert">${e(store.error)}</div>` : ""}`;
    return;
  }
  let view;
  if (store.route.name === "task") view = taskView();
  else if (store.route.name === "new") view = newTaskView();
  else if (store.route.name === "evaluations") view = evaluationsView();
  else if (store.route.name === "models") view = modelsView();
  else view = homeView();
  root.innerHTML = `<div class="app-shell">${sidebar()}<div class="workspace">${topbar()}${view}</div></div>${store.error ? `<div class="error-banner" role="alert">${e(store.error)}</div>` : ""}`;
}

async function runDoctor() {
  store.busy = true;
  store.error = null;
  render();
  try {
    store.doctor = await api("/api/doctor");
  } catch (error) {
    store.error = error.message;
  } finally {
    store.busy = false;
    render();
  }
}

async function approve(button) {
  const taskId = button.dataset.taskId;
  const version = Number(button.dataset.version);
  store.busy = true;
  store.error = null;
  render();
  try {
    await api(`/api/tasks/${encodeURIComponent(taskId)}/approve`, {
      method: "POST",
      body: JSON.stringify({ expected_version: version }),
    });
    store.task = await api(`/api/tasks/${encodeURIComponent(taskId)}`);
    store.overview = await api("/api/overview");
    store.approvalPending = false;
  } catch (error) {
    store.error = error.message;
  } finally {
    store.busy = false;
    render();
  }
}

root.addEventListener("click", (event) => {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  if (button.dataset.action === "doctor") runDoctor();
  if (button.dataset.action === "approve-intent") {
    store.approvalPending = true;
    render();
  }
  if (button.dataset.action === "approve-cancel") {
    store.approvalPending = false;
    render();
  }
  if (button.dataset.action === "approve-confirm") approve(button);
});

window.addEventListener("hashchange", syncRoute);

async function boot() {
  if (!sessionToken) {
    render();
    return;
  }
  try {
    store.overview = await api("/api/overview");
  } catch (error) {
    store.error = error.message;
  }
  await syncRoute();
}

boot();
