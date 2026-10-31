from fastapi import APIRouter, Depends
from psycopg import Connection

from .. import pipeline, sequences
from ..contracts import AcceptInput, ApproveInput, DraftInput, RejectInput, ReplyInput, RunOut, SequenceInput, TaskOut, TickInput
from ..db import get_conn
from ..repo import NotFound

router = APIRouter(prefix="/v1", tags=["workflow"])


@router.post("/leads/{lead_id}/drafts")
def create_draft(lead_id: int, inp: DraftInput, conn: Connection = Depends(get_conn)):
    gap = None
    if inp.stage == "follow_up":
        last = conn.execute("SELECT rule_trace FROM bant_assessments WHERE lead_id = %s ORDER BY id DESC LIMIT 1", [lead_id]).fetchone()
        gap = "need" if not last else None
    out = pipeline.create_draft(conn, lead_id, inp.stage, gap=gap)
    return {**out, "draftId": str(out["draftId"]), "runId": str(out["runId"])}


@router.post("/drafts/{draft_id}/approve")
def approve(draft_id: int, inp: ApproveInput, conn: Connection = Depends(get_conn)):
    return pipeline.approve(conn, draft_id, inp.approvedBy, inp.body, inp.expectedVersion)


@router.post("/drafts/{draft_id}/reject")
def reject(draft_id: int, inp: RejectInput, conn: Connection = Depends(get_conn)):
    return pipeline.reject(conn, draft_id, inp.rejectedBy, inp.reason)


@router.post("/drafts/{draft_id}/send")
def send(draft_id: int, conn: Connection = Depends(get_conn)):
    return pipeline.send(conn, draft_id)


@router.post("/replies")
def ingest_reply(inp: ReplyInput, conn: Connection = Depends(get_conn)):
    if not inp.leadId.isdigit() or not conn.execute("SELECT 1 FROM leads WHERE id = %s", [int(inp.leadId)]).fetchone():
        raise NotFound(f"lead {inp.leadId}")
    return pipeline.ingest_reply(conn, int(inp.leadId), inp.messageId, inp.threadId, inp.bodyText, inp.origin)


@router.get("/tasks", response_model=list[TaskOut])
def tasks(status: str | None = None, conn: Connection = Depends(get_conn)):
    return pipeline.list_tasks(conn, status)


@router.post("/tasks/{task_id}/accept")
def accept(task_id: str, inp: AcceptInput, conn: Connection = Depends(get_conn)):
    return pipeline.accept_task(conn, task_id, inp.acceptedBy)


@router.get("/runs/{run_id}", response_model=RunOut)
def get_run(run_id: int, conn: Connection = Depends(get_conn)):
    r = conn.execute("SELECT r.*, a.key FROM agent_runs r JOIN agents a ON a.id = r.agent_id WHERE r.id = %s", [run_id]).fetchone()
    if not r:
        raise NotFound(f"run {run_id}")
    return RunOut(runId=str(r["id"]), agent=r["key"], status=r["status"], summary=r["summary"], trigger=r["trigger"],
                  leadId=str(r["lead_id"]) if r["lead_id"] else None, durationMs=r["duration_ms"], error=r["error"], output=r["output"])


@router.get("/sequences")
def list_sequences(conn: Connection = Depends(get_conn)):
    return sequences.list_all(conn)


@router.post("/sequences")
def create_sequence(inp: SequenceInput, conn: Connection = Depends(get_conn)):
    return sequences.create(conn, inp.leadIds, inp.theme, inp.cadenceHours)


@router.post("/sequences/{sequence_id}/tick")
def tick(sequence_id: int, inp: TickInput, conn: Connection = Depends(get_conn)):
    return sequences.tick(conn, sequence_id, inp.advanceHours)
