# Local application contract

Python 3.9+ standard library; HTTP server on 127.0.0.1:8920; SQLite local persistence. No network calls in sample mode. Brand: Campaign Radar.

Snapshot: `{id, collected_at, mode: "live"|"demo", scope: {platform: "linkedin", date_range: "last-30-days", countries: [], max_ads_per_company: 10}, companies: [{key, name, linkedin_url, status: "complete"|"partial"|"failed", notes: [], run_id?, usage_usd?, ads: [Ad]}]}`.

Ad: `{id, advertiser_name, advertiser_id, text, headline, cta, landing_url, library_url, format, start_date, end_date, media_urls: [], targeting: {}, landing_page?: {url, status: "ok"|"failed"|"skipped", text, title, fetched_at, error?}}`. Ads have stable LinkedIn ID; absent IDs excluded. Data is untrusted. Never render as HTML. Textual audience inference is separate from disclosed targeting.

Event: `{id, company_key, company_name, type: "first_observed"|"offer_changed"|"messaging_changed"|"landing_page_changed"|"not_observed", title, before, after, before_at, after_at, before_url, after_url, explanation, suggested_action, confidence: "observed"|"review", ad_id}`. Never interpret absence as a campaign stop. Demo comparison stays in demo data.

GET /api/bootstrap -> `{csrf, connected, watchlist, budget: {cap_usd, spent_usd, reserved_usd, remaining_usd}, snapshots: [{id, collected_at, mode, ad_count, status}], job: null|Job}`. Five real CRM vendors are initial watchlist. Token connection is memory-only and not returned.

GET /api/report?mode=demo|live&snapshot_id=<optional> -> `{mode, snapshot: Snapshot|null, previous: {id,collected_at}|null, events: [Event], feedback: {event_id: "useful"|"not_useful"|"acted_on"}, summary: {companies, ads, changes, collection_status, baseline}, company_summary: [{key,name,ad_count,status,notes}], methodology: string}`. No live snapshots => snapshot:null, empty arrays and summary with zeroes.

GET /api/export?mode=demo|live&snapshot_id=<optional> -> CSV of ads, safely escaped spreadsheet cells. GET /api/events/export similarly change CSV. GET /api/job -> Job|null.

All POST endpoints require X-Session-Token header from bootstrap and same-origin check.
POST /api/connect {token} -> {connected:true}; POST /api/disconnect {} -> {connected:false}.
POST /api/scan {max_ads_per_company: 10, capture_pages: true} -> Job `{id,status:"running"|"done"|"error",message,snapshot_id?}`. Hard global development cap US$5 and a bounded per-Actor-run maxTotalChargeUsd; no automatic repeat starts. No arbitrary watchlist input in MVP. Check global job before starting. Status polls via GET /api/job.
POST /api/feedback {mode, snapshot_id, event_id, value:"useful"|"not_useful"|"acted_on"|""} -> {ok:true}.

Static UI: index.html, app.js, style.css. Frontend uses five watchlist, sample/live selection, snapshot history, overview counts, company and type filtering, ad search, evidence cards, expandable ad library, collection controls, CSV exports. Live scan never triggered on load or connect. Key only submitted on explicit connect then cleared from DOM. Show actual budget, async job, failures clearly, first-run baseline explanation. Demo uses fictional companies and deliberately illustrative changes, always labelled.
