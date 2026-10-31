"""Deterministic reply analysis: intent + BANT with verbatim evidence.
Adapted from the reference prototype (reference/python-prototype/app/analyzer.py), where it is covered by tests.
Owner C replaces or wraps this with a model-backed engine; keep the output shape.
"""
import re
from datetime import date, timedelta


SIGNALS = ["budget", "authority", "need", "timeline"]
INTENTS = ["interested", "not_now", "not_interested", "wrong_person", "referral", "unsubscribe", "out_of_office", "unclear"]
SENIOR = {"C-level", "VP", "Head", "Director"}
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
# Whole month words only, so "maybe" or "market" never read as a date.
MONTH_RE = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b"


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _signal(status="missing", evidence="", source="none", note=""):
    return {"status": status, "evidence": evidence, "source": source, "note": note}


# ---------- timeline date estimation (shared by both engines) ----------

def estimate_date(text: str, today: date | None = None) -> date | None:
    today = today or date.today()
    t = text.lower()
    m = re.search(r"\bq([1-4])\s*(20\d\d)\b", t)
    if m:
        q, y = int(m.group(1)), int(m.group(2))
        return date(y, q * 3, 28)
    m = re.search(rf"\b{MONTH_RE}\.?\s+(20\d\d)\b", t)
    if m:
        return date(int(m.group(2)), MONTHS[m.group(1)[:3]], 28)
    m = re.search(rf"\b(?:by|in|before|from|until)\s+{MONTH_RE}", t)
    if m:
        month = MONTHS[m.group(1)[:3]]
        y = today.year if month > today.month else today.year + 1
        return date(y, month, 28)
    m = re.search(r"\bq([1-4])\b", t)
    if m:
        q = int(m.group(1))
        y = today.year if q * 3 >= today.month else today.year + 1
        return date(y, q * 3, 28)
    m = re.search(r"\bwithin\s+(\d+)\s+(week|month)s?\b", t)
    if m:
        n = int(m.group(1))
        return today + timedelta(days=n * (7 if m.group(2) == "week" else 30))
    if re.search(r"\bnext (month|quarter)\b", t):
        return today + timedelta(days=30 if "month" in t else 90)
    m = re.search(r"\b(20\d\d)\b", t)
    if m:
        return date(int(m.group(1)), 6, 30)
    return None


def within_12_months(evidence: str) -> str:
    d = estimate_date(evidence)
    if d is None:
        return "unknown"
    return "yes" if d <= date.today() + timedelta(days=365) else "no"


# ---------- deterministic engine ----------

def analyse(reply: str, person: dict, product_keywords: list[str]) -> dict:
    """person: {"title", "seniority"}; product_keywords: keywords of every active product."""
    text = reply.strip()
    low = text.lower()
    sents = _sentences(text)
    keywords = set(product_keywords)
    keywords.update(["platform", "system", "solution", "monitoring"])

    def first(pattern, pool=sents, flags=re.I):
        for s in pool:
            if re.search(pattern, s, flags):
                return s
        return None

    bant = {k: _signal() for k in SIGNALS}

    # Budget
    s = first(r"\b(approved|allocated|secured|sanctioned|signed[- ]off)\b.*\b(fund\w*|budget)\b|\b(budget|funding)\b.*\b(approved|allocated|secured|sanctioned|in place)\b")
    if s:
        bant["budget"] = _signal("confirmed", s, "reply")
    else:
        s = first(r"\bbudget\b.*\b(discuss\w*|planning|planned|committee|cycle|review\w*)\b|\b(investment|steering) committee\b")
        if s:
            bant["budget"] = _signal("inferred", s, "reply", "Budget under discussion, not yet approved.")
    if re.search(r"\bno budget\b", low):
        bant["budget"] = _signal("missing", first(r"\bno budget\b") or "", "reply", "Prospect states there is no budget.")

    # Authority
    s = first(r"\bI\s+(lead|own|run|head|am responsible for|am the decision)\b|\bI'?m the (decision|budget) (maker|owner|holder)\b|\b(involve|together with|alongside) our (cio|ceo|cro|cfo|cto|coo|board)\b")
    if s:
        bant["authority"] = _signal("confirmed", s, "reply")
    elif person.get("seniority") in SENIOR and person.get("title"):
        bant["authority"] = _signal("inferred", person["title"], "enrichment", "Senior title from enrichment; decision role not stated.")

    # Need
    need_pat = r"\b(replac\w*|moderni[sz]\w*|upgrad\w*|migrat\w*|implement\w*|evaluat\w*|looking (at|for)|pain|problem|challenge|struggl\w*|too many|legacy)\b"
    for sent in sents:
        if re.search(need_pat, sent, re.I) and not re.search(r"\bnot (looking|evaluating|planning)\b", sent, re.I):
            if any(k in sent.lower() for k in keywords):
                bant["need"] = _signal("confirmed", sent, "reply")
                break
            if bant["need"]["status"] == "missing":
                bant["need"] = _signal("inferred", sent, "reply", "Project mentioned but not tied to a product area.")
    if bant["need"]["status"] == "missing":
        s = first(r"\b(interested|keen|explore|learn more)\b")
        if s:
            bant["need"] = _signal("inferred", s, "reply", "General interest; no specific project stated.")

    # Timeline
    # A bare month word needs a preposition or a year next to it ("we may decide" is not May).
    date_pat = (rf"\b(q[1-4](\s*20\d\d)?|(?:by|in|before|from|until|end of|early|mid|late)\s+{MONTH_RE}|{MONTH_RE}\s+20\d\d"
                r"|20\d\d|within \d+ (weeks|months)|next (month|quarter))\b")
    s = first(rf"\b(select\w*|decid\w*|decision|live|launch\w*|go[- ]live|rfp|tender|implement\w*|start\w*|kick[- ]?off|award\w*)\b.*{date_pat}|{date_pat}.*\b(select\w*|decision|live|launch|go[- ]live|rfp|tender)\b")
    if s:
        bant["timeline"] = _signal("confirmed", s, "reply")
    else:
        s = first(date_pat)
        if s and not bant["budget"]["evidence"] == s:
            bant["timeline"] = _signal("inferred", s, "reply", "Date mentioned without a stated project milestone.")
    if re.search(r"\bno (fixed |firm |clear |set )?(timeline|timeframe|date)\b", low):
        bant["timeline"] = _signal("missing", first(r"\bno (fixed |firm |clear |set )?(timeline|timeframe|date)\b") or "", "reply",
                                   "Prospect states there is no timeline yet.")

    # Intent
    referral = {"name": "", "email": "", "title": ""}
    em = re.search(r"[\w.+-]+@[\w-]+(\.[\w-]+)+", text)
    nm = re.search(r"(?:speak to|speak with|contact|reach out to|talk to|email|connect with)\s+([A-Z][a-z]+(?:\s[A-Z][a-zA-Z'-]+)+)", text)
    tl = re.search(r"our\s+((?:Head|Chief|Director|VP)[A-Za-z ]+?)(?:,|\.| at\b)", text)
    if re.search(r"\b(unsubscribe|remove me|stop (emailing|contacting)|opt[- ]?out|do not contact)\b", low):
        intent, conf = "unsubscribe", 0.95
    elif re.search(r"\b(out of (the )?office|on (annual )?leave|limited access to email|auto[- ]?reply)\b", low):
        intent, conf = "out_of_office", 0.9
    elif re.search(r"\b(not the right person|wrong person|not my area|not responsible for)\b", low) or (nm and em):
        if nm or em:
            intent, conf = "referral", 0.9
            referral = {"name": nm.group(1) if nm else "", "email": em.group(0) if em else "", "title": tl.group(1).strip() if tl else ""}
        else:
            intent, conf = "wrong_person", 0.85
    elif re.search(r"\b(not interested|no thanks|no thank you|already (have|use|signed with) (a|another) (vendor|provider))\b", low):
        intent, conf = "not_interested", 0.9
    elif re.search(r"\b(not (a|the) priority|not now|not at the moment|maybe (later|next year)|reach out next year|revisit (later|next year))\b", low):
        intent, conf = "not_now", 0.85
    elif any(bant[k]["status"] != "missing" and bant[k]["source"] == "reply" for k in SIGNALS) or re.search(r"\b(interested|yes|keen|happy to|sounds good)\b", low):
        confirmed = sum(1 for k in SIGNALS if bant[k]["status"] == "confirmed")
        intent, conf = "interested", 0.75 + 0.05 * confirmed
    else:
        intent, conf = "unclear", 0.3

    if intent != "interested":
        for v in bant.values():
            if v["status"] == "missing" and not v["note"]:
                v["note"] = "Not assessed for this intent."

    tl_ev = bant["timeline"]["evidence"] if bant["timeline"]["status"] != "missing" else ""
    return {
        "intent": intent,
        "intent_confidence": round(min(conf, 0.95), 2),
        "bant": bant,
        "timeline_within_12_months": within_12_months(tl_ev) if tl_ev else "unknown",
        "referral": referral,
        "asks_for_pricing": bool(re.search(r"\b(pric\w*|quote|quotation|commercials|how much)\b", low)),
        "summary": _summary(intent, bant),
        "engine": "heuristic",
    }


def _summary(intent: str, bant: dict) -> str:
    parts = [f"{k.title()}: {v['status']}" for k, v in bant.items()]
    return f"Intent '{intent}'. " + "; ".join(parts) + "."
