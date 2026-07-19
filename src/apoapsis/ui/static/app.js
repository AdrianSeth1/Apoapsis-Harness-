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
  plans: null,
  plan: null,
  reviews: null,
  review: null,
  reviewOperation: null,
  reviewConfirm: null,
  reviewAdditionalTurns: 5,
  route: { name: "home" },
  busy: false,
  error: null,
  approvalPending: false,
  planApprovalPending: false,
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

function planStatusClass(status) {
  const normalized = String(status || "").toLowerCase();
  if (normalized === "approved" || normalized === "executed") return "good";
  if (normalized === "validated") return "purple";
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
  if (parts[0] === "plan" && parts[1]) {
    return { name: "plan", planId: decodeURIComponent(parts[1]), view: parts[2] || "overview" };
  }
  if (parts[0] === "review" && parts[1]) {
    return { name: "review", taskId: decodeURIComponent(parts[1]) };
  }
  const allowed = new Set(["home", "new", "evaluations", "models", "plans", "reviews"]);
  return { name: allowed.has(parts[0]) ? parts[0] : "home" };
}

async function syncRoute() {
  store.route = parseRoute();
  store.error = null;
  store.approvalPending = false;
  store.planApprovalPending = false;
  if (store.route.name !== "review") {
    store.reviewConfirm = null;
  }
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
    } else if (store.route.name === "plans" && store.plans === null) {
      store.busy = true;
      render();
      store.plans = await api("/api/plans");
    } else if (store.route.name === "plan") {
      if (!store.plan || store.plan.plan.plan_id !== store.route.planId) {
        store.busy = true;
        render();
        store.plan = await api(`/api/plans/${encodeURIComponent(store.route.planId)}`);
      }
    } else if (store.route.name === "reviews" && store.reviews === null) {
      store.busy = true;
      render();
      store.reviews = await api("/api/reviews");
    } else if (store.route.name === "review") {
      if (!store.review || store.review.task_id !== store.route.taskId) {
        store.busy = true;
        render();
        store.reviewOperation = null;
        store.review = await api(`/api/reviews/${encodeURIComponent(store.route.taskId)}`);
        resumePendingReviewOperationPoll(store.route.taskId);
      }
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
  const plan = route.name === "plan" ? store.plan?.plan : null;
  const planLinks = plan ? `
    <p class="nav-label">Current plan · ${e(plan.plan_id)}</p>
    <div class="nav-list current-task-nav">
      ${planNavLink("overview", "Overview")}
      ${planNavLink("slices", "Implementation slices")}
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
        <a class="nav-link${active("plans")}" href="#/plans"><span class="nav-dot"></span><span>Plans</span></a>
        <a class="nav-link${active("reviews")}" href="#/reviews"><span class="nav-dot"></span><span>Human review queue</span></a>
        <a class="nav-link${active("evaluations")}" href="#/evaluations"><span class="nav-dot"></span><span>Evaluations</span></a>
        <a class="nav-link${active("models")}" href="#/models"><span class="nav-dot"></span><span>Models & environment</span></a>
      </nav>
      ${taskLinks}
      ${planLinks}
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

function planNavLink(view, label, extra = "") {
  const route = store.route;
  const current = route.name === "plan" && route.view === view ? " active" : "";
  return `<a class="nav-link${current}" href="#/plan/${encodeURIComponent(route.planId)}/${view}"><span class="nav-dot"></span><span class="task-objective">${e(label)}</span>${extra}</a>`;
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
      ${metric("Completion policy", titleCase(overview.execution?.completion_policy || "—"), overview.execution?.completion_policy === "baseline" ? "No acceptance-coverage gate" : "Gates COMPLETE on proven acceptance criteria")}
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

function plansView() {
  const plans = store.plans?.plans || [];
  return `<main class="content">
    <div class="page-heading"><div><p class="eyebrow">ARCHITECT MODE / PLANS</p><h1>Ideas, decomposed<br>into small, verifiable slices.</h1><p>Plans are proposed by a strong model you run manually (<span class="mono orange">apoapsis plan export</span>), then reviewed and approved here. Approving a plan never executes anything.</p></div><span class="pill ${plans.length ? "good" : "warn"}">${plans.length ? `${plans.length} recorded` : "No plans yet"}</span></div>
    <section class="card">
      ${plans.length ? `<div class="task-list">${plans.map(planRow).join("")}</div>` : emptyState("No plans yet", 'Run apoapsis plan export "<idea>" to create a reproducible planning package, then apoapsis plan import <response.json> once a model responds.')}
    </section>
  </main>`;
}

function planRow(plan) {
  return `<a class="task-row" href="#/plan/${encodeURIComponent(plan.plan_id)}/overview">
    <div class="task-main"><strong>${e(plan.architecture_summary)}</strong><span>${e(plan.plan_id)} · v${e(plan.version)} · ${e(plan.slice_count)} slice(s)</span></div>
    <span class="pill ${planStatusClass(plan.status)}">${e(titleCase(plan.status))}</span>
    <span class="meta">${e(formatDate(plan.updated_at))}</span><span class="arrow">→</span>
  </a>`;
}

function planView() {
  if (!store.plan) return loadingView();
  const detail = store.plan;
  let body;
  switch (store.route.view) {
    case "slices": body = planSlicesView(detail); break;
    default: body = planOverviewView(detail);
  }
  return `${planBanner(detail)}${body}`;
}

function planBanner(detail) {
  const record = detail.plan;
  const views = ["overview", "slices"];
  const labels = { overview: "Overview", slices: "Implementation slices" };
  return `
    <section class="task-banner">
      <div class="task-banner-main">
        <div class="task-title">
          <p>${e(record.plan_id)} · VERSION ${e(record.version)}</p>
          <h1>${e(record.plan.architecture_summary)}</h1>
          <p>${e(titleCase(record.status))} · UPDATED ${e(formatDate(record.updated_at))}</p>
        </div>
        <nav class="phase-nav" aria-label="Plan views">
          ${views.map((view) => `<a class="${store.route.view === view ? "current" : ""}" href="#/plan/${encodeURIComponent(record.plan_id)}/${view}">${e(labels[view])}</a>`).join("")}
        </nav>
      </div>
    </section>`;
}

function planOverviewView(detail) {
  const record = detail.plan;
  const plan = record.plan;
  const decisions = plan.decisions || [];
  const validation = record.validation;
  const findings = validation?.findings || [];
  const canApprove = detail.available_actions?.includes("approve_plan");
  return `<main class="content narrow">
    <div class="page-heading"><div><p class="eyebrow">ARCHITECTURE / DECISIONS</p><h1>Design record,<br>not an execution order.</h1><p>Architect Mode designs; it never runs a shell command, edits a file, or executes a slice. Approving this plan only records a reviewed status -- nothing executes as a result.</p></div><span class="pill ${planStatusClass(record.status)}">${e(titleCase(record.status))}</span></div>
    <article class="card card-pad"><p class="section-title mt-0">Idea</p><blockquote class="objective">${e(record.idea_text)}</blockquote></article>
    <p class="section-title">Architecture summary</p>
    <article class="card card-pad"><p class="objective">${e(plan.architecture_summary)}</p></article>
    <p class="section-title">Decisions · ${decisions.length}</p>
    ${decisions.length ? decisions.map(decisionCard).join("") : `<div class="notice">No decisions were recorded for this plan.</div>`}
    <p class="section-title">Validation findings · ${findings.length}</p>
    <section class="card card-pad">
      ${validation ? (findings.length ? `<div class="verification-list">${findings.map(findingItem).join("")}</div>` : `<p class="muted">No findings -- the plan validated cleanly against the current configuration.</p>`) : `<p class="muted">This plan has not been validated yet. Run <span class="mono orange">apoapsis plan validate ${e(record.plan_id)}</span>.</p>`}
    </section>
    <p class="section-title">Package & provenance</p>
    <section class="card card-pad">
      <div class="file-item"><span>Originating package</span><code>${e(record.package_id)}</code></div>
      <div class="file-item"><span>Plan version</span><strong>${e(record.version)}</strong></div>
      <div class="file-item"><span>Created</span><strong>${e(formatDate(record.created_at))}</strong></div>
      <div class="file-item"><span>Updated</span><strong>${e(formatDate(record.updated_at))}</strong></div>
    </section>
    <p class="section-title">Audit artifacts · ${(detail.artifacts || []).length}</p>
    <section class="card card-pad">${(detail.artifacts || []).length ? `<div class="artifact-list">${detail.artifacts.map((artifact) => `<div class="artifact-item"><code>${e(artifact)}</code></div>`).join("")}</div>` : `<p class="muted">No plan artifacts were discovered.</p>`}</section>
    ${canApprove ? `<div class="approval-bar"><div><strong>${store.planApprovalPending ? "Confirm this version" : "Validated — ready for approval"}</strong><span>${store.planApprovalPending ? `Approve version ${e(record.version)} of ${e(record.plan_id)}. This records a reviewed status only; it does not execute any slice.` : "A human must explicitly approve before this plan is considered reviewed. Approval never executes a slice."}</span></div><div class="approval-actions">${store.planApprovalPending ? `<button class="button ghost" data-action="plan-approve-cancel">Cancel</button><button class="button primary" data-action="plan-approve-confirm" data-plan-id="${e(record.plan_id)}" data-version="${e(record.version)}" ${store.busy ? "disabled" : ""}>Confirm approval →</button>` : `<button class="button primary" data-action="plan-approve-intent">Approve plan →</button>`}</div></div>` : ""}
  </main>`;
}

function decisionCard(decision) {
  const alternatives = decision.alternatives_considered || [];
  return `<article class="constraint"><div class="constraint-head"><span class="constraint-id">${e(decision.decision_id)}</span></div><blockquote>${e(decision.title)}</blockquote><div class="interpretation"><strong>Rationale</strong><br>${e(decision.rationale)}${alternatives.length ? `<br><span class="meta">ALTERNATIVES CONSIDERED: ${alternatives.map((item) => e(item)).join("; ")}</span>` : ""}</div></article>`;
}

function findingItem(finding) {
  return `<div class="verification-item"><div><strong>${e(finding.code)}</strong><p>${e(finding.message)}</p>${finding.slice_id ? `<p class="mono">${e(finding.slice_id)}</p>` : ""}</div><span class="pill ${finding.severity === "error" ? "bad" : "warn"}">${e(titleCase(finding.severity))}</span></div>`;
}

function planSlicesView(detail) {
  const record = detail.plan;
  const plan = record.plan;
  const order = detail.dependency_order && detail.dependency_order.length
    ? detail.dependency_order
    : plan.slices.map((item) => item.slice_id);
  const byId = new Map(plan.slices.map((item) => [item.slice_id, item]));
  const orderedSlices = order.map((id) => byId.get(id)).filter(Boolean);
  return `<main class="content">
    <div class="page-heading"><div><p class="eyebrow">IMPLEMENTATION SLICES / DEPENDENCY ORDER</p><h1>Small, independently<br>verifiable work packets.</h1><p>Rendered in dependency order. Suggested paths and symbols are advisory hints for the local coding model, not a grant to write outside the repository.</p></div><span class="pill purple">${orderedSlices.length} slice(s)</span></div>
    ${orderedSlices.length ? orderedSlices.map(sliceCard).join("") : emptyState("No slices in this plan", "The imported plan did not include any implementation slices.")}
  </main>`;
}

function sliceRiskClass(risk) {
  const normalized = String(risk || "unclassified").toLowerCase();
  if (normalized === "low") return "good";
  if (normalized === "medium") return "warn";
  if (normalized === "high" || normalized === "critical") return "bad";
  return "purple";
}

function sliceCard(slice) {
  const references = [...(slice.inherited_constraint_ids || []), ...(slice.acceptance_criterion_ids || [])];
  return `<article class="card card-pad mt-16">
    <div class="constraint-head"><span class="constraint-id">${e(slice.slice_id)}</span><span class="pill ${sliceRiskClass(slice.risk_level)}">${e(titleCase(slice.risk_level || "unclassified"))} risk</span></div>
    <h3>${e(slice.title)}</h3>
    <p class="objective">${e(slice.objective)}</p>
    <div class="grid two mt-14">
      <div>
        <p class="section-title mt-0">Exclusions</p>
        ${(slice.exclusions || []).length ? `<ul>${slice.exclusions.map((item) => `<li>${e(item)}</li>`).join("")}</ul>` : `<p class="muted">None recorded.</p>`}
        <p class="section-title">Dependencies</p>
        <p class="mono">${(slice.dependencies || []).map((item) => e(item)).join(", ") || "None -- no prerequisite slices."}</p>
        <p class="section-title">Inherited constraints / criteria</p>
        <p class="mono">${references.map((item) => e(item)).join(", ") || "None recorded."}</p>
      </div>
      <div>
        <p class="section-title mt-0">Verification commands</p>
        <p class="mono">${(slice.verification_commands || []).map((item) => e(item)).join(", ") || "None named -- validation will flag this."}</p>
        <p class="section-title">Suggested paths (advisory)</p>
        <p class="mono">${(slice.suggested_paths || []).map((item) => e(item)).join(", ") || "None suggested."}</p>
        <p class="section-title">Stop / escalation conditions</p>
        ${(slice.stop_conditions || []).length ? `<ul>${slice.stop_conditions.map((item) => `<li>${e(item)}</li>`).join("")}</ul>` : `<p class="muted">None recorded.</p>`}
      </div>
    </div>
    <p class="section-title">Local-model-fit rationale</p>
    <blockquote>${e(slice.local_model_fit_rationale)}</blockquote>
    <p class="section-title">Work brief</p>
    <blockquote>${e(slice.work_brief)}</blockquote>
  </article>`;
}

const REVIEW_ACTION_LABELS = {
  inspect_only: "Inspect only",
  abandon: "Abandon & roll back",
  verification_only_retry: "Retry verification",
  local_continuation: "Continue locally",
  frontier_continuation: "Continue with frontier",
};

const REVIEW_OPERATION_STAGE = {
  recorded: { label: "Recorded", pill: "warn" },
  running: { label: "Running", pill: "purple" },
  succeeded: { label: "Succeeded", pill: "good" },
  failed: { label: "Failed", pill: "bad" },
};

function reviewsView() {
  const cases = store.reviews?.cases || [];
  return `<main class="content">
    <div class="page-heading"><div><p class="eyebrow">HUMAN REVIEW / EXPLICIT CONTROL</p><h1>Tasks waiting<br>on a decision.</h1><p>Only actions the deterministic review service actually authorizes for each exact stop reason are ever offered here -- never a fixed menu.</p></div><span class="pill ${cases.length ? "warn" : "good"}">${cases.length ? `${cases.length} awaiting review` : "None waiting"}</span></div>
    <section class="card">
      ${cases.length ? `<div class="task-list">${cases.map(reviewRow).join("")}</div>` : emptyState("Nothing needs review", "Tasks that stop for a human decision will appear here with their exact stop reason and available actions.")}
    </section>
  </main>`;
}

function reviewRow(item) {
  return `<a class="task-row" href="#/review/${encodeURIComponent(item.task_id)}">
    <div class="task-main"><strong>${e(item.objective_text || item.task_id)}</strong><span>${e(item.task_id)} · v${e(item.task_version)}</span></div>
    <span class="pill warn">${e(titleCase(item.stop_reason_kind))}</span>
    <span class="meta">${e(formatDate(item.generated_at))}</span><span class="arrow">→</span>
  </a>`;
}

function reviewView() {
  if (!store.review) return loadingView();
  return reviewDetailView(store.review);
}

function reviewDetailView(detail) {
  const eligible = detail.eligible_actions || [];
  const localBudget = detail.configured_local_budget;
  const frontierBudget = detail.configured_frontier_budget;
  return `<main class="content">
    <div class="page-heading"><div><p class="eyebrow">HUMAN REVIEW / ${e(detail.task_id)}</p><h1>${e(titleCase(detail.stop_reason_kind))}</h1><p>${e(detail.stop_reason_text)}</p></div><span class="pill warn">${e(titleCase(detail.workflow_state))}</span></div>

    <div class="grid four">
      ${metric("Task version", detail.task_version, "Optimistic concurrency")}
      ${metric("Worktree", detail.worktree_exists ? "Present" : "None", detail.worktree_exists ? "Fingerprint tracked" : "Stopped before implementation began")}
      ${metric("Continuations used", `${detail.continuations_used} / ${detail.max_continuations_per_task}`, "Per-task ceiling")}
      ${metric("Frontier", detail.frontier_available ? "Configured" : "Not configured", "Checked fresh, not from the original stop")}
    </div>

    ${reviewOperationPanel()}

    <p class="section-title">Active hard constraints · ${(detail.active_hard_constraints || []).length}</p>
    ${(detail.active_hard_constraints || []).length ? detail.active_hard_constraints.map(constraintCard).join("") : `<div class="notice">No active hard constraints are recorded.</div>`}

    ${detail.worktree_exists ? `
    <p class="section-title">Current diff</p>
    <section class="card card-pad"><div class="mono" style="white-space: pre-wrap; overflow-wrap: anywhere;">${e(detail.current_diff || "(no diff)")}</div></section>
    ` : `<div class="notice mt-16">No worktree exists for this task yet -- it stopped before implementation began.</div>`}

    <p class="section-title">Verification & acceptance</p>
    <section class="card card-pad">
      ${(detail.verification_results || []).length ? `<div class="verification-list">${detail.verification_results.flatMap((result) => (result.commands || []).map((command) => verificationItem(command, result))).join("")}</div>` : `<p class="muted">No verification results are recorded.</p>`}
      ${(detail.acceptance_coverage || []).length ? `<div class="verification-list mt-14">${detail.acceptance_coverage.map(acceptanceCoverageItem).join("")}</div>` : ""}
      ${(detail.normalized_failures || []).length ? `<div class="mt-14">${detail.normalized_failures.map((failure) => `<div class="constraint"><div class="constraint-head"><span class="constraint-id">${e(failure.command_name)}</span><span class="pill bad">${e(failure.status)}</span></div><blockquote>${e(failure.root_error)}</blockquote></div>`).join("")}</div>` : ""}
    </section>

    <p class="section-title">Budgets</p>
    <div class="grid two mt-14">
      <section class="card card-pad"><p class="section-title mt-0">Local agent</p><div class="grid two">${metric("Turns", `${detail.consumed_local_turns} / ${localBudget?.max_turns ?? "—"}`, "Used / ceiling")}${metric("Patch attempts", `${detail.consumed_local_patch_attempts} / ${localBudget?.max_patch_attempts ?? "—"}`, "Used / ceiling")}${metric("Verify runs", `${detail.consumed_local_verification_runs} / ${localBudget?.max_verification_runs ?? "—"}`, "Used / ceiling")}</div></section>
      <section class="card card-pad"><p class="section-title mt-0">Frontier agent</p><div class="grid two">${metric("Turns", `${detail.consumed_frontier_turns} / ${frontierBudget?.max_turns ?? "—"}`, "Used / ceiling")}${metric("Patch attempts", `${detail.consumed_frontier_patch_attempts} / ${frontierBudget?.max_patch_attempts ?? "—"}`, "Used / ceiling")}${metric("Verify runs", `${detail.consumed_frontier_verification_runs} / ${frontierBudget?.max_verification_runs ?? "—"}`, "Used / ceiling")}</div></section>
    </div>

    <p class="section-title">Models used</p>
    <section class="card card-pad">${(detail.models_used || []).length ? detail.models_used.map((model) => `<div class="file-item"><code>${e(model)}</code></div>`).join("") : `<p class="muted">No model calls were recorded.</p>`}</section>

    <p class="section-title">Audit artifacts · ${(detail.audit_artifact_locations || []).length}</p>
    <section class="card card-pad">${(detail.audit_artifact_locations || []).length ? `<div class="artifact-list">${detail.audit_artifact_locations.map((artifact) => `<div class="artifact-item"><code>${e(artifact)}</code></div>`).join("")}</div>` : `<p class="muted">No artifacts were discovered.</p>`}</section>

    <p class="section-title">Eligible actions</p>
    <section class="card card-pad">${reviewActionPanel(detail, eligible)}</section>
  </main>`;
}

function reviewOperationPanel() {
  const op = store.reviewOperation;
  if (!op) return "";
  const stage = REVIEW_OPERATION_STAGE[op.status] || { label: op.status, pill: "warn" };
  let note;
  if (op.status === "running") note = "A background worker is performing this action now. It is safe to close this tab -- progress is persisted and will still be here on reconnect.";
  else if (op.status === "recorded") note = "Accepted and durably recorded; waiting for the background worker to pick it up.";
  else note = op.result_summary || op.error || "";
  return `<section class="card card-pad mt-16">
    <div class="constraint-head"><span class="constraint-id">OPERATION ${e(op.operation_id)} · ${e(REVIEW_ACTION_LABELS[op.action] || op.action)}</span><span class="pill ${stage.pill}">${e(stage.label)}</span></div>
    <p class="muted">${e(note)}</p>
  </section>`;
}

function reviewActionPanel(detail, eligible) {
  if (!eligible.length) {
    return `<p class="muted">No actions are currently eligible for this task.</p>`;
  }
  if (store.reviewConfirm) {
    return reviewConfirmPanel(detail, store.reviewConfirm.action);
  }
  return `<div class="grid two">${eligible.map((action) => reviewActionButton(action)).join("")}</div>`;
}

function reviewActionButton(action) {
  const label = REVIEW_ACTION_LABELS[action] || action;
  const description = {
    inspect_only: "No mutation -- just records that a human looked at this case.",
    abandon: "Cleans up the worktree (if one exists) and marks the task rolled back.",
    verification_only_retry: "Re-runs configured verification against the current worktree. No model is called.",
    local_continuation: "Resumes the local coding agent with additional authorized turns.",
    frontier_continuation: "Resumes the frontier coding agent with additional authorized turns.",
  }[action] || "";
  return `<article class="card card-pad"><h3>${e(label)}</h3><p class="muted">${e(description)}</p><button class="button ${action === "inspect_only" ? "ghost" : "primary"}" data-action="review-act-intent" data-review-action="${e(action)}">${e(label)} →</button></article>`;
}

function reviewConfirmPanel(detail, action) {
  const needsTurns = action === "local_continuation" || action === "frontier_continuation";
  const copy = {
    inspect_only: "Records that a human explicitly reviewed this case. No other state changes.",
    abandon: "This cleans up the worktree (if one exists) and marks the task ROLLED_BACK. This cannot be undone.",
    verification_only_retry: "Re-runs configured verification against the current worktree right now. No model is called.",
    local_continuation: "Resumes the local coding agent from exactly where it stopped, with the authorized additional turns. This calls a model.",
    frontier_continuation: "Resumes the frontier coding agent from exactly where it stopped, with the authorized additional turns. This calls a hosted/frontier model and may incur cost.",
  }[action] || "";
  return `<div class="approval-bar">
    <div>
      <strong>Confirm: ${e(REVIEW_ACTION_LABELS[action] || action)}</strong>
      <span>${e(copy)}</span>
      ${needsTurns ? `<div class="mt-14"><label class="section-title mt-0" for="review-additional-turns">Additional turns (max ${e(detail.max_additional_turns_per_continuation)})</label><input id="review-additional-turns" type="number" min="1" max="${e(detail.max_additional_turns_per_continuation)}" value="${e(store.reviewAdditionalTurns)}" class="mono"></div>` : ""}
    </div>
    <div class="approval-actions">
      <button class="button ghost" data-action="review-act-cancel">Cancel</button>
      <button class="button primary" data-action="review-act-confirm" data-review-action="${e(action)}" ${store.busy ? "disabled" : ""}>Confirm →</button>
    </div>
  </div>`;
}

function reviewOperationStorageKey(taskId) {
  return `apoapsis-review-operation-${taskId}`;
}

function reviewGenerateOperationId() {
  const raw = (window.crypto && window.crypto.randomUUID)
    ? window.crypto.randomUUID()
    : `${Date.now()}-${Math.random()}`;
  return `RVOP-${raw.replaceAll("-", "").slice(0, 24).toUpperCase()}`;
}

let reviewPollHandle = null;

async function submitReviewAction(taskId, action) {
  const detail = store.review;
  const additionalTurnsField = document.getElementById("review-additional-turns");
  const additionalTurns = additionalTurnsField ? Number(additionalTurnsField.value) : undefined;
  const operationId = reviewGenerateOperationId();
  const payload = { action, operation_id: operationId, expected_version: detail.task_version };
  if (detail.worktree_fingerprint) payload.expected_worktree_fingerprint = detail.worktree_fingerprint;
  if (additionalTurns !== undefined && !Number.isNaN(additionalTurns)) payload.additional_turns = additionalTurns;

  window.sessionStorage.setItem(
    reviewOperationStorageKey(taskId),
    JSON.stringify({ operationId, action })
  );
  store.busy = true;
  store.error = null;
  store.reviewConfirm = null;
  render();
  try {
    const record = await api(`/api/reviews/${encodeURIComponent(taskId)}/operations`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    store.reviewOperation = record;
    pollReviewOperation(taskId, operationId);
  } catch (error) {
    store.error = error.message;
    window.sessionStorage.removeItem(reviewOperationStorageKey(taskId));
  } finally {
    store.busy = false;
    render();
  }
}

async function pollReviewOperation(taskId, operationId) {
  if (reviewPollHandle) {
    clearTimeout(reviewPollHandle);
    reviewPollHandle = null;
  }
  try {
    const record = await api(
      `/api/reviews/${encodeURIComponent(taskId)}/operations/${encodeURIComponent(operationId)}`
    );
    store.reviewOperation = record;
    render();
    if (record.status === "recorded" || record.status === "running") {
      reviewPollHandle = setTimeout(() => pollReviewOperation(taskId, operationId), 2000);
      return;
    }
    window.sessionStorage.removeItem(reviewOperationStorageKey(taskId));
    if (store.route.name === "review" && store.route.taskId === taskId) {
      store.review = await api(`/api/reviews/${encodeURIComponent(taskId)}`);
      store.reviews = null;
      render();
    }
  } catch (error) {
    store.error = error.message;
    render();
  }
}

function resumePendingReviewOperationPoll(taskId) {
  const raw = window.sessionStorage.getItem(reviewOperationStorageKey(taskId));
  if (!raw) return;
  try {
    const parsed = JSON.parse(raw);
    if (parsed && parsed.operationId) {
      pollReviewOperation(taskId, parsed.operationId);
    }
  } catch (error) {
    window.sessionStorage.removeItem(reviewOperationStorageKey(taskId));
  }
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
  else if (store.route.name === "plan") view = planView();
  else if (store.route.name === "plans") view = plansView();
  else if (store.route.name === "review") view = reviewView();
  else if (store.route.name === "reviews") view = reviewsView();
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

async function approvePlan(button) {
  const planId = button.dataset.planId;
  const version = Number(button.dataset.version);
  store.busy = true;
  store.error = null;
  render();
  try {
    await api(`/api/plans/${encodeURIComponent(planId)}/approve`, {
      method: "POST",
      body: JSON.stringify({ expected_version: version }),
    });
    store.plan = await api(`/api/plans/${encodeURIComponent(planId)}`);
    store.plans = null;
    store.planApprovalPending = false;
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
  if (button.dataset.action === "plan-approve-intent") {
    store.planApprovalPending = true;
    render();
  }
  if (button.dataset.action === "plan-approve-cancel") {
    store.planApprovalPending = false;
    render();
  }
  if (button.dataset.action === "plan-approve-confirm") approvePlan(button);
  if (button.dataset.action === "review-act-intent") {
    store.reviewConfirm = { action: button.dataset.reviewAction };
    render();
  }
  if (button.dataset.action === "review-act-cancel") {
    store.reviewConfirm = null;
    render();
  }
  if (button.dataset.action === "review-act-confirm") {
    submitReviewAction(store.route.taskId, button.dataset.reviewAction);
  }
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
