"""Deterministic qualification policy (workstream C). The model proposes signals; this code decides the route,
so the same evidence always routes the same way and the rule trace can be shown to the rep and the judges."""
from .heuristic_bant import within_12_months

DIMENSIONS = ["budget", "authority", "need", "timeline"]
RANK = {"missing": 0, "conflict": 0, "inferred": 1, "confirmed": 2}


def merge(previous: list[dict], current: dict) -> dict:
    """Strongest evidence per dimension across the whole thread. previous: rows from bant_signals."""
    merged = {k: dict(v) for k, v in current.items()}
    for row in previous:
        k = row["dimension"]
        if RANK[row["status"]] > RANK[merged[k]["status"]]:
            merged[k] = {"status": row["status"], "evidence": row["evidence_quote"] or "",
                         "source": "message" if row["evidence_source"] == "message" else (row["evidence_source"] or "none"),
                         "note": "from an earlier reply"}
    return merged


def bant_score(merged: dict, points: dict) -> int:
    return sum(points[merged[k]["status"]] for k in DIMENSIONS)


def decide(intent: str, confidence: float, merged: dict, followups_sent: int, policy: dict) -> dict:
    """Returns route (leads.route), action (bant_assessments.recommended_action), reason, trace, gaps, salesReady."""
    trace = [f"Intent '{intent}' at confidence {confidence:.2f} (minimum {policy['minIntentConfidence']})."]
    gaps = [k for k in policy["gapOrder"] if merged[k]["status"] != "confirmed"]

    def out(route, action, reason, **extra):
        return {"route": route, "action": action, "reason": reason, "trace": trace, "gaps": gaps,
                "salesReady": route == "rep_review", **extra}

    if intent == "unsubscribe":
        return out("suppress", "suppress", "Contact asked to stop receiving emails.")
    if intent == "not_interested":
        return out("suppress", "suppress", "Contact is not interested.")
    if intent == "out_of_office":
        return out("wait", "wait", "Out-of-office reply; no follow-up until they return.")
    if intent == "not_now":
        return out("nurture", "nurture", "Explicit deferral; move to nurture.")
    if intent in ("referral", "wrong_person"):
        return out("human_review", "human_review", "Contact pointed us elsewhere; SDR decides whether to contact the referral.")
    if intent == "unclear" or confidence < policy["minIntentConfidence"]:
        trace.append("Below confidence threshold: escalate to a human by default.")
        return out("human_review", "human_review", "Reply could not be classified with confidence.")

    window = within_12_months(merged["timeline"]["evidence"]) if merged["timeline"]["status"] == "confirmed" else "unknown"
    need_ok = merged["need"]["status"] == "confirmed"
    tl_ok = merged["timeline"]["status"] == "confirmed" and window != "no"
    ba_ok = "confirmed" in (merged["budget"]["status"], merged["authority"]["status"])
    trace += [f"Need confirmed: {need_ok}.", f"Timeline confirmed within 12 months: {tl_ok} (window {window}).",
              f"Budget or Authority confirmed: {ba_ok}."]
    if need_ok and tl_ok and ba_ok:
        return out("rep_review", "handoff_to_rep", "Meets the BANT qualification rule.")
    if followups_sent < policy["maxGapFollowups"]:
        trace.append(f"Ask about '{gaps[0]}' (first unresolved in {policy['gapOrder']}); {followups_sent}/{policy['maxGapFollowups']} follow-ups used.")
        return out("gap_followup", "send_followup", f"Partially qualified; next question is about {gaps[0]}.", nextQuestion=gaps[0])
    trace.append("Gap follow-up already used; hand to a human SDR.")
    return out("human_review", "human_review", f"Still missing {', '.join(gaps)} after one follow-up.")


def next_action_text(decision: dict, company: str | None, product: str | None, referral: dict | None, asks_pricing: bool) -> str:
    route = decision["route"]
    if route == "rep_review":
        text = f"Book a discovery call with {company or 'the account'} within 2 business days and prepare an overview of {product or 'the product'}."
        open_gaps = [g for g in decision["gaps"] if g in ("budget", "authority")]
        if open_gaps:
            text += f" Confirm {' and '.join(open_gaps)} on the call."
        if asks_pricing:
            text += " Prospect asked about pricing; commercials are for the AE (the agent never quotes)."
        return text
    if route == "gap_followup":
        return f"Review and approve the follow-up asking about {decision.get('nextQuestion')}."
    if route == "human_review" and referral and (referral.get("name") or referral.get("email")):
        return f"Decide whether to contact the referral {referral.get('name') or ''} {referral.get('email') or ''}".strip() + "."
    if route == "human_review":
        return "Review the reply personally; fill gaps: " + ", ".join(decision["gaps"]) + "."
    return {"suppress": "No further outreach.", "nurture": "Low-touch nurture; revisit next quarter.",
            "wait": "Wait for the contact to return from leave."}.get(route, "Review the lead.")
