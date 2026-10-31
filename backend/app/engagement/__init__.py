"""Engagement boundary (workstream D): drafts from approved product facts only.

compose(stage, ctx, facts, gap=None, asks_pricing=False) -> {"kind", "subject", "body",
        "qualifyingQuestions": [str], "productFactsUsed": [fact ids]}

`facts` are product_facts rows ({"id", "statement"}). The template composer below works with no API key;
D swaps in a model-backed writer behind the same function and keeps the guardrail check in the backend.
"""
FOOTER = "\n\n--\nAzentio Sales Development (hackathon demo, sample content)\nReply \"unsubscribe\" and we will not contact you again."

QUESTIONS = {
    "need": "What is the main problem or project you are looking to address?",
    "timeline": "Is there a target date for vendor selection or go-live?",
    "authority": "Who else would be involved in evaluating and approving a decision like this?",
    "budget": "Is there a funded programme or budget set aside for this initiative?",
}


def _opener(ctx: dict) -> tuple[str, str]:
    src, sc = ctx["source"], ctx.get("sourceContext") or {}
    product = (ctx.get("product") or {}).get("name", "our products")
    if src == "webinar":
        topic = sc.get("eventName") or sc.get("topic") or "our webinar"
        return f"Following up on {topic}", f"Thank you for attending {topic}. I hope the session was useful."
    if src == "event":
        name = sc.get("eventName") or "our event"
        return f"Great to connect at {name}", f"Thank you for registering for {name}."
    if src in ("linkedin_ads", "google_ads"):
        asset = sc.get("asset") or "our guide"
        return f"Your copy of {asset}", f"Thanks for downloading {asset}. I hope it is a useful read."
    if src == "marketing_campaign":
        return f"Following up on {product}", f"Thank you for engaging with our {sc.get('campaignId') or 'recent campaign'}."
    opener = "Thank you for getting in touch through our website."
    if ctx.get("enquiry"):
        opener += f' You mentioned: "{ctx["enquiry"].strip()[:300]}"'
    return f"Re: your enquiry about {product}", opener


def compose(stage: str, ctx: dict, facts: list[dict], gap: str | None = None, asks_pricing: bool = False) -> dict:
    first = ctx["contact"].get("firstName") or "there"
    fact = facts[0] if facts else None
    if stage == "follow_up":
        q = QUESTIONS[gap or "need"]
        pricing = ("\n\nOn commercials: I can't share pricing by email, but your account executive will cover it "
                   "directly once we understand your requirements.") if asks_pricing else ""
        body = f"Hi {first},\n\nThank you, that is really helpful. One quick question so we bring the right people:\n\n  {q}{pricing}{FOOTER}"
        return {"kind": "followup", "subject": "Re: one quick question", "body": body, "qualifyingQuestions": [q], "productFactsUsed": []}
    subject, opener = _opener(ctx)
    qs = [QUESTIONS[k] for k in ("need", "timeline", "authority", "budget")]
    claim = f"\n\n{fact['statement']}" if fact else ""
    body = (f"Hi {first},\n\n{opener}{claim}\n\nSo the right specialist can follow up, could you share a little more?\n"
            + "\n".join(f"  {i}. {q}" for i, q in enumerate(qs, 1)) + f"\n\nA short reply is perfectly fine.{FOOTER}")
    return {"kind": "intro", "subject": subject, "body": body, "qualifyingQuestions": qs,
            "productFactsUsed": [fact["id"]] if fact else []}
