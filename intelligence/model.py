"""Pure, deterministic normalization and evidence-based snapshot comparison.

No model calls, network calls or implied campaign-performance measurements.
Audience words in creative are text signals, separate from disclosed targeting.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def _text(value, limit=20000):
    if value is None or isinstance(value, (dict, list, tuple, bool)):
        return ""
    return str(value).strip()[:limit]


def _normalized(value):
    return " ".join(unicodedata.normalize("NFKC", html.unescape(_text(value))).split())


def _first(raw, *keys):
    for key in keys:
        value = _text(raw.get(key))
        if value:
            return value
    return ""


def safe_url(value):
    """Retain only credential-free HTTP(S) URLs; fetching has stricter SSRF checks."""
    value = _text(value, 4000)
    try:
        parts = urlsplit(value)
        if (parts.scheme not in {"http", "https"} or not parts.hostname
                or parts.username or parts.password or any(ord(c) < 32 for c in value)):
            return ""
        # Accessing port also rejects malformed ports before a later network request.
        _ = parts.port
    except ValueError:
        return ""
    return value


def canonical_page_url(value):
    """Drop tracking parameters only; preserve content-selecting query parameters."""
    value = safe_url(value)
    if not value:
        return ""
    parts = urlsplit(value)
    query = [(key, val) for key, val in parse_qsl(parts.query, keep_blank_values=True)
             if not key.lower().startswith("utm_")
             and key.lower() not in {"gclid", "fbclid", "msclkid", "li_fat_id"}]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/",
                       urlencode(sorted(query)), ""))


def _links(value):
    if isinstance(value, str):
        candidate = safe_url(value)
        return [candidate] if candidate else []
    if isinstance(value, dict):
        return _links(value.get("url") or value.get("href") or value.get("link"))
    if isinstance(value, list):
        return [url for entry in value[:100] for url in _links(entry)]
    return []


def _external_url(value):
    value = safe_url(value)
    if not value:
        return ""
    host = urlsplit(value).hostname.lower()
    return "" if host == "linkedin.com" or host.endswith(".linkedin.com") else value


def _advertiser_id_from_url(value):
    """Only a numeric LinkedIn company URL proves an ID; a slug does not."""
    value = safe_url(value)
    if not value:
        return ""
    parts = urlsplit(value)
    host = parts.hostname.lower()
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return ""
    match = re.fullmatch(r"/company/(\d+)/?", parts.path)
    return match.group(1) if match else ""


def normalize_ad(raw, company, observed_at):
    """Normalize oski v0.1.8 output; reject conflicting advertiser IDs.

    A provider may omit details. Such rows retain identity/detail flags so the
    collector can mark the collection partial instead of claiming full coverage.
    Missing IDs are not replaced with unstable hashes of editable ad copy.
    """
    if not isinstance(raw, dict):
        return None
    identifier = _first(raw, "ad_id", "adId", "id")
    if not identifier or not re.fullmatch(r"[A-Za-z0-9_:-]{1,160}", identifier):
        return None
    advertiser_id = _first(raw, "advertiser_company_id", "advertiser_id", "companyId")
    identity_source = next((key for key in ("advertiser_company_id", "advertiser_id", "companyId")
                            if _text(raw.get(key))), "")
    if not advertiser_id:
        advertiser_id = _advertiser_id_from_url(raw.get("advertiser_url"))
        if advertiser_id:
            identity_source = "advertiser_url"
    expected_id = _text(company.get("linkedin_id"))
    if advertiser_id and expected_id and advertiser_id != expected_id:
        return None
    advertiser_name = _first(raw, "advertiser_name", "advertiserName")
    names = [company.get("name", "")] + list(company.get("aliases", []))
    exact_name = bool(advertiser_name) and _normalized(advertiser_name).casefold() in {
        _normalized(name).casefold() for name in names if name}
    identity = "verified_id" if advertiser_id and advertiser_id == expected_id else (
        "name_only" if exact_name else "unverified")

    library_url = safe_url(_first(raw, "ad_url", "library_url", "adUrl"))
    if library_url:
        hostname = urlsplit(library_url).hostname.lower()
        if hostname != "linkedin.com" and not hostname.endswith(".linkedin.com"):
            library_url = ""
    if not library_url and identifier.isdigit():
        library_url = "https://www.linkedin.com/ad-library/detail/" + identifier

    explicit = _external_url(_first(raw, "landing_page_url", "landing_url", "destination_url"))
    candidates = [(url, "message_cta_link") for url in _links(raw.get("message_ctas"))]
    candidates += [(url, "commentary_link") for url in _links(raw.get("commentary_links"))]
    if explicit:
        candidates.insert(0, (explicit, "explicit_destination"))
    external = []
    sources = []
    seen = set()
    for candidate, source in candidates:
        url = _external_url(candidate)
        if not url:
            continue
        canonical = canonical_page_url(url)
        if canonical not in seen:
            seen.add(canonical)
            external.append(url)
            sources.append(source)
    # Several links do not establish which one is the ad's landing page.
    landing_url = explicit or (external[0] if len(external) == 1 else "")
    landing_source = "explicit_destination" if explicit else (sources[0] if len(external) == 1 else "")
    media = []
    for key in ("image_urls", "video_urls", "document_page_urls", "media_urls"):
        media.extend(_links(raw.get(key)))
    targeting = raw.get("targeting")
    if not isinstance(targeting, dict):
        targeting = {}
    disclosures = {key: raw[key] for key in (
        "has_eu_transparency_data", "impressions_range", "impressions_min",
        "impressions_max", "impressions_by_country", "targeting_parameters", "paid_by")
        if key in raw}
    return {
        "id": identifier, "advertiser_name": advertiser_name,
        "advertiser_id": advertiser_id, "identity_status": identity, "identity_source": identity_source,
        "text": _first(raw, "commentary", "text", "message_body"),
        "headline": _first(raw, "headline", "title"),
        "cta": _first(raw, "cta", "call_to_action"),
        "landing_url": landing_url, "landing_url_source": landing_source, "landing_url_candidates": external,
        "library_url": library_url,
        "format": _first(raw, "format", "format_label", "creative_type"),
        "start_date": _first(raw, "first_shown", "start_date"),
        "end_date": _first(raw, "last_shown", "end_date"),
        "media_urls": list(dict.fromkeys(media)), "targeting": targeting,
        "disclosures": disclosures, "observed_at": _text(observed_at),
        "details_status": _first(raw, "details_status") or "unknown",
    }


OFFER_PATTERNS = (
    r"\b\d+[ -]day(?:s)?\s+(?:free\s+)?trial\b",
    r"\bfree\s+(?:trial|demo|assessment|consultation|guide|report|webinar)\b",
    r"\b(?:save|get)\s+\d+(?:\.\d+)?\s*%",
    r"\b\d+(?:\.\d+)?\s*%\s*(?:off|discount)\b",
    r"[$€£]\s*\d+(?:[.,]\d+)?(?:\s*(?:/|per\s+)(?:month|year|seat|user|mo|yr))?",
    r"\b(?:book|request|get)\s+(?:a\s+)?demo\b",
    r"\b(?:webinar|ebook|e-book|whitepaper|free trial)\b",
)
AUDIENCE_PATTERNS = (
    r"\b(?:sales|revenue|marketing|customer success|operations|revops)\s+(?:teams|leaders|managers|directors|professionals)\b",
    r"\b(?:founders|startups|small businesses|enterprises|agencies|recruiters)\b",
    r"\b(?:CFOs|CMOs|CROs|CEOs|COOs)\b",
)


def _copy(ad):
    return "\n".join(_text(ad.get(key)) for key in ("headline", "text", "cta") if _text(ad.get(key)))


def classify_ad(ad):
    """Return regex-matched text signals, never audience-targeting claims."""
    text = _copy(ad)
    def matches(patterns):
        return sorted({_normalized(match.group()).casefold() for pattern in patterns
                       for match in re.finditer(pattern, text, flags=re.IGNORECASE)})
    return {"offer_terms": matches(OFFER_PATTERNS), "audience_terms": matches(AUDIENCE_PATTERNS)}


def _scope_key(snapshot):
    scope = dict(snapshot.get("scope") or {})
    if isinstance(scope.get("countries"), list):
        scope["countries"] = sorted(str(country).upper() for country in scope["countries"])
    return json.dumps(scope, sort_keys=True, separators=(",", ":"))


def _ad_map(company):
    # Duplicate IDs are provider duplicates, not extra ads. Keep the first row.
    ads = {}
    for ad in company.get("ads", []):
        if isinstance(ad, dict) and ad.get("id"):
            ads.setdefault(str(ad["id"]), ad)
    return ads


def compare_snapshots(before, after):
    """Compare observations with compatible scopes. A first scan is a baseline."""
    if not before or not after or before.get("mode") != after.get("mode"):
        return []
    if _scope_key(before) != _scope_key(after):
        return []
    if before.get("id") == after.get("id") or before.get("collected_at", "") >= after.get("collected_at", ""):
        return []
    previous_companies = {c["key"]: c for c in before.get("companies", []) if c.get("key")}
    events = []
    before_at, after_at = before.get("collected_at", ""), after.get("collected_at", "")

    def emit(company, ad_id, kind, title, old, new, old_url, new_url, explanation, action, confidence="observed"):
        identity = [before.get("id"), after.get("id"), company["key"], ad_id, kind]
        event_id = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()[:24]
        events.append({
            "id": event_id, "company_key": company["key"], "company_name": company.get("name", company["key"]),
            "ad_id": ad_id, "type": kind, "title": title,
            "before": old, "after": new, "before_at": before_at, "after_at": after_at,
            "before_url": safe_url(old_url), "after_url": safe_url(new_url),
            "explanation": explanation, "suggested_action": action, "confidence": confidence,
        })

    for company in after.get("companies", []):
        previous = previous_companies.get(company.get("key"))
        if not previous or company.get("status") == "failed" or previous.get("status") == "failed":
            continue
        old_ads, new_ads = _ad_map(previous), _ad_map(company)
        for ad_id, ad in sorted(new_ads.items()):
            old = old_ads.get(ad_id)
            if old is None:
                detail_note = (" Creative details were unavailable, so this finding establishes the ad identity only."
                               if ad.get("details_status") != "ok" else "")
                emit(company, ad_id, "first_observed", "Ad first observed in this comparison", "", _copy(ad), "", ad.get("library_url", ""),
                     "This ad ID was collected now and was absent from the earlier saved snapshot. This is not evidence of its launch date; earlier collections can miss ads." + detail_note,
                     "Review the creative and decide whether it changes your positioning or test backlog.")
                continue
            old_copy, new_copy = _copy(old), _copy(ad)
            details_available = old.get("details_status") == ad.get("details_status") == "ok"
            if details_available and _normalized(old_copy) != _normalized(new_copy):
                old_signals, new_signals = classify_ad(old), classify_ad(ad)
                offer_change = old_signals["offer_terms"] != new_signals["offer_terms"]
                kind = "offer_changed" if offer_change else "messaging_changed"
                explanation = ("The same ad ID contains different offer-related text. A deterministic text rule flagged this for review; verify the offer's terms on the linked evidence."
                               if offer_change else "The same ad ID contains different headline, body or CTA text. Audience words describe the creative's message, not confirmed targeting.")
                emit(company, ad_id, kind, "Offer wording changed" if offer_change else "Ad messaging changed",
                     old_copy, new_copy, old.get("library_url", ""), ad.get("library_url", ""), explanation,
                     "Check whether your offer or messaging needs a response, then log a specific experiment.",
                     "review" if offer_change else "observed")
            old_page, new_page = old.get("landing_page") or {}, ad.get("landing_page") or {}
            old_page_url, new_page_url = old_page.get("url", ""), new_page.get("url", "")
            old_text, new_text = _text(old_page.get("text")), _text(new_page.get("text"))
            if (old_page.get("status") == new_page.get("status") == "ok"
                    and canonical_page_url(old_page_url)
                    and canonical_page_url(old_page_url) == canonical_page_url(new_page_url)
                    and old_text and new_text and _normalized(old_text) != _normalized(new_text)):
                emit(company, ad_id, "landing_page_changed", "Landing-page copy changed", old_text, new_text,
                     old_page_url, new_page_url,
                     "Text captured from the same destination changed between successful fetches. Personalization, geography or experiments can also cause differences; review before treating this as a deliberate campaign change.",
                     "Open the destination and review the offer or CTA change before proposing a response.", "review")
        if company.get("status") == previous.get("status") == "complete":
            for ad_id in sorted(set(old_ads) - set(new_ads)):
                old = old_ads[ad_id]
                emit(company, ad_id, "not_observed", "Previously collected ad not observed", _copy(old), "", old.get("library_url", ""), "",
                     "This ad was absent from a successful collection with matching search settings. This does not establish that it stopped: rolling date windows, library indexing and source coverage can change.",
                     "Recheck the ad library before drawing a conclusion about campaign status.", "review")
    return events
