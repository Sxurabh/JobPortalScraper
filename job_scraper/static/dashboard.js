(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const state = { view: "overview", status: null, jobs: [], allJobs: [], jobScope: "matching", companies: [], editing: null, toastTimer: null };
  const icon = {
    pin: '<svg viewBox="0 0 24 24"><path d="M20 10c0 5-8 11-8 11S4 15 4 10a8 8 0 1 1 16 0Z"/><circle cx="12" cy="10" r="2.5"/></svg>',
    arrow: '<svg viewBox="0 0 24 24"><path d="M7 17 17 7M7 7h10v10"/></svg>',
    briefcase: '<svg viewBox="0 0 24 24"><path d="M4 7.5h16v12H4z"/><path d="M9 7.5V5h6v2.5"/></svg>',
    trash: '<svg viewBox="0 0 24 24"><path d="M4 7h16M10 11v6m4-6v6M6 7l1 14h10l1-14M9 7V4h6v3"/></svg>',
    edit: '<svg viewBox="0 0 24 24"><path d="m14 5 5 5M4 20l4.2-.9L19 8.3a2.1 2.1 0 0 0-3-3L5.2 16.1 4 20Z"/></svg>',
  };

  const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  const initials = (name) => String(name || "?").trim().split(/\s+/).slice(0, 2).map((part) => part[0]?.toUpperCase() || "").join("");
  const parseList = (value) => String(value || "").split(",").map((item) => item.trim()).filter(Boolean);

  async function api(path, options = {}) {
    const response = await fetch(path, { headers: { "Content-Type": "application/json", ...(options.headers || {}) }, ...options });
    let data;
    try { data = await response.json(); } catch { data = {}; }
    if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
    return data;
  }

  function showToast(message, isError = false) {
    const toast = $("#toast");
    toast.textContent = message;
    toast.classList.toggle("error", isError);
    toast.classList.add("show");
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => toast.classList.remove("show"), 3400);
  }

  function formatDate(value) {
    if (!value) return "Not checked yet";
    const date = new Date(value.replace(" ", "T") + (value.includes("Z") || value.includes("+") ? "" : "Z"));
    if (Number.isNaN(date.getTime())) return value;
    return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }).format(date);
  }

  function relativeTime(value) {
    if (!value) return "Just found";
    const date = new Date(value.replace(" ", "T") + (value.includes("Z") || value.includes("+") ? "" : "Z"));
    const minutes = Math.max(0, Math.floor((Date.now() - date.getTime()) / 60000));
    if (!Number.isFinite(minutes)) return "Recently";
    if (minutes < 1) return "Just now";
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.floor(hours / 24);
    if (days < 7) return `${days}d ago`;
    return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" }).format(date);
  }

  function safeHref(value) {
    try {
      const parsed = new URL(value);
      return ["http:", "https:"].includes(parsed.protocol) ? parsed.href : "#";
    } catch { return "#"; }
  }

  function switchView(view) {
    if (!["overview", "jobs", "companies", "settings"].includes(view)) return;
    state.view = view;
    $$(".view").forEach((section) => section.classList.toggle("active", section.id === `view-${view}`));
    $$(".nav-item[data-view]").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
    const label = view === "jobs" ? "Job matches" : view[0].toUpperCase() + view.slice(1);
    $("#crumb-current").textContent = label;
    $("#sidebar").classList.remove("open");
    if (view === "jobs") renderAllJobs();
    if (view === "companies") renderCompanies();
  }

  function renderJob(job) {
    const link = safeHref(job.url);
    const company = escapeHtml(job.company_name);
    const title = escapeHtml(job.title);
    const location = job.location ? `<span class="job-location">${icon.pin}${escapeHtml(job.location)}</span>` : "";
    return `<article class="job-row"><span class="company-monogram">${escapeHtml(initials(job.company_name))}</span><div class="job-copy"><a class="job-title" href="${escapeHtml(link)}" target="_blank" rel="noopener noreferrer">${title}</a><div class="job-company-line">${company}${location}</div></div><span class="job-time">${escapeHtml(relativeTime(job.discovered_at))}</span><a class="job-arrow" href="${escapeHtml(link)}" target="_blank" rel="noopener noreferrer" aria-label="Open job">${icon.arrow}</a></article>`;
  }

  function renderJobs() {
    const list = $("#recent-jobs");
    const recent = state.jobs.slice(0, 5);
    if (!recent.length) {
      list.innerHTML = `<div class="empty-state"><span class="empty-icon">${icon.briefcase}</span><strong>No new matches just yet</strong><p>Your current openings are baselined quietly. New roles that match your filters will appear here and go to Telegram.</p></div>`;
      return;
    }
    list.innerHTML = recent.map(renderJob).join("");
  }

  function renderAllJobs() {
    const query = $("#job-search").value.trim().toLowerCase();
    const sourceJobs = state.jobScope === "all" ? state.allJobs : state.jobs;
    const filtered = sourceJobs.filter((job) => `${job.company_name} ${job.title} ${job.location}`.toLowerCase().includes(query));
    const container = $("#all-jobs");
    $("#jobs-heading").textContent = state.jobScope === "all" ? "All current openings" : "Matching openings";
    $("#jobs-subtitle").textContent = state.jobScope === "all" ? `${filtered.length} openings discovered across your tracked portals` : `${filtered.length} openings match your company filters`;
    $("#scope-matching").classList.toggle("active", state.jobScope === "matching");
    $("#scope-matching").setAttribute("aria-pressed", String(state.jobScope === "matching"));
    $("#scope-all").classList.toggle("active", state.jobScope === "all");
    $("#scope-all").setAttribute("aria-pressed", String(state.jobScope === "all"));
    if (!query) {
      $("#matching-count").textContent = state.status?.jobs ?? state.jobs.length;
      $("#all-openings-count").textContent = state.status?.all_jobs ?? state.allJobs.length;
    }
    container.innerHTML = filtered.length ? filtered.map(renderJob).join("") : `<div class="empty-state"><span class="empty-icon">${icon.briefcase}</span><strong>${query ? "No roles match that search" : state.jobScope === "matching" ? "No openings match your filters" : "No openings found yet"}</strong><p>${query ? "Try another title, company, or location." : state.jobScope === "matching" ? "Edit a company’s keywords or location filters to see more roles here." : "Openings will appear after the first scan of each portal."}</p></div>`;
  }

  async function setJobScope(scope) {
    state.jobScope = scope;
    try {
      const jobs = await api(`/api/jobs?scope=${scope}`);
      if (scope === "all") state.allJobs = jobs;
      else state.jobs = jobs;
      if (state.view === "jobs") renderAllJobs();
    } catch (error) { showToast(`Could not load openings: ${error.message}`, true); }
  }

  function companyStatus(company) {
    const failed = company.last_scan_status === "error";
    const status = failed ? "Issue" : company.last_scanned_at ? `${Number(company.last_job_count) || 0} roles` : "Waiting";
    const title = failed ? ` title="${escapeHtml(company.last_scan_error || "Portal scan failed")}"` : "";
    return `<span class="health-indicator${failed ? " error" : ""}"${title}><i></i>${status}</span>`;
  }

  function renderCompanies() {
    const mini = $("#mini-companies");
    const grid = $("#company-grid");
    if (!state.companies.length) {
      const empty = `<div class="empty-state"><span class="empty-icon">${icon.briefcase}</span><strong>Your watchlist starts here</strong><p>Add a company careers page and your radar will start looking for new roles.</p></div>`;
      mini.innerHTML = empty;
      grid.innerHTML = `<div class="surface" style="grid-column:1/-1">${empty}</div>`;
      return;
    }
    mini.innerHTML = state.companies.slice(0, 4).map((company) => `<div class="mini-company"><span class="company-monogram">${escapeHtml(initials(company.name))}</span><span class="mini-company-name" title="${escapeHtml(company.name)}">${escapeHtml(company.name)}</span>${companyStatus(company)}</div>`).join("");
    grid.innerHTML = state.companies.map((company) => {
      let keywords = [], locations = [];
      try { keywords = JSON.parse(company.keywords || "[]"); } catch {}
      try { locations = JSON.parse(company.locations || "[]"); } catch {}
      const filters = [...keywords.map((item) => `Role · ${item}`), ...locations.map((item) => `Place · ${item}`)];
      return `<article class="company-card"><div class="company-card-top"><span class="company-monogram">${escapeHtml(initials(company.name))}</span><div class="company-card-heading"><h3>${escapeHtml(company.name)}</h3><a class="company-url" href="${escapeHtml(safeHref(company.url))}" target="_blank" rel="noopener noreferrer">${escapeHtml(company.url)}</a></div><div class="company-menu"><button class="icon-text-button edit-company" data-name="${escapeHtml(company.name)}" title="Edit company" aria-label="Edit ${escapeHtml(company.name)}">${icon.edit}</button><button class="icon-text-button delete-company" data-name="${escapeHtml(company.name)}" title="Remove company" aria-label="Remove ${escapeHtml(company.name)}">${icon.trash}</button></div></div><div class="company-card-tags">${filters.length ? filters.map((filter) => `<span class="filter-tag">${escapeHtml(filter)}</span>`).join("") : '<span class="filter-tag">All new roles</span>'}</div><div class="company-card-footer"><span class="company-status${company.last_scan_status === "error" ? " error" : ""}" title="${escapeHtml(company.last_scan_error || "")}"><i></i>${company.last_scan_status === "error" ? "Needs attention" : company.last_scanned_at ? `${Number(company.last_job_count) || 0} current openings` : "First scan pending"}</span><span>${company.last_scanned_at ? `Checked ${escapeHtml(relativeTime(company.last_scanned_at))}` : "Not checked yet"}</span></div></article>`;
    }).join("");
    $$(".edit-company", grid).forEach((button) => button.addEventListener("click", () => openCompanyModal(state.companies.find((company) => company.name === button.dataset.name))));
    $$(".delete-company", grid).forEach((button) => button.addEventListener("click", () => deleteCompany(button.dataset.name)));
  }

  function renderStatus() {
    const status = state.status;
    if (!status) return;
    const companyErrors = (status.companies_detail || []).filter((company) => company.last_scan_status === "error");
    const card = $("#monitor-card");
    card.classList.toggle("is-running", status.monitoring);
    card.classList.toggle("is-scanning", status.scanning || status.scan_requested);
    $("#monitor-label").textContent = status.scanning ? "CHECKING CAREER PAGES" : status.monitoring ? "RADAR IS ACTIVE" : "MONITORING PAUSED";
    $("#monitor-title").textContent = status.scanning ? "A fresh look around." : status.monitoring ? "Your radar is watching." : "Your radar is ready.";
    $("#monitor-description").textContent = companyErrors.length ? `${companyErrors.length} portal${companyErrors.length === 1 ? " needs" : "s need"} attention. See the company list for details.` : status.pending ? `${status.pending} alert${status.pending === 1 ? " is" : "s are"} waiting for Telegram. Check your delivery settings.` : status.monitoring ? "We’ll let you know when something worth a look appears." : "Start monitoring to catch new roles as soon as they appear.";
    $("#monitor-action-text").textContent = status.monitoring ? "Pause monitoring" : "Start monitoring";
    $("#monitor-action-icon").textContent = status.monitoring ? "Ⅱ" : "▶";
    $("#monitor-action").disabled = status.scanning && !status.monitoring;
    $("#scan-now").textContent = status.scanning || status.scan_requested ? "Scan queued…" : "Scan now ↗";
    $("#scan-now").disabled = status.scanning || status.scan_requested;
    $("#company-count-label").textContent = `${status.companies} ${status.companies === 1 ? "company" : "companies"} tracked`;
    $("#last-scan-label").textContent = status.scanning ? "Scan in progress" : status.last_scan_finished ? `Last checked ${formatDate(status.last_scan_finished)}` : "No scans yet";
    $("#stat-jobs").textContent = status.jobs;
    $("#stat-companies").textContent = status.companies;
    $("#nav-job-count").textContent = status.jobs;
    $("#stat-next").textContent = status.scanning ? "In progress" : status.next_scan_at ? formatDate(status.next_scan_at) : status.monitoring ? "Coming up" : "When started";
    $("#stat-interval").textContent = `Every ${status.interval_minutes} ${status.interval_minutes === 1 ? "minute" : "minutes"}`;
    const connected = status.telegram_configured;
    $("#telegram-card").classList.toggle("is-connected", connected);
    $("#telegram-status").textContent = connected ? "Alerts are ready for your next match." : "Set up your channel to get new matches delivered.";
    $("#telegram-link").textContent = connected ? "Manage delivery →" : "Connect Telegram →";
    $("#telegram-check").textContent = connected ? "✓" : "•";
    renderCompanies();
  }

  async function refresh() {
    try {
      const [status, jobs, allJobs, companies] = await Promise.all([api("/api/status"), api("/api/jobs?scope=matching"), api("/api/jobs?scope=all"), api("/api/companies")]);
      state.status = status;
      state.jobs = jobs;
      state.allJobs = allJobs;
      state.companies = companies;
      renderStatus();
      renderJobs();
      if (state.view === "jobs") renderAllJobs();
      if (state.view === "companies") renderCompanies();
    } catch (error) {
      showToast(`Could not refresh dashboard: ${error.message}`, true);
    }
  }

  async function control(action) {
    try {
      await api(`/api/control/${action}`, { method: "POST", body: "{}" });
      if (action === "start") showToast("Your radar is on. The first scan is starting.");
      if (action === "pause") showToast("Monitoring paused.");
      if (action === "scan") showToast("A fresh scan has been queued.");
      setTimeout(refresh, 250);
    } catch (error) { showToast(error.message, true); }
  }

  function openCompanyModal(company = null) {
    state.editing = company || null;
    $("#company-modal-title").textContent = company ? "Edit company" : "Add a company";
    $("#save-company").innerHTML = company ? "Save changes <span>→</span>" : "Add to radar <span>→</span>";
    $("#company-name").value = company?.name || "";
    $("#company-name").readOnly = Boolean(company);
    $("#company-name").classList.toggle("read-only", Boolean(company));
    $("#company-url").value = company?.url || "";
    let keywords = [], locations = [];
    try { keywords = JSON.parse(company?.keywords || "[]"); } catch {}
    try { locations = JSON.parse(company?.locations || "[]"); } catch {}
    $("#company-keywords").value = keywords.join(", ");
    $("#company-locations").value = locations.join(", ");
    $("#company-message").textContent = "";
    $("#company-message").classList.remove("error");
    $("#company-modal").classList.add("open");
    $("#company-modal").setAttribute("aria-hidden", "false");
    setTimeout(() => (company ? $("#company-url") : $("#company-name")).focus(), 80);
  }

  function closeCompanyModal() {
    $("#company-modal").classList.remove("open");
    $("#company-modal").setAttribute("aria-hidden", "true");
    $("#company-form").reset();
    $("#company-name").readOnly = false;
    state.editing = null;
  }

  async function deleteCompany(name) {
    if (!window.confirm(`Remove ${name} from your radar? Its stored job history will be removed too.`)) return;
    try {
      await api("/api/companies/remove", { method: "POST", body: JSON.stringify({ name }) });
      showToast(`${name} removed from your radar.`);
      await refresh();
    } catch (error) { showToast(error.message, true); }
  }

  async function loadSettings() {
    try {
      const settings = await api("/api/settings");
      $("#chat-id").value = settings.chat_id || "";
      $("#interval").value = String(settings.interval_minutes);
      $("#token-hint").textContent = settings.bot_token_configured ? "A bot token is saved. Leave this blank to keep it unchanged." : "Your token is stored locally and never shown after saving.";
    } catch (error) { $("#settings-message").textContent = error.message; }
  }

  function connectEvents() {
    $$(".nav-item[data-view]").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));
    $$('[data-open-view="jobs"]').forEach((button) => button.addEventListener("click", () => switchView("jobs")));
    $$('[data-open-view="companies"]').forEach((button) => button.addEventListener("click", () => switchView("companies")));
    $$('[data-open-view="settings"]').forEach((button) => button.addEventListener("click", () => { switchView("settings"); loadSettings(); }));
    $("#monitor-action").addEventListener("click", () => control(state.status?.monitoring ? "pause" : "start"));
    $("#scan-now").addEventListener("click", () => control("scan"));
    $("#refresh-button").addEventListener("click", refresh);
    $("#mobile-menu").addEventListener("click", () => $("#sidebar").classList.toggle("open"));
    $("#quit-app").addEventListener("click", async () => {
      if (!window.confirm("Quit Career Radar? Monitoring will stop until you open the app again.")) return;
      try {
        await api("/api/control/quit", { method: "POST", body: "{}" });
        document.body.innerHTML = '<main style="display:grid;min-height:100vh;place-items:center;background:#f6f7f5;font:14px Inter,Segoe UI,sans-serif;color:#526057"><div style="text-align:center"><div style="font-size:18px;font-weight:650;margin-bottom:8px">Radar is off for now.</div><div style="color:#8b978e">You can close this tab and reopen Career Radar whenever you’re ready.</div></div></main>';
      } catch (error) { showToast(error.message, true); }
    });
    $("#add-company-top").addEventListener("click", () => openCompanyModal());
    $("#add-company-page").addEventListener("click", () => openCompanyModal());
    $("#add-company-small").addEventListener("click", () => openCompanyModal());
    $("#close-modal").addEventListener("click", closeCompanyModal);
    $("#cancel-modal").addEventListener("click", closeCompanyModal);
    $("#company-modal").addEventListener("click", (event) => { if (event.target === $("#company-modal")) closeCompanyModal(); });
    document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeCompanyModal(); });
    $("#job-search").addEventListener("input", renderAllJobs);
    $("#scope-matching").addEventListener("click", () => { if (state.jobScope !== "matching") setJobScope("matching"); });
    $("#scope-all").addEventListener("click", () => { if (state.jobScope !== "all") setJobScope("all"); });
    $("#settings-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = $("#save-settings");
      const message = $("#settings-message");
      button.disabled = true;
      message.classList.remove("error");
      message.textContent = "Saving your preferences…";
      try {
        const data = await api("/api/settings", { method: "POST", body: JSON.stringify({ bot_token: $("#bot-token").value, chat_id: $("#chat-id").value, interval_minutes: Number($("#interval").value) }) });
        $("#bot-token").value = "";
        $("#token-hint").textContent = data.bot_token_configured ? "A bot token is saved. Leave this blank to keep it unchanged." : "Your token is stored locally and never shown after saving.";
        message.textContent = "Settings saved. Your radar will use these preferences on its next check.";
        showToast("Settings saved.");
        await refresh();
      } catch (error) { message.textContent = error.message; message.classList.add("error"); }
      finally { button.disabled = false; }
    });
    $("#test-telegram").addEventListener("click", async () => {
      const button = $("#test-telegram"), message = $("#settings-message");
      button.disabled = true;
      message.classList.remove("error");
      message.textContent = "Sending a test message…";
      try {
        const result = await api("/api/settings/test-telegram", { method: "POST", body: "{}" });
        message.textContent = result.message;
        showToast(result.message);
      } catch (error) { message.textContent = error.message; message.classList.add("error"); }
      finally { button.disabled = false; }
    });
    $("#company-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = $("#save-company"), message = $("#company-message");
      button.disabled = true;
      message.textContent = "";
      message.classList.remove("error");
      try {
        const name = $("#company-name").value.trim();
        const wasEditing = Boolean(state.editing);
        await api("/api/companies", { method: "POST", body: JSON.stringify({ name, url: $("#company-url").value.trim(), keywords: parseList($("#company-keywords").value), locations: parseList($("#company-locations").value) }) });
        closeCompanyModal();
        showToast(wasEditing ? `${name} updated.` : `${name} added to your radar.`);
        await refresh();
      } catch (error) { message.textContent = error.message; message.classList.add("error"); }
      finally { button.disabled = false; }
    });
  }

  function init() {
    $("#today-label").textContent = new Intl.DateTimeFormat(undefined, { weekday: "short", month: "short", day: "numeric" }).format(new Date());
    connectEvents();
    refresh();
    setInterval(refresh, 10000);
  }

  document.addEventListener("DOMContentLoaded", init);
})();
