"""Decision engine boundary (workstream C).

The backend (B) calls only the functions on `Engine`. C can replace `FixtureEngine` with a model-backed
engine (OpenAI, Jev, Claude...) without touching routes or persistence, as long as the return shapes
below are kept. Shapes are plain dicts so they serialise straight into the jsonb columns.

LeadContext (built by the backend from the database):
    {"leadId", "source", "sourceContext": {...}, "enquiry",
     "contact": {"firstName", "lastName", "email", "jobTitle", "seniority", "phone"},
     "account": {"name", "domain", "industry", "institutionType", "country", "region", "employeeCount"} | None,
     "product": {"slug", "name", "keywords": [...]} | None}

enrich(ctx) -> {"provider", "status": complete|partial|failed,
                "person": {"jobTitle", "seniority", "department"}, "company": {...account fields...},
                "data": {field: {"value", "confidence", "source"}}, "missing": [str], "uncertain": [str]}

score_icp(ctx, profile, criteria) -> {"score": 0-100, "fit": high|medium|low|insufficient_data,
                "missingCount", "breakdown": [{"dimension","weight","points","status","evidence","explanation"}],
                "summary"}

assess_reply(text, ctx, product_keywords) -> {"intent", "intent_confidence",
                "bant": {dim: {"status", "evidence", "source", "note"}}, "timeline_within_12_months",
                "referral": {"name","email","title"}, "asks_for_pricing", "summary", "engine"}
"""
from typing import Protocol

from .fixture import FixtureEngine


class Engine(Protocol):
    name: str

    def enrich(self, ctx: dict) -> dict: ...

    def score_icp(self, ctx: dict, profile: dict, criteria: list[dict]) -> dict: ...

    def assess_reply(self, text: str, ctx: dict, product_keywords: list[str]) -> dict: ...


def get_engine() -> Engine:
    # C: switch on os.environ["DECISION_PROVIDER"] here (fixture | openai | jev ...).
    return FixtureEngine()
