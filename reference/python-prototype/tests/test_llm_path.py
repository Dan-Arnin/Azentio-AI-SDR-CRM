"""Claude-path behaviour without network: evidence verification and fallback to the deterministic engine."""
import os
import tempfile
import unittest
from unittest import mock

os.environ["SDR_DB"] = os.path.join(tempfile.mkdtemp(), "test_llm.db")

from app import analyzer, llm  # noqa: E402

REPLY = "We are replacing our legacy lending platform. Vendor selection is planned for Q1 2027."
LEAD = {"person": {"first_name": "Sara", "title": "Head of Digital Lending", "seniority": "head"}, "company": {"name": "Najd"}, "product": "kastle"}


def sig(status, evidence="", source="none"):
    return {"status": status, "evidence": evidence, "source": source, "note": ""}


def model_output(**bant):
    base = {k: sig("missing") for k in analyzer.SIGNALS}
    base.update(bant)
    return {"intent": "interested", "intent_confidence": 0.9, "bant": base, "timeline_within_12_months": "unknown",
            "referral": {"name": "", "email": "", "title": ""}, "asks_for_pricing": False, "summary": ""}


class LLMPath(unittest.TestCase):
    def test_fabricated_evidence_is_downgraded(self):
        out = model_output(
            need=sig("confirmed", "replacing our legacy lending platform", "reply"),
            budget=sig("confirmed", "We have approved funding", "reply"),           # not in the reply
            authority=sig("inferred", "Head of Digital Lending", "enrichment"),
            timeline=sig("confirmed", "Vendor selection is planned for Q1 2027", "reply"),
        )
        with mock.patch.object(llm, "enabled", return_value=True), mock.patch.object(llm, "structured", return_value=out):
            r = analyzer.analyse(REPLY, LEAD)
        self.assertTrue(r["engine"].startswith("claude:"))
        self.assertEqual(r["bant"]["need"]["status"], "confirmed")
        self.assertEqual(r["bant"]["budget"]["status"], "missing")
        self.assertIn("downgraded", r["bant"]["budget"]["note"])
        self.assertEqual(r["bant"]["authority"]["status"], "inferred")
        self.assertEqual(r["timeline_within_12_months"], "yes")

    def test_llm_failure_falls_back(self):
        with mock.patch.object(llm, "enabled", return_value=True), \
             mock.patch.object(llm, "structured", side_effect=llm.LLMUnavailable("rate limited")):
            r = analyzer.analyse(REPLY, LEAD)
        self.assertEqual(r["engine"], "heuristic")
        self.assertIn("rate limited", r["engine_note"])
        self.assertEqual(r["bant"]["need"]["status"], "confirmed")


if __name__ == "__main__":
    unittest.main()
