"""API contracts. Request models match openapi.json field-for-field (camelCase, same enums and limits).
Response models are new: openapi.json declared every 200 as untyped, which left the website and CRM
owners nothing to code against.

Contract decisions (see README "Contract decisions"):
* IDs are database integers, sent as strings.
* `source` accepts the openapi values and the schema values; it is stored using the schema vocabulary.
* `contactPermission` allowed/unknown/denied maps to consent_status opted_in/unknown/opted_out.
"""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ApiSource = Literal[
    # openapi.json values
    "website", "webinar", "event", "ad", "marketing_csv", "target_account",
    # schema values (preferred: Level 1 names LinkedIn and Google ads separately)
    "website_enquiry", "marketing_campaign", "linkedin_ads", "google_ads", "outbound",
]

SOURCE_TO_DB = {
    "website": "website_enquiry", "marketing_csv": "marketing_campaign", "ad": "linkedin_ads",
    "target_account": "outbound",
}
PERMISSION_TO_CONSENT = {"allowed": "opted_in", "unknown": "unknown", "denied": "opted_out"}


EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ───────────── requests (openapi.json) ─────────────

class LeadInput(_In):
    sourceRecordId: str = Field(max_length=200)
    source: ApiSource
    fullName: str = Field(min_length=1, max_length=200)
    email: str = Field(max_length=320, pattern=EMAIL_PATTERN)
    companyName: str = Field(min_length=1, max_length=200)
    country: str = Field(min_length=1, max_length=80)
    productId: str = Field(min_length=1, max_length=80)
    inquiry: str = Field(min_length=1, max_length=12000)
    contactPermission: Literal["allowed", "unknown", "denied"] = "unknown"
    phone: str | None = None
    role: str | None = None
    companyDomain: str | None = None
    institutionType: str | None = None
    employeeCount: int | None = None
    eventName: str | None = None
    campaignId: str | None = None
    topic: str | None = None
    asset: str | None = None
    submittedAt: str | None = None
    ownerId: str | None = None
    existingOpportunityId: str | None = None


class ImportInput(_In):
    leads: list[dict[str, Any]]


class DraftInput(_In):
    stage: Literal["initial", "follow_up", "outbound"] = "initial"


class ApproveInput(_In):
    approvedBy: str = Field(max_length=200)
    # Additions (optional, backward compatible): edit-on-approve and stale-approval protection.
    body: str | None = None
    expectedVersion: int | None = None


class RejectInput(_In):
    rejectedBy: str = Field(max_length=200)
    reason: str = Field(max_length=2000)


class ReplyInput(_In):
    messageId: str = Field(max_length=200)
    threadId: str = Field(max_length=200)
    leadId: str = Field(max_length=200)
    bodyText: str = Field(min_length=1, max_length=12000)
    receivedAt: str | None = None
    origin: Literal["scripted", "test_inbox"] = "scripted"


class AcceptInput(_In):
    acceptedBy: str = Field(max_length=200)


class SequenceInput(_In):
    leadIds: list[str]
    theme: str = Field(max_length=500)
    cadenceHours: list[int] = [0, 72, 168]


class TickInput(_In):
    advanceHours: int = Field(default=0, ge=0, le=720)


# ───────────── responses (new) ─────────────

class LeadCreated(BaseModel):
    leadId: str
    disposition: Literal["created", "existing", "duplicate", "blocked"]
    runId: str | None = None
    stage: str
    reasons: list[str] = []


class LeadSummary(BaseModel):
    leadId: str
    fullName: str
    email: str
    company: str | None
    country: str | None
    source: str
    product: str | None
    stage: str
    route: str | None
    icpScore: int | None
    icpFit: str | None
    bantScore: int | None
    bantStatus: str
    readyForFollowup: bool
    owner: str | None
    nextAction: str | None
    createdAt: str


class ImportRowResult(BaseModel):
    row: int
    status: Literal["created", "duplicate", "rejected"]
    leadId: str | None = None
    errors: list[str] = []


class ImportResult(BaseModel):
    importId: str
    status: str
    total: int
    accepted: int
    rejected: int
    rows: list[ImportRowResult]


class RunOut(BaseModel):
    runId: str
    agent: str
    status: str
    summary: str | None
    trigger: str
    leadId: str | None
    durationMs: int | None
    error: str | None
    output: Any = None


class TaskOut(BaseModel):
    taskId: str          # "handoff-<id>" or "action-<id>"
    kind: Literal["handoff", "next_action"]
    leadId: str
    type: str
    description: str
    status: str
    assignee: str | None
    card: Any = None
    createdAt: str
