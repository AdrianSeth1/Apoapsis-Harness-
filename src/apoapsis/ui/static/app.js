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
  planSlice: null,
  planSliceApprovalPending: false,
  reviews: null,
  review: null,
  reviewOperation: null,
  reviewConfirm: null,
  reviewAdditionalTurns: 5,
  intakeOperation: null,
  intakeRequestText: "",
  executionOperation: null,
  executionConfirmPending: false,
  manualFrontierPreviews: null,
  manualFrontierExportPending: false,
  manualFrontierExport: null,
  manualFrontierImportForm: { packageId: "", responseText: "", declaredModelName: "" },
  manualFrontierApprovePendingId: null,
  manualFrontierApplyPreviewId: null,
  discoverSessions: null,
  discoverSession: null,
  discoverOperation: null,
  discoverIdeaText: "",
  discoverAnswerDrafts: {},
  discoverFrontierAnswerDrafts: {},
  discoverTransportChoice: "manual",
  discoverResearchChoice: "auto",
  discoverApiSpendUsd: "1.00",
  discoverManualImportForm: { packageId: "", responseText: "", declaredModelName: "" },
  discoverFrontierExportPaths: null,
  discoverBriefApprovePending: false,
  route: { name: "home" },
  busy: false,
  error: null,
  approvalPending: false,
  planApprovalPending: false,
};

const ROUTE_TITLES = {
  home: "Home",
  new: "Quick change",
  plans: "Plans",
  reviews: "Needs attention",
  discover: "Plan a larger change",
  evaluations: "Evaluations",
  models: "Models & Environment",
};

const TASK_VIEW_TITLES = {
  spec: "Specification",
  control: "Control Room",
  changes: "Changes & Verification",
  review: "Human Review",
  report: "Report & Audit",
};

const PLAN_VIEW_TITLES = {
  overview: "Overview",
  slices: "Implementation Slices",
};

function updateDocumentTitle() {
  const route = store.route;
  let label = "Apoapsis — Verified Coding Harness";
  if (route.name === "task") {
    label = `${TASK_VIEW_TITLES[route.view] || "Task"} · ${route.taskId} — Apoapsis`;
  } else if (route.name === "plan") {
    label = `${PLAN_VIEW_TITLES[route.view] || "Plan"} · ${route.planId} — Apoapsis`;
  } else if (route.name === "planSlice") {
    label = `Slice ${route.sliceId} · ${route.planId} — Apoapsis`;
  } else if (route.name === "review") {
    label = `Human Review · ${route.taskId} — Apoapsis`;
  } else if (route.name === "discoverSession") {
    label = `Discovery Session · ${route.sessionId} — Apoapsis`;
  } else if (ROUTE_TITLES[route.name]) {
    label = `${ROUTE_TITLES[route.name]} — Apoapsis`;
  }
  document.title = label;
}

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
  if (parts[0] === "plan" && parts[1] && parts[2] === "slice" && parts[3]) {
    return {
      name: "planSlice",
      planId: decodeURIComponent(parts[1]),
      sliceId: decodeURIComponent(parts[3]),
    };
  }
  if (parts[0] === "plan" && parts[1]) {
    return { name: "plan", planId: decodeURIComponent(parts[1]), view: parts[2] || "overview" };
  }
  if (parts[0] === "review" && parts[1]) {
    return { name: "review", taskId: decodeURIComponent(parts[1]) };
  }
  if (parts[0] === "discover" && parts[1]) {
    return { name: "discoverSession", sessionId: decodeURIComponent(parts[1]) };
  }
  const allowed = new Set(["home", "new", "evaluations", "models", "plans", "reviews", "discover"]);
  return { name: allowed.has(parts[0]) ? parts[0] : "home" };
}

async function syncRoute() {
  store.route = parseRoute();
  store.error = null;
  store.approvalPending = false;
  store.planApprovalPending = false;
  store.executionConfirmPending = false;
  if (store.route.name !== "review") {
    store.reviewConfirm = null;
  }
  try {
    if (store.route.name === "task") {
      if (!store.task || store.task.task.task_id !== store.route.taskId) {
        store.task = null;
        store.busy = true;
        render();
        store.executionOperation = null;
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
        store.plan = null;
        store.busy = true;
        render();
        store.plan = await api(`/api/plans/${encodeURIComponent(store.route.planId)}`);
      }
    } else if (store.route.name === "planSlice") {
      store.planSlice = null;
      store.busy = true;
      store.planSliceApprovalPending = false;
      render();
      store.planSlice = await api(
        `/api/plans/${encodeURIComponent(store.route.planId)}/slices/${encodeURIComponent(store.route.sliceId)}`
      );
    } else if (store.route.name === "reviews" && store.reviews === null) {
      store.busy = true;
      render();
      store.reviews = await api("/api/reviews");
    } else if (store.route.name === "review") {
      if (!store.review || store.review.task_id !== store.route.taskId) {
        store.review = null;
        store.busy = true;
        render();
        store.reviewOperation = null;
        store.manualFrontierExport = null;
        store.manualFrontierApprovePendingId = null;
        store.manualFrontierApplyPreviewId = null;
        store.review = await api(`/api/reviews/${encodeURIComponent(store.route.taskId)}`);
        store.manualFrontierPreviews = await api(
          `/api/reviews/${encodeURIComponent(store.route.taskId)}/manual-frontier/previews`
        ).catch(() => ({ previews: [] }));
        resumePendingReviewOperationPoll(store.route.taskId);
      }
    } else if (store.route.name === "new") {
      resumePendingIntakeOperationPoll();
    } else if (store.route.name === "discover" && store.discoverSessions === null) {
      store.busy = true;
      render();
      store.discoverSessions = await api("/api/discovery/sessions");
    } else if (store.route.name === "discoverSession") {
      if (!store.discoverSession || store.discoverSession.session.session_id !== store.route.sessionId) {
        store.discoverSession = null;
        store.busy = true;
        render();
        store.discoverOperation = null;
        store.discoverSession = await api(
          `/api/discovery/sessions/${encodeURIComponent(store.route.sessionId)}`
        );
        resumePendingDiscoveryOperationPoll(store.route.sessionId);
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
        <a class="nav-link${active("home")}" href="#/home"><span class="nav-dot"></span><span>Home</span></a>
        <a class="nav-link${active("new")}" href="#/new"><span class="nav-dot"></span><span>Quick change</span></a>
        <a class="nav-link${active("discover") || active("discoverSession")}" href="#/discover"><span class="nav-dot"></span><span>Plan a larger change</span></a>
        <a class="nav-link${active("plans")}" href="#/plans"><span class="nav-dot"></span><span>Plans</span></a>
        <a class="nav-link${active("reviews")}" href="#/reviews"><span class="nav-dot"></span><span>Needs attention</span></a>
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
  if (store.route.name === "discoverSession") return "Plan a larger change";
  return ROUTE_TITLES[store.route.name] || titleCase(store.route.name);
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
        <div><p class="eyebrow">CURRENT PROJECT</p><h1>What would you like to do?</h1><p>This window manages one Git project. Every task, plan, slice, check, and review below belongs to <strong>${e(overview.project.name)}</strong>.</p></div>
      </div>
      <div class="grid three workflow-choices">
        ${journeyChoice("Quick change", "Describe one focused change, approve its specification, then start coding.", "#/new", "Start a quick change →")}
        ${journeyChoice("Larger project", "Clarify an idea, optionally research it, ask a frontier model for a plan, then work through its slices.", "#/discover", "Start planning →")}
        ${journeyChoice("Needs attention", "Continue a task that stopped, including a manual ChatGPT or Claude handoff.", "#/reviews", casesLabel(overview))}
      </div>
      <section class="card hero-card">
        <div>
          <span class="pill ${overview.project.initialized ? "good" : "warn"}">${overview.project.initialized ? "Project ready" : "Initialization required"}</span>
          <h2>${e(overview.project.name)}</h2>
          <p class="mono">${e(overview.project.root)}</p>
          <p>${overview.repository.is_clean === true ? "The Git worktree is clean." : `${overview.repository.changed_files?.length || 0} local path(s) currently differ from HEAD.`} To use another repository, close this window and launch <span class="mono">OPEN_APOAPSIS.cmd "C:\\path\\to\\project"</span>. Run <span class="mono">apoapsis init</span> in that repository once if it has not been initialized.</p>
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
          ${tasks.length ? `<div class="task-list">${tasks.slice(0, 8).map(taskRow).join("")}</div>` : emptyState("No tasks yet", "Choose Quick change for one focused request, or Larger project when the work should be planned as slices first.")}
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

function journeyChoice(title, description, href, action) {
  return `<a class="card card-pad journey-choice" href="${href}"><h2>${e(title)}</h2><p>${e(description)}</p><strong class="orange">${e(action)}</strong></a>`;
}

function casesLabel(overview) {
  const waiting = (overview.tasks || []).filter((task) => task.state === "HUMAN_REVIEW_REQUIRED").length;
  return waiting ? `${waiting} waiting →` : "Open review queue →";
}

function workflowSteps(items, currentIndex) {
  return `<ol class="workflow-steps" aria-label="Workflow progress">${items.map((item, index) => `<li class="${index < currentIndex ? "done" : index === currentIndex ? "current" : "upcoming"}"><span>${index + 1}</span><strong>${e(item)}</strong></li>`).join("")}</ol>`;
}

function taskRow(task) {
  return `<a class="task-row" href="#/task/${encodeURIComponent(task.task_id)}/spec">
    <div class="task-main"><strong>${e(task.objective)}</strong><span>${e(task.task_id)} · v${e(task.version)}</span></div>
    <span class="pill ${statusClass(task.state)}">${e(titleCase(task.state))}</span>
    <span class="meta">${e(formatDate(task.updated_at))}</span><span class="arrow">→</span>
  </a>`;
}

function newTaskView() {
  const op = store.intakeOperation;
  if (!op) return newTaskFormView();
  if (op.status === "recorded" || op.status === "running") return newTaskRunningView(op);
  if (op.status === "pending_specification_approval") return newTaskDraftedView(op);
  return newTaskFailedView(op);
}

function newTaskFormView() {
  return `<main class="content narrow">
    <div class="page-heading"><div><p class="eyebrow">QUICK CHANGE</p><h1>Describe the outcome.</h1><p>This path is for one focused change. You will review the proposed specification before anything edits the project.</p></div></div>
    ${workflowSteps(["Describe", "Approve specification", "Start coding", "Verify or review"], 0)}
    <section class="card">
      <div class="card-body">
        <label class="section-title" for="intake-request">Natural-language request</label>
        <textarea id="intake-request" class="mono" rows="6" placeholder="Add resumable downloads without changing the public API.">${e(store.intakeRequestText)}</textarea>
        <div class="notice mt-16">This runs a durable, crash-safe operation: you can close this tab or lose the connection while extraction runs and come back to see the result. A second, bounded correction attempt is made automatically if the first response is invalid -- never more than one.</div>
        <div class="flex-end mt-18"><button class="button primary" data-action="intake-submit" ${store.busy ? "disabled" : ""}>Extract specification →</button></div>
      </div>
    </section>
  </main>`;
}

function newTaskRunningView(op) {
  const stage = INTAKE_OPERATION_STAGE[op.status] || { label: op.status, pill: "warn" };
  return `<main class="content narrow">
    <div class="page-heading"><div><p class="eyebrow">QUICK CHANGE / ${e(op.task_id)}</p><h1>Drafting the specification.</h1><p>A background worker is drafting the scope now. It is safe to close this tab; progress is persisted.</p></div><span class="pill ${stage.pill}">${e(stage.label)}</span></div>
    ${workflowSteps(["Describe", "Approve specification", "Start coding", "Verify or review"], 0)}
    <section class="card card-pad">
      <div class="constraint-head"><span class="constraint-id">OPERATION ${e(op.operation_id)}</span><span class="pill ${stage.pill}">${e(stage.label)}</span></div>
      <p class="muted mt-14">${e(op.result_summary || "Waiting for the background worker to run this operation.")}</p>
    </section>
  </main>`;
}

function newTaskFailedView(op) {
  const stage = INTAKE_OPERATION_STAGE[op.status] || { label: op.status, pill: "bad" };
  const ambiguous = op.status === "ambiguous";
  return `<main class="content narrow">
    <div class="page-heading"><div><p class="eyebrow">NEW TASK / ${e(op.task_id)}</p><h1>${ambiguous ? "Outcome uncertain." : "Extraction failed."}</h1><p>${ambiguous
      ? "The process running this operation may have crashed. Whether the extraction call was transmitted before that happened is unknown -- it was never automatically repeated."
      : "Both the original attempt and its one bounded correction failed validation. The task stopped deterministically; nothing was retried a third time."}</p></div><span class="pill ${stage.pill}">${e(stage.label)}</span></div>
    <section class="card card-pad">
      <p class="muted">${e(op.error || op.result_summary || "No further detail was recorded.")}</p>
      ${ambiguous ? `<div class="notice mt-14">Check the <a href="#/reviews">Human review queue</a> for <span class="mono">${e(op.task_id)}</span> -- a stranded task is returned there automatically so it can be inspected and abandoned.</div>` : ""}
    </section>
    <div class="flex-end mt-18"><button class="button ghost" data-action="intake-reset">Start over</button></div>
  </main>`;
}

function newTaskDraftedView(op) {
  const task = store.task && store.task.task.task_id === op.task_id ? store.task.task : null;
  const spec = task?.specification;
  const correctionAttempted = (op.audit_artifact_locations || []).some(
    (path) => path.includes("specification-extraction-failure-")
  );
  return `<main class="content narrow">
    <div class="page-heading"><div><p class="eyebrow">QUICK CHANGE / ${e(op.task_id)}</p><h1>Review the scope.</h1><p>The model proposed a specification but has not edited the project. Approve it, then start coding as a separate step.</p></div><span class="pill good">Pending approval</span></div>
    ${workflowSteps(["Describe", "Approve specification", "Start coding", "Verify or review"], 1)}
    <section class="card card-pad">
      ${spec ? `<p class="section-title mt-0">Objective</p><p class="objective">${e(spec.objective.text)}</p>` : ""}
      <div class="grid two mt-14">
        ${metric("Provider role", titleCase(op.provider_role), "Recorded on the operation")}
        ${metric("Extraction attempts", correctionAttempted ? "2 (one bounded correction)" : "1", "ADR 0018's one-correction contract")}
        ${spec ? metric("Hard constraints", spec.hard_constraints.length, "Exact verbatim wording") : ""}
        ${spec ? metric("Acceptance criteria", spec.acceptance_criteria.length, `Risk: ${titleCase(spec.risk_level)}`) : ""}
      </div>
      <p class="section-title">Audit artifacts · ${(op.audit_artifact_locations || []).length}</p>
      <div class="artifact-list">${(op.audit_artifact_locations || []).map((artifact) => `<div class="artifact-item"><code>${e(artifact)}</code></div>`).join("") || `<p class="muted">No artifacts were recorded.</p>`}</div>
    </section>
    <div class="approval-bar">
      <div><strong>Ready for review</strong><span>Constraints, acceptance criteria, and the two-step approval action live on the task page -- the same approval every other task-creation path already uses.</span></div>
      <div class="approval-actions">
        <button class="button ghost" data-action="intake-reset">Start another request</button>
        <a class="button primary" href="#/task/${encodeURIComponent(op.task_id)}/spec">Review & approve →</a>
      </div>
    </div>
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
    <div class="page-heading"><div><p class="eyebrow">LARGER PROJECTS / PLANS</p><h1>Approved work,<br>split into slices.</h1><p>Start in <a href="#/discover">Plan a larger change</a>. A local model clarifies the idea, optional Research can inform it, and either an API or a manual ChatGPT or Claude handoff proposes the plan. You still review and approve every plan and slice.</p></div><span class="pill ${plans.length ? "good" : "warn"}">${plans.length ? `${plans.length} recorded` : "No plans yet"}</span></div>
    <section class="card">
      ${plans.length ? `<div class="task-list">${plans.map(planRow).join("")}</div>` : `<div class="empty"><h2>No plans yet</h2><p>Begin with the guided planning flow. It supports both API access and a normal ChatGPT or Claude subscription.</p><a class="button primary" href="#/discover">Plan a larger change →</a></div>`}
    </section>
  </main>`;
}

function planRow(plan) {
  return `<a class="task-row" href="#/plan/${encodeURIComponent(plan.plan_id)}/overview">
    <div class="task-main"><strong>${e(plan.idea_text)}</strong><span>${e(plan.plan_id)} · v${e(plan.version)} · ${e(plan.slice_count)} slice(s)</span></div>
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
          <h1>${e(record.idea_text)}</h1>
          <p>${e(titleCase(record.status))} · ${e(record.plan.slices.length)} SLICES · UPDATED ${e(formatDate(record.updated_at))}</p>
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
  const statusById = new Map((detail.slices || []).map((item) => [item.slice_id, item]));
  return `<main class="content">
    <div class="page-heading"><div><p class="eyebrow">IMPLEMENTATION SLICES / ONE AT A TIME</p><h1>Work through the plan.</h1><p>Open a Ready slice, package and approve it, then start coding. If it finishes, commit and merge that slice's work into this project before starting a dependent slice. Apoapsis checks that dependency from Git; it never merges automatically.</p></div><span class="pill purple">${orderedSlices.length} slice(s)</span></div>
    ${orderedSlices.length ? orderedSlices.map((slice) => sliceCard(slice, record.plan_id, statusById.get(slice.slice_id))).join("") : emptyState("No slices in this plan", "The imported plan did not include any implementation slices.")}
  </main>`;
}

function sliceRiskClass(risk) {
  const normalized = String(risk || "unclassified").toLowerCase();
  if (normalized === "low") return "good";
  if (normalized === "medium") return "warn";
  if (normalized === "high" || normalized === "critical") return "bad";
  return "purple";
}

const SLICE_STATUS_LABELS = {
  ready_or_blocked: "Not started",
  packaged: "Packaged",
  approved: "Approved",
  running: "Running",
  complete: "Complete",
  human_review: "Human review",
  failed: "Failed",
  superseded: "Superseded",
};

function sliceStatusClass(status) {
  if (status === "complete") return "good";
  if (status === "running" || status === "approved") return "purple";
  if (status === "packaged") return "warn";
  if (status === "failed" || status === "superseded") return "bad";
  if (status === "human_review") return "warn";
  return "purple";
}

function sliceStatusLabel(statusEntry) {
  const status = statusEntry?.status || "ready_or_blocked";
  if (status === "ready_or_blocked" && statusEntry?.readiness) {
    return statusEntry.readiness.ready ? "Ready" : "Waiting for dependencies";
  }
  return SLICE_STATUS_LABELS[status] || titleCase(status);
}

function sliceCard(slice, planId, statusEntry) {
  const status = statusEntry?.status || "ready_or_blocked";
  const actionLabel = status === "human_review" ? "Resolve stopped slice →" : status === "approved" ? "Start coding →" : status === "complete" ? "View completed slice →" : "Open slice →";
  return `<article class="card card-pad mt-16">
    <div class="constraint-head">
      <span class="constraint-id">${e(slice.slice_id)}</span>
      <span class="pill ${sliceRiskClass(slice.risk_level)}">${e(titleCase(slice.risk_level || "unclassified"))} risk</span>
      <span class="pill ${sliceStatusClass(status)}">${e(sliceStatusLabel(statusEntry))}</span>
      <a class="button ${status === "human_review" || status === "approved" ? "primary" : "ghost"}" href="#/plan/${encodeURIComponent(planId)}/slice/${encodeURIComponent(slice.slice_id)}">${e(actionLabel)}</a>
    </div>
    <h3>${e(slice.title)}</h3>
    <p class="objective">${e(slice.objective)}</p>
    <p class="meta">${(slice.dependencies || []).length ? `WAITING ON: ${(slice.dependencies || []).map((item) => e(item)).join(", ")}` : "NO PREREQUISITE SLICES"} · CHECK: ${(slice.verification_commands || []).map((item) => e(item)).join(", ") || "not named"}</p>
    <details class="mt-14"><summary>Show scope, constraints, and technical details</summary>
      <div class="grid two mt-14"><div><p class="section-title mt-0">Exclusions</p>${(slice.exclusions || []).length ? `<ul>${slice.exclusions.map((item) => `<li>${e(item)}</li>`).join("")}</ul>` : `<p class="muted">None recorded.</p>`}<p class="section-title">Constraints / criteria</p><p class="mono">${[...(slice.inherited_constraint_ids || []), ...(slice.acceptance_criterion_ids || [])].map((item) => e(item)).join(", ") || "None recorded."}</p></div><div><p class="section-title mt-0">Suggested paths</p><p class="mono">${(slice.suggested_paths || []).map((item) => e(item)).join(", ") || "None suggested."}</p><p class="section-title">Stop conditions</p>${(slice.stop_conditions || []).length ? `<ul>${slice.stop_conditions.map((item) => `<li>${e(item)}</li>`).join("")}</ul>` : `<p class="muted">None recorded.</p>`}</div></div>
      <p class="section-title">Why it should fit the local model</p><blockquote>${e(slice.local_model_fit_rationale)}</blockquote><p class="section-title">Full work brief</p><blockquote>${e(slice.work_brief)}</blockquote>
    </details>
  </article>`;
}

function planSliceView() {
  if (!store.planSlice) return loadingView();
  const detail = store.planSlice;
  const status = detail.status;
  const slice = detail.slice;
  const pkg = detail.package;
  const task = detail.task;
  return `<main class="content narrow">
    <p><a href="#/plan/${encodeURIComponent(detail.plan_id)}/slices">← Back to implementation slices</a></p>
    <div class="page-heading">
      <div><p class="eyebrow">PLAN SLICE / ${e(detail.plan_id)}</p><h1>${e(slice.title)}</h1><p>${e(slice.objective)}</p></div>
      <span class="pill ${sliceStatusClass(status.status)}">${e(sliceStatusLabel(status))}</span>
    </div>
    ${sliceDependencySection(slice, pkg)}
    ${pkg ? slicePackagePreview(pkg) : slicePackageActionPanel(detail)}
    ${pkg && status.status === "packaged" ? sliceApproveActionPanel(detail) : ""}
    ${task ? sliceTaskLinksSection(detail) : ""}
  </main>`;
}

function sliceDependencySection(slice, pkg) {
  const dependencies = slice.dependencies || [];
  if (!dependencies.length) return "";
  const evidence = pkg?.dependency_evidence || [];
  return `<section class="card card-pad mt-16">
    <p class="section-title mt-0">Dependency evidence · ${dependencies.length}</p>
    ${evidence.length
      ? `<div class="verification-list">${evidence.map(sliceDependencyEvidenceItem).join("")}</div>`
      : `<p class="muted">Depends on ${dependencies.map((item) => e(item)).join(", ")}. Package this slice to compute real, git-proven dependency evidence -- reaching COMPLETE alone is never enough; a dependency's work must actually be committed and merged into the current repository first.</p>`}
  </section>`;
}

function sliceDependencyEvidenceItem(item) {
  return `<div class="verification-item"><div><strong>${e(item.slice_id)}</strong><p>${e(item.reason)}</p>${item.dependency_branch ? `<p class="mono">${e(item.dependency_branch)}</p>` : ""}</div><span class="pill ${item.satisfied ? "good" : "bad"}">${item.satisfied ? "Satisfied" : "Not satisfied"}</span></div>`;
}

function slicePackageActionPanel(detail) {
  return `<section class="card card-pad mt-16">
    <p class="section-title mt-0">Package this slice</p>
    <p class="muted">Deterministically compiles an immutable record of exactly what approving this slice would authorize -- the exact inherited hard constraints and acceptance criteria, configured verification commands, and dependency evidence. No model call, no task created yet.</p>
    <button class="button primary" data-action="slice-package" data-plan-id="${e(detail.plan_id)}" data-slice-id="${e(detail.slice_id)}" data-plan-version="${e(detail.plan_version)}" ${store.busy ? "disabled" : ""}>Package this slice →</button>
  </section>`;
}

function slicePackagePreview(pkg) {
  const criteria = pkg.acceptance_criteria || [];
  const constraints = pkg.inherited_hard_constraints || [];
  return `<section class="card card-pad mt-16">
    <p class="section-title mt-0">Immutable package · ${e(pkg.package_id)}</p>
    <div class="mono">HASH ${e(pkg.package_sha256)}</div>
    <div class="mt-14 mono">REPOSITORY: ${e(pkg.repository_root)}</div>
    <div class="mono">HEAD ${e(pkg.repository_head_commit)} · FINGERPRINT ${e(pkg.repository_fingerprint)}</div>
    <p class="section-title">Exclusions</p>
    ${(pkg.exclusions || []).length ? `<ul>${pkg.exclusions.map((item) => `<li>${e(item)}</li>`).join("")}</ul>` : `<p class="muted">None recorded.</p>`}
    <p class="section-title">Interface contracts</p>
    ${(pkg.interface_contracts || []).length ? `<ul>${pkg.interface_contracts.map((item) => `<li class="mono">${e(item)}</li>`).join("")}</ul>` : `<p class="muted">None recorded.</p>`}
    <p class="section-title">Inherited hard constraints · ${constraints.length}</p>
    ${constraints.length ? constraints.map(constraintCard).join("") : `<p class="muted">None inherited.</p>`}
    <p class="section-title">Acceptance criteria · ${criteria.length}</p>
    ${criteria.length ? criteria.map(sliceCriterionCard).join("") : `<p class="muted">None inherited.</p>`}
    <p class="section-title">Configured verification commands</p>
    <p class="mono">${(pkg.verification_commands || []).map((item) => e(item)).join(", ") || "None named."}</p>
    <p class="section-title">Advisory (hints for the local coding model, never a filesystem allowlist)</p>
    <div class="file-item"><span>Suggested paths</span><code>${e((pkg.advisory_suggested_paths || []).join(", ") || "none")}</code></div>
    <div class="file-item"><span>Suggested symbols</span><code>${e((pkg.advisory_suggested_symbols || []).join(", ") || "none")}</code></div>
    <div class="file-item"><span>Context seeds</span><code>${e((pkg.advisory_context_seeds || []).join(", ") || "none")}</code></div>
  </section>`;
}

function sliceCriterionCard(criterion) {
  return `<article class="constraint"><div class="constraint-head"><span class="constraint-id">${e(criterion.id)}</span><span class="pill ${criterion.status === "active" ? "good" : "warn"}">${e(criterion.status)}</span></div><blockquote>${e(criterion.text)}</blockquote>${criterion.verification_method ? `<div class="interpretation"><span class="meta">VERIFY: ${e(criterion.verification_method)}</span></div>` : ""}</article>`;
}

function sliceApproveActionPanel(detail) {
  const pkg = detail.package;
  if (!store.planSliceApprovalPending) {
    return `<div class="approval-bar mt-16"><div><strong>Ready for approval</strong><span>Approving authorizes exactly the package above. This creates the derived task but does not start it -- starting is always a separate, later action.</span></div><div class="approval-actions"><button class="button primary" data-action="slice-approve-intent">Approve this slice →</button></div></div>`;
  }
  return `<div class="approval-bar mt-16">
    <div>
      <strong>Confirm: approve this slice</strong>
      <span>This creates a real task from the exact package above, through the normal specification-approval transitions. Nothing executes yet.</span>
      <div class="mt-14 mono">PACKAGE HASH: ${e(pkg.package_sha256)}</div>
    </div>
    <div class="approval-actions">
      <button class="button ghost" data-action="slice-approve-cancel">Cancel</button>
      <button class="button primary" data-action="slice-approve-confirm" data-plan-id="${e(detail.plan_id)}" data-slice-id="${e(detail.slice_id)}" data-package-sha256="${e(pkg.package_sha256)}" ${store.busy ? "disabled" : ""}>Confirm approval →</button>
    </div>
  </div>`;
}

function sliceTaskLinksSection(detail) {
  const task = detail.task;
  const taskRecord = task.task;
  const taskId = taskRecord.task_id;
  const preview = task.execution_preview;
  const humanReview = taskRecord.state === "HUMAN_REVIEW_REQUIRED";
  const stopped = taskRecord.state === "HUMAN_REVIEW_REQUIRED";
  const complete = taskRecord.state === "COMPLETE";
  const primaryHref = stopped ? `#/review/${encodeURIComponent(taskId)}` : `#/task/${encodeURIComponent(taskId)}/control`;
  const primaryLabel = stopped ? "Open recovery options →" : complete ? "View completed task →" : "Open control room →";
  const stateExplanation = stopped
    ? "Coding stopped and needs your decision. Open recovery options to continue locally, retry verification, or create a manual ChatGPT or Claude handoff."
    : complete
      ? "This slice passed its configured completion checks. Commit its worktree changes and merge that branch into the project before starting any dependent slice."
      : "This approved slice is ready to use the normal coding control room.";
  return `<section class="card card-pad mt-16">
    <p class="section-title mt-0">Derived task · ${e(taskId)}</p>
    <p class="muted">${e(stateExplanation)}</p>
    ${preview ? `<div class="mt-14 mono">PREDICTED ROUTE: ${e(preview.predicted_route || "n/a")} · LOCAL MODEL: ${e(preview.local_model || "unknown")}${preview.frontier_available ? ` · FRONTIER MODEL: ${e(preview.frontier_model || "unknown")}` : " · FRONTIER: not configured"}</div>
    <div class="mono">COMPLETION POLICY: ${e(preview.completion_policy)} · SANDBOX: ${e(preview.verification_backend)}</div>` : ""}
    <div class="approval-actions mt-14">
      <a class="button primary" href="${primaryHref}">${e(primaryLabel)}</a>
      <a class="button ghost" href="#/task/${encodeURIComponent(taskId)}/changes">Changes & verification</a>
      <a class="button ghost" href="#/task/${encodeURIComponent(taskId)}/report">Report & audit</a>
      ${humanReview ? `<a class="button ghost" href="#/review/${encodeURIComponent(taskId)}">Open Human Review case →</a>` : ""}
    </div>
  </section>`;
}

const REVIEW_ACTION_LABELS = {
  inspect_only: "Inspect only",
  abandon: "Abandon & roll back",
  verification_only_retry: "Retry verification",
  local_continuation: "Continue locally",
  frontier_continuation: "Continue with frontier",
  authorize_frontier_stage: "Authorize a fresh frontier stage",
};

const REVIEW_OPERATION_STAGE = {
  recorded: { label: "Recorded", pill: "warn" },
  running: { label: "Running", pill: "purple" },
  succeeded: { label: "Succeeded", pill: "good" },
  failed: { label: "Failed", pill: "bad" },
};

const INTAKE_OPERATION_STAGE = {
  recorded: { label: "Recorded", pill: "warn" },
  running: { label: "Extracting", pill: "purple" },
  pending_specification_approval: { label: "Specification drafted", pill: "good" },
  failed: { label: "Failed", pill: "bad" },
  ambiguous: { label: "Ambiguous", pill: "bad" },
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
      ${metric("Frontier", detail.frontier_available ? (detail.frontier_model || "Configured") : "Not configured", "Checked fresh, not from the original stop")}
    </div>

    ${reviewOperationPanel()}

    <p class="section-title">What can I do next?</p>
    <section class="card card-pad">${reviewActionPanel(detail, eligible)}</section>

    ${manualFrontierSection(detail, eligible)}

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

    <details class="card card-pad mt-16"><summary>Audit artifacts · ${(detail.audit_artifact_locations || []).length}</summary>${(detail.audit_artifact_locations || []).length ? `<div class="artifact-list mt-14">${detail.audit_artifact_locations.map((artifact) => `<div class="artifact-item"><code>${e(artifact)}</code></div>`).join("")}</div>` : `<p class="muted">No artifacts were discovered.</p>`}</details>
  </main>`;
}

// ---- Manual subscription-based frontier coding handoff (ADR 0031, 0033) ----

function manualFrontierSection(detail, eligible) {
  const previews = store.manualFrontierPreviews?.previews || [];
  const eligibleForHandoff = eligible.includes("manual_frontier_handoff");
  if (!eligibleForHandoff && !previews.length) return "";
  return `
    <p class="section-title">Continue with ChatGPT or Claude</p>
    <section class="card card-pad">
      <p class="muted">Use a normal ChatGPT or Claude subscription without API credentials. Apoapsis never signs in for you: download one exact file, upload it yourself, then paste the model's response back here.</p>
      ${workflowSteps(["Export file", "Upload to ChatGPT or Claude", "Paste response", "Review, apply, and verify"], store.manualFrontierExport ? 2 : 0)}
      ${eligibleForHandoff ? manualFrontierExportPanel(detail) : `<div class="notice mt-14">Manual handoff is not currently eligible for this task${previews.length ? " -- showing prior previews below." : "."}</div>`}
      ${manualFrontierImportPanel(detail)}
      ${previews.length ? manualFrontierPreviewList(detail, previews) : ""}
    </section>`;
}

function manualFrontierExportPanel(detail) {
  const exported = store.manualFrontierExport;
  return `
    <div class="mt-14">
      <button class="button primary" data-action="manual-frontier-export" ${store.busy ? "disabled" : ""}>${store.manualFrontierExportPending ? "Exporting…" : "Export handoff package →"}</button>
    </div>
    ${exported ? manualFrontierExportResult(exported) : ""}`;
}

function manualFrontierExportResult(exported) {
  const pkg = exported.package;
  return `<div class="notice mt-14">
    <strong>Upload this file to ChatGPT or Claude:</strong>
    <div class="mt-14 file-item"><span>File to upload</span><code>${e(exported.markdown_artifact_absolute_path)}</code><button class="button ghost" data-action="copy-path" data-copy-path="${e(exported.markdown_artifact_absolute_path)}">Copy path</button></div>
    <div class="file-item"><span>Canonical package (JSON)</span><code>${e(exported.package_artifact_absolute_path)}</code></div>
    <div class="mono mt-14">PACKAGE ID: ${e(pkg.package_id)} · HASH: ${e(pkg.package_sha256)} · REPAIR ROUND: ${e(pkg.repair_round)}</div>
    <p class="mt-14">Ask the model to return <strong>only</strong> the JSON response object the file describes, then paste it below (or upload the saved response file) once you have it.</p>
  </div>`;
}

function manualFrontierImportPanel(detail) {
  const form = store.manualFrontierImportForm;
  return `<div class="mt-18">
    <p class="section-title mt-0">Paste the response, or upload it as a file</p>
    <div class="grid two">
      <div>
        <label class="section-title mt-0" for="mf-package-id">Package ID</label>
        <input id="mf-package-id" class="mono" type="text" placeholder="MFH-..." value="${e(form.packageId)}">
        <label class="section-title" for="mf-declared-model">Declared subscription model (operator-provided, unverified)</label>
        <input id="mf-declared-model" class="mono" type="text" placeholder="claude-opus-4.6-web" value="${e(form.declaredModelName)}">
      </div>
      <div>
        <label class="section-title mt-0" for="mf-response-text">Pasted response JSON</label>
        <textarea id="mf-response-text" class="mono" rows="6" placeholder="{...}">${e(form.responseText)}</textarea>
        <input id="mf-response-file" type="file" accept=".json,.txt,application/json,text/plain" class="mt-14">
      </div>
    </div>
    <div class="flex-end mt-16"><button class="button primary" data-action="manual-frontier-import" ${store.busy ? "disabled" : ""}>Validate & preview →</button></div>
  </div>`;
}

const MANUAL_FRONTIER_PREVIEW_STATUS = {
  previewed: { label: "Previewed", pill: "warn" },
  approved: { label: "Approved -- ready to apply", pill: "purple" },
  applied: { label: "Applied", pill: "good" },
  superseded: { label: "Superseded", pill: "bad" },
};

function manualFrontierPreviewList(detail, previews) {
  return `<p class="section-title">Imported previews · ${previews.length}</p>
    <div class="verification-list">${previews.slice().reverse().map((preview) => manualFrontierPreviewItem(detail, preview)).join("")}</div>`;
}

function manualFrontierPreviewItem(detail, preview) {
  const stage = MANUAL_FRONTIER_PREVIEW_STATUS[preview.status] || { label: preview.status, pill: "warn" };
  const canApprove = preview.status === "previewed";
  const canApply = preview.status === "approved";
  const pendingApprove = store.manualFrontierApprovePendingId === preview.preview_id;
  const pendingApply = store.manualFrontierApplyPreviewId === preview.preview_id;
  return `<div class="verification-item">
    <div style="flex:1">
      <strong>${e(preview.preview_id)}</strong>
      <p class="mono">DECLARED MODEL (operator-declared, unverified): ${e(preview.declared_model_name)}</p>
      <p>${e(preview.summary || "(no summary provided)")}</p>
      <p class="meta">${e(preview.files_changed.length)} file(s) · ${e(preview.changed_lines)} changed line(s) · TOKENS/COST: UNMEASURED</p>
      ${preview.patch ? `<details class="mt-14"><summary>Patch preview</summary><div class="mono" style="white-space: pre-wrap; overflow-wrap: anywhere;">${e(preview.patch)}</div></details>` : ""}
      ${canApprove ? (pendingApprove
        ? `<div class="approval-actions mt-14"><button class="button ghost" data-action="manual-frontier-approve-cancel">Cancel</button><button class="button primary" data-action="manual-frontier-approve-confirm" data-preview-id="${e(preview.preview_id)}" data-task-version="${e(detail.task_version)}" ${store.busy ? "disabled" : ""}>Confirm approval →</button></div>`
        : `<div class="mt-14"><button class="button primary" data-action="manual-frontier-approve-intent" data-preview-id="${e(preview.preview_id)}">Approve preview (step 1 of 2) →</button></div>`) : ""}
      ${canApply ? (pendingApply
        ? `<div class="approval-actions mt-14"><button class="button ghost" data-action="manual-frontier-apply-cancel">Cancel</button><button class="button primary" data-action="manual-frontier-apply-confirm" data-preview-id="${e(preview.preview_id)}" ${store.busy ? "disabled" : ""}>Confirm apply & verify →</button></div>`
        : `<div class="mt-14"><button class="button primary" data-action="manual-frontier-apply-intent" data-preview-id="${e(preview.preview_id)}">Apply & verify (step 2 of 2) →</button></div>`) : ""}
    </div>
    <span class="pill ${stage.pill}">${e(stage.label)}</span>
  </div>`;
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
  // manual_frontier_handoff has its own dedicated, fully self-contained
  // section below (export/import/approve/apply) -- showing it again here
  // as a generic action card would just duplicate that section with a
  // raw, un-humanized label.
  const genericEligible = eligible.filter((action) => action !== "manual_frontier_handoff");
  if (!genericEligible.length) {
    return `<p class="muted">No actions are currently eligible for this task${eligible.includes("manual_frontier_handoff") ? " other than the manual frontier handoff below" : ""}.</p>`;
  }
  if (store.reviewConfirm) {
    return reviewConfirmPanel(detail, store.reviewConfirm.action);
  }
  return `<div class="grid two">${genericEligible.map((action) => reviewActionButton(action)).join("")}</div>`;
}

function reviewActionButton(action) {
  const label = REVIEW_ACTION_LABELS[action] || action;
  const description = {
    inspect_only: "No mutation -- just records that a human looked at this case.",
    abandon: "Cleans up the worktree (if one exists) and marks the task rolled back.",
    verification_only_retry: "Re-runs configured verification against the current worktree. No model is called.",
    local_continuation: "Resumes the local coding agent with additional authorized turns.",
    frontier_continuation: "Resumes the frontier coding agent with additional authorized turns.",
    authorize_frontier_stage: "Starts a brand-new frontier coding-agent stage using the local session's diff, failures, and full configured frontier budget. Never launches automatically.",
  }[action] || "";
  return `<article class="card card-pad"><h3>${e(label)}</h3><p class="muted">${e(description)}</p><button class="button ${action === "inspect_only" ? "ghost" : "primary"}" data-action="review-act-intent" data-review-action="${e(action)}">${e(label)} →</button></article>`;
}

function reviewConfirmPanel(detail, action) {
  const needsTurns = action === "local_continuation" || action === "frontier_continuation";
  const isFrontierStage = action === "authorize_frontier_stage";
  const frontierBudget = detail.configured_frontier_budget;
  const copy = {
    inspect_only: "Records that a human explicitly reviewed this case. No other state changes.",
    abandon: "This cleans up the worktree (if one exists) and marks the task ROLLED_BACK. This cannot be undone.",
    verification_only_retry: "Re-runs configured verification against the current worktree right now. No model is called.",
    local_continuation: "Resumes the local coding agent from exactly where it stopped, with the authorized additional turns. This calls a model.",
    frontier_continuation: "Resumes the frontier coding agent from exactly where it stopped, with the authorized additional turns. This calls a hosted/frontier model and may incur cost.",
    authorize_frontier_stage: "Starts a fresh frontier coding-agent stage from the local session's exact diff and failures. This is a new session, not a continuation, and calls a hosted/frontier model -- it may incur cost.",
  }[action] || "";
  return `<div class="approval-bar">
    <div>
      <strong>Confirm: ${e(REVIEW_ACTION_LABELS[action] || action)}</strong>
      <span>${e(copy)}</span>
      ${isFrontierStage ? `<div class="mt-14 mono">MODEL: ${e(detail.frontier_model || "unknown")} · BUDGET: ${e(frontierBudget?.max_turns ?? "—")} turns / ${e(frontierBudget?.max_patch_attempts ?? "—")} patch attempts / ${e(frontierBudget?.max_verification_runs ?? "—")} verify runs</div>` : ""}
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

const INTAKE_OPERATION_STORAGE_KEY = "apoapsis-intake-operation";

function intakeGenerateOperationId() {
  const raw = (window.crypto && window.crypto.randomUUID)
    ? window.crypto.randomUUID()
    : `${Date.now()}-${Math.random()}`;
  return `INOP-${raw.replaceAll("-", "").slice(0, 24).toUpperCase()}`;
}

async function submitIntakeOperation() {
  const field = document.getElementById("intake-request");
  const requestText = (field ? field.value : store.intakeRequestText || "").trim();
  if (!requestText) {
    store.error = "Describe the outcome before extracting a specification.";
    render();
    return;
  }
  const operationId = intakeGenerateOperationId();
  window.sessionStorage.setItem(INTAKE_OPERATION_STORAGE_KEY, JSON.stringify({ operationId }));
  store.intakeRequestText = requestText;
  store.busy = true;
  store.error = null;
  render();
  try {
    const record = await api("/api/intake/operations", {
      method: "POST",
      body: JSON.stringify({ request_text: requestText, operation_id: operationId }),
    });
    store.intakeOperation = record;
    pollIntakeOperation(operationId);
  } catch (error) {
    store.error = error.message;
    window.sessionStorage.removeItem(INTAKE_OPERATION_STORAGE_KEY);
  } finally {
    store.busy = false;
    render();
  }
}

let intakePollHandle = null;

async function pollIntakeOperation(operationId) {
  if (intakePollHandle) {
    clearTimeout(intakePollHandle);
    intakePollHandle = null;
  }
  try {
    const record = await api(`/api/intake/operations/${encodeURIComponent(operationId)}`);
    store.intakeOperation = record;
    if (record.status === "recorded" || record.status === "running") {
      render();
      intakePollHandle = setTimeout(() => pollIntakeOperation(operationId), 2000);
      return;
    }
    window.sessionStorage.removeItem(INTAKE_OPERATION_STORAGE_KEY);
    if (record.status === "pending_specification_approval") {
      store.task = await api(`/api/tasks/${encodeURIComponent(record.task_id)}`);
    }
    render();
  } catch (error) {
    store.error = error.message;
    render();
  }
}

function resumePendingIntakeOperationPoll() {
  if (store.intakeOperation) return;
  const raw = window.sessionStorage.getItem(INTAKE_OPERATION_STORAGE_KEY);
  if (!raw) return;
  try {
    const parsed = JSON.parse(raw);
    if (parsed && parsed.operationId) {
      pollIntakeOperation(parsed.operationId);
    }
  } catch (error) {
    window.sessionStorage.removeItem(INTAKE_OPERATION_STORAGE_KEY);
  }
}

function executionGenerateOperationId() {
  const raw = (window.crypto && window.crypto.randomUUID)
    ? window.crypto.randomUUID()
    : `${Date.now()}-${Math.random()}`;
  return `EXOP-${raw.replaceAll("-", "").slice(0, 24).toUpperCase()}`;
}

async function submitExecutionStart(taskId, version, authorizationSha256) {
  const operationId = executionGenerateOperationId();
  store.busy = true;
  store.error = null;
  store.executionConfirmPending = false;
  render();
  try {
    const record = await api(`/api/tasks/${encodeURIComponent(taskId)}/execute`, {
      method: "POST",
      body: JSON.stringify({
        operation_id: operationId,
        expected_version: version,
        expected_authorization_sha256: authorizationSha256,
      }),
    });
    store.executionOperation = record;
    pollExecutionOperation(taskId, operationId);
  } catch (error) {
    store.error = error.message;
  } finally {
    store.busy = false;
    render();
  }
}

let executionPollHandle = null;

async function pollExecutionOperation(taskId, operationId) {
  if (executionPollHandle) {
    clearTimeout(executionPollHandle);
    executionPollHandle = null;
  }
  try {
    const record = await api(
      `/api/execution/operations/${encodeURIComponent(operationId)}`
    );
    store.executionOperation = record;
    // Refresh persisted task events and recent agent turns on every tick,
    // not only once the operation reaches a terminal status -- otherwise
    // the control room's timeline and turn feed stay frozen at whatever
    // they were when the page loaded for the entire RUNNING duration.
    if (store.route.name === "task" && store.route.taskId === taskId) {
      store.task = await api(`/api/tasks/${encodeURIComponent(taskId)}`);
    }
    render();
    if (record.status === "recorded" || record.status === "running") {
      executionPollHandle = setTimeout(() => pollExecutionOperation(taskId, operationId), 2000);
    }
  } catch (error) {
    store.error = error.message;
    render();
  }
}

function taskView() {
  if (!store.task) return loadingView();
  const detail = store.task;
  let body;
  switch (store.route.view) {
    case "control": body = controlView(detail); break;
    case "changes": body = changesView(detail); break;
    case "review": body = taskReviewTabView(detail); break;
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

const EXECUTION_OPERATION_STAGE = {
  recorded: { label: "Recorded", pill: "warn" },
  running: { label: "Running", pill: "purple" },
  succeeded: { label: "Succeeded", pill: "good" },
  failed: { label: "Failed", pill: "bad" },
  ambiguous: { label: "Ambiguous", pill: "bad" },
};

function controlView(detail) {
  const report = detail.report;
  const events = detail.events || [];
  const task = detail.task;
  const canStart = (detail.available_actions || []).includes("start_execution");
  const activeOp = detail.active_execution_operation;
  if (
    activeOp &&
    (!store.executionOperation || store.executionOperation.operation_id !== activeOp.operation_id)
  ) {
    store.executionOperation = activeOp;
    if (activeOp.status === "recorded" || activeOp.status === "running") {
      pollExecutionOperation(task.task_id, activeOp.operation_id);
    }
  }
  const operationActive = store.executionOperation
    && ["recorded", "running"].includes(store.executionOperation.status);
  return `<main class="content">
    <div class="page-heading"><div><p class="eyebrow">CONTROL ROOM / EVENT RECORD</p><h1>Every transition is evidence.</h1><p>The timeline comes from persisted workflow events and durable operation records. Models cannot append, remove, or advance these states.</p></div><span class="pill ${statusClass(task.state)}">${e(titleCase(task.state))}</span></div>

    ${canStart && !operationActive ? executionStartPanel(detail) : ""}
    ${store.executionOperation ? executionOperationPanel(detail) : ""}
    ${task.state === "HUMAN_REVIEW_REQUIRED" ? `<div class="notice mt-16">This task stopped for a human decision. <a href="#/review/${encodeURIComponent(task.task_id)}">Open the Human Review case →</a></div>` : ""}

    <div class="split mt-18">
      <section class="card"><div class="card-header"><div><h2>Workflow timeline</h2><p>${events.length} deterministic events</p></div></div><div class="card-body"><div class="timeline">${events.map(eventCard).join("")}</div></div></section>
      <aside class="grid">
        <section class="card card-pad"><p class="section-title mt-0">Bounded execution</p><div class="grid two">${metric("Turns", report?.agent_turns ?? 0, "Recorded")}${metric("Patch attempts", report?.agent_patch_attempts ?? 0, "Recorded")}${metric("Verify runs", report?.agent_verification_runs ?? 0, "Recorded")}${metric("Calls", report?.number_of_calls ?? 0, "Provider telemetry")}</div></section>
        ${report ? `<section class="card card-pad"><p class="section-title mt-0">Usage &amp; telemetry</p><div class="grid two">${metric("Tokens in/out", `${compactNumber(report.input_tokens)} / ${compactNumber(report.output_tokens)}`, "Provider telemetry")}${metric("Estimated cost", `$${report.estimated_cost_usd.toFixed(4)}`, "Configured pricing")}${metric("Latency", `${report.latency_seconds.toFixed(1)}s`, "Wall-clock provider time")}${metric("Audit artifacts", report.audit_artifact_locations.length, "Persisted files")}</div></section>` : ""}
        <section class="card card-pad"><p class="section-title mt-0">Authority boundary</p><div class="notice">The UI observes workflow events and operation records. It does not run shell commands, apply patches, decide completion, choose routing, or extend retry ceilings.</div></section>
      </aside>
    </div>

    ${(detail.recent_agent_turns || []).length ? `<p class="section-title">Recent tool actions · ${detail.recent_agent_turns.length}</p><section class="card card-pad"><div class="verification-list">${detail.recent_agent_turns.slice().reverse().map(agentTurnItem).join("")}</div></section>` : ""}
  </main>`;
}

function agentTurnItem(turn) {
  return `<div class="file-item"><span class="pill ${turn.accepted ? "good" : "bad"}">${e(turn.stage)} · turn ${e(turn.turn)}</span><code>${e(turn.action)}</code><span class="meta">${e((turn.summary || "").slice(0, 140))}</span></div>`;
}

function executionStartPanel(detail) {
  const task = detail.task;
  const preview = detail.execution_preview || {};
  if (!store.executionConfirmPending) {
    return `<div class="approval-bar mt-16"><div><strong>Ready to execute</strong><span>Route, models, budgets, and verification are shown before anything runs.</span></div><div class="approval-actions"><button class="button primary" data-action="execution-start-intent">Start coding →</button></div></div>`;
  }
  const localBudget = preview.local_budget;
  const frontierBudget = preview.frontier_budget;
  return `<div class="approval-bar mt-16">
    <div>
      <strong>Confirm: start coding</strong>
      <span>This calls a model and creates an isolated worktree. Nothing has run yet.</span>
      <div class="mt-14 mono">MODE: ${e(preview.execution_mode)} · PREDICTED ROUTE: ${e(preview.predicted_route || "n/a")}</div>
      <div class="mt-14 mono">LOCAL MODEL: ${e(preview.local_model || "unknown")} ${preview.frontier_available ? `· FRONTIER MODEL: ${e(preview.frontier_model || "unknown")}` : "· FRONTIER: not configured"}</div>
      <div class="mt-14 mono">LOCAL BUDGET: ${e(localBudget?.max_turns ?? "—")} turns / ${e(localBudget?.max_patch_attempts ?? "—")} patch attempts / ${e(localBudget?.max_verification_runs ?? "—")} verify runs</div>
      ${preview.frontier_available ? `<div class="mt-14 mono">FRONTIER BUDGET: ${e(frontierBudget?.max_turns ?? "—")} turns / ${e(frontierBudget?.max_patch_attempts ?? "—")} patch attempts / ${e(frontierBudget?.max_verification_runs ?? "—")} verify runs</div>` : ""}
      <div class="mt-14 mono">COMPLETION POLICY: ${e(preview.completion_policy)} · SANDBOX: ${e(preview.verification_backend)}</div>
      <div class="mt-14 mono">VERIFICATION COMMANDS: ${e((preview.verification_commands || []).join(", ") || "none configured")}</div>
      <div class="mt-14 mono">AUTHORIZATION: ${e(preview.authorization_sha256 || "unavailable")}</div>
    </div>
    <div class="approval-actions">
      <button class="button ghost" data-action="execution-start-cancel">Cancel</button>
      <button class="button primary" data-action="execution-start-confirm" data-task-id="${e(task.task_id)}" data-version="${e(task.version)}" data-authorization-sha256="${e(preview.authorization_sha256 || "")}" ${store.busy ? "disabled" : ""}>Confirm &amp; start →</button>
    </div>
  </div>`;
}

function executionOperationPanel(detail) {
  const op = store.executionOperation;
  if (!op) return "";
  const stage = EXECUTION_OPERATION_STAGE[op.status] || { label: op.status, pill: "warn" };
  let note;
  if (op.status === "running") note = "A background worker is executing this task now. It is safe to close this tab -- progress is persisted and will still be here on reconnect.";
  else if (op.status === "recorded") note = "Accepted and durably recorded; waiting for the background worker to pick it up.";
  else if (op.status === "ambiguous") note = "The process running this operation may have crashed. Its outcome is unknown and it was never automatically repeated; the task was returned to Human Review with its worktree preserved.";
  else note = op.result_summary || op.error || "";
  return `<section class="card card-pad mt-16">
    <div class="constraint-head"><span class="constraint-id">OPERATION ${e(op.operation_id)}</span><span class="pill ${stage.pill}">${e(stage.label)}</span></div>
    <p class="muted">${e(note)}</p>
  </section>`;
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
  const completionPolicy = report?.completion_policy;
  return `<main class="content">
    <div class="page-heading"><div><p class="eyebrow">CHANGES / VERIFICATION</p><h1>Proposal versus proof.</h1><p>Changed paths, constraint dispositions, and command results are rendered from the final report. An absent report remains pending.</p></div><span class="pill ${report ? statusClass(report.outcome) : "warn"}">${report ? e(report.outcome) : "Pending"}</span></div>
    ${report ? `<div class="grid four">${metric("Files changed", files.length, "Validated paths")}${metric("Transmitted files", report.transmitted_files, "Provider context")}${metric("Transmitted lines", report.transmitted_lines, "Provider context")}${metric("Verify runs", verifications.length, "Recorded results")}</div>` : `<div class="notice">No final report is present for this task yet.</div>`}
    <div class="grid two mt-22">
      <section class="card"><div class="card-header"><div><h2>Files changed</h2><p>Accepted report paths</p></div></div><div class="card-body">${files.length ? `<div class="file-list">${files.map((file) => `<div class="file-item"><code>${e(file)}</code><span class="pill purple">changed</span></div>`).join("")}</div>` : `<p class="muted">No changed files are recorded.</p>`}</div></section>
      <section class="card"><div class="card-header"><div><h2>Constraint coverage</h2><p>Model disposition, not independent proof</p></div></div><div class="card-body">${coverage.length ? coverage.map((item) => `<div class="constraint"><div class="constraint-head"><span class="constraint-id">${e(item.constraint_id)}</span><span class="pill ${item.disposition === "included" ? "good" : "warn"}">${e(item.disposition)}</span></div><blockquote>${e(item.reason)}</blockquote></div>`).join("") : `<p class="muted">No coverage entries are recorded.</p>`}</div></section>
    </div>
    <p class="section-title">Acceptance coverage${completionPolicy ? ` · ${e(titleCase(completionPolicy))} completion policy` : ""}</p>
    <section class="card card-pad">${acceptanceCoverage.length ? `<div class="verification-list">${acceptanceCoverage.map(acceptanceCoverageItem).join("")}</div>` : `<p class="muted">${!report ? "No final report exists yet, so acceptance coverage has not been computed." : completionPolicy === "strict" ? "No active acceptance criteria are configured for this task." : "The baseline completion policy does not gate on acceptance coverage; this task recorded none."}</p>`}</section>
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

function taskReviewTabView(detail) {
  const required = detail.task.state === "HUMAN_REVIEW_REQUIRED";
  return `<main class="content narrow">
    <div class="page-heading"><div><p class="eyebrow">HUMAN REVIEW / EXPLICIT CONTROL</p><h1>${required ? "A person must decide." : "No review stop is active."}</h1><p>Resume choices must come from workflow state and deterministic policy. This first interface slice does not invent generic approve or retry buttons.</p></div><span class="pill ${required ? "warn" : "good"}">${required ? "Review required" : "No active stop"}</span></div>
    <section class="card result-hero"><div class="result-outcome"><span class="result-orb ${required ? "failed" : ""}"></span><div><h2>${required ? "Paused" : "Clear"}</h2><p>${required ? "Inspect the persisted event timeline and audit artifacts before choosing a supported resume path." : "The deterministic workflow has not requested human intervention."}</p></div></div></section>
    ${required ? `<div class="notice mt-16">Open the full <a href="#/review/${encodeURIComponent(detail.task.task_id)}">Human Review case →</a> to inspect the exact stop reason and available actions.</div>` : `<div class="grid three mt-18">${reviewOption("Resume local", "Only when the workflow exposes this transition.")}${reviewOption("Escalate frontier", "Only through deterministic routing and configured ceilings.")}${reviewOption("Roll back", "Recoverable worktree cleanup remains a CLI action today.")}</div>`}
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

// ---- Local-first discovery and frontier planning handoff (ADR 0032, 0033) ----

const DISCOVERY_STATUS_LABELS = {
  idea_entered: "Idea entered",
  local_questions_proposed: "Local questions proposed",
  local_answers_recorded: "Local answers recorded",
  brief_proposed: "Idea brief proposed",
  brief_approved: "Idea brief approved",
  research_completed: "Research completed",
  frontier_package_exported: "Frontier package exported",
  frontier_clarification_proposed: "Frontier clarification proposed",
  frontier_answers_recorded: "Frontier answers recorded",
  plan_imported: "Plan imported",
  failed: "Failed",
};

const DISCOVERY_OPERATION_STAGE = {
  recorded: { label: "Recorded", pill: "warn" },
  running: { label: "Running", pill: "purple" },
  succeeded: { label: "Succeeded", pill: "good" },
  failed: { label: "Failed", pill: "bad" },
  ambiguous: { label: "Ambiguous", pill: "bad" },
};

function discoverListView() {
  const sessions = store.discoverSessions?.sessions || [];
  return `<main class="content narrow">
    <div class="page-heading"><div><p class="eyebrow">PLAN A LARGER CHANGE</p><h1>Turn an idea into<br>workable slices.</h1><p>Your local model asks a few basic questions and drafts a brief. You approve it, optionally run Research, then give one planning file to ChatGPT or Claude, or use a configured API. The resulting plan is reviewed before any slice can run.</p></div></div>
    ${workflowSteps(["Clarify", "Approve brief", "Optional research", "Frontier plan", "Approve and run slices"], 0)}
    <section class="card card-pad">
      <label class="section-title mt-0" for="discover-idea">Describe the idea</label>
      <textarea id="discover-idea" class="mono" rows="4" placeholder="Add resumable downloads with a pluggable storage backend.">${e(store.discoverIdeaText)}</textarea>
      <div class="flex-end mt-16"><button class="button primary" data-action="discover-start" ${store.busy ? "disabled" : ""}>Start discovery →</button></div>
    </section>
    <p class="section-title">Sessions · ${sessions.length}</p>
    <section class="card">${sessions.length ? `<div class="task-list">${sessions.map(discoverSessionRow).join("")}</div>` : emptyState("No discovery sessions yet", "Start one above, or use apoapsis discover start \"<idea>\" from the CLI.")}</section>
  </main>`;
}

function discoverSessionRow(session) {
  const stage = { label: DISCOVERY_STATUS_LABELS[session.status] || session.status, pill: session.status === "failed" ? "bad" : session.status === "plan_imported" ? "good" : "purple" };
  return `<a class="task-row" href="#/discover/${encodeURIComponent(session.session_id)}">
    <div class="task-main"><strong>${e(session.idea_text)}</strong><span>${e(session.session_id)} · v${e(session.version)}</span></div>
    <span class="pill ${stage.pill}">${e(stage.label)}</span>
    <span class="meta">${e(formatDate(session.updated_at))}</span><span class="arrow">→</span>
  </a>`;
}

function discoverGenerateOperationId() {
  const raw = (window.crypto && window.crypto.randomUUID) ? window.crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
  return `DISCOP-${raw.replaceAll("-", "").slice(0, 24).toUpperCase()}`;
}

function discoverSessionView() {
  if (!store.discoverSession) return loadingView();
  const detail = store.discoverSession;
  const session = detail.session;
  const activeOp = detail.active_operation;
  if (activeOp && (!store.discoverOperation || store.discoverOperation.operation_id !== activeOp.operation_id)) {
    store.discoverOperation = activeOp;
    if (["recorded", "running"].includes(activeOp.status)) {
      pollDiscoveryOperation(session.session_id, activeOp.operation_id);
    }
  }
  const operationActive = store.discoverOperation && ["recorded", "running"].includes(store.discoverOperation.status);
  return `<main class="content">
    <p><a href="#/discover">← Back to discovery sessions</a></p>
    <div class="page-heading"><div><p class="eyebrow">PLANNING / ${e(session.session_id)}</p><h1>${e(session.idea_text)}</h1><p>Version ${e(session.version)} · updated ${e(formatDate(session.updated_at))}</p></div><span class="pill ${session.status === "failed" ? "bad" : session.status === "plan_imported" ? "good" : "purple"}">${e(DISCOVERY_STATUS_LABELS[session.status] || session.status)}</span></div>
    ${workflowSteps(["Clarify", "Approve brief", "Optional research", "Frontier plan", "Approve and run slices"], discoveryStepIndex(session.status))}
    ${discoveryOperationPanel()}
    ${!operationActive ? discoverySessionBody(detail) : ""}
  </main>`;
}

function discoveryOperationPanel() {
  const op = store.discoverOperation;
  if (!op) return "";
  const stage = DISCOVERY_OPERATION_STAGE[op.status] || { label: op.status, pill: "warn" };
  let note;
  if (op.status === "running") note = "A background worker is calling the model now. It is safe to close this tab -- progress is persisted and will still be here on reconnect.";
  else if (op.status === "recorded") note = "Accepted and durably recorded; waiting for the background worker to pick it up.";
  else if (op.status === "ambiguous") note = "The process running this operation may have crashed. Whether the model call was transmitted before that happened is unknown -- it was never automatically repeated.";
  else note = op.result_summary || op.error || "";
  return `<section class="card card-pad mt-16">
    <div class="constraint-head"><span class="constraint-id">OPERATION ${e(op.operation_id)} · ${e(titleCase(op.action))}</span><span class="pill ${stage.pill}">${e(stage.label)}</span></div>
    <p class="muted">${e(note)}</p>
  </section>`;
}

function discoverySessionBody(detail) {
  const session = detail.session;
  switch (session.status) {
    case "idea_entered": return discoveryIdeaEnteredView(detail);
    case "local_questions_proposed": return discoveryAnswerQuestionsView(detail, session.local_questions, "local");
    case "local_answers_recorded": return discoveryProposeBriefView(detail);
    case "brief_proposed": return discoveryBriefApprovalView(detail);
    case "brief_approved": return discoveryResearchChoiceView(detail);
    case "research_completed": return `${discoveryResearchResultView(detail)}${discoveryTransportChoiceView(detail)}`;
    case "frontier_package_exported": return discoveryFrontierPackageView(detail);
    case "frontier_clarification_proposed": return discoveryAnswerQuestionsView(detail, session.frontier_questions, "frontier");
    case "frontier_answers_recorded": return discoveryTransportChoiceView(detail);
    case "plan_imported": return discoveryPlanImportedView(detail);
    case "failed": return discoveryFailedView(detail);
    default: return "";
  }
}

function discoveryStepIndex(status) {
  if (["idea_entered", "local_questions_proposed", "local_answers_recorded", "brief_proposed"].includes(status)) return status === "brief_proposed" ? 1 : 0;
  if (status === "brief_approved" || status === "research_completed") return 2;
  if (["frontier_package_exported", "frontier_clarification_proposed", "frontier_answers_recorded"].includes(status)) return 3;
  if (status === "plan_imported") return 4;
  return 0;
}

function discoveryIdeaEnteredView(detail) {
  return `<section class="card card-pad mt-16">
    <p class="section-title mt-0">Local clarification (optional)</p>
    <p class="muted">A configured local model may propose up to ${e(detail.max_clarification_questions ?? 5)} clarification questions -- the harness caps the count regardless of how many the model returns. This step is optional; you may skip straight to an idea brief.</p>
    <div class="approval-actions mt-14">
      <button class="button primary" data-action="discover-op-submit" data-op-action="local_questions" data-session-id="${e(detail.session.session_id)}" data-version="${e(detail.session.version)}" ${store.busy ? "disabled" : ""}>Propose clarification questions →</button>
      <button class="button ghost" data-action="discover-op-submit" data-op-action="idea_brief" data-session-id="${e(detail.session.session_id)}" data-version="${e(detail.session.version)}" ${store.busy ? "disabled" : ""}>Skip to idea brief →</button>
    </div>
  </section>`;
}

function discoveryAnswerQuestionsView(detail, questions, kind) {
  const drafts = kind === "local" ? store.discoverAnswerDrafts : store.discoverFrontierAnswerDrafts;
  return `<section class="card card-pad mt-16">
    <p class="section-title mt-0">${kind === "local" ? "Local" : "Frontier"} clarification questions · ${questions.length}</p>
    <p class="muted">Answer in your own words -- your answers are preserved verbatim, never rewritten or answered on your behalf.</p>
    ${questions.map((q) => `<div class="mt-14"><label class="section-title mt-0" for="discover-answer-${e(q.question_id)}">${e(q.text)}</label><textarea id="discover-answer-${e(q.question_id)}" data-question-id="${e(q.question_id)}" data-answer-kind="${kind}" class="mono" rows="2">${e(drafts[q.question_id] || "")}</textarea></div>`).join("")}
    <div class="flex-end mt-16"><button class="button primary" data-action="discover-answer-submit" data-answer-kind="${kind}" data-session-id="${e(detail.session.session_id)}" data-version="${e(detail.session.version)}" ${store.busy ? "disabled" : ""}>Submit answers →</button></div>
  </section>`;
}

function discoveryProposeBriefView(detail) {
  return `<section class="card card-pad mt-16">
    <p class="section-title mt-0">Answers recorded</p>
    ${(detail.session.local_answers || []).map((a) => `<div class="file-item"><span>${e(a.question_id)}</span><span>${e(a.text)}</span></div>`).join("")}
    <div class="mt-16"><button class="button primary" data-action="discover-op-submit" data-op-action="idea_brief" data-session-id="${e(detail.session.session_id)}" data-version="${e(detail.session.version)}" ${store.busy ? "disabled" : ""}>Propose idea brief →</button></div>
  </section>`;
}

function discoveryBriefApprovalView(detail) {
  const brief = detail.session.idea_brief;
  const pending = store.discoverBriefApprovePending;
  return `<section class="card card-pad mt-16">
    <p class="section-title mt-0">Proposed idea brief</p>
    <p class="objective">${e(brief.summary)}</p>
    ${brief.goals.length ? `<p class="section-title">Goals</p><ul>${brief.goals.map((g) => `<li>${e(g)}</li>`).join("")}</ul>` : ""}
    ${brief.non_goals.length ? `<p class="section-title">Non-goals</p><ul>${brief.non_goals.map((g) => `<li>${e(g)}</li>`).join("")}</ul>` : ""}
    ${brief.key_constraints.length ? `<p class="section-title">Key constraints (verbatim)</p>${brief.key_constraints.map(constraintCard).join("")}` : ""}
    ${brief.open_questions.length ? `<p class="section-title">Open questions</p><ul>${brief.open_questions.map((g) => `<li>${e(g)}</li>`).join("")}</ul>` : ""}
    <div class="approval-bar mt-16"><div><strong>${pending ? "Confirm approval" : "Only you can approve this brief"}</strong><span>${pending ? `Approve version ${e(detail.session.version)}.` : "The local model proposed this brief; it has no authority to approve it itself."}</span></div><div class="approval-actions">${pending ? `<button class="button ghost" data-action="discover-brief-approve-cancel">Cancel</button><button class="button primary" data-action="discover-brief-approve-confirm" data-session-id="${e(detail.session.session_id)}" data-version="${e(detail.session.version)}" ${store.busy ? "disabled" : ""}>Confirm approval →</button>` : `<button class="button primary" data-action="discover-brief-approve-intent">Approve idea brief →</button>`}</div></div>
  </section>`;
}

const RESEARCH_CHOICES = {
  auto: { action: "research_auto", label: "Auto", description: "Run only when the approved brief triggers the deterministic research rules." },
  github: { action: "research_github", label: "GitHub", description: "Look for implementation precedent in configured GitHub sources." },
  community: { action: "research_community", label: "Community", description: "Use configured community sources for user pain and practical experience." },
  full: { action: "research_full", label: "Full", description: "Use every configured research source within the fixed research budget." },
};

function discoveryResearchChoiceView(detail) {
  const selectedValue = RESEARCH_CHOICES[store.discoverResearchChoice] ? store.discoverResearchChoice : "auto";
  const selected = RESEARCH_CHOICES[selectedValue];
  const available = detail.planning_research_available;
  return `<section class="card card-pad mt-16">
    <p class="section-title mt-0">Optional planning research</p>
    <p class="muted">Research happens before the frontier planning handoff. Network access belongs only to restricted source adapters; the local research model receives sanitized evidence and has no tools. Only its compact brief and evidence IDs enter the planning package.</p>
    ${available ? `<div class="grid two mt-14">${Object.entries(RESEARCH_CHOICES).map(([value, choice]) => `<label class="constraint research-choice"><input type="radio" name="discover-research" value="${e(value)}" ${selectedValue === value ? "checked" : ""}> <strong>${e(choice.label)}</strong><span>${e(choice.description)}</span></label>`).join("")}</div><div class="mt-14 mono">LOCAL RESEARCH MODEL: ${e(detail.planning_research_model)}</div><div class="flex-end mt-16"><button class="button primary" data-action="discover-op-submit" data-op-action="${e(selected.action)}" data-session-id="${e(detail.session.session_id)}" data-version="${e(detail.session.version)}" ${store.busy ? "disabled" : ""}>Run ${e(selected.label)} research →</button></div>` : `<div class="notice mt-14">Planning research is unavailable because this project has no <span class="mono">models.local_research</span> role configured. You can continue to planning without it.</div>`}
    <details class="mt-16"><summary>Skip research and continue</summary>${discoveryTransportChoiceView(detail, true)}</details>
  </section>`;
}

function discoveryResearchResultView(detail) {
  const session = detail.session;
  const produced = session.research_triggered && session.research_brief;
  return `<section class="card card-pad mt-16">
    <div class="constraint-head"><span class="constraint-id">PLANNING RESEARCH</span><span class="pill ${produced ? "good" : "purple"}">${produced ? "Brief ready" : "Not triggered"}</span></div>
    <p class="muted">Mode: ${e(titleCase(session.research_mode))}. ${produced ? `${session.research_evidence_ids.length} provenance-bound evidence item(s) will be included by ID in the frontier planning package.` : "The deterministic trigger did not produce a research brief; planning can continue normally."}</p>
    ${produced ? `<details class="mt-14"><summary>Read the research brief</summary><div class="mono mt-14" style="white-space: pre-wrap; overflow-wrap: anywhere;">${e(session.research_brief)}</div></details>` : ""}
  </section>`;
}

function discoveryTransportChoiceView(detail, embedded = false) {
  const choice = store.discoverTransportChoice;
  const content = `
    <p class="section-title mt-0">Choose a frontier planning transport</p>
    <p class="muted">Manual subscription means one file uploaded to ChatGPT or Claude and one response pasted back. API uses separately configured credentials and an explicit spend ceiling. Neither path can approve or execute the plan.</p>
    <div class="grid two mt-14">
      <label class="constraint" style="cursor:pointer"><input type="radio" name="discover-transport" value="api" ${choice === "api" ? "checked" : ""} ${detail.frontier_api_configured ? "" : "disabled"}> <strong>API</strong> -- explicitly configured, spend-ceilinged.${detail.frontier_api_configured ? "" : " (not configured)"}</label>
      <label class="constraint" style="cursor:pointer"><input type="radio" name="discover-transport" value="manual" ${choice === "manual" ? "checked" : ""}> <strong>Manual subscription</strong> -- upload one file, paste one response.</label>
    </div>
    <div class="flex-end mt-16"><button class="button primary" data-action="discover-export-package" data-session-id="${e(detail.session.session_id)}" data-version="${e(detail.session.version)}" ${store.busy ? "disabled" : ""}>Prepare planning handoff →</button></div>`;
  return embedded ? `<div class="mt-14">${content}</div>` : `<section class="card card-pad mt-16">${content}</section>`;
}

function discoveryFrontierPackageView(detail) {
  const pkg = detail.frontier_package;
  const session = detail.session;
  return `<section class="card card-pad mt-16">
    <p class="section-title mt-0">Frontier package exported · ${e(session.frontier_transport)} transport</p>
    <div class="mono">PACKAGE ID: ${e(pkg.package_id)} · HASH: ${e(pkg.package_sha256)} · ROUND ${e(pkg.frontier_round)} of ${e(pkg.max_clarification_rounds)}</div>
    ${session.frontier_transport === "manual" ? discoveryManualTransportPanel(detail) : discoveryApiTransportPanel(detail)}
  </section>`;
}

function discoveryManualTransportPanel(detail) {
  const exported = store.discoverFrontierExportPaths;
  const form = store.discoverManualImportForm;
  return `
    ${workflowSteps(["Download planning file", "Upload to ChatGPT or Claude", "Paste response", "Review the plan"], exported ? 2 : 0)}
    ${exported ? `<div class="notice mt-14"><strong>Upload this file to ChatGPT or Claude:</strong><div class="mt-14 file-item"><span>File to upload</span><code>${e(exported.markdown_artifact_absolute_path)}</code><button class="button ghost" data-action="copy-path" data-copy-path="${e(exported.markdown_artifact_absolute_path)}">Copy path</button></div></div>` : `<p class="muted mt-14">The self-contained handoff Markdown file was written to this task's audit directory; re-export to see its path again if you navigated away.</p>`}
    <p class="section-title">Paste the response, or upload it as a file</p>
    <div class="grid two">
      <div>
        <label class="section-title mt-0" for="discover-mf-package-id">Package ID</label>
        <input id="discover-mf-package-id" class="mono" type="text" value="${e(form.packageId || detail.frontier_package.package_id)}">
        <label class="section-title" for="discover-mf-declared-model">Declared subscription model (operator-provided, unverified)</label>
        <input id="discover-mf-declared-model" class="mono" type="text" placeholder="claude-opus-4.6-web" value="${e(form.declaredModelName)}">
      </div>
      <div>
        <label class="section-title mt-0" for="discover-mf-response-text">Pasted response JSON</label>
        <textarea id="discover-mf-response-text" class="mono" rows="6">${e(form.responseText)}</textarea>
        <input id="discover-mf-response-file" type="file" accept=".json,.txt,application/json,text/plain" class="mt-14">
      </div>
    </div>
    <div class="flex-end mt-16"><button class="button primary" data-action="discover-import-manual" data-session-id="${e(detail.session.session_id)}" ${store.busy ? "disabled" : ""}>Validate & apply response →</button></div>`;
}

function discoveryApiTransportPanel(detail) {
  const preview = detail.api_preview;
  if (!preview) return `<div class="notice mt-14">No API provider is configured for this project.</div>`;
  return `
    <div class="mt-14 mono">PROVIDER: ${e(preview.provider)} · MODEL: ${e(preview.model)}</div>
    <div class="mono">MAX CALLS THIS ROUND: ${e(preview.max_calls_this_round)} · WORST-CASE COST: $${Number(preview.worst_case_call_cost_usd).toFixed(4)}</div>
    <div class="mt-14"><label class="section-title mt-0" for="discover-spend-ceiling">Authorized spend ceiling (USD) for this one call</label><input id="discover-spend-ceiling" type="number" min="0" step="0.01" class="mono" value="${e(store.discoverApiSpendUsd)}"></div>
    <div class="flex-end mt-16"><button class="button primary" data-action="discover-call-api" data-session-id="${e(detail.session.session_id)}" data-version="${e(detail.session.version)}" ${store.busy ? "disabled" : ""}>Authorize & call →</button></div>`;
}

function discoveryPlanImportedView(detail) {
  const session = detail.session;
  const plan = detail.plan_summary;
  return `<section class="card result-hero mt-16"><div class="result-outcome"><span class="result-orb"></span><div><h2>Plan imported</h2><p>The frontier model returned a complete plan. It became an entirely ordinary Architect Mode plan -- review, validate, and approve it on the Plans page exactly as any other plan.</p></div></div></section>
  ${plan ? `<section class="card card-pad mt-16"><p class="section-title mt-0">${e(plan.architecture_summary)}</p><div class="mono">${e(plan.plan_id)} · ${e(titleCase(plan.status))} · ${e(plan.slice_count)} slice(s)</div><div class="mt-14"><a class="button primary" href="#/plan/${encodeURIComponent(session.plan_id)}/overview">Open plan →</a></div></section>` : ""}`;
}

function discoveryFailedView(detail) {
  return `<div class="notice mt-16">This discovery session stopped: ${e(detail.session.failure_reason || "no reason recorded")}</div>`;
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
  updateDocumentTitle();
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
  else if (store.route.name === "planSlice") view = planSliceView();
  else if (store.route.name === "plan") view = planView();
  else if (store.route.name === "plans") view = plansView();
  else if (store.route.name === "review") view = reviewView();
  else if (store.route.name === "reviews") view = reviewsView();
  else if (store.route.name === "discoverSession") view = discoverSessionView();
  else if (store.route.name === "discover") view = discoverListView();
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

async function packagePlanSlice(button) {
  const planId = button.dataset.planId;
  const sliceId = button.dataset.sliceId;
  const planVersion = Number(button.dataset.planVersion);
  store.busy = true;
  store.error = null;
  render();
  try {
    await api(
      `/api/plans/${encodeURIComponent(planId)}/slices/${encodeURIComponent(sliceId)}/package`,
      { method: "POST", body: JSON.stringify({ expected_plan_version: planVersion }) }
    );
    store.planSlice = await api(
      `/api/plans/${encodeURIComponent(planId)}/slices/${encodeURIComponent(sliceId)}`
    );
  } catch (error) {
    store.error = error.message;
  } finally {
    store.busy = false;
    render();
  }
}

async function approvePlanSlice(button) {
  const planId = button.dataset.planId;
  const sliceId = button.dataset.sliceId;
  const packageSha256 = button.dataset.packageSha256;
  store.busy = true;
  store.error = null;
  render();
  try {
    await api(
      `/api/plans/${encodeURIComponent(planId)}/slices/${encodeURIComponent(sliceId)}/approve`,
      { method: "POST", body: JSON.stringify({ expected_package_sha256: packageSha256 }) }
    );
    store.planSlice = await api(
      `/api/plans/${encodeURIComponent(planId)}/slices/${encodeURIComponent(sliceId)}`
    );
    store.planSliceApprovalPending = false;
    store.plan = null;
  } catch (error) {
    store.error = error.message;
  } finally {
    store.busy = false;
    render();
  }
}

// ---- Manual frontier coding handoff actions ----

async function exportManualFrontierHandoff(taskId) {
  store.busy = true;
  store.error = null;
  store.manualFrontierExportPending = true;
  render();
  try {
    store.manualFrontierExport = await api(
      `/api/reviews/${encodeURIComponent(taskId)}/manual-frontier/export`,
      { method: "POST", body: JSON.stringify({}) }
    );
  } catch (error) {
    store.error = error.message;
  } finally {
    store.busy = false;
    store.manualFrontierExportPending = false;
    render();
  }
}

async function readFileFieldAsText(inputId) {
  const field = document.getElementById(inputId);
  if (!field || !field.files || !field.files.length) return null;
  return await field.files[0].text();
}

async function importManualFrontierResponse(taskId) {
  const packageId = (document.getElementById("mf-package-id")?.value || "").trim();
  const declaredModelName = (document.getElementById("mf-declared-model")?.value || "").trim();
  let responseText = (document.getElementById("mf-response-text")?.value || "").trim();
  const fileText = await readFileFieldAsText("mf-response-file");
  if (fileText) responseText = fileText;
  store.manualFrontierImportForm = { packageId, responseText, declaredModelName };
  if (!packageId || !responseText || !declaredModelName) {
    store.error = "Package ID, declared model name, and a response (pasted or uploaded) are all required.";
    render();
    return;
  }
  const previewId = `MFPV-${(window.crypto?.randomUUID ? window.crypto.randomUUID() : `${Date.now()}-${Math.random()}`).replaceAll("-", "").slice(0, 24).toUpperCase()}`;
  store.busy = true;
  store.error = null;
  render();
  try {
    await api(`/api/reviews/${encodeURIComponent(taskId)}/manual-frontier/import`, {
      method: "POST",
      body: JSON.stringify({ package_id: packageId, response_text: responseText, declared_model_name: declaredModelName, preview_id: previewId }),
    });
    store.manualFrontierImportForm = { packageId: "", responseText: "", declaredModelName: "" };
    store.manualFrontierPreviews = await api(`/api/reviews/${encodeURIComponent(taskId)}/manual-frontier/previews`);
  } catch (error) {
    store.error = error.message;
  } finally {
    store.busy = false;
    render();
  }
}

async function approveManualFrontierPreview(taskId, previewId, taskVersion) {
  store.busy = true;
  store.error = null;
  render();
  try {
    await api(
      `/api/reviews/${encodeURIComponent(taskId)}/manual-frontier/previews/${encodeURIComponent(previewId)}/approve`,
      { method: "POST", body: JSON.stringify({ expected_version: taskVersion }) }
    );
    store.manualFrontierPreviews = await api(`/api/reviews/${encodeURIComponent(taskId)}/manual-frontier/previews`);
    store.manualFrontierApprovePendingId = null;
  } catch (error) {
    store.error = error.message;
  } finally {
    store.busy = false;
    render();
  }
}

async function applyManualFrontierPreview(taskId, previewId) {
  const detail = store.review;
  const operationId = reviewGenerateOperationId();
  const payload = {
    action: "manual_frontier_handoff",
    operation_id: operationId,
    expected_version: detail.task_version,
    manual_frontier_preview_id: previewId,
  };
  if (detail.worktree_fingerprint) payload.expected_worktree_fingerprint = detail.worktree_fingerprint;
  window.sessionStorage.setItem(reviewOperationStorageKey(taskId), JSON.stringify({ operationId, action: "manual_frontier_handoff" }));
  store.busy = true;
  store.error = null;
  store.manualFrontierApplyPreviewId = null;
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

// ---- Discovery and frontier planning handoff actions ----

async function startDiscoverySession() {
  const field = document.getElementById("discover-idea");
  const ideaText = (field ? field.value : store.discoverIdeaText || "").trim();
  if (!ideaText) {
    store.error = "Describe the idea before starting discovery.";
    render();
    return;
  }
  store.discoverIdeaText = ideaText;
  store.busy = true;
  store.error = null;
  render();
  try {
    const record = await api("/api/discovery/sessions", {
      method: "POST",
      body: JSON.stringify({ idea_text: ideaText }),
    });
    store.discoverIdeaText = "";
    store.discoverSessions = null;
    window.location.hash = `#/discover/${encodeURIComponent(record.session_id)}`;
  } catch (error) {
    store.error = error.message;
  } finally {
    store.busy = false;
    render();
  }
}

let discoveryPollHandle = null;

async function pollDiscoveryOperation(sessionId, operationId) {
  if (discoveryPollHandle) {
    clearTimeout(discoveryPollHandle);
    discoveryPollHandle = null;
  }
  try {
    const record = await api(`/api/discovery/operations/${encodeURIComponent(operationId)}`);
    store.discoverOperation = record;
    render();
    if (record.status === "recorded" || record.status === "running") {
      discoveryPollHandle = setTimeout(() => pollDiscoveryOperation(sessionId, operationId), 2000);
      return;
    }
    window.sessionStorage.removeItem(discoveryOperationStorageKey(sessionId));
    if (store.route.name === "discoverSession" && store.route.sessionId === sessionId) {
      store.discoverSession = await api(`/api/discovery/sessions/${encodeURIComponent(sessionId)}`);
      render();
    }
  } catch (error) {
    store.error = error.message;
    render();
  }
}

function discoveryOperationStorageKey(sessionId) {
  return `apoapsis-discovery-operation-${sessionId}`;
}

function resumePendingDiscoveryOperationPoll(sessionId) {
  const raw = window.sessionStorage.getItem(discoveryOperationStorageKey(sessionId));
  if (!raw) return;
  try {
    const parsed = JSON.parse(raw);
    if (parsed && parsed.operationId) pollDiscoveryOperation(sessionId, parsed.operationId);
  } catch (error) {
    window.sessionStorage.removeItem(discoveryOperationStorageKey(sessionId));
  }
}

async function submitDiscoveryOperation(sessionId, action, version, extra = {}) {
  const operationId = discoverGenerateOperationId();
  window.sessionStorage.setItem(discoveryOperationStorageKey(sessionId), JSON.stringify({ operationId, action }));
  store.busy = true;
  store.error = null;
  render();
  try {
    const record = await api(`/api/discovery/sessions/${encodeURIComponent(sessionId)}/operations`, {
      method: "POST",
      body: JSON.stringify({ action, operation_id: operationId, expected_version: version, ...extra }),
    });
    store.discoverOperation = record;
    pollDiscoveryOperation(sessionId, operationId);
  } catch (error) {
    store.error = error.message;
    window.sessionStorage.removeItem(discoveryOperationStorageKey(sessionId));
  } finally {
    store.busy = false;
    render();
  }
}

async function submitDiscoveryAnswers(sessionId, kind, version) {
  const drafts = kind === "local" ? store.discoverAnswerDrafts : store.discoverFrontierAnswerDrafts;
  const questions = kind === "local" ? store.discoverSession.session.local_questions : store.discoverSession.session.frontier_questions;
  const answers = questions.map((q) => ({ question_id: q.question_id, text: (drafts[q.question_id] || "").trim() }));
  if (answers.some((a) => !a.text)) {
    store.error = "Answer every question before submitting.";
    render();
    return;
  }
  store.busy = true;
  store.error = null;
  render();
  try {
    const endpoint = kind === "local" ? "answers" : "frontier-answers";
    await api(`/api/discovery/sessions/${encodeURIComponent(sessionId)}/${endpoint}`, {
      method: "POST",
      body: JSON.stringify({ answers, expected_version: version }),
    });
    store.discoverSession = await api(`/api/discovery/sessions/${encodeURIComponent(sessionId)}`);
    if (kind === "local") store.discoverAnswerDrafts = {}; else store.discoverFrontierAnswerDrafts = {};
  } catch (error) {
    store.error = error.message;
  } finally {
    store.busy = false;
    render();
  }
}

async function approveDiscoveryBrief(sessionId, version) {
  store.busy = true;
  store.error = null;
  render();
  try {
    await api(`/api/discovery/sessions/${encodeURIComponent(sessionId)}/approve-brief`, {
      method: "POST",
      body: JSON.stringify({ expected_version: version }),
    });
    store.discoverSession = await api(`/api/discovery/sessions/${encodeURIComponent(sessionId)}`);
    store.discoverBriefApprovePending = false;
  } catch (error) {
    store.error = error.message;
  } finally {
    store.busy = false;
    render();
  }
}

async function exportDiscoveryFrontierPackage(sessionId, version) {
  const selected = document.querySelector('input[name="discover-transport"]:checked');
  const transport = selected ? selected.value : store.discoverTransportChoice;
  store.discoverTransportChoice = transport;
  store.busy = true;
  store.error = null;
  render();
  try {
    const result = await api(`/api/discovery/sessions/${encodeURIComponent(sessionId)}/export-frontier-package`, {
      method: "POST",
      body: JSON.stringify({ transport, expected_version: version }),
    });
    store.discoverFrontierExportPaths = result;
    store.discoverSession = await api(`/api/discovery/sessions/${encodeURIComponent(sessionId)}`);
  } catch (error) {
    store.error = error.message;
  } finally {
    store.busy = false;
    render();
  }
}

async function importDiscoveryManualResponse(sessionId) {
  const packageId = (document.getElementById("discover-mf-package-id")?.value || "").trim();
  const declaredModelName = (document.getElementById("discover-mf-declared-model")?.value || "").trim();
  let responseText = (document.getElementById("discover-mf-response-text")?.value || "").trim();
  const fileText = await readFileFieldAsText("discover-mf-response-file");
  if (fileText) responseText = fileText;
  if (!packageId || !responseText || !declaredModelName) {
    store.error = "Package ID, declared model name, and a response (pasted or uploaded) are all required.";
    render();
    return;
  }
  store.busy = true;
  store.error = null;
  render();
  try {
    await api(`/api/discovery/sessions/${encodeURIComponent(sessionId)}/import-manual-response`, {
      method: "POST",
      body: JSON.stringify({ package_id: packageId, response_text: responseText, declared_model_name: declaredModelName }),
    });
    store.discoverManualImportForm = { packageId: "", responseText: "", declaredModelName: "" };
    store.discoverSession = await api(`/api/discovery/sessions/${encodeURIComponent(sessionId)}`);
  } catch (error) {
    store.error = error.message;
  } finally {
    store.busy = false;
    render();
  }
}

async function callDiscoveryFrontierApi(sessionId, version) {
  const spendField = document.getElementById("discover-spend-ceiling");
  const authorizedMaxSpendUsd = Number(spendField ? spendField.value : store.discoverApiSpendUsd);
  if (!Number.isFinite(authorizedMaxSpendUsd) || authorizedMaxSpendUsd < 0) {
    store.error = "Enter a valid, non-negative spend ceiling before authorizing.";
    render();
    return;
  }
  store.discoverApiSpendUsd = String(authorizedMaxSpendUsd);
  await submitDiscoveryOperation(sessionId, "frontier_api_call", version, { authorized_max_spend_usd: authorizedMaxSpendUsd });
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
  if (button.dataset.action === "slice-package") packagePlanSlice(button);
  if (button.dataset.action === "slice-approve-intent") {
    store.planSliceApprovalPending = true;
    render();
  }
  if (button.dataset.action === "slice-approve-cancel") {
    store.planSliceApprovalPending = false;
    render();
  }
  if (button.dataset.action === "slice-approve-confirm") approvePlanSlice(button);
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
  if (button.dataset.action === "intake-submit") submitIntakeOperation();
  if (button.dataset.action === "intake-reset") {
    if (intakePollHandle) {
      clearTimeout(intakePollHandle);
      intakePollHandle = null;
    }
    window.sessionStorage.removeItem(INTAKE_OPERATION_STORAGE_KEY);
    store.intakeOperation = null;
    store.intakeRequestText = "";
    render();
  }
  if (button.dataset.action === "execution-start-intent") {
    store.executionConfirmPending = true;
    render();
  }
  if (button.dataset.action === "execution-start-cancel") {
    store.executionConfirmPending = false;
    render();
  }
  if (button.dataset.action === "execution-start-confirm") {
    submitExecutionStart(
      button.dataset.taskId,
      Number(button.dataset.version),
      button.dataset.authorizationSha256
    );
  }
  if (button.dataset.action === "copy-path") {
    const path = button.dataset.copyPath || "";
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(path).then(() => {
        button.textContent = "Copied";
        setTimeout(() => { button.textContent = "Copy path"; }, 1500);
      }).catch(() => {});
    }
  }
  if (button.dataset.action === "manual-frontier-export") {
    exportManualFrontierHandoff(store.route.taskId);
  }
  if (button.dataset.action === "manual-frontier-import") {
    importManualFrontierResponse(store.route.taskId);
  }
  if (button.dataset.action === "manual-frontier-approve-intent") {
    store.manualFrontierApprovePendingId = button.dataset.previewId;
    render();
  }
  if (button.dataset.action === "manual-frontier-approve-cancel") {
    store.manualFrontierApprovePendingId = null;
    render();
  }
  if (button.dataset.action === "manual-frontier-approve-confirm") {
    approveManualFrontierPreview(store.route.taskId, button.dataset.previewId, Number(button.dataset.taskVersion));
  }
  if (button.dataset.action === "manual-frontier-apply-intent") {
    store.manualFrontierApplyPreviewId = button.dataset.previewId;
    render();
  }
  if (button.dataset.action === "manual-frontier-apply-cancel") {
    store.manualFrontierApplyPreviewId = null;
    render();
  }
  if (button.dataset.action === "manual-frontier-apply-confirm") {
    applyManualFrontierPreview(store.route.taskId, button.dataset.previewId);
  }
  if (button.dataset.action === "discover-start") startDiscoverySession();
  if (button.dataset.action === "discover-op-submit") {
    submitDiscoveryOperation(button.dataset.sessionId, button.dataset.opAction, Number(button.dataset.version));
  }
  if (button.dataset.action === "discover-answer-submit") {
    submitDiscoveryAnswers(button.dataset.sessionId, button.dataset.answerKind, Number(button.dataset.version));
  }
  if (button.dataset.action === "discover-brief-approve-intent") {
    store.discoverBriefApprovePending = true;
    render();
  }
  if (button.dataset.action === "discover-brief-approve-cancel") {
    store.discoverBriefApprovePending = false;
    render();
  }
  if (button.dataset.action === "discover-brief-approve-confirm") {
    approveDiscoveryBrief(button.dataset.sessionId, Number(button.dataset.version));
  }
  if (button.dataset.action === "discover-export-package") {
    exportDiscoveryFrontierPackage(button.dataset.sessionId, Number(button.dataset.version));
  }
  if (button.dataset.action === "discover-import-manual") {
    importDiscoveryManualResponse(button.dataset.sessionId);
  }
  if (button.dataset.action === "discover-call-api") {
    callDiscoveryFrontierApi(button.dataset.sessionId, Number(button.dataset.version));
  }
});

root.addEventListener("input", (event) => {
  const target = event.target;
  if (!target) return;
  if (target.id === "intake-request") {
    store.intakeRequestText = target.value;
  }
  if (target.id === "discover-idea") {
    store.discoverIdeaText = target.value;
  }
  if (target.dataset && target.dataset.questionId) {
    const drafts = target.dataset.answerKind === "local" ? store.discoverAnswerDrafts : store.discoverFrontierAnswerDrafts;
    drafts[target.dataset.questionId] = target.value;
  }
  if (target.id === "mf-package-id") store.manualFrontierImportForm.packageId = target.value;
  if (target.id === "mf-declared-model") store.manualFrontierImportForm.declaredModelName = target.value;
  if (target.id === "mf-response-text") store.manualFrontierImportForm.responseText = target.value;
  if (target.id === "discover-mf-package-id") store.discoverManualImportForm.packageId = target.value;
  if (target.id === "discover-mf-declared-model") store.discoverManualImportForm.declaredModelName = target.value;
  if (target.id === "discover-mf-response-text") store.discoverManualImportForm.responseText = target.value;
  if (target.id === "discover-spend-ceiling") store.discoverApiSpendUsd = target.value;
});

root.addEventListener("change", (event) => {
  if (event.target && event.target.name === "discover-transport") {
    store.discoverTransportChoice = event.target.value;
  }
  if (event.target && event.target.name === "discover-research") {
    store.discoverResearchChoice = event.target.value;
    render();
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
