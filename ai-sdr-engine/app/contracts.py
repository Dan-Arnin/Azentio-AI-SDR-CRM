# app/contracts.py - Frozen shared data contracts (Pydantic) for every SDR module
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

Source = Literal["website", "webinar", "event", "ad", "marketing_csv", "target_account", "linkedin"]
ProductId = Literal["lending_platform", "islamic_banking", "aml_compliance", "unknown"]
Permission = Literal["allowed", "unknown", "denied"]

Stage = Literal[
    "NEW", "ENRICHED", "ENGAGING", "QUALIFYING", "REP_REVIEW", "OPPORTUNITY",
    "HUMAN_REVIEW", "NURTURE", "SUPPRESSED", "CLOSED", "QUARANTINED",
]
FitBand = Literal["High", "Medium", "Low"]
IcpStatus = Literal["matches_icp", "outside_icp", "needs_info", "needs_review"]
EvidenceStatus = Literal["confirmed", "inferred", "missing", "conflict"]
EvidenceOrigin = Literal["buyer_statement", "form", "mock_enrichment", "public_research", "apollo", "crm"]
ReplyIntent = Literal[
    "interested", "not_now", "wrong_person", "referral",
    "unsubscribe", "out_of_office", "not_interested", "other",
]
Route = Literal["rep_review", "human_review", "nurture", "suppressed", "closed", "existing_owner"]
DraftKind = Literal[
    "qualifying_email", "event_followup", "ad_followup", "gap_fill",
    "outbound_touch_1", "outbound_touch_2", "outbound_touch_3", "nurture_reminder", "linkedin_note", "sms_message",
]
DraftStatus = Literal["DRAFT", "APPROVED", "REJECTED", "SIMULATED_SENT", "STALE", "BLOCKED"]
EnrollmentState = Literal["ACTIVE", "STOPPED_REPLY", "STOPPED_SUPPRESSION", "COMPLETED"]
TaskType = Literal["ae_handoff", "sdr_followup", "sdr_review", "owner_task", "referral_review", "identity_review"]
TaskStatus = Literal["open", "acknowledged", "accepted", "returned", "done"]
BantKey = Literal["budget", "authority", "need", "timeline"]
BANT_KEYS: tuple[str, ...] = ("budget", "authority", "need", "timeline")


# Generate a prefixed opaque id
def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# Current UTC timestamp in ISO 8601
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class LeadInput(BaseModel):
    """What the website / CSV import / ad form sends us. Spec page 5 LeadInput."""
    sourceRecordId: str
    source: Source
    fullName: str
    email: str
    companyName: str
    country: str
    productId: ProductId = "unknown"
    inquiry: str = ""
    contactPermission: Permission = "allowed"
    phone: Optional[str] = None
    role: Optional[str] = None
    companyDomain: Optional[str] = None
    institutionType: Optional[str] = None
    employeeCount: Optional[int] = None
    eventName: Optional[str] = None
    campaignId: Optional[str] = None
    assetName: Optional[str] = None
    submittedAt: Optional[str] = None

    # Contact permission defaults to allowed; only an explicit "denied" blocks outreach
    @field_validator("contactPermission", mode="before")
    @classmethod
    def _default_permission(cls, value: Any) -> str:
        return "denied" if str(value or "").strip().lower() == "denied" else "allowed"


class Evidence(BaseModel):
    id: str = Field(default_factory=lambda: new_id("ev"))
    field: str
    value: Optional[str] = None
    excerpt: Optional[str] = None
    origin: EvidenceOrigin
    status: EvidenceStatus
    messageId: Optional[str] = None
    observedAt: str = Field(default_factory=now_iso)


class Enrichment(BaseModel):
    provider: str = "mock_apollo"
    mock: bool = True
    fetchedAt: str = Field(default_factory=now_iso)
    found: bool = False
    company: dict[str, Any] = Field(default_factory=dict)
    person: dict[str, Any] = Field(default_factory=dict)
    missing: list[str] = Field(default_factory=list)
    uncertain: list[str] = Field(default_factory=list)
    conflicts: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class IcpCheck(BaseModel):
    rule: str
    passed: Optional[bool]
    weight: int
    points: int
    detail: str
    origin: Optional[EvidenceOrigin] = None


class IcpResult(BaseModel):
    productId: str
    policyVersion: str
    score: int
    fit: FitBand
    status: IcpStatus
    checks: list[IcpCheck]
    reasons: list[str]


class BantField(BaseModel):
    status: EvidenceStatus = "missing"
    evidence: list[Evidence] = Field(default_factory=list)
    note: str = ""


class DecisionResult(BaseModel):
    """Normalised output of any judgment provider (OpenAI Decisions | Agents SDK judge | fixture)."""
    provider: str
    model: str
    questionVersion: str = "reply-intent-v1"
    intent: ReplyIntent
    intentConfidence: float
    bantSupport: dict[str, Literal["yes", "no", "unclear"]] = Field(default_factory=dict)
    confidenceByQuestion: dict[str, float] = Field(default_factory=dict)
    refusal: bool = False
    rationale: str = ""
    durationMs: int = 0
    usage: Optional[dict[str, Any]] = None


class Assessment(BaseModel):
    id: str = Field(default_factory=lambda: new_id("asm"))
    leadId: str
    leadVersion: int
    policyVersion: str
    icp: Optional[IcpResult] = None
    replyIntent: Optional[ReplyIntent] = None
    intentConfidence: Optional[float] = None
    bant: dict[str, BantField] = Field(default_factory=dict)
    bantScore: int = 0
    qualificationScore: int = 0
    missingFields: list[str] = Field(default_factory=list)
    route: Route
    routeReasons: list[str] = Field(default_factory=list)
    nextQuestion: Optional[str] = None
    nextAction: str = ""
    decision: Optional[DecisionResult] = None
    intentStrength: Literal["buying", "exploring", "none"] = "none"
    objections: list[dict[str, Any]] = Field(default_factory=list)
    competitors: list[dict[str, Any]] = Field(default_factory=list)
    buyingSignals: list[dict[str, Any]] = Field(default_factory=list)
    mode: Literal["live", "fixture"] = "live"
    droppedQuotes: list[dict[str, Any]] = Field(default_factory=list)  # proposed excerpts not found verbatim in the reply
    injectionExcerpt: Optional[str] = None  # set when the reply tried to instruct the system; scores were left unchanged
    createdAt: str = Field(default_factory=now_iso)


class Draft(BaseModel):
    id: str = Field(default_factory=lambda: new_id("drf"))
    leadId: str
    version: int = 1
    kind: DraftKind
    to: str
    subject: str
    body: str
    discoveryQuestion: Optional[str] = None
    nextAction: str = ""
    claimIds: list[str] = Field(default_factory=list)
    evidenceIds: list[str] = Field(default_factory=list)
    unresolvedClaims: list[str] = Field(default_factory=list)
    guardrailFlags: list[str] = Field(default_factory=list)
    status: DraftStatus = "DRAFT"
    contentHash: str = ""
    enrollmentId: Optional[str] = None
    dueAt: Optional[str] = None
    approvedBy: Optional[str] = None
    approvedAt: Optional[str] = None
    model: Optional[str] = None
    mode: Literal["live", "fixture"] = "live"
    createdAt: str = Field(default_factory=now_iso)


class Message(BaseModel):
    id: str = Field(default_factory=lambda: new_id("msg"))
    leadId: str
    threadId: str
    direction: Literal["outbound", "inbound"]
    subject: str = ""
    body: str
    at: str = Field(default_factory=now_iso)
    origin: Literal["simulated", "scripted", "test_inbox", "form", "resend", "twilio", "phantombuster"] = "simulated"
    draftId: Optional[str] = None
    enrollmentId: Optional[str] = None
    transport: Literal["simulated", "resend", "twilio", "phantombuster"] = "simulated"
    intendedTo: Optional[str] = None
    deliveredTo: Optional[str] = None
    providerMessageId: Optional[str] = None
    deliveryStatus: Optional[Literal["accepted", "simulated"]] = None


class ReplyInput(BaseModel):
    """Inbound reply to a thread. Inbound + outbound replies use the same path (ingestReply)."""
    bodyText: str
    messageId: Optional[str] = None
    threadId: Optional[str] = None
    receivedAt: Optional[str] = None
    origin: Literal["scripted", "test_inbox"] = "scripted"


class CohortDefinition(BaseModel):
    productId: ProductId = "islamic_banking"
    roles: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)
    institutionTypes: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    limit: int = 10


Channel = Literal["email", "linkedin", "whatsapp", "sms"]


class CampaignInput(BaseModel):
    name: str
    theme: str
    cohort: CohortDefinition
    touchDays: list[int] = Field(default_factory=lambda: [0, 3, 7])
    channels: list[Channel] = Field(default_factory=lambda: ["email"])


class Campaign(BaseModel):
    id: str = Field(default_factory=lambda: new_id("cmp"))
    name: str
    theme: str
    cohort: CohortDefinition
    touchDays: list[int]
    channels: list[Channel] = Field(default_factory=lambda: ["email"])
    status: Literal["DRAFTING", "ACTIVE", "COMPLETED"] = "DRAFTING"
    enrollmentIds: list[str] = Field(default_factory=list)
    exclusions: list[dict[str, Any]] = Field(default_factory=list)
    createdAt: str = Field(default_factory=now_iso)


class Enrollment(BaseModel):
    id: str = Field(default_factory=lambda: new_id("enr"))
    campaignId: str
    leadId: str
    state: EnrollmentState = "ACTIVE"
    nextTouch: int = 1
    nextDueAt: Optional[str] = None
    touchDraftIds: list[str] = Field(default_factory=list)
    stopReason: Optional[str] = None


class HandoffCard(BaseModel):
    """What the AE/SDR sees: spec D3 handoff card."""
    leadId: str
    contact: dict[str, Any]
    account: dict[str, Any]
    productId: str
    source: str
    owner: str
    icp: Optional[IcpResult] = None
    bant: dict[str, BantField] = Field(default_factory=dict)
    qualificationScore: int = 0
    replyIntent: Optional[str] = None
    threadId: Optional[str] = None
    unresolved: list[str] = Field(default_factory=list)
    recommendedNextAction: str = ""
    summary: str = ""
    policyVersion: str = ""
    models: dict[str, str] = Field(default_factory=dict)
    engagement: dict[str, Any] = Field(default_factory=dict)
    routingReason: str = ""


class Task(BaseModel):
    id: str = Field(default_factory=lambda: new_id("tsk"))
    leadId: str
    type: TaskType
    ownerId: str
    title: str
    card: Optional[HandoffCard] = None
    status: TaskStatus = "open"
    createdAt: str = Field(default_factory=now_iso)


class CrmEvent(BaseModel):
    """Outbox record pushed to the teammates' CRM via webhook."""
    id: str = Field(default_factory=lambda: new_id("crm"))
    type: str
    leadId: Optional[str] = None
    payload: dict[str, Any]
    status: Literal["pending", "delivered", "failed", "no_target"] = "pending"
    attempts: int = 0
    lastError: Optional[str] = None
    createdAt: str = Field(default_factory=now_iso)


class Run(BaseModel):
    """One background pipeline execution (enrich/assess/draft/reply/campaign)."""
    id: str = Field(default_factory=lambda: new_id("run"))
    leadId: Optional[str] = None
    kind: str
    status: Literal["queued", "running", "succeeded", "failed"] = "queued"
    mode: Literal["live", "fixture"] = "live"
    steps: list[dict[str, Any]] = Field(default_factory=list)
    errorCode: Optional[str] = None
    startedAt: str = Field(default_factory=now_iso)
    finishedAt: Optional[str] = None


class Lead(BaseModel):
    """The SDR engine's working record for one contact+product."""
    id: str = Field(default_factory=lambda: new_id("lead"))
    version: int = 1
    input: LeadInput
    sources: list[dict[str, Any]] = Field(default_factory=list)
    normalizedEmail: str
    accountKey: Optional[str] = None
    accountOwnerId: Optional[str] = None
    stage: Stage = "NEW"
    ownerId: Optional[str] = None
    enrichment: Optional[Enrichment] = None
    icp: Optional[IcpResult] = None
    latestAssessmentId: Optional[str] = None
    threadId: str = Field(default_factory=lambda: new_id("thr"))
    gapFillsSent: int = 0
    suppressed: bool = False
    suppressionReason: Optional[str] = None
    flags: list[str] = Field(default_factory=list)
    history: list[dict[str, Any]] = Field(default_factory=list)
    createdAt: str = Field(default_factory=now_iso)
    updatedAt: str = Field(default_factory=now_iso)
