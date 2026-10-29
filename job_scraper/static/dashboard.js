(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const state = { view: "overview", status: null, jobs: [], allJobs: [], jobScope: "matching", jobPage: 1, companyPage: 1, trackerPage: 1, trackerStatus: "", companies: [], editing: null, detailJob: null, toastTimer: null };
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

  function companyLogo(name) {
    const normalized = String(name || "").toLowerCase().replace(/[^a-z0-9]/g, "");
    const knownDomains = {
      statestreet: "statestreet.com", northerntrust: "northerntrust.com", ubs: "ubs.com",
      fis: "fisglobal.com", fisglobal: "fisglobal.com", apexgroup: "apexgroup.com", apexfundservices: "apexgroup.com",
      sstechnologies: "ssctech.com", ssctechnologies: "ssctech.com",
    };
    let domain = knownDomains[normalized];
    if (!domain) {
      const company = state.companies.find((item) => item.name === name);
      try { domain = new URL(company?.url).hostname; } catch { domain = ""; }
    }
    const source = domain ? `https://www.google.com/s2/favicons?domain=${encodeURIComponent(domain)}&sz=64` : "";
    return `<span class="company-monogram has-logo"><img data-company-logo src="${escapeHtml(source)}" alt="" loading="lazy"><span class="company-fallback">${escapeHtml(initials(name))}</span></span>`;
  }

  function activateCompanyLogos(root) {
    $$('img[data-company-logo]', root).forEach((image) => image.addEventListener("error", () => {
      image.closest(".company-monogram")?.classList.remove("has-logo");
      image.remove();
    }, { once: true }));
  }

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
    if (!["overview", "jobs", "applications", "companies", "settings"].includes(view)) return;
    state.view = view;
    $$(".view").forEach((section) => section.classList.toggle("active", section.id === `view-${view}`));
    $$(".nav-item[data-view]").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
    const label = ({ jobs: "Job matches", applications: "Applications" })[view] || view[0].toUpperCase() + view.slice(1);
    $("#crumb-current").textContent = label;
    $("#sidebar").classList.remove("open");
    if (view === "jobs") renderAllJobs();
    if (view === "applications") renderTracker();
    if (view === "companies") renderCompanies();
  }

  function renderJob(job) {
    const link = safeHref(job.url);
    const company = escapeHtml(job.company_name);
    const title = escapeHtml(job.title);
    const location = job.location ? `<span class="job-location">${icon.pin}${escapeHtml(job.location)}</span>` : "";
    const appStatus = job.application_status && job.application_status !== "new" ? `<span class="application-pill status-${escapeHtml(job.application_status)}">${escapeHtml(applicationStatusLabel(job.application_status))}</span>` : "";
    return `<article class="job-row">${companyLogo(job.company_name)}<div class="job-copy"><a class="job-title" href="${escapeHtml(link)}" target="_blank" rel="noopener noreferrer">${title}</a><div class="job-company-line">${company}${location}${appStatus}</div></div><span class="job-time">${escapeHtml(relativeTime(job.discovered_at))}</span><button class="job-details-button" type="button" data-company="${escapeHtml(job.company_name)}" data-url="${escapeHtml(job.url)}">Details</button><a class="job-arrow" href="${escapeHtml(link)}" target="_blank" rel="noopener noreferrer" aria-label="Open job">${icon.arrow}</a></article>`;
  }

  function applicationStatusLabel(status) {
    return ({ new: "Not started", interested: "Interested", applied: "Applied", interviewing: "Interviewing", offer: "Offer", rejected: "Rejected", withdrawn: "Withdrawn" })[status] || "Not started";
  }

  function getFilteredAllJobs() {
    const query = $("#job-search").value.trim().toLowerCase();
    const companyFilter = $("#job-company-filter").value;
    const sourceJobs = state.jobScope === "all" ? state.allJobs : state.jobs;
    return sourceJobs.filter((job) => (!companyFilter || job.company_name === companyFilter) && `${job.company_name} ${job.title} ${job.location}`.toLowerCase().includes(query));
  }

  function renderJobs() {
    const list = $("#recent-jobs");
    const companyFilter = $("#recent-company-filter").value;
    const matchingJobs = companyFilter ? state.jobs.filter((job) => job.company_name === companyFilter) : state.jobs;
    const recent = matchingJobs.slice(0, 5);
    if (!recent.length) {
      const message = companyFilter ? `No recent matches from ${escapeHtml(companyFilter)}` : "No new matches just yet";
      list.innerHTML = `<div class="empty-state"><span class="empty-icon">${icon.briefcase}</span><strong>${message}</strong><p>Your current openings are baselined quietly. New roles that match your filters will appear here and go to Telegram.</p></div>`;
      return;
    }
    list.innerHTML = recent.map(renderJob).join("");
    activateCompanyLogos(list);
  }

  function renderAllJobs() {
    const query = $("#job-search").value.trim().toLowerCase();
    const companyFilter = $("#job-company-filter").value;
    const filtered = getFilteredAllJobs();
    const container = $("#all-jobs");
    const pageSize = 20;
    const pageCount = Math.max(1, Math.ceil(filtered.length / pageSize));
    state.jobPage = Math.min(state.jobPage, pageCount);
    const pageJobs = filtered.slice((state.jobPage - 1) * pageSize, state.jobPage * pageSize);
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
    container.innerHTML = filtered.length ? pageJobs.map(renderJob).join("") : `<div class="empty-state"><span class="empty-icon">${icon.briefcase}</span><strong>${companyFilter || query ? "No roles match those filters" : state.jobScope === "matching" ? "No openings match your filters" : "No openings found yet"}</strong><p>${companyFilter || query ? "Choose another company or adjust the search." : state.jobScope === "matching" ? "Edit a company’s keywords or location filters to see more roles here." : "Openings will appear after the first scan of each portal."}</p></div>`;
    activateCompanyLogos(container);
    renderJobPagination(filtered.length, pageSize, pageCount);
  }

  function renderJobPagination(total, pageSize, pageCount) {
    const pagination = $("#job-pagination");
    if (total <= pageSize) {
      pagination.hidden = true;
      pagination.innerHTML = "";
      return;
    }
    pagination.hidden = false;
    const first = (state.jobPage - 1) * pageSize + 1;
    const last = Math.min(state.jobPage * pageSize, total);
    pagination.innerHTML = `<button class="pagination-button" type="button" data-job-page="${state.jobPage - 1}" ${state.jobPage === 1 ? "disabled" : ""}>← Previous</button><span class="pagination-summary">Showing ${first}–${last} of ${total} · Page ${state.jobPage} of ${pageCount}</span><button class="pagination-button" type="button" data-job-page="${state.jobPage + 1}" ${state.jobPage === pageCount ? "disabled" : ""}>Next →</button>`;
  }

  function renderMiniCompanies() {
    const list = $("#mini-companies");
    const pagination = $("#mini-company-pagination");
    const pageSize = 6;
    if (!state.companies.length) {
      list.innerHTML = `<div class="empty-state mini-company-empty"><strong>No companies yet</strong><p>Add a company to start your watchlist.</p></div>`;
      pagination.hidden = true;
      pagination.innerHTML = "";
      return;
    }
    const pageCount = Math.max(1, Math.ceil(state.companies.length / pageSize));
    state.companyPage = Math.min(state.companyPage, pageCount);
    const start = (state.companyPage - 1) * pageSize;
    const companies = state.companies.slice(start, start + pageSize);
    list.innerHTML = companies.map((company) => `<div class="mini-company"><input class="company-scan-toggle" type="checkbox" data-name="${escapeHtml(company.name)}" ${company.enabled ? "checked" : ""} aria-label="Include ${escapeHtml(company.name)} in live scans" title="${company.enabled ? "Included in live scans" : "Excluded from live scans"}">${companyLogo(company.name)}<span class="mini-company-name" title="${escapeHtml(company.name)}">${escapeHtml(company.name)}</span>${companyStatus(company)}</div>`).join("");
    activateCompanyLogos(list);
    bindCompanyScanToggles(list);
    pagination.hidden = state.companies.length <= pageSize;
    pagination.innerHTML = pagination.hidden ? "" : `<button class="mini-page-button" type="button" data-company-page="${state.companyPage - 1}" aria-label="Previous companies" ${state.companyPage === 1 ? "disabled" : ""}>‹</button><span>${state.companyPage} / ${pageCount}</span><button class="mini-page-button" type="button" data-company-page="${state.companyPage + 1}" aria-label="Next companies" ${state.companyPage === pageCount ? "disabled" : ""}>›</button>`;
  }

  function renderTracker() {
    const tracked = state.allJobs.filter((job) => job.application_status && job.application_status !== "new");
    const counts = Object.fromEntries(["interested", "applied", "interviewing", "offer", "rejected", "withdrawn"].map((status) => [status, tracked.filter((job) => job.application_status === status).length]));
    const activeApplications = counts.applied + counts.interviewing + counts.offer;
    $("#tracker-summary").innerHTML = `<article class="tracker-stat"><span>Tracked roles</span><strong>${tracked.length}</strong></article><article class="tracker-stat"><span>Applied or in process</span><strong>${activeApplications}</strong></article><article class="tracker-stat"><span>Offers</span><strong>${counts.offer}</strong></article>`;
    $("#nav-tracker-count").textContent = tracked.length;
    const filtered = state.trackerStatus ? tracked.filter((job) => job.application_status === state.trackerStatus) : tracked;
    const pageSize = 20;
    const pageCount = Math.max(1, Math.ceil(filtered.length / pageSize));
    state.trackerPage = Math.min(state.trackerPage, pageCount);
    const pageJobs = filtered.slice((state.trackerPage - 1) * pageSize, state.trackerPage * pageSize);
    $("#tracker-subtitle").textContent = `${filtered.length} saved ${filtered.length === 1 ? "application" : "applications"}`;
    const container = $("#tracked-jobs");
    container.innerHTML = pageJobs.length ? pageJobs.map(renderJob).join("") : `<div class="empty-state"><span class="empty-icon">${icon.briefcase}</span><strong>${state.trackerStatus ? `No ${escapeHtml(applicationStatusLabel(state.trackerStatus).toLowerCase())} roles` : "No applications tracked yet"}</strong><p>Open a job’s details and update its status to add it to your tracker.</p></div>`;
    activateCompanyLogos(container);
    renderSimplePagination("#tracker-pagination", filtered.length, pageSize, state.trackerPage, pageCount, "application-page");
  }

  function renderSimplePagination(selector, total, pageSize, page, pageCount, attribute) {
    const pagination = $(selector);
    if (total <= pageSize) {
      pagination.hidden = true;
      pagination.innerHTML = "";
      return;
    }
    pagination.hidden = false;
    const first = (page - 1) * pageSize + 1;
    const last = Math.min(page * pageSize, total);
    pagination.innerHTML = `<button class="pagination-button" type="button" data-${attribute}="${page - 1}" ${page === 1 ? "disabled" : ""}>← Previous</button><span class="pagination-summary">Showing ${first}–${last} of ${total} · Page ${page} of ${pageCount}</span><button class="pagination-button" type="button" data-${attribute}="${page + 1}" ${page === pageCount ? "disabled" : ""}>Next →</button>`;
  }

  function openJobDetails(job) {
    state.detailJob = job;
    $("#job-details-title").textContent = job.title;
    $("#job-details-company").textContent = job.company_name;
    $("#job-details-location").textContent = job.location || "Location not listed";
    $("#job-details-added").textContent = `Added ${relativeTime(job.discovered_at)}`;
    $("#job-details-description").textContent = job.description || "No description was included in the career portal listing.";
    $("#job-details-link").href = safeHref(job.url);
    $("#tracking-status").value = job.application_status || "new";
    $("#tracking-notes").value = job.application_notes || "";
    $("#tracking-message").textContent = "";
    $("#job-details-modal").classList.add("open");
    $("#job-details-modal").setAttribute("aria-hidden", "false");
    const fallbackDescription = job.description || "No description was included in the career portal listing.";
    $("#job-details-description").textContent = fallbackDescription === "No description was included in the career portal listing."
      ? "Loading the full job description…"
      : `${fallbackDescription}\n\nLoading full description…`;
    const params = new URLSearchParams({ company: job.company_name, url: job.url });
    api(`/api/jobs/details?${params}`).then((details) => {
      if (state.detailJob !== job) return;
      if (details.description) {
        job.description = details.description;
        [state.jobs, state.allJobs].forEach((jobs) => jobs.forEach((item) => {
          if (item.company_name === job.company_name && item.url === job.url) item.description = details.description;
        }));
        $("#job-details-description").textContent = details.description;
      } else {
        $("#job-details-description").textContent = fallbackDescription;
      }
      if (details.location) $("#job-details-location").textContent = details.location;
    }).catch(() => {
      if (state.detailJob === job) $("#job-details-description").textContent = fallbackDescription;
    });
  }

  function closeJobDetails() {
    $("#job-details-modal").classList.remove("open");
    $("#job-details-modal").setAttribute("aria-hidden", "true");
    state.detailJob = null;
  }

  async function saveTracking() {
    const job = state.detailJob;
    if (!job) return;
    const button = $("#save-tracking");
    const message = $("#tracking-message");
    button.disabled = true;
    message.classList.remove("error");
    message.textContent = "Saving…";
    try {
      const applicationStatus = $("#tracking-status").value;
      const applicationNotes = $("#tracking-notes").value;
      await api("/api/jobs/tracking", {
        method: "PATCH",
        body: JSON.stringify({ company_name: job.company_name, url: job.url, application_status: applicationStatus, application_notes: applicationNotes }),
      });
      [state.jobs, state.allJobs].forEach((jobs) => jobs.forEach((item) => {
        if (item.company_name === job.company_name && item.url === job.url) {
          item.application_status = applicationStatus;
          item.application_notes = applicationNotes;
        }
      }));
      message.textContent = "Tracking details saved.";
      renderJobs();
      renderTracker();
      if (state.view === "jobs") renderAllJobs();
      showToast("Application tracking saved.");
    } catch (error) {
      message.textContent = error.message;
      message.classList.add("error");
    } finally { button.disabled = false; }
  }

  function exportJobs(format) {
    const jobs = getFilteredAllJobs();
    if (!jobs.length) {
      showToast("There are no jobs in the current view to export.", true);
      return;
    }
    const fields = ["company_name", "title", "location", "application_status", "application_notes", "discovered_at", "url", "description"];
    let content, mime, extension;
    if (format === "json") {
      content = JSON.stringify(jobs.map((job) => Object.fromEntries(fields.map((field) => [field, job[field] || ""]))), null, 2);
      mime = "application/json;charset=utf-8";
      extension = "json";
    } else {
      const csvValue = (value) => {
        const raw = String(value ?? "");
        const safe = /^[\s]*[=+\-@]/.test(raw) ? `'${raw}` : raw;
        return `"${safe.replace(/"/g, '""')}"`;
      };
      content = [fields.join(","), ...jobs.map((job) => fields.map((field) => csvValue(job[field])).join(","))].join("\r\n");
      mime = "text/csv;charset=utf-8";
      extension = "csv";
    }
    const blob = new Blob(["\uFEFF", content], { type: mime });
    const downloadUrl = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = downloadUrl;
    link.download = `career-radar-${state.jobScope}-${new Date().toISOString().slice(0, 10)}.${extension}`;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(downloadUrl), 0);
    showToast(`Exported ${jobs.length} ${jobs.length === 1 ? "job" : "jobs"} as ${extension.toUpperCase()}.`);
  }

  function updateCompanyFilters() {
    ["#recent-company-filter", "#job-company-filter"].forEach((selector) => {
      const select = $(selector);
      const selected = select.value;
      select.innerHTML = `<option value="">All companies</option>${state.companies.map((company) => `<option value="${escapeHtml(company.name)}">${escapeHtml(company.name)}</option>`).join("")}`;
      if (state.companies.some((company) => company.name === selected)) select.value = selected;
    });
  }

  function setTheme(theme) {
    const dark = theme === "dark";
    document.body.dataset.theme = dark ? "dark" : "light";
    const button = $("#theme-toggle");
    button.setAttribute("aria-pressed", String(dark));
    button.setAttribute("aria-label", `Switch to ${dark ? "light" : "dark"} mode`);
    button.title = `Switch to ${dark ? "light" : "dark"} mode`;
    button.innerHTML = dark
      ? '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M4.93 4.93l1.42 1.42m11.3 11.3 1.42 1.42M2 12h2m16 0h2M4.93 19.07l1.42-1.42m11.3-11.3 1.42-1.42"/></svg>'
      : '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20.2 15.3A8.6 8.6 0 0 1 8.7 3.8 8.7 8.7 0 1 0 20.2 15.3Z"/></svg>';
    const themeColor = $("meta[name='theme-color']");
    if (themeColor) themeColor.content = dark ? "#151b17" : "#f6f7f5";
    try { localStorage.setItem("career-radar-theme", dark ? "dark" : "light"); } catch {}
  }

  async function setJobScope(scope) {
    state.jobScope = scope;
    state.jobPage = 1;
    try {
      const jobs = await api(`/api/jobs?scope=${scope}&limit=10000`);
      if (scope === "all") state.allJobs = jobs;
      else state.jobs = jobs;
      if (state.view === "jobs") renderAllJobs();
    } catch (error) { showToast(`Could not load openings: ${error.message}`, true); }
  }

  function companyStatus(company) {
    const failed = company.last_scan_status === "error";
    const status = company.enabled === 0 || company.enabled === false ? "Paused" : failed ? "Issue" : company.last_scanned_at ? `${Number(company.last_job_count) || 0} roles` : "Waiting";
    const title = failed ? ` title="${escapeHtml(company.last_scan_error || "Portal scan failed")}"` : "";
    return `<span class="health-indicator${failed ? " error" : ""}"${title}><i></i>${status}</span>`;
  }

  function renderCompanies() {
    renderMiniCompanies();
    const grid = $("#company-grid");
    if (!state.companies.length) {
      const empty = `<div class="empty-state"><span class="empty-icon">${icon.briefcase}</span><strong>Your watchlist starts here</strong><p>Add a company careers page and your radar will start looking for new roles.</p></div>`;
      grid.innerHTML = `<div class="surface" style="grid-column:1/-1">${empty}</div>`;
      return;
    }
    grid.innerHTML = state.companies.map((company) => {
      let keywords = [], locations = [];
      try { keywords = JSON.parse(company.keywords || "[]"); } catch {}
      try { locations = JSON.parse(company.locations || "[]"); } catch {}
      const filters = [...keywords.map((item) => `Role · ${item}`), ...locations.map((item) => `Place · ${item}`)];
      return `<article class="company-card${company.enabled ? "" : " company-disabled"}"><div class="company-card-top"><input class="company-scan-toggle" type="checkbox" data-name="${escapeHtml(company.name)}" ${company.enabled ? "checked" : ""} aria-label="Include ${escapeHtml(company.name)} in live scans" title="${company.enabled ? "Included in live scans" : "Excluded from live scans"}">${companyLogo(company.name)}<div class="company-card-heading"><h3>${escapeHtml(company.name)}</h3><a class="company-url" href="${escapeHtml(safeHref(company.url))}" title="${escapeHtml(company.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(company.url)}</a></div><div class="company-menu"><button class="icon-text-button edit-company" data-name="${escapeHtml(company.name)}" title="Edit company" aria-label="Edit ${escapeHtml(company.name)}">${icon.edit}</button><button class="icon-text-button delete-company" data-name="${escapeHtml(company.name)}" title="Remove company" aria-label="Remove ${escapeHtml(company.name)}">${icon.trash}</button></div></div><div class="company-card-tags">${filters.length ? filters.map((filter) => `<span class="filter-tag">${escapeHtml(filter)}</span>`).join("") : '<span class="filter-tag">All new roles</span>'}</div><div class="company-card-footer"><span class="company-status${company.last_scan_status === "error" ? " error" : ""}" title="${escapeHtml(company.last_scan_error || "")}"><i></i>${!company.enabled ? "Excluded from scans" : company.last_scan_status === "error" ? "Needs attention" : company.last_scanned_at ? `${Number(company.last_job_count) || 0} current openings` : "First scan pending"}</span><span>${company.last_scanned_at ? `Checked ${escapeHtml(relativeTime(company.last_scanned_at))}` : "Not checked yet"}</span></div></article>`;
    }).join("");
    activateCompanyLogos(grid);
    bindCompanyScanToggles(grid);
    $$(".edit-company", grid).forEach((button) => button.addEventListener("click", () => openCompanyModal(state.companies.find((company) => company.name === button.dataset.name))));
    $$(".delete-company", grid).forEach((button) => button.addEventListener("click", () => deleteCompany(button.dataset.name)));
  }

  function bindCompanyScanToggles(root) {
    $$(".company-scan-toggle", root).forEach((toggle) => toggle.addEventListener("change", async () => {
      const company = state.companies.find((item) => item.name === toggle.dataset.name);
      if (!company) return;
      const enabled = toggle.checked;
      toggle.disabled = true;
      let keywords = [], locations = [];
      try { keywords = JSON.parse(company.keywords || "[]"); } catch {}
      try { locations = JSON.parse(company.locations || "[]"); } catch {}
      try {
        await api("/api/companies", { method: "POST", body: JSON.stringify({ name: company.name, url: company.url, keywords, locations, enabled }) });
        showToast(`${company.name} ${enabled ? "included in" : "excluded from"} live scans.`);
        await refresh();
      } catch (error) {
        toggle.checked = !enabled;
        showToast(`Could not update ${company.name}: ${error.message}`, true);
      } finally {
        toggle.disabled = false;
      }
    }));
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
    const sheets = status.google_sheets || {};
    if (sheets.configured) {
      $("#sheets-hint").textContent = sheets.last_sync_status === "error"
        ? `Google Sheets sync error: ${sheets.last_sync_error || "check spreadsheet access and credentials"}`
        : sheets.pending
          ? `${sheets.pending} job record${sheets.pending === 1 ? " is" : "s are"} queued for Google Sheets.`
          : sheets.last_sync_at ? `Last Google Sheets sync: ${relativeTime(sheets.last_sync_at)}.` : "Google Sheets is configured; waiting for the first sync.";
    }
    $("#monitor-description").textContent = companyErrors.length ? `${companyErrors.length} portal${companyErrors.length === 1 ? " needs" : "s need"} attention. See the company list for details.`
      : sheets.configured && sheets.last_sync_status === "error" ? "Google Sheets sync needs attention. Check the spreadsheet settings and logs."
        : sheets.configured && sheets.pending ? `${sheets.pending} job record${sheets.pending === 1 ? " is" : "s are"} waiting to sync to Google Sheets.`
          : status.job_descriptions_pending ? `${status.job_descriptions_pending} job description${status.job_descriptions_pending === 1 ? " is" : "s are"} being enriched in the background.`
            : status.pending ? `${status.pending} alert${status.pending === 1 ? " is" : "s are"} waiting for Telegram. Check your delivery settings.`
              : status.monitoring ? "We’ll let you know when something worth a look appears." : "Start monitoring to catch new roles as soon as they appear.";
    $("#monitor-action-text").textContent = status.monitoring ? "Pause monitoring" : "Start monitoring";
    $("#monitor-action-icon").textContent = status.monitoring ? "Ⅱ" : "▶";
    $("#monitor-action").disabled = status.scanning && !status.monitoring;
    const scanInProgress = status.scanning || status.scan_requested;
    $("#scan-now-label").textContent = status.scanning ? "Scanning" : status.scan_requested ? "Queued" : "Scan now";
    $("#scan-now").classList.toggle("is-scanning", scanInProgress);
    $("#scan-now").setAttribute("aria-busy", String(scanInProgress));
    $("#scan-now").disabled = scanInProgress;
    const scanProgress = Math.max(0, Math.min(100, Number(status.scan_progress) || 0));
    const progress = $("#scan-progress");
    progress.hidden = !scanInProgress;
    $("#scan-progress-company").textContent = status.scan_requested && !status.scanning
      ? "Scan starting…"
      : status.scan_company ? `Scanning ${status.scan_company}…` : "Preparing scan…";
    $("#scan-button-percent").textContent = `${scanProgress}%`;
    $("#scan-button-percent").hidden = !scanInProgress;
    $("#scan-button-fill").style.width = `${scanProgress}%`;
    $("#scan-button-icon").hidden = scanInProgress;
    $("#scan-button-arrow").hidden = scanInProgress;
    if (scanInProgress) {
      $("#scan-now").setAttribute("role", "progressbar");
      $("#scan-now").setAttribute("aria-label", "Job scan progress");
      $("#scan-now").setAttribute("aria-valuemin", "0");
      $("#scan-now").setAttribute("aria-valuemax", "100");
      $("#scan-now").setAttribute("aria-valuenow", String(scanProgress));
    } else {
      $("#scan-now").removeAttribute("role");
      $("#scan-now").removeAttribute("aria-label");
      $("#scan-now").removeAttribute("aria-valuemin");
      $("#scan-now").removeAttribute("aria-valuemax");
      $("#scan-now").removeAttribute("aria-valuenow");
    }
    $("#company-count-label").textContent = `${status.companies} ${status.companies === 1 ? "company" : "companies"} tracked`;
    $("#last-scan-label").textContent = status.scanning ? "Scan in progress" : status.last_scan_finished ? `Last checked ${formatDate(status.last_scan_finished)}` : "No scans yet";
    $("#stat-jobs").textContent = status.jobs;
    $("#stat-companies").textContent = status.companies;
    $("#nav-job-count").textContent = status.jobs;
    $("#stat-next").textContent = status.scanning ? "In progress" : status.next_scan_at ? formatDate(status.next_scan_at) : status.monitoring ? "Coming up" : "When started";
    $("#stat-interval").textContent = `Every ${status.interval_minutes} ${status.interval_minutes === 1 ? "minute" : "minutes"}`;
    renderCompanies();
  }

  async function refresh() {
    try {
      const [status, jobs, allJobs, companies] = await Promise.all([api("/api/status"), api("/api/jobs?scope=matching&limit=10000"), api("/api/jobs?scope=all&limit=10000"), api("/api/companies")]);
      state.status = status;
      state.jobs = jobs;
      state.allJobs = allJobs;
      state.companies = companies;
      updateCompanyFilters();
      renderStatus();
      renderJobs();
      renderTracker();
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
    $("#company-enabled").checked = company?.enabled !== false;
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
      $("#google-spreadsheet-id").value = settings.google_spreadsheet_id || "";
      $("#sheets-hint").textContent = settings.google_credentials_configured
        ? "Service-account key found. Spreadsheet changes sync in the background."
        : "Job rows will wait locally until GOOGLE_SERVICE_ACCOUNT_FILE points to the shared service-account key.";
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
    $("#theme-toggle").addEventListener("click", () => setTheme(document.body.dataset.theme === "dark" ? "light" : "dark"));
    $("#recent-company-filter").addEventListener("change", renderJobs);
    $("#job-company-filter").addEventListener("change", () => { state.jobPage = 1; renderAllJobs(); });
    $("#job-search").addEventListener("input", () => { state.jobPage = 1; renderAllJobs(); });
    $("#tracker-status-filter").addEventListener("change", (event) => { state.trackerStatus = event.target.value; state.trackerPage = 1; renderTracker(); });
    $("#job-pagination").addEventListener("click", (event) => {
      const button = event.target.closest("[data-job-page]");
      if (!button || button.disabled) return;
      state.jobPage = Number(button.dataset.jobPage);
      renderAllJobs();
      $("#view-jobs").scrollIntoView({ behavior: "smooth", block: "start" });
    });
    $("#mini-company-pagination").addEventListener("click", (event) => {
      const button = event.target.closest("[data-company-page]");
      if (!button || button.disabled) return;
      state.companyPage = Number(button.dataset.companyPage);
      renderMiniCompanies();
    });
    $("#tracker-pagination").addEventListener("click", (event) => {
      const button = event.target.closest("[data-application-page]");
      if (!button || button.disabled) return;
      state.trackerPage = Number(button.dataset.applicationPage);
      renderTracker();
    });
    $("#tracked-jobs").addEventListener("click", (event) => {
      const button = event.target.closest(".job-details-button");
      if (!button) return;
      const job = state.allJobs.find((item) => item.company_name === button.dataset.company && item.url === button.dataset.url);
      if (job) openJobDetails(job);
    });
    $("#recent-jobs").addEventListener("click", (event) => {
      const button = event.target.closest(".job-details-button");
      if (!button) return;
      const job = state.jobs.find((item) => item.company_name === button.dataset.company && item.url === button.dataset.url);
      if (job) openJobDetails(job);
    });
    $("#all-jobs").addEventListener("click", (event) => {
      const button = event.target.closest(".job-details-button");
      if (!button) return;
      const jobs = state.jobScope === "all" ? state.allJobs : state.jobs;
      const job = jobs.find((item) => item.company_name === button.dataset.company && item.url === button.dataset.url);
      if (job) openJobDetails(job);
    });
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
    $("#close-job-details").addEventListener("click", closeJobDetails);
    $("#close-tracking").addEventListener("click", closeJobDetails);
    $("#job-details-modal").addEventListener("click", (event) => { if (event.target === $("#job-details-modal")) closeJobDetails(); });
    $("#tracking-form").addEventListener("submit", (event) => { event.preventDefault(); saveTracking(); });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        if ($("#job-details-modal").classList.contains("open")) closeJobDetails();
        if ($("#company-modal").classList.contains("open")) closeCompanyModal();
      }
    });
    $("#export-csv").addEventListener("click", () => exportJobs("csv"));
    $("#export-json").addEventListener("click", () => exportJobs("json"));
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
        const data = await api("/api/settings", { method: "POST", body: JSON.stringify({ bot_token: $("#bot-token").value, chat_id: $("#chat-id").value, interval_minutes: Number($("#interval").value), google_spreadsheet_id: $("#google-spreadsheet-id").value.trim() }) });
        $("#bot-token").value = "";
        $("#token-hint").textContent = data.bot_token_configured ? "A bot token is saved. Leave this blank to keep it unchanged." : "Your token is stored locally and never shown after saving.";
        message.textContent = data.google_spreadsheet_id ? "Settings saved. Jobs will sync to Google Sheets in the background." : "Settings saved. Your radar will use these preferences on its next check.";
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
        const result = await api("/api/settings/test-telegram", { method: "POST", body: JSON.stringify({ bot_token: $("#bot-token").value, chat_id: $("#chat-id").value }) });
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
        await api("/api/companies", { method: "POST", body: JSON.stringify({ name, url: $("#company-url").value.trim(), keywords: parseList($("#company-keywords").value), locations: parseList($("#company-locations").value), enabled: $("#company-enabled").checked }) });
        closeCompanyModal();
        showToast(wasEditing ? `${name} updated.` : `${name} added to your radar.`);
        await refresh();
      } catch (error) { message.textContent = error.message; message.classList.add("error"); }
      finally { button.disabled = false; }
    });
  }

  function init() {
    let savedTheme = "light";
    try { savedTheme = localStorage.getItem("career-radar-theme") || "light"; } catch {}
    setTheme(savedTheme);
    $("#today-label").textContent = new Intl.DateTimeFormat(undefined, { weekday: "short", month: "short", day: "numeric" }).format(new Date());
    connectEvents();
    refresh();
    setInterval(refresh, 10000);
  }

  document.addEventListener("DOMContentLoaded", init);
})();
