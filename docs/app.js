"use strict";
const $ = (id) => document.getElementById(id);
const PUBLIC_API = window.CAMPAIGN_RADAR_PUBLIC_API || null;
const state = {
  bootstrap: null,
  report: null,
  mode: "demo",
  view: "changes",
  viewChosen: false,
  snapshotId: "",
  busy: false,
  loading: false,
  pollTimer: null,
  requestSequence: 0
};
const TYPES = {
  first_observed: "First observed", offer_changed: "Offer change", messaging_changed: "Messaging change", landing_page_changed: "Landing page", not_observed: "Not observed"
};
function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}
function content(value) {
  if (value === undefined || value === null || value === "") return "No observation in this snapshot.";
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}
function date(value, short = false) {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  return new Intl.DateTimeFormat("en-GB", short ? {
    day: "numeric", month: "short", timeZone: "UTC"
  } : {
    day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", timeZone: "UTC"
  }).format(d) + (short ? "" : " UTC");
}
function money(value) {
  return typeof value === "number" && Number.isFinite(value) ? new Intl.NumberFormat("en-US", {
    style: "currency", currency: "USD"
  }).format(value) : "—";
}
function safeURL(value) {
  try {
    const u = new URL(value);
    return u.protocol === "https:" && !u.username && !u.password ? u.href : null;
  } catch (_) {
    return null;
  }
}
function link(label, href, className) {
  const url = safeURL(href);
  if (!url) return null;
  const node = element("a", className, label);
  node.href = url;
  node.target = "_blank";
  node.rel = "noopener noreferrer";
  return node;
}
function initials(name) {
  return String(name || "?").split(/\s+/).slice(0, 2).map(s => s[0]).join("").toUpperCase();
}
function sourceLabel(value, hasLink = false) {
  const labels = {
    explicit_destination: "Explicit destination", message_cta_link: "Message CTA link", commentary_link: "Link in ad copy"
  };
  return labels[value] || (value ? String(value).replace(/_/g, " ") : hasLink ? "Source unspecified" : "Not exposed");
}
function icon(name) {
  return element("span", "company-icon", initials(name));
}
function showError(message) {
  $("global-error").textContent = String(message);
  $("global-error").hidden = false;
}
function clearError() {
  $("global-error").hidden = true;
  $("global-error").textContent = "";
}
async function api(path, data) {
  if (PUBLIC_API) return PUBLIC_API.request(path, data);
  const options = data === undefined ? {
  } : {
    method: "POST", headers: {
      "Content-Type": "application/json", "X-Session-Token": state.bootstrap?.csrf || ""
    }, body: JSON.stringify(data)
  };
  const response = await fetch(path, {
    cache: "no-store", ...options
  });
  let result;
  try {
    result = await response.json();
  } catch (_) {
    throw new Error("The local server returned an unexpected response. Reload the dashboard and try again.");
  }
  if (!response.ok) throw new Error(result.error || "The request could not be completed.");
  return result;
}
function reportQuery() {
  const q = new URLSearchParams({
    mode: state.mode
  });
  if (state.snapshotId) q.set("snapshot_id", state.snapshotId);
  return q.toString();
}
function modeButtons() {
  ["live", "demo"].forEach(mode => {
    const button = $("mode-" + mode);
    button.classList.toggle("selected", state.mode === mode);
    button.setAttribute("aria-pressed", String(state.mode === mode));
  });
}
function setConnected(connected) {
  if (state.bootstrap) state.bootstrap.connected = connected;
  $("connection-state").textContent = connected ? "● Connected · token held in memory" : "Not connected";
  $("connection-state").classList.toggle("connected", connected);
  $("disconnect-button").hidden = !connected;
  $("connect-button").textContent = connected ? "Replace token" : "Connect";
  updateScanControls();
}
function updateScanControls() {
  const connected = Boolean(state.bootstrap?.connected);
  const exhausted = Number(state.bootstrap?.budget?.remaining_usd ?? 5) < 0.5;
  $("start-scan").disabled = !connected || state.busy || exhausted;
  $("start-scan").textContent = state.busy ? "Collecting…" : exhausted ? "Insufficient run allowance" : "Start collection";
  $("max-ads").disabled = state.busy;
  $("capture-pages").disabled = state.busy;
  $("disconnect-button").disabled = state.busy;
  $("connect-button").disabled = state.busy;
}
function showCollection(show) {
  $("collection-panel").hidden = !show;
  $("open-collection").setAttribute("aria-expanded", String(show));
  if (show) $("collection-panel").scrollIntoView({
    behavior: "smooth", block: "nearest"
  });
}
function renderBootstrap() {
  const b = state.bootstrap;
  const list = $("watchlist");
  list.replaceChildren();
  for (const company of b.watchlist || []) {
    const li = element("li");
    const name = company.name || company.key || "Company";
    li.append(icon(name), document.createTextNode(name));
    list.append(li);
  }
  const budget = b.budget || {
  };
  $("budget-remaining").textContent = money(budget.remaining_usd);
  $("budget-detail").textContent = `${money(budget.spent_usd)} provider-reported spend · ${money(budget.reserved_usd)} retained reservations · ${money(budget.cap_usd)} cap` + (Number(budget.unknown_cost_runs) > 0 ? ` · ${budget.unknown_cost_runs} run cost${Number(budget.unknown_cost_runs) === 1 ? "" : "s"} unconfirmed` : "");
  setConnected(Boolean(b.connected));
  renderSnapshotSelect();
  if (b.job) renderJob(b.job);
}
function renderSnapshotSelect() {
  const select = $("snapshot-select");
  select.replaceChildren();
  const snapshots = (state.bootstrap?.snapshots || []).filter(s => s.mode === state.mode);
  select.append(new Option(snapshots.length ? "Latest snapshot" : state.mode === "demo" ? "Illustrative comparison" : "No live snapshots yet", ""));
  for (const snapshot of snapshots) select.append(new Option(`${date(snapshot.collected_at)} · ${snapshot.ad_count ?? "?"} ads`, snapshot.id));
  if (snapshots.some(s => s.id === state.snapshotId)) select.value = state.snapshotId;
  else if (state.snapshotId) state.snapshotId = "";
  select.disabled = !snapshots.length || state.loading;
}
async function refreshBootstrap() {
  state.bootstrap = await api("/api/bootstrap");
  renderBootstrap();
}
async function loadReport() {
  const sequence = ++state.requestSequence;
  state.loading = true;
  $("snapshot-select").disabled = true;
  $("results-count").textContent = "Loading…";
  try {
    const report = await api("/api/report?" + reportQuery());
    if (sequence !== state.requestSequence) return;
    state.report = report;
    if (!state.viewChosen && state.mode === "live" && report.summary?.baseline && Number(report.summary?.ads) > 0) {
      state.view = "ads";
    }
    renderReport();
  } catch (error) {
    if (sequence === state.requestSequence) {
      showError(error.message);
      $("results-count").textContent = "Could not load data";
    }
  }   finally {
    if (sequence === state.requestSequence) {
      state.loading = false;
      renderSnapshotSelect();
    }
  }
}
function renderReport() {
  const r = state.report;
  const s = r.summary || {
  };
  modeButtons();
  $("metric-companies").textContent = s.companies ?? 0;
  $("metric-ads").textContent = s.ads ?? 0;
  $("metric-changes").textContent = r.events?.length ?? 0;
  $("nav-change-count").textContent = r.events?.length ?? 0;
  $("nav-ad-count").textContent = s.ads ?? 0;
  $("metric-companies-note").textContent = state.mode === "demo" ? "Fictional companies for the walkthrough" : "In this saved collection";
  $("metric-ads-note").textContent = state.mode === "demo" ? "Illustrative ads, not real research" : "Observed ads, not all active ads";
  $("metric-changes-note").textContent = s.baseline ? "Baseline · collect again to compare" : "Compared with prior comparable snapshot";
  renderReviewMetric();
  renderNotice();
  renderCoverage();
  const selected = $("company-filter").value;
  $("company-filter").replaceChildren(new Option("All companies", ""));
  for (const company of r.snapshot?.companies || []) $("company-filter").append(new Option(company.name, company.key));
  if ((r.snapshot?.companies || []).some(c => c.key === selected)) $("company-filter").value = selected;
  $("methodology-detail").textContent = r.methodology || "";
  renderView();
}
function renderReviewMetric() {
  const values = Object.values(state.report?.feedback || {
  });
  $("metric-useful").textContent = values.filter(v => v === "useful" || v === "acted_on").length;
  const acted = values.filter(v => v === "acted_on").length;
  $("metric-useful-note").textContent = PUBLIC_API ? "Demo review · this tab only" : acted ? `${acted} acted on · includes useful findings` : "Your review, not an AI score";
}
function renderNotice() {
  const r = state.report;
  const notice = $("dataset-notice");
  notice.replaceChildren();
  notice.className = "dataset-notice";
  let title, text, badge;
  if (state.mode === "demo") {
    title = "Illustrative walkthrough";
    text = "Fictional companies, ads and changes. This demonstrates the workflow; it is not real competitor research.";
    badge = "SAMPLE DATA";
  }   else if (!r.snapshot) {
    title = "Your live research starts here";
    text = "Connect Apify and collect the five-company watchlist. Your first collection becomes the baseline.";
    badge = "NO DATA YET";
    notice.classList.add("live-notice");
  }   else if (PUBLIC_API) {
    title = "Real-company baseline · " + date(r.snapshot.collected_at);
    text = "Historical sample from a real test run. This public demo does not collect new ads. Explore the change walkthrough or run the app locally with your own API key.";
    badge = "SAVED RESEARCH";
    notice.classList.add("live-notice");
  }   else if (r.summary?.baseline) {
    title = "Baseline saved";
    text = "These are real observations. Collect again later with the same settings to identify changes.";
    badge = "FIRST OBSERVATION";
    notice.classList.add("live-notice");
  }   else {
    title = "Live observations";
    text = "Changes compare saved observations. Public ad evidence does not reveal campaign performance.";
    badge = "REAL RESEARCH";
    notice.classList.add("live-notice");
  }
  const body = element("div");
  body.append(element("strong", "", title), element("span", "", text));
  notice.append(element("span", "notice-symbol", state.mode === "demo" ? "◈" : "◎"), body, element("span", "notice-badge", badge));
}
function renderCoverage() {
  const r = state.report;
  const target = $("company-coverage");
  target.replaceChildren();
  const previousDetails = $("coverage-section").querySelector(".coverage-details");
  if (previousDetails) previousDetails.remove();
  const issues = [];
  for (const company of r.company_summary || []) {
    const status = company.status || "unknown";
    const pill = element("span", "coverage-pill " + (status === "partial" || status === "failed" ? status : ""));
    pill.append(element("span", "coverage-dot"), document.createTextNode(company.name), element("span", "coverage-count", `${company.ad_count ?? 0} ads · ${status}`));
    target.append(pill);
    for (const note of company.notes || []) issues.push(`${company.name}: ${content(note)}`);
  }
  const providerLinks = [];
  if (state.mode === "live") for (const company of r.snapshot?.companies || []) {
    const provider = link(`${company.name}: view Apify run ↗`, company.run_url);
    if (provider) {
      const item = element("li");
      item.append(provider);
      providerLinks.push(item);
    }
  }
  if (issues.length || providerLinks.length) {
    const details = element("details", "coverage-details");
    details.append(element("summary", "", issues.length ? `${issues.length} collection note${issues.length === 1 ? "" : "s"} — review coverage${providerLinks.length ? " and source runs" : ""}` : "Collection source runs"));
    const list = element("ul");
    for (const issue of issues) list.append(element("li", "", issue));
    for (const provider of providerLinks) list.append(provider);
    details.append(list);
    $("coverage-section").append(details);
  }
  $("comparison-label").textContent = r.snapshot ? (r.previous ? `${date(r.previous.collected_at, true)} → ${date(r.snapshot.collected_at, true)} · UTC` : `${date(r.snapshot.collected_at)} · baseline`) : "No live collection yet";
}
function setView(view) {
  state.view = view;
  state.viewChosen = true;
  renderView();
}
function renderView() {
  document.querySelectorAll("[data-view]").forEach(button => {
    const active = button.dataset.view === state.view;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  const method = state.view === "method";
  $("research-view").hidden = method;
  $("method-view").hidden = !method;
  if (method) return;
  const ads = state.view === "ads";
  $("feed-title").textContent = ads ? "Ad library" : "Change feed";
  $("feed-description").textContent = ads ? (PUBLIC_API && state.mode === "live"
    ? "Saved excerpts from 27 October 2026. Open source links for the full creative."
    : "Inspect the copy and available evidence behind each collected ad.")
    : "What changed, why it may matter and where to verify it.";
  $("type-filter").hidden = ads;
  $("changes-list").hidden = ads;
  $("ads-list").hidden = !ads;
  $("export-link").textContent = ads ? "↓ Export ads" : "↓ Export changes";
  const exportPath = (ads ? "/api/export?" : "/api/events/export?") + reportQuery();
  $("export-link").href = PUBLIC_API ? "#download" : exportPath;
  $("export-link").dataset.exportPath = exportPath;
  $("export-link").setAttribute("aria-disabled", String(!state.report?.snapshot));
  renderItems();
}
function matchesQuery(item, company) {
  const q = $("search-input").value.trim().toLowerCase();
  const companyKey = $("company-filter").value;
  return (!companyKey || (company?.key || item.company_key) === companyKey) && (!q || [company?.name, item.company_name, item.text, item.headline, item.cta, item.title, item.before, item.after, item.explanation, item.landing_url].map(v => content(v)).join(" ").toLowerCase().includes(q));
}
function renderItems() {
  if (!state.report) return;
  const r = state.report;
  const target = state.view === "ads" ? $("ads-list") : $("changes-list");
  target.replaceChildren();
  if (!r.snapshot) {
    target.append(emptyState("Your first snapshot starts the story.", "Connect your Apify account to collect LinkedIn ads for the five companies. A later collection will let you compare what changed.", true));
    $("results-count").textContent = "No live data";
    return;
  }
  let count = 0;
  if (state.view === "ads") {
    for (const company of r.snapshot.companies || []) {
      for (const ad of company.ads || []) if (matchesQuery(ad, company)) {
        target.append(adCard(ad, company));
        count++;
      }
    }
  }   else {
    const type = $("type-filter").value;
    for (const event of r.events || []) if ((!type || event.type === type) && matchesQuery(event)) {
      target.append(changeCard(event));
      count++;
    }
  }
  $("results-count").textContent = `${count} ${state.view === "ads" ? "ad" : "finding"}${count === 1 ? "" : "s"}`;
  if (!count) {
    const filtered = $("company-filter").value || $("search-input").value || (state.view !== "ads" && $("type-filter").value);
    target.append(filtered ? emptyState("No matches in this view.", "Try a different company, change type or search phrase.") : r.summary?.baseline && state.view !== "ads" ? emptyState("Baseline captured. Changes come next.", "Explore the saved ads in the Ad library. Collect again later with the same scope to compare observations.") : emptyState(state.view === "ads" ? "No ads were returned." : "No changes to review.", state.view === "ads" ? "Check the collection coverage notes. An empty result does not prove the company is not advertising." : "The selected snapshots contain no comparable changes. Check collection notes for gaps or partial results."));
  }
}
function emptyState(title, description, button = false) {
  const node = element("div", "empty-state");
  node.append(element("span", "empty-symbol", "◎"), element("h3", "", title), element("p", "", description));
  if (button) {
    const action = element("button", "button button-primary", "Set up live collection");
    action.type = "button";
    action.addEventListener("click", () => showCollection(true));
    node.append(action);
  }
  return node;
}
function evidenceBox(label, value, timestamp, href, after = false) {
  const box = element("div", "evidence-box" + (after ? " after" : ""));
  const heading = element("div", "evidence-label");
  const time = element("time", "", date(timestamp, true));
  if (timestamp) time.dateTime = timestamp;
  heading.append(document.createTextNode(label), time);
  box.append(heading);
  const quote = content(value);
  box.append(element("blockquote", "", quote.length > 330 ? quote.slice(0, 327) + "…" : quote));
  if (quote.length > 330) {
    const details = element("details", "quote-details");
    details.append(element("summary", "", "Read full evidence"), element("p", "", quote));
    box.append(details);
  }
  if (state.mode === "demo") box.append(element("span", "sample-evidence", "Illustrative evidence"));
  else {
    const source = link("View source ↗", href, "evidence-link");
    if (source) box.append(source);
  }
  return box;
}
function changeCard(event) {
  const card = element("article", "change-card");
  const top = element("div", "card-top");
  const company = element("div", "card-company");
  company.append(icon(event.company_name), document.createTextNode(event.company_name));
  top.append(company, element("span", "change-tag " + (Object.prototype.hasOwnProperty.call(TYPES, event.type) ? event.type : ""), TYPES[event.type] || "Observation"));
  card.append(top);
  const body = element("div", "card-body");
  body.append(element("h3", "change-title", event.title));
  const evidence = element("div", "evidence-grid");
  evidence.append(evidenceBox("Before", event.before, event.before_at, event.before_url), evidenceBox("After", event.after, event.after_at, event.after_url, true));
  body.append(evidence);
  if (event.explanation) body.append(element("p", "interpretation", event.explanation));
  if (event.suggested_action) {
    const action = element("p", "suggested-action");
    action.append(element("strong", "", "Next step: "), document.createTextNode(event.suggested_action));
    body.append(action);
  }
  body.append(element("span", "confidence-label", event.confidence === "review" ? "◇ Interpretation requires review" : "✓ Observed difference · review its meaning"));
  card.append(body);
  const footer = element("div", "card-footer");
  footer.append(element("span", "review-label", PUBLIC_API ? "Try reviewing · this tab only" : "Was this useful?"));
  for (const [value, label] of [["useful", "Useful"], ["not_useful", "Not useful"], ["acted_on", "Acted on"]]) {
    const button = element("button", "feedback-button", label);
    button.type = "button";
    button.dataset.value = value;
    const selected = state.report.feedback?.[event.id] === value;
    button.classList.toggle("selected", selected);
    button.setAttribute("aria-pressed", String(selected));
    button.setAttribute("aria-label", `${label}: ${event.title}`);
    button.addEventListener("click", () => saveFeedback(event, value, footer));
    footer.append(button);
  }
  card.append(footer);
  return card;
}
async function saveFeedback(event, value, footer) {
  const snapshotId = state.report.snapshot.id;
  const mode = state.mode;
  const next = state.report.feedback?.[event.id] === value ? "" : value;
  const buttons = footer.querySelectorAll("button");
  buttons.forEach(b => b.disabled = true);
  try {
    await api("/api/feedback", {
      mode, snapshot_id: snapshotId, event_id: event.id, value: next
    });
    if (state.mode !== mode || state.report?.snapshot?.id !== snapshotId) return;
    state.report.feedback = state.report.feedback || {
    };
    if (next) state.report.feedback[event.id] = next;
    else delete state.report.feedback[event.id];
    renderReviewMetric();
    buttons.forEach(b => {
      const selected = b.dataset.value === next;
      b.classList.toggle("selected", selected);
      b.setAttribute("aria-pressed", String(selected));
    });
  } catch (error) {
    showError(error.message);
  } finally {
    buttons.forEach(b => b.disabled = false);
  }
}
function metaLine(label, value) {
  const row = element("div", "ad-meta-line");
  row.append(element("span", "", label), element("span", "", value));
  return row;
}
function adCard(ad, company) {
  const card = element("details", "ad-card");
  const summary = element("summary");
  const heading = element("div", "ad-summary");
  const headline = ad.headline || ad.text || "Ad copy unavailable";
  heading.append(element("div", "ad-summary-title", headline), element("div", "ad-summary-meta", `${company.name} · Ad ${ad.id}`));
  summary.append(icon(company.name), heading, element("span", "ad-format", ad.format || "Ad"));
  card.append(summary);
  const body = element("div", "ad-content");
  const copy = element("div");
  if (ad.headline) copy.append(element("h3", "ad-headline", ad.headline));
  copy.append(element("div", "ad-copy", ad.text || (PUBLIC_API && ad.is_excerpt
    ? "Open the LinkedIn source for the full creative."
    : "No ad copy was exposed by the source.")));
  if (PUBLIC_API && ad.is_excerpt) {
    copy.append(element("p", "help", ad.excerpt_notice || "Saved excerpt. Open the LinkedIn source for the full creative."));
  }
  const sources = element("div", "ad-source-links");
  if (state.mode === "demo") sources.append(element("span", "sample-evidence", "Fictional ad for demonstration. No live source."));
  else {
    const library = link("LinkedIn Ad Library ↗", ad.library_url);
    const source = String(ad.landing_url_source || "");
    const landingLabel = source.includes("commentary") ? "Link found in ad copy ↗" : source.includes("cta") || source === "explicit_destination" ? "Destination page ↗" : "Source-exposed link ↗";
    const landing = link(landingLabel, ad.landing_url);
    if (library) sources.append(library);
    if (landing) sources.append(landing);
  }
  copy.append(sources);
  const classification = ad.classification || {
  };
  const offers = Array.isArray(classification.offer_terms) ? classification.offer_terms : [];
  const audiences = Array.isArray(classification.audience_terms) ? classification.audience_terms : [];
  if (offers.length || audiences.length) {
    const cues = element("details", "ad-targeting");
    cues.append(element("summary", "", "Message cues · rule-based"));
    if (offers.length) cues.append(element("p", "", "Offer language: " + offers.join(", ")));
    if (audiences.length) cues.append(element("p", "", "Audience language: " + audiences.join(", ") + ". This is copy interpretation, not confirmed ad targeting."));
    copy.append(cues);
  }
  body.append(copy);
  const meta = element("div", "ad-meta");
  meta.append(metaLine("CTA", ad.cta || (PUBLIC_API && ad.is_excerpt ? "Open source for CTA" : "Not disclosed")), metaLine("Link evidence", sourceLabel(ad.landing_url_source, Boolean(ad.landing_url))), metaLine("Ad first shown", ad.start_date ? date(ad.start_date, true) : "Not disclosed"), metaLine("Ad last shown", ad.end_date ? date(ad.end_date, true) : "Not disclosed"), metaLine("Page capture", ad.landing_page?.status || "Not available"));
  if (ad.landing_page?.error) meta.append(element("p", "help", ad.landing_page.error));
  const targeting = element("details", "ad-targeting");
  targeting.append(element("summary", "", "Disclosed targeting"));
  const t = ad.targeting;
  const hasTargeting = t && (typeof t === "string" ? t.trim().length : Object.keys(t).length);
  targeting.append(hasTargeting ? element("pre", "", content(t)) : element("p", "", "No targeting data was exposed for this ad. Audience language in ad copy does not establish who was targeted."));
  meta.append(targeting);
  const disclosures = ad.disclosures;
  if (disclosures && typeof disclosures === "object" && Object.keys(disclosures).length) {
    const disclosure = element("details", "ad-targeting");
    disclosure.append(element("summary", "", "Provider disclosures"), element("p", "", "Public transparency data, not an assessment of campaign results. These fields do not establish conversions, revenue or ROAS."));
    const labels = {
      has_eu_transparency_data: "EU transparency", impressions_range: "Impressions range", impressions_min: "Minimum impressions", impressions_max: "Maximum impressions", impressions_by_country: "Impressions by country", targeting_parameters: "Targeting parameters", paid_by: "Paid by"
    };
    for (const [key, value] of Object.entries(disclosures)) {
      const row = element("div", "disclosure-field");
      row.append(element("strong", "", labels[key] || key.replace(/_/g, " ")), element("pre", "", typeof value === "boolean" ? value ? "Yes" : "No" : content(value)));
      disclosure.append(row);
    }
    meta.append(disclosure);
  }
  body.append(meta);
  card.append(body);
  return card;
}
function renderJob(job) {
  if (!job) return;
  const active = job.status === "running";
  state.busy = active;
  $("collection-status").hidden = false;
  $("collection-status").className = "job-status " + (active ? "running" : job.status === "error" ? "error" : "done");
  $("collection-status").textContent = job.message || (active ? "Collecting LinkedIn ads…" : "Collection finished.");
  updateScanControls();
  if (active) schedulePoll();
}
function schedulePoll() {
  if (state.pollTimer) return;
  state.pollTimer = setTimeout(pollJob, 1800);
}
async function pollJob() {
  state.pollTimer = null;
  try {
    const job = await api("/api/job");
    if (!job) {
      state.busy = false;
      updateScanControls();
      return;
    }
    const wasBusy = state.busy;
    renderJob(job);
    if (wasBusy && job.status !== "running") {
      await refreshBootstrap();
      if (job.snapshot_id) {
        state.mode = "live";
        state.snapshotId = job.snapshot_id;
        modeButtons();
        renderSnapshotSelect();
      }
      await loadReport();
    }
  } catch (error) {
    $("collection-status").textContent = "Connection interrupted. Checking collection status again…";
    if (state.busy) schedulePoll();
    else showError(error.message);
  }
}
$("open-collection").addEventListener("click", () => showCollection($("collection-panel").hidden));
$("export-link").addEventListener("click", async event => {
  if (!PUBLIC_API) return;
  event.preventDefault();
  if (!state.report?.snapshot) return;
  clearError();
  try {
    await PUBLIC_API.exportCsv($("export-link").dataset.exportPath);
  } catch (error) {
    showError(error.message);
  }
});
$("close-collection").addEventListener("click", () => {
  showCollection(false);
  $("open-collection").focus();
});
document.querySelectorAll("[data-view]").forEach(button => button.addEventListener("click", () => setView(button.dataset.view)));
for (const mode of ["live", "demo"]) $("mode-" + mode).addEventListener("click", () => {
  if (state.mode === mode) return;
  state.mode = mode;
  state.viewChosen = false;
  if (mode === "demo") state.view = "changes";
  state.snapshotId = "";
  $("company-filter").value = "";
  clearError();
  modeButtons();
  renderSnapshotSelect();
  loadReport();
});
$("snapshot-select").addEventListener("change", () => {
  state.snapshotId = $("snapshot-select").value;
  clearError();
  loadReport();
});
for (const id of ["company-filter", "type-filter"]) $(id).addEventListener("change", renderItems);
$("search-input").addEventListener("input", renderItems);
$("connect-form").addEventListener("submit", async event => {
  event.preventDefault();
  clearError();
  const input = $("apify-token");
  let token = input.value.trim();
  if (!token) {
    showError("Enter your Apify API token to connect.");
    return;
  }
  $("connect-button").disabled = true;
  input.value = "";
  try {
    await api("/api/connect", {
      token
    });
    token = "";
    setConnected(true);
  } catch (error) {
    token = "";
    showError(error.message);
  } finally {
    updateScanControls();
  }
});
$("disconnect-button").addEventListener("click", async () => {
  clearError();
  try {
    await api("/api/disconnect", {
    });
    setConnected(false);
    $("apify-token").value = "";
  } catch (error) {
    showError(error.message);
  }
});
$("start-scan").addEventListener("click", async () => {
  if (state.busy || !state.bootstrap?.connected) return;
  clearError();
  state.busy = true;
  updateScanControls();
  $("collection-status").hidden = false;
  $("collection-status").className = "job-status running";
  $("collection-status").textContent = "Preparing the collection…";
  try {
    const job = await api("/api/scan", {
      max_ads_per_company: Number($("max-ads").value), capture_pages: $("capture-pages").checked
    });
    renderJob(job);
    if (job.status !== "running") {
      await refreshBootstrap();
      await loadReport();
    }
  } catch (error) {
    state.busy = false;
    updateScanControls();
    $("collection-status").className = "job-status error";
    $("collection-status").textContent = error.message;
  }
});
async function init() {
  if (PUBLIC_API) configurePublicMode();
  try {
    await refreshBootstrap();
    const live = (state.bootstrap.snapshots || []).some(s => s.mode === "live");
    state.mode = live ? "live" : "demo";
    if (state.busy) showCollection(true);
    renderSnapshotSelect();
    modeButtons();
    await loadReport();
  } catch (error) {
    showError("Could not connect to Campaign Radar. " + error.message);
    $("results-count").textContent = "Server unavailable";
  }
}
function configurePublicMode() {
  document.documentElement.classList.add("public-preview");
  $("open-collection").textContent = "Run with your own API key";
  $("collection-eyebrow").textContent = "RUN IT YOURSELF";
  $("collection-title").textContent = "Collect fresh ads on your computer";
  $("collection-description").textContent = "Explore the saved results here, then use the local Python app to collect and compare your own snapshots. No extra Python packages are required.";
  $("public-setup").hidden = false;
  $("collection-panel").querySelector(".collection-grid").hidden = true;
  $("apify-token").disabled = true;
  $("apify-token").value = "";
  $("mode-live").textContent = "Real company sample";
  $("mode-demo").textContent = "Change walkthrough";
  const watchlistLabel = document.querySelector(".sidebar-heading .sidebar-label");
  if (watchlistLabel) watchlistLabel.textContent = "OBSERVED COMPANIES";
  document.querySelector(".sidebar-footer").textContent = "Public portfolio demo · v0.1";
  document.querySelector(".main-footer > span:last-child").textContent = "Saved observations · No live collection on this site";
}
init();
