"""Outbound motion: cohort -> personalised multi-step sequence -> human approval -> cadence -> stop on reply.
Replies re-enter pipeline.receive_reply, the same qualification flow as inbound."""
from . import db, emails, hygiene, pipeline
from .config import cfg, region_of
from .enrichment import apollo, crm_check


def cohort_definition(cohort_key: str | None, custom: dict | None = None) -> dict:
    if custom:
        return custom
    if cohort_key not in cfg()["cohorts"]:
        raise pipeline.ValidationError([f"unknown cohort {cohort_key!r}"])
    return cfg()["cohorts"][cohort_key]


def preview(cohort: dict) -> dict:
    included, excluded = [], []
    kws = [k.lower() for k in cohort["title_keywords"]]
    for person, org in apollo.search_people(kws):
        region = region_of(org["country"])
        reasons = []
        if region not in cohort["regions"]:
            reasons.append(f"Region {region or org['country']} not in cohort ({', '.join(cohort['regions'])})")
        if org["institution_type"] not in cohort["institution_types"]:
            reasons.append(f"Institution type '{org['institution_type']}' not in cohort")
        hyg = hygiene.check(person["email"], region, consent=None)
        reasons += hyg["blocks"]
        crm = crm_check(person["email"], org["domain"], org["name"])
        if crm["open_opportunities"]:
            o = crm["open_opportunities"][0]
            reasons.append(f"Open opportunity '{o['name']}' owned by {o['owner']}")
        active = db.query("SELECT id, state FROM leads WHERE email = ? AND state NOT IN ('suppressed','nurture','blocked')", [person["email"]])
        if active:
            reasons.append(f"Already an active lead ({active[0]['state']})")
        row = {"name": f"{person['first_name']} {person['last_name']}", "title": person["title"], "email": person["email"],
               "company": org["name"], "country": org["country"], "institution_type": org["institution_type"]}
        (excluded if reasons else included).append({**row, "reasons": reasons} if reasons else row)
    return {"included": included, "excluded": excluded}


def create(name: str, cohort_key: str | None, theme: str | None = None, product: str | None = None, custom: dict | None = None) -> dict:
    cohort = cohort_definition(cohort_key, custom)
    theme = theme or cohort.get("theme")
    product = product or cohort.get("product")
    errors = []
    if theme not in cfg()["themes"]:
        errors.append(f"unknown theme {theme!r}")
    if product not in cfg()["products"]:
        errors.append(f"unknown product {product!r}")
    if errors:
        raise pipeline.ValidationError(errors)
    steps, engine, notes = emails.draft_steps(theme, product, cohort.get("label", name))
    checks = [{"step": s["step"], "violations": hygiene.copy_violations(s["subject"] + " " + s["body"])} for s in steps]
    sid = db.new_id("seq")
    db.insert("sequences", {"id": sid, "name": name, "cohort_key": cohort_key, "cohort": {**cohort, "preview": preview(cohort)},
                            "theme": theme, "product": product, "steps": {"steps": steps, "notes": notes, "checks": checks},
                            "status": "draft", "engine": engine, "created_at": db.now(), "sends": 0})
    db.log_event(None, "sequence_drafted", {"sequence_id": sid, "engine": engine, "notes": notes})
    return db.get("sequences", sid)


def update_steps(sequence_id: str, steps: list[dict]) -> dict:
    seq = db.get("sequences", sequence_id)
    if seq["status"] != "draft":
        raise ValueError("only draft sequences can be edited")
    checks = [{"step": s["step"], "violations": hygiene.copy_violations(s["subject"] + " " + s["body"])} for s in steps]
    db.update("sequences", sequence_id, {"steps": {**seq["steps"], "steps": steps, "checks": checks}})
    return db.get("sequences", sequence_id)


def approve(sequence_id: str, approver: str) -> dict:
    seq = db.get("sequences", sequence_id)
    if seq["status"] != "draft":
        raise ValueError("sequence is not a draft")
    bad = [c for c in seq["steps"]["checks"] if c["violations"]]
    if bad:
        raise ValueError(f"steps fail copy guardrails: {bad}")
    db.update("sequences", sequence_id, {"status": "active", "approved_by": approver})
    db.log_event(None, "sequence_approved", {"sequence_id": sequence_id, "approver": approver})
    enrolled = []
    for row in preview(seq["cohort"])["included"]:  # re-check at launch time: hygiene can change after drafting
        person, org = apollo.match_person(row["email"])
        lead = pipeline.capture({"source": "outbound", "email": row["email"], "first_name": person["first_name"],
                                 "last_name": person["last_name"], "company": org["name"], "title": person["title"],
                                 "country": org["country"], "product": seq["product"], "sequence": seq["name"]})
        db.update("leads", lead["id"], {"sequence_id": sequence_id})
        lead = pipeline.process(lead["id"])
        if lead["state"] != "icp_checked":
            continue
        db.insert("enrollments", {"id": db.new_id("enr"), "sequence_id": sequence_id, "lead_id": lead["id"], "email": lead["email"],
                                  "next_step": 1, "status": "active", "created_at": db.now()})
        enrolled.append(lead["id"])
    advance(sequence_id)
    return {"sequence": db.get("sequences", sequence_id), "enrolled": enrolled}


def advance(sequence_id: str) -> dict:
    """One cadence tick: each active enrollment gets its next step, unless its previous step is still awaiting approval."""
    seq = db.get("sequences", sequence_id)
    steps = seq["steps"]["steps"]
    sent, held = [], []
    for enr in db.query("SELECT * FROM enrollments WHERE sequence_id = ? AND status = 'active'", [sequence_id]):
        waiting = db.query("SELECT id FROM messages WHERE lead_id = ? AND sequence_id = ? AND status = 'pending_approval'", [enr["lead_id"], sequence_id])
        if waiting:
            held.append(enr["lead_id"])
            continue
        if enr["next_step"] > len(steps):
            db.update("enrollments", enr["id"], {"status": "completed", "reason": "All steps sent without reply"})
            pipeline._nurture(enr["lead_id"], "Sequence completed with no reply; moved to nurture.")
            continue
        lead = pipeline.lead(enr["lead_id"])
        step = steps[enr["next_step"] - 1]
        mail = emails.render(step, lead["person"], lead["company"])
        msg = pipeline.queue_email(lead["id"], f"sequence_step_{step['step']}", mail, sequence_id=sequence_id, step=step["step"])
        db.update("enrollments", enr["id"], {"next_step": enr["next_step"] + 1})
        (sent if msg["status"] == "sent" else held).append(lead["id"])
    db.update("sequences", sequence_id, {"sends": (seq["sends"] or 0) + len(sent)})
    return {"sent": sent, "held": held}
