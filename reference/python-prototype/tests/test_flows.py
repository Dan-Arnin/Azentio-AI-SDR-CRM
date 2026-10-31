"""End-to-end tests for the two demo scenarios and the guardrails, run on the deterministic engine.

    SDR_ENGINE=heuristic python3 -m unittest discover -s tests -v
"""
import json
import os
import tempfile
import unittest

os.environ.setdefault("SDR_ENGINE", "heuristic")
os.environ["SDR_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.config import load  # noqa: E402
from app.main import app  # noqa: E402

S = load("samples")
R = S["scripted_replies"]


class Flows(unittest.TestCase):
    def setUp(self):
        self.c = TestClient(app)
        self.c.post("/api/reset")

    def approve_pending(self):
        for m in self.c.get("/api/approvals").json():
            self.assertEqual(self.c.post(f"/api/messages/{m['id']}/approve", json={"approver": "test"}).status_code, 200)

    # Scenario 1: website enquiry -> enrich -> ICP -> qualifying email -> reply -> BANT -> AE alert
    def test_website_enquiry_qualifies(self):
        lead = self.c.post("/api/leads/inbound", json=S["inbound"]["website_form"]).json()
        self.assertEqual(lead["icp"]["verdict"], "high_fit")
        self.assertEqual(lead["state"], "pending_approval")  # approval mode on first sends
        self.approve_pending()
        detail = self.c.get(f"/api/leads/{lead['id']}").json()
        self.assertEqual(detail["lead"]["state"], "awaiting_reply")
        self.assertTrue(any(m["kind"] == "qualifying" and m["status"] == "sent" for m in detail["messages"]))

        out = self.c.post(f"/api/leads/{lead['id']}/reply", json={"body": R["qualified"]}).json()
        self.assertEqual(out["decision"]["route"], "qualified", out["decision"])
        for k in ("budget", "authority", "need", "timeline"):
            self.assertEqual(out["merged_bant"][k]["status"], "confirmed", k)
        alerts = self.c.get("/api/alerts").json()
        ae = [a for a in alerts if a["type"] == "ae_alert"]
        self.assertEqual(len(ae), 1)
        self.assertIn("Aisha Rahman", ae[0]["assignee"])
        self.assertTrue(ae[0]["summary"]["thread"])
        self.assertEqual(len(self.c.get("/api/crm").json()["opportunities"]), 1)

    # Scenario 2: outbound sequence -> reply stops sequence -> same qualification flow
    def test_outbound_reply_feeds_qualification(self):
        seq = self.c.post("/api/sequences", json={"name": "GCC compliance", "cohort_key": "gcc_compliance_heads"}).json()
        prev = seq["cohort"]["preview"]
        included = {r["email"] for r in prev["included"]}
        excluded = {r["email"]: r["reasons"] for r in prev["excluded"]}
        self.assertIn("hessa.almannai@dunebank.example", included)
        self.assertIn("noura.alsabah@oasisbank.example", excluded)   # open opportunity
        self.assertIn("lukas.becker@rhinedigital.example", excluded)  # EU / region
        self.assertIn("rashid.binsaeed@falconfx.example", excluded)   # exchange house not a bank

        res = self.c.post(f"/api/sequences/{seq['id']}/approve", json={"approver": "test"}).json()
        self.assertEqual(len(res["enrolled"]), len(included))
        self.approve_pending()                      # first 2 sends reviewed by a human
        self.c.post(f"/api/sequences/{seq['id']}/advance")
        leads = {l["email"]: l for l in self.c.get("/api/leads").json()}
        hessa = leads["hessa.almannai@dunebank.example"]

        out = self.c.post(f"/api/leads/{hessa['id']}/reply", json={"body": R["partial"]}).json()
        self.assertEqual(out["decision"]["route"], "gap_followup")
        enr = [e for s in self.c.get("/api/sequences").json() for e in s["enrollments"] if e["lead_id"] == hessa["id"]]
        self.assertEqual(enr[0]["status"], "stopped_reply")
        self.approve_pending()
        out = self.c.post(f"/api/leads/{hessa['id']}/reply", json={"body": R["gap_answer"]}).json()
        self.assertEqual(out["decision"]["route"], "qualified", out["decision"])
        self.assertEqual(out["merged_bant"]["need"]["status"], "confirmed")  # carried from first reply

    def test_routes_for_other_intents(self):
        seq = self.c.post("/api/sequences", json={"name": "Islamic transformation", "cohort_key": "islamic_bank_transformation"}).json()
        self.c.post(f"/api/sequences/{seq['id']}/approve", json={"approver": "test"})
        self.approve_pending()
        leads = {l["email"]: l for l in self.c.get("/api/leads").json()}
        layla = leads["layla.mansour@pearlgulf.example"]
        khalid = leads["khalid.alharbi@najd-capital.example"]
        self.assertEqual(self.c.post(f"/api/leads/{layla['id']}/reply", json={"body": R["unsubscribe"]}).json()["decision"]["route"], "suppress")
        self.assertEqual(self.c.post(f"/api/leads/{khalid['id']}/reply", json={"body": R["referral"]}).json()["decision"]["route"], "sdr")
        sup = [s["email"] for s in self.c.get("/api/crm").json()["suppressions"]]
        self.assertIn("layla.mansour@pearlgulf.example", sup)
        task = [a for a in self.c.get("/api/alerts").json() if a["type"] == "sdr_task"][0]
        self.assertEqual(task["summary"]["decision"]["referral"]["email"], "omar.haddad@najd-capital.example")

    def test_low_confidence_escalates(self):
        lead = self.c.post("/api/leads/inbound", json=S["inbound"]["website_form"]).json()
        self.approve_pending()
        out = self.c.post(f"/api/leads/{lead['id']}/reply", json={"body": R["ambiguous"]}).json()
        self.assertEqual(out["decision"]["route"], "sdr")

    def test_webinar_rows_validated_not_dropped(self):
        res = self.c.post("/api/leads/webinar", data={"csv_text": S["webinar_csv"], "webinar": S["webinar_name"], "product": "amlock"}).json()
        self.assertEqual(len(res["rejected"]), 1)
        self.assertEqual(len(res["created"]), 4)
        states = {l["email"]: l["state"] for l in self.c.get("/api/leads").json()}
        self.assertEqual(states["lukas.becker@rhinedigital.example"], "blocked")          # GDPR consent
        self.assertEqual(states["john.miller@acmeretail.example"], "nurture")             # non-ICP
        self.assertEqual(states["noura.alsabah@oasisbank.example"], "existing_opportunity")
        self.assertEqual(states["ananya.iyer@indusfin.example"], "pending_approval")

    def test_pricing_never_quoted(self):
        lead = self.c.post("/api/leads/inbound", json=S["inbound"]["website_form"]).json()
        self.approve_pending()
        out = self.c.post(f"/api/leads/{lead['id']}/reply", json={"body": R["pricing_ask"]}).json()
        self.assertEqual(out["decision"]["route"], "gap_followup")
        msgs = self.c.get(f"/api/leads/{lead['id']}").json()["messages"]
        follow = [m for m in msgs if m["kind"] == "gap_followup"][0]
        self.assertIn("not able to share pricing", follow["body"])

    def test_invalid_lead_rejected(self):
        r = self.c.post("/api/leads/inbound", json={"source": "website_form", "email": "nope"})
        self.assertEqual(r.status_code, 422)

    # Level 1 spec (challenge pack v1, section 13) additions
    def test_marketing_campaign_lead_and_account_association(self):
        lead = self.c.post("/api/leads/inbound", json=S["inbound"]["marketing_campaign"]).json()
        self.assertEqual(lead["source"], "marketing_campaign")
        self.assertEqual(lead["crm"]["existing_account"]["owner"], "Rohan Mehta")
        self.assertEqual(lead["icp"]["label"], "High Fit")
        rel = [c for c in lead["icp"]["checks"] if c["criterion"] == "product_relevance"][0]
        self.assertEqual(rel["result"], "pass")
        msg = self.c.get(f"/api/leads/{lead['id']}").json()["messages"][0]
        self.assertEqual(msg["status"], "pending_approval")  # every draft is reviewed
        self.assertIn("campaign", msg["body"].lower())

    def test_bulk_import_flags_missing_data_without_inventing(self):
        res = self.c.post("/api/leads/import", data={"text": json.dumps(S["bulk_json"])}).json()
        self.assertEqual(len(res["created"]), 3)
        self.assertEqual(len(res["rejected"]), 1)                  # unknown source + invalid email
        leads = {l["email"]: l for l in self.c.get("/api/leads").json()}
        pat = leads["pat.lee@gmail.com"]
        self.assertEqual(pat["icp"]["verdict"], "insufficient_data")
        self.assertIn("institution type", pat["enrichment"]["missing"])
        self.assertTrue(any("personal email" in u for u in pat["enrichment"]["uncertain"]))
        self.assertIsNone(pat["company"]["institution_type"])     # not fabricated
        self.assertTrue(pat["next"]["ready_for_human"])
        self.assertEqual(leads["vikram.rao@gangacoop.example"]["icp"]["verdict"], "medium_fit")  # junior, small bank, relevant interest

    def test_next_action_after_reply(self):
        lead = self.c.post("/api/leads/inbound", json=S["inbound"]["website_form"]).json()
        self.approve_pending()
        out = self.c.post(f"/api/leads/{lead['id']}/reply", json={"body": R["partial"]}).json()
        self.assertIn("approve the follow-up", out["decision"]["next_action"])
        row = [l for l in self.c.get("/api/leads").json() if l["id"] == lead["id"]][0]
        self.assertEqual(row["bant_status"]["need"], "confirmed")
        self.assertEqual(row["next"]["owner"], "SDR reviewer")


if __name__ == "__main__":
    unittest.main()
