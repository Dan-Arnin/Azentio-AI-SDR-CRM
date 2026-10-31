"""Deterministic, per-product ICP scoring. Every point awarded or withheld comes with a reason."""
from .config import cfg

LABELS = {"high_fit": "High Fit", "medium_fit": "Medium Fit", "low_fit": "Low Fit", "insufficient_data": "Insufficient data"}


def _relevance(product: str, person: dict, context: str) -> tuple[bool | None, str]:
    rel = cfg()["products"][product]["icp"]["relevance"]
    keywords = cfg()["products"][product]["keywords"]
    title = (person.get("title") or "").lower()
    dept = (person.get("department") or "").lower()
    ctx = (context or "").lower()
    hits = []
    if dept and dept in rel["departments"]:
        hits.append(f"department '{dept}'")
    hits += [f"title mentions '{k}'" for k in rel["title_keywords"] if k in title][:1]
    hits += [f"enquiry mentions '{k}'" for k in keywords if k in ctx][:1]
    if hits:
        return True, "; ".join(hits)
    if not (title or dept or ctx):
        return None, "no role or enquiry context to judge relevance"
    return False, f"role '{person.get('title') or dept}' and enquiry do not relate to this product"


def score(product: str, person: dict, company: dict, context: str = "") -> dict:
    conf = cfg()
    p = conf["products"][product]
    icp, weights = p["icp"], conf["icp_weights"]
    checks, unknown = [], []

    def add(name, ok, reason, value):
        if ok is None:
            unknown.append(name)
        checks.append({"criterion": name, "result": "unknown" if ok is None else ("pass" if ok else "fail"),
                       "points": weights[name] if ok else 0, "max": weights[name], "value": value, "reason": reason})

    region = company.get("region")
    add("geography", None if not company.get("country") else region in icp["regions"],
        f"{company.get('country') or 'unknown country'} -> {region or 'no target region'}; ICP regions: {', '.join(icp['regions'])}",
        region)
    itype = company.get("institution_type")
    add("institution_type", None if not itype else itype in icp["institution_types"],
        f"{itype or 'unknown'}; ICP types: {', '.join(icp['institution_types'])}", itype)
    seniority = person.get("seniority")
    add("seniority", None if not seniority else seniority in icp["seniority"],
        f"{person.get('title') or 'no title'} ({seniority or 'unknown'}); ICP seniority: {', '.join(icp['seniority'])}", seniority)
    emp = company.get("employees")
    add("size", None if emp is None else emp >= icp["min_employees"],
        f"{emp if emp is not None else 'unknown'} employees; minimum {icp['min_employees']}", emp)
    ok, why = _relevance(product, person, context)
    add("product_relevance", ok, why, None)

    total = sum(c["points"] for c in checks)
    th = conf["icp_thresholds"]
    hard_fail = [c["criterion"] for c in checks if c["result"] == "fail" and c["criterion"] in ("geography", "institution_type")]
    if len(unknown) >= 3:
        verdict, why = "insufficient_data", f"Cannot judge ICP fit: {', '.join(unknown)} unknown. A human SDR reviews it instead of guessing."
    elif hard_fail:
        verdict, why = "low_fit", f"Low Fit: fails hard ICP criteria for {p['name']} ({', '.join(hard_fail)})."
    elif total >= th["high"]:
        verdict, why = "high_fit", f"High Fit: {total}/100 meets the threshold of {th['high']}."
    elif total >= th["medium"]:
        verdict, why = "medium_fit", f"Medium Fit: {total}/100 is between {th['medium']} and {th['high'] - 1}; engage, flagged for the rep."
    else:
        verdict, why = "low_fit", f"Low Fit: {total}/100 is below {th['medium']}."
    if unknown and verdict != "insufficient_data":
        why += f" Unknown, scored 0: {', '.join(unknown)}."
    return {"product": product, "product_name": p["name"], "score": total, "verdict": verdict, "label": LABELS[verdict],
            "reason": why, "checks": checks}


def infer_product(text: str) -> str | None:
    text = (text or "").lower()
    best, hits = None, 0
    for key, p in cfg()["products"].items():
        n = sum(1 for k in p["keywords"] if k in text)
        if n > hits:
            best, hits = key, n
    return best
