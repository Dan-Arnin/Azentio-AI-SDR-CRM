"""Read-only Apollo enrichment. Secrets never enter evidence or browser responses.

https://docs.apollo.io/reference/organization-enrichment
https://docs.apollo.io/reference/people-enrichment
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any

import httpx

from app.contracts import Enrichment, Evidence, LeadInput
from app.engine.enrichment import company_domain_for, normalize_country, normalize_domain, _differs

BASE = "https://api.apollo.io/api/v1"
CACHE_SECONDS = 21600


def _public_domain(value: str | None) -> str | None:
    domain = normalize_domain(value)
    if not domain or any(c in domain for c in "@:?#[\\ ") or "." not in domain:
        return None
    if domain.rsplit(".", 1)[-1] in {"example", "test", "invalid", "localhost", "local", "internal"}:
        return None
    if domain in {"example.com", "example.org", "example.net"}:
        return None
    try:
        ipaddress.ip_address(domain)
        return None
    except ValueError:
        return domain


def _cache(path: Path | None, key: str, value: dict | None = None) -> dict | None:
    if path is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=5) as con:
        con.execute("CREATE TABLE IF NOT EXISTS apollo_cache (key TEXT PRIMARY KEY, payload TEXT NOT NULL, at REAL NOT NULL)")
        if value is not None:
            con.execute("INSERT OR REPLACE INTO apollo_cache VALUES (?,?,?)", (key, json.dumps(value), time.time()))
        else:
            row = con.execute("SELECT payload,at FROM apollo_cache WHERE key=?", (key,)).fetchone()
            if row and time.time() - row[1] < CACHE_SECONDS:
                return json.loads(row[0])
    os.chmod(path, 0o600)
    return None


async def _request(client: httpx.AsyncClient, method: str, endpoint: str, params: dict,
                   api_key: str, cache_path: Path | None) -> tuple[dict, str | None]:
    cache_key = hashlib.sha256((api_key + endpoint + json.dumps(params, sort_keys=True)).encode()).hexdigest()
    cached = _cache(cache_path, cache_key)
    if cached is not None:
        return cached, None
    try:
        response = await client.request(method, BASE + endpoint, params=params,
            headers={"x-api-key": api_key, "accept": "application/json"}, timeout=15, follow_redirects=False)
        if response.status_code != 200:
            return {}, {401: "apollo_unauthorized", 403: "apollo_forbidden", 429: "apollo_rate_limited"}.get(
                response.status_code, "apollo_unavailable")
        data = response.json()
        if not isinstance(data, dict) or data.get("error"):
            return {}, "apollo_invalid_response"
        _cache(cache_path, cache_key, data)
        return data, None
    except httpx.TimeoutException:
        return {}, "apollo_timeout"
    except (httpx.HTTPError, ValueError):
        return {}, "apollo_unavailable"


def _institution(org: dict) -> str | None:
    industry = str(org.get("industry") or "").lower()
    text = " ".join(str(org.get(k) or "") for k in ("name", "industry", "short_description")).lower()
    if "islamic" in text and "bank" in text:
        return "islamic_bank"
    if industry in {"banking", "banks"}:
        return "bank"
    if "insurance" in industry:
        return "insurer"
    if "non-banking financial" in text or "non banking financial" in text:
        return "nbfc"
    return "other" if industry else None


async def enrich_apollo(inp: LeadInput, *, api_key: str, client: httpx.AsyncClient | None = None,
                        cache_path: Path | None = None, people: bool = False) -> Enrichment:
    result = Enrichment(provider="apollo", mock=False)
    domain = _public_domain(company_domain_for(inp))
    if not domain:
        result.errors.append("synthetic_domain" if ".example" in inp.email else "company_domain_unavailable")
        return result
    if not api_key:
        result.errors.append("apollo_key_missing")
        return result
    if client is None:
        async with httpx.AsyncClient() as owned:
            return await enrich_apollo(inp, api_key=api_key, client=owned, cache_path=cache_path, people=people)
    data, error = await _request(client, "GET", "/organizations/enrich", {"domain": domain}, api_key, cache_path)
    if error:
        result.errors.append(error)
        return result
    org = data.get("organization") or {}
    if not isinstance(org, dict):
        result.errors.append("apollo_invalid_response")
        return result
    if org:
        returned_domain = normalize_domain(org.get("primary_domain") or org.get("website_url"))
        if returned_domain != domain:
            result.errors.append("apollo_domain_mismatch")
            return result
        mapped = {"name": org.get("name"), "domain": returned_domain, "industry": org.get("industry"),
                  "country": normalize_country(org.get("country")), "hq": ", ".join(
                      str(org[k]) for k in ("city", "state", "country") if org.get(k)),
                  "revenueBand": org.get("annual_revenue_printed"), "apolloId": org.get("id"),
                  "institutionType": _institution(org)}
        size = org.get("estimated_num_employees")
        if isinstance(size, int) and not isinstance(size, bool) and size > 0:
            mapped["employeeCount"] = size
        result.company = {k: v for k, v in mapped.items() if v not in (None, "")}
        if mapped.get("institutionType"):
            result.uncertain.append("institutionType")  # Derived from industry; not an explicit Apollo classification.
        result.found = True
    if people:
        person_data, person_error = await _request(client, "POST", "/people/match", {
            "email": inp.email, "reveal_personal_emails": "false", "reveal_phone_number": "false",
            "run_waterfall_email": "false", "run_waterfall_phone": "false"}, api_key, cache_path)
        if person_error:
            result.errors.append(person_error)
        else:
            person = person_data.get("person") or {}
            confidence = person_data.get("match_confidence") or person.get("match_confidence")
            matched_domain = normalize_domain((person.get("organization") or {}).get("primary_domain"))
            if (person and confidence == "high" and str(person.get("email") or "").lower() == inp.email.lower()
                    and (not matched_domain or matched_domain == domain)):
                result.person = {k: v for k, v in {
                    "title": person.get("title"), "seniority": person.get("seniority"),
                    "department": ", ".join(person.get("departments") or []), "apolloId": person.get("id")
                }.items() if v not in (None, "")}
                result.found = True
            elif person:
                result.errors.append("apollo_person_not_verified")
    form = {"title": inp.role, "companyName": inp.companyName, "country": normalize_country(inp.country),
            "employeeCount": inp.employeeCount, "institutionType": inp.institutionType}
    for key, val in {**result.company, **result.person}.items():
        if key not in {"name", "country", "employeeCount", "industry", "institutionType", "title", "seniority", "department", "hq", "revenueBand"}:
            continue
        field = "companyName" if key == "name" else key
        conflict = form.get(field) not in (None, "") and _differs(field, form[field], val)
        if conflict:
            result.conflicts.append({"field": field, "form": form[field], "apollo": val})
        result.evidence.append(Evidence(field=field, value=str(val), origin="apollo",
            status="conflict" if conflict else "inferred", excerpt=f"Apollo returned {key}: {val}"))
    for field in ("employeeCount", "country", "institutionType", "title", "seniority", "department"):
        if result.company.get(field) in (None, "") and result.person.get(field) in (None, "") and form.get(field) in (None, ""):
            result.missing.append(field)
    return result
