"""Mock Apollo enrichment and mock CRM lookups.

`MockApollo` mirrors the shape of Apollo's people/organization match so a real
Apollo adapter can replace it behind the same two methods.
"""
from . import db
from .config import load, region_of

PERSONAL_DOMAINS = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com"}


class MockApollo:
    name = "mock_apollo"

    def __init__(self):
        data = load("mock_apollo")
        self.orgs = {o["id"]: o for o in data["organizations"]}
        self.orgs_by_domain = {o["domain"]: o for o in data["organizations"]}
        self.people = data["people"]

    def match_person(self, email: str) -> tuple[dict | None, dict | None]:
        email = email.lower()
        person = next((p for p in self.people if p["email"] == email), None)
        domain = email.split("@")[-1]
        org = self.orgs.get(person["organization_id"]) if person else self.orgs_by_domain.get(domain)
        return person, org

    def search_people(self, title_keywords: list[str]) -> list[tuple[dict, dict]]:
        """Title search only; region/type filtering happens in the cohort builder so exclusions get reasons."""
        out = []
        for p in self.people:
            org = self.orgs[p["organization_id"]]
            if any(k in p["title"].lower() for k in title_keywords):
                out.append((p, org))
        return out


apollo = MockApollo()


def enrich(capture: dict) -> dict:
    """Returns {person, company, status, notes}. Never invents attributes that the source lacks."""
    email = capture["email"].lower()
    domain = email.split("@")[-1]
    notes = []
    person, org = apollo.match_person(email)
    if domain in PERSONAL_DOMAINS:
        notes.append(f"Personal email domain ({domain}); company cannot be confirmed from the address.")
    if person:
        notes.append(f"Person matched in {apollo.name} by email.")
    else:
        notes.append("No person match; using details supplied on the form.")
        person = {"first_name": capture.get("first_name"), "last_name": capture.get("last_name"),
                  "email": email, "title": capture.get("title"), "seniority": guess_seniority(capture.get("title")),
                  "department": None, "_from_form": True}
    if org:
        notes.append(f"Organization matched by {'person record' if person.get('organization_id') else 'email domain'}.")
        company = dict(org)
    else:
        notes.append("No organization match; institution type and size unknown.")
        company = {"name": capture.get("company"), "domain": domain, "country": capture.get("country"),
                   "institution_type": None, "employees": None, "_from_form": True}
    company["region"] = region_of(company.get("country") or capture.get("country"))
    status = "full" if person.get("id") and org else ("partial" if org or person.get("title") else "none")
    missing, uncertain = [], []
    for label, value in [("industry", company.get("industry")), ("geography", company.get("country")),
                         ("institution type", company.get("institution_type")), ("employee size", company.get("employees")),
                         ("designation", person.get("title")), ("seniority", person.get("seniority"))]:
        if value in (None, ""):
            missing.append(label)
    if person.get("_from_form") and person.get("seniority"):
        uncertain.append(f"seniority '{person['seniority']}' guessed from the job title on the form, not verified")
    if person.get("_from_form") and person.get("title"):
        uncertain.append("designation is self-reported on the form")
    if company.get("_from_form") and company.get("country"):
        uncertain.append("geography is self-reported on the form")
    if domain in PERSONAL_DOMAINS:
        uncertain.append("personal email address: employer not confirmed")
    if org and capture.get("company") and capture["company"].strip().lower() != org["name"].lower():
        uncertain.append(f"form says company '{capture['company']}' but the email domain belongs to '{org['name']}'")
    return {"person": person, "company": company, "status": status, "source": apollo.name, "notes": notes,
            "missing": missing, "uncertain": uncertain}


def guess_seniority(title: str | None) -> str | None:
    if not title:
        return None
    t = title.lower()
    if t.startswith("chief") or t in {"ceo", "cfo", "cio", "cto", "coo", "cro"}:
        return "c_suite"
    for word, level in [("vp", "vp"), ("vice president", "vp"), ("head", "head"), ("director", "director"),
                        ("manager", "manager"), ("analyst", "entry"), ("associate", "entry")]:
        if word in t:
            return level
    return None


def crm_check(email: str, domain: str, account: str | None) -> dict:
    """Duplicate and existing-opportunity detection against the mock CRM plus leads already in this system."""
    crm = load("mock_crm")
    contact = next((c for c in crm["contacts"] if c["email"] == email.lower()), None)
    opps = [o for o in crm["opportunities"] if o["account_domain"] == domain and o["status"] == "open"]
    if account:
        opps += db.query("SELECT * FROM crm_opportunities WHERE account = ?", [account])
    existing_leads = db.query("SELECT id, state, source FROM leads WHERE email = ?", [email.lower()])
    account_match = next((a for a in crm.get("accounts", []) if a["domain"] == domain), None)
    return {
        "existing_account": account_match,
        "existing_contact": contact,
        "open_opportunities": [{"name": o["name"], "owner": o["owner"], "stage": o["stage"]} for o in opps],
        "existing_leads": existing_leads,
    }
