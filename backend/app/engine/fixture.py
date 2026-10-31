"""Fixture engine: mock Apollo enrichment, rule-based ICP scoring, deterministic reply analysis.
Runs with no API keys so the whole flow works on day one. Owner C replaces pieces as providers come online.

ICP rule formats (icp_criteria.rule jsonb):
    geography          {"countries": ["SA", ...]}
    institution_type   {"in": ["bank", ...]}
    role               {"seniority_in": ["C-level", "VP", "Head", "Director"]}
    org_size           {"min_employees": 200}
    product_relevance  {"title_keywords": [...], "departments": [...]}  (+ product keywords vs enquiry text)
"""
import json
from pathlib import Path

from . import heuristic_bant

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures"
SENIORITY = {"c_suite": "C-level", "vp": "VP", "head": "Head", "director": "Director", "manager": "Manager", "entry": "Individual"}
PERSONAL_DOMAINS = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com"}


def _guess_seniority(title: str | None) -> str | None:
    if not title:
        return None
    t = title.lower()
    if t.startswith("chief") or t.split()[0] in {"ceo", "cfo", "cio", "cto", "coo", "cro"}:
        return "C-level"
    for word, level in [("vice president", "VP"), ("vp", "VP"), ("head", "Head"), ("director", "Director"),
                        ("manager", "Manager"), ("analyst", "Individual"), ("associate", "Individual")]:
        if word in t:
            return level
    return None


class FixtureEngine:
    name = "fixture"

    def __init__(self):
        data = json.loads((FIXTURES / "mock_apollo.json").read_text())
        self.orgs = {o["id"]: o for o in data["organizations"]}
        self.orgs_by_domain = {o["domain"]: o for o in data["organizations"]}
        self.people = {p["email"]: p for p in data["people"]}

    # ---------- enrichment ----------
    def enrich(self, ctx: dict) -> dict:
        contact, supplied = ctx["contact"], ctx.get("account") or {}
        email = contact["email"].lower()
        domain = email.split("@")[-1]
        person = self.people.get(email)
        org = self.orgs.get(person["organization_id"]) if person else self.orgs_by_domain.get(domain)
        uncertain = []

        def field(value, source, confidence):
            return {"value": value, "source": source, "confidence": confidence}

        title = person["title"] if person else contact.get("jobTitle")
        seniority = SENIORITY.get(person["seniority"]) if person else _guess_seniority(contact.get("jobTitle"))
        data = {
            "jobTitle": field(title, "mock_apollo" if person else "form", 0.95 if person else 0.6),
            "seniority": field(seniority, "mock_apollo" if person else "title_guess", 0.95 if person else 0.5),
            "department": field(person.get("department") if person else None, "mock_apollo", 0.9),
            "companyName": field(org["name"] if org else supplied.get("name"), "mock_apollo" if org else "form", 0.95 if org else 0.6),
            "industry": field(org.get("industry") if org else None, "mock_apollo", 0.9),
            "institutionType": field(org["institution_type"] if org else supplied.get("institutionType"),
                                     "mock_apollo" if org else "form", 0.95 if org else 0.5),
            "country": field(org["country"] if org else supplied.get("country"), "mock_apollo" if org else "form", 0.95 if org else 0.6),
            "employeeCount": field(org["employees"] if org else supplied.get("employeeCount"),
                                   "mock_apollo" if org else "form", 0.9 if org else 0.5),
        }
        missing = [k for k, v in data.items() if v["value"] in (None, "")]
        for k, v in data.items():
            if v["value"] not in (None, "") and v["source"] in ("form", "title_guess"):
                uncertain.append(f"{k} is {'guessed from the job title' if v['source'] == 'title_guess' else 'self-reported on the form'}, not verified")
        if domain in PERSONAL_DOMAINS:
            uncertain.append("personal email address: employer not confirmed")
        if org and supplied.get("name") and supplied["name"].strip().lower() != org["name"].lower():
            uncertain.append(f"form says company '{supplied['name']}' but the email domain belongs to '{org['name']}'")
        status = "complete" if person and org else ("partial" if person or org else "failed")
        return {
            "provider": "mock_apollo", "status": status, "data": data, "missing": missing, "uncertain": uncertain,
            "person": {"jobTitle": title, "seniority": seniority, "department": data["department"]["value"]},
            "company": {"name": data["companyName"]["value"], "domain": None if domain in PERSONAL_DOMAINS else domain,
                        "industry": data["industry"]["value"], "institutionType": data["institutionType"]["value"],
                        "country": data["country"]["value"], "employeeCount": data["employeeCount"]["value"]},
        }

    # ---------- ICP ----------
    def score_icp(self, ctx: dict, profile: dict, criteria: list[dict]) -> dict:
        contact, account, product = ctx["contact"], ctx.get("account") or {}, ctx.get("product") or {}
        breakdown, unknown = [], []
        for c in criteria:
            rule, dim = c["rule"], c["dimension"]
            if dim == "geography":
                value = account.get("country")
                ok = None if not value else value in rule["countries"]
                why = f"{value or 'unknown country'}; allowed: {', '.join(rule['countries'])}"
            elif dim == "institution_type":
                value = account.get("institutionType")
                ok = None if not value else value in rule["in"]
                why = f"{value or 'unknown'}; allowed: {', '.join(rule['in'])}"
            elif dim == "role":
                value = contact.get("seniority")
                ok = None if not value else value in rule["seniority_in"]
                why = f"{contact.get('jobTitle') or 'no title'} ({value or 'unknown seniority'})"
            elif dim == "org_size":
                value = account.get("employeeCount")
                ok = None if value is None else value >= rule["min_employees"]
                why = f"{value if value is not None else 'unknown'} employees; minimum {rule['min_employees']}"
            elif dim == "product_relevance":
                title = (contact.get("jobTitle") or "").lower()
                dept = (contact.get("department") or "").lower()
                enquiry = (ctx.get("enquiry") or "").lower() + " " + json.dumps(ctx.get("sourceContext") or {}).lower()
                hits = ([f"department '{dept}'"] if dept and dept in rule.get("departments", []) else []) + \
                       [f"title mentions '{k}'" for k in rule.get("title_keywords", []) if k in title][:1] + \
                       [f"enquiry mentions '{k}'" for k in product.get("keywords", []) if k in enquiry][:1]
                value = None
                ok = True if hits else (None if not (title or dept or enquiry.strip()) else False)
                why = "; ".join(hits) if hits else "role and enquiry do not relate to this product"
            else:
                ok, value, why = None, None, f"unsupported dimension {dim}"
            if ok is None:
                unknown.append(dim)
            breakdown.append({"dimension": dim, "weight": c["weight"], "points": c["weight"] if ok else 0,
                              "status": "unknown" if ok is None else ("pass" if ok else "fail"),
                              "evidence": value, "explanation": why})
        total_weight = sum(c["weight"] for c in criteria) or 1
        score = round(100 * sum(b["points"] for b in breakdown) / total_weight)
        hard_fail = [b["dimension"] for b in breakdown if b["status"] == "fail" and b["dimension"] in ("geography", "institution_type")]
        if len(unknown) >= 3:
            fit, summary = "insufficient_data", f"Cannot judge fit: {', '.join(unknown)} unknown. Human review instead of guessing."
        elif hard_fail:
            fit, summary = "low", f"Low fit: fails required {', '.join(hard_fail)}."
        elif score >= profile["high_threshold"]:
            fit, summary = "high", f"High fit: {score}/100 (threshold {profile['high_threshold']})."
        elif score >= profile["medium_threshold"]:
            fit, summary = "medium", f"Medium fit: {score}/100 (thresholds {profile['medium_threshold']}-{profile['high_threshold'] - 1})."
        else:
            fit, summary = "low", f"Low fit: {score}/100 below {profile['medium_threshold']}."
        if unknown and fit != "insufficient_data":
            summary += f" Unknown, scored 0: {', '.join(unknown)}."
        return {"score": score, "fit": fit, "missingCount": len(unknown), "breakdown": breakdown, "summary": summary}

    # ---------- replies ----------
    def assess_reply(self, text: str, ctx: dict, product_keywords: list[str]) -> dict:
        person = {"title": ctx["contact"].get("jobTitle"), "seniority": ctx["contact"].get("seniority")}
        return heuristic_bant.analyse(text, person, product_keywords)
