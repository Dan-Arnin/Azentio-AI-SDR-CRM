"""CRM webhook from ai-sdr-engine, replayed from real captured events (fixtures/engine_crm_events.jsonl).

The fixture was captured from the engine with CRM_WEBHOOK_SECRET=test-secret, so the original signatures verify.
"""
import hashlib
import hmac
import json
import os
from pathlib import Path

import pytest

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to a disposable PostgreSQL database")
if TEST_DB:
    os.environ["DATABASE_URL"] = TEST_DB

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402

SECRET = "test-secret"
CAPTURED = [json.loads(line) for line in (Path(__file__).resolve().parents[2] / "fixtures" / "engine_crm_events.jsonl").open()]
LAYLA, OMAR, NOURA = "lead_d1002b95870a", "lead_e29ebb7aa871", "lead_d7fc36942dc1"


@pytest.fixture()
def c(monkeypatch):
    monkeypatch.setenv("CRM_WEBHOOK_SECRET", SECRET)
    with db.connect(TEST_DB) as conn:
        db.reset(conn)
        db.migrate(conn)
    return TestClient(app)


def sign(body: str) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()


def post(c, body: str, signature: str | None = None):
    return c.post("/v1/crm/events", content=body.encode(),
                  headers={"content-type": "application/json", "X-SDR-Signature": signature or sign(body)})


def replay(c):
    for e in CAPTURED:
        r = post(c, e["body"], e["signature"])
        assert r.status_code == 200, r.text


def handoff(lead_id: str, event_id: str = "crm_handoff_1") -> str:
    """alert.ae_handoff shaped exactly as the engine emits it (fixture mode never qualifies a lead, so none was captured)."""
    base = next(json.loads(e["body"]) for e in CAPTURED
                if json.loads(e["body"])["type"] == "task.sdr_followup" and json.loads(e["body"])["leadId"] == lead_id)
    p = base["payload"]
    payload = {"lead": {**p["lead"], "stage": "REP_REVIEW", "version": p["lead"]["version"] + 1, "ownerId": "ae.sara"},
               "leadName": p["leadName"], "taskId": "tsk_handoff", "ownerId": "ae.sara", "card": {**p["card"], "owner": "ae.sara"}}
    return json.dumps({"id": event_id, "type": "alert.ae_handoff", "leadId": lead_id,
                       "occurredAt": "2026-10-31T12:00:00+00:00", "payload": payload}, separators=(",", ":"))


def test_real_engine_events_build_inbound_and_outbound_pipeline(c):
    replay(c)
    m = c.get("/v1/crm/metrics").json()
    assert {k: m["inbound"][k] for k in ("leads", "sent", "contacted", "replied")} == {"leads": 1, "sent": 1, "contacted": 1, "replied": 1}
    assert {k: m["outbound"][k] for k in ("leads", "drafted", "sent", "contacted", "replied")} == \
        {"leads": 2, "drafted": 8, "sent": 2, "contacted": 2, "replied": 1}
    assert m["outbound"]["replyRate"] == 0.5 and m["eventsReceived"] == len(CAPTURED)
    assert m["humanInterventions"]["approvedSends"] == 3 and m["escalatedToHuman"] == 2
    omar = c.get(f"/v1/crm/leads/{OMAR}").json()
    assert omar["lead"]["motion"] == "outbound" and omar["lead"]["region"] == "GCC" and omar["lead"]["stage"] == "HUMAN_REVIEW"
    assert omar["lead"]["account_name"] == "Gulf Crescent Bank" and omar["lead"]["bant"]["need"] == "confirmed"
    assert omar["lead"]["reply_intent"] == "interested" and m["outbound"]["positive"] == 1
    assert [e["type"] for e in omar["timeline"]].count("message.sent") == 1
    assert {r["engine_lead_id"] for r in c.get("/v1/crm/leads?motion=outbound").json()} == {OMAR, NOURA}


def test_replay_is_idempotent(c):
    replay(c)
    before = c.get("/v1/crm/metrics").json()
    r = post(c, CAPTURED[0]["body"], CAPTURED[0]["signature"])
    assert r.json()["duplicate"] is True
    replay(c)
    assert c.get("/v1/crm/metrics").json() == before


def test_signature_is_required(c, monkeypatch):
    body = CAPTURED[0]["body"]
    assert post(c, body.replace("Layla", "Mallory"), CAPTURED[0]["signature"]).status_code == 401  # tampered body
    assert post(c, body, "sha256=" + "0" * 64).json()["code"] == "invalid_signature"
    monkeypatch.delenv("CRM_WEBHOOK_SECRET")
    r = post(c, body)
    assert r.status_code == 503 and r.json()["code"] == "webhook_not_configured"  # fail closed
    assert c.get("/v1/crm/metrics").json()["eventsReceived"] == 0


def test_reused_event_id_with_different_body_is_rejected(c):
    post(c, CAPTURED[0]["body"], CAPTURED[0]["signature"])
    env = json.loads(CAPTURED[0]["body"])
    env["payload"]["lead"]["stage"] = "OPPORTUNITY"
    r = post(c, json.dumps(env))
    assert r.status_code == 409 and r.json()["code"] == "event_id_conflict"


def test_out_of_order_events_do_not_roll_back_the_snapshot(c):
    events = [e for e in CAPTURED if json.loads(e["body"])["leadId"] == LAYLA]
    for e in reversed(events):  # newest first
        assert post(c, e["body"], e["signature"]).status_code == 200
    lead = c.get(f"/v1/crm/leads/{LAYLA}").json()["lead"]
    assert lead["stage"] == "HUMAN_REVIEW" and lead["engine_version"] == 12 and lead["emails_sent"] == 1


def test_handoff_routes_to_regional_ae_and_opens_one_opportunity(c):
    replay(c)
    assert post(c, handoff(LAYLA)).status_code == 200
    detail = c.get(f"/v1/crm/leads/{LAYLA}").json()
    assert detail["lead"]["ae_owner"] == "Aisha Rahman" and "GCC region AE" in detail["lead"]["routing_reason"]
    opp = detail["opportunity"]
    assert opp["stage"] == "qualified" and opp["product_key"] == "lending_platform" and opp["bant_snapshot"]["summary"]
    assert post(c, handoff(LAYLA, "crm_handoff_2")).status_code == 200  # a repeat alert refreshes, never duplicates
    opps = c.get("/v1/crm/opportunities").json()
    assert len([o for o in opps if o["external_ref"] == f"engine:{LAYLA}"]) == 1
    m = c.get("/v1/crm/metrics").json()
    assert m["inbound"]["opportunities"] == 1 and m["inbound"]["qualified"] == 1
    assert m["opportunitiesByAe"] == [{"name": "Aisha Rahman", "region": "GCC", "opportunities": 1}]
    # the account is now owned, so the next lead from that bank goes to the same AE whatever the country
    assert c.get("/v1/crm/routing", params={"country": "IN", "email": "x@gulfcrescentbank.ae"}).json()["name"] == "Aisha Rahman"


def test_routing_policy(c):
    route = lambda **q: c.get("/v1/crm/routing", params=q).json()  # noqa: E731
    owner = route(country="IN", email="new.person@oasisbank.example")
    assert owner["name"] == "Aisha Rahman" and "existing owner" in owner["reason"]  # account owner beats region
    assert route(country="IN", email="a@new-nbfc.example")["name"] == "Rohan Mehta"
    intl = route(country="DE", email="a@bank.example")
    assert intl["name"] == "Daniel Ortiz" and "International" in intl["reason"]  # EU has no regional AE
    assert route(country="AE", email="someone@gmail.com")["name"] == "Aisha Rahman"  # personal mailbox: region only


def test_personal_mailbox_never_creates_an_account(c):
    env = json.loads(CAPTURED[0]["body"])
    env["id"], env["leadId"] = "crm_personal", "lead_personal"
    env["payload"]["lead"].update(leadId="lead_personal", email="pat.lee@gmail.com", companyName="Unknown")
    assert post(c, json.dumps(env)).status_code == 200
    lead = c.get("/v1/crm/leads/lead_personal").json()["lead"]
    assert lead["account_id"] is None
    assert not [a for a in c.get("/v1/accounts").json() if a["domain"] == "gmail.com"]


def test_resync_fills_gaps_and_matches_webhook_bytes(c):
    """Events missed while the backend slept are recovered from the engine outbox; already-received ones are duplicates."""
    from app import crm

    half = len(CAPTURED) // 2
    for e in CAPTURED[:half]:
        assert post(c, e["body"], e["signature"]).status_code == 200
    outbox = []
    for e in reversed(CAPTURED):  # the engine lists its outbox newest first
        env = json.loads(e["body"])
        outbox.append({"id": env["id"], "type": env["type"], "leadId": env["leadId"], "createdAt": env["occurredAt"],
                       "payload": env["payload"], "status": "failed", "attempts": 3})
    with db.connect(TEST_DB) as conn:
        counts = crm.resync(conn, outbox)
    assert counts == {"imported": len(CAPTURED) - half, "duplicates": half, "conflicts": 0, "invalid": 0}
    assert c.get("/v1/crm/metrics").json()["outbound"]["sent"] == 2


def test_resync_needs_engine_url(c, monkeypatch):
    monkeypatch.delenv("ENGINE_URL", raising=False)
    assert c.post("/v1/crm/resync").json()["code"] == "engine_url_not_configured"
