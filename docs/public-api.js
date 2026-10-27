"use strict";

// Static GitHub Pages adapter. It never accepts credentials or starts collectors.
// The only network request is for the sanitized, checked-in public data bundle.
(() => {
  const reviews = new Map();
  let bundlePromise;

  function clone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  async function bundle() {
    if (!bundlePromise) {
      bundlePromise = fetch(new URL("./public-data.json", document.baseURI), {cache: "no-cache"})
        .then(response => {
          if (!response.ok) throw new Error("The public sample could not be loaded. Reload the page to try again.");
          return response.json();
        });
    }
    return bundlePromise;
  }

  async function report(mode, snapshotId = "") {
    const data = await bundle();
    if (!Object.prototype.hasOwnProperty.call(data.reports, mode)) throw new Error("Unknown dataset.");
    const value = clone(data.reports[mode]);
    if (snapshotId && value.snapshot?.id !== snapshotId) throw new Error("This snapshot is not part of the public sample.");
    const key = mode + ":" + (value.snapshot?.id || "");
    value.feedback = clone(reviews.get(key) || {});
    return value;
  }

  async function request(path, input) {
    const url = new URL(path, "https://campaign-radar.invalid");
    if (url.pathname === "/api/bootstrap" && input === undefined) {
      const data = await bundle();
      return {...clone(data.bootstrap), connected: false, csrf: "", job: null};
    }
    if (url.pathname === "/api/report" && input === undefined) {
      return report(url.searchParams.get("mode") || "demo", url.searchParams.get("snapshot_id") || "");
    }
    if (url.pathname === "/api/job" && input === undefined) return null;
    if (url.pathname === "/api/feedback" && input) {
      const current = await report(input.mode, input.snapshot_id);
      if (!current.events.some(event => event.id === input.event_id)) throw new Error("Finding not found.");
      if (!["useful", "not_useful", "acted_on", ""].includes(input.value)) throw new Error("Unknown review choice.");
      const key = input.mode + ":" + current.snapshot.id;
      const value = {...(reviews.get(key) || {})};
      if (input.value) value[input.event_id] = input.value;
      else delete value[input.event_id];
      reviews.set(key, value);
      return {ok: true};
    }
    throw new Error("Live collection runs in the local Python app. This public demo does not accept API keys.");
  }

  function csvCell(value) {
    if (value !== null && typeof value === "object") value = JSON.stringify(value);
    let text = String(value ?? "");
    // CSV quoting does not neutralize spreadsheet formulas on its own.
    if (/^[\s]*[=+\-@]/.test(text) || /^[\t\r\n]/.test(text)) text = "'" + text;
    return '"' + text.replace(/"/g, '""') + '"';
  }

  async function exportCsv(path) {
    const url = new URL(path, "https://campaign-radar.invalid");
    if (!["/api/export", "/api/events/export"].includes(url.pathname)) throw new Error("Unknown export.");
    const current = await report(url.searchParams.get("mode") || "demo", url.searchParams.get("snapshot_id") || "");
    const changes = url.pathname === "/api/events/export";
    let columns, rows;
    if (changes) {
      columns = ["dataset", "company_name", "type", "title", "before", "after", "before_at", "after_at", "before_url", "after_url", "explanation", "suggested_action", "confidence", "feedback"];
      rows = current.events.map(event => ({...event, dataset: current.mode, feedback: current.feedback[event.id] || ""}));
    } else {
      columns = ["dataset", "company", "observed_at", "collection_status", "id", "text", "headline", "cta", "format", "library_url", "landing_url", "landing_url_source", "start_date", "end_date", "targeting"];
      rows = (current.snapshot?.companies || []).flatMap(company => company.ads.map(ad => ({
        ...ad, dataset: current.mode, company: company.name,
        observed_at: current.snapshot.collected_at, collection_status: company.status
      })));
    }
    const csv = "\ufeff" + [columns, ...rows.map(row => columns.map(key => row[key]))]
      .map(row => row.map(csvCell).join(",")).join("\r\n") + "\r\n";
    const objectUrl = URL.createObjectURL(new Blob([csv], {type: "text/csv;charset=utf-8"}));
    const anchor = document.createElement("a");
    anchor.href = objectUrl;
    anchor.download = "campaign-radar-" + (changes ? "changes" : "ads") + ".csv";
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
  }

  window.CAMPAIGN_RADAR_PUBLIC_API = Object.freeze({request, exportCsv});
})();
