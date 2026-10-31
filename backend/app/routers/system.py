from fastapi import APIRouter, Depends, Query
from psycopg import Connection

from ..db import get_conn

router = APIRouter(tags=["system"])


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/ready")
def ready(conn: Connection = Depends(get_conn)):
    applied = [r["filename"] for r in conn.execute("SELECT filename FROM schema_migrations ORDER BY filename").fetchall()]
    return {"status": "ready", "migrations": applied}


@router.get("/v1/config")
def config(conn: Connection = Depends(get_conn)):
    products = conn.execute("SELECT id, slug, name, category, summary, keywords FROM products WHERE active ORDER BY id").fetchall()
    for p in products:
        p["facts"] = conn.execute("SELECT id, fact_type, statement, source_ref FROM product_facts WHERE product_id = %s AND approved_at IS NOT NULL",
                                  [p["id"]]).fetchall()
        p["icp"] = conn.execute("SELECT id, name, version, high_threshold, medium_threshold FROM icp_profiles WHERE product_id = %s AND active",
                                [p["id"]]).fetchone()
        p["icp"]["criteria"] = conn.execute("SELECT dimension, weight, rule, description FROM icp_criteria WHERE icp_profile_id = %s ORDER BY id",
                                            [p["icp"]["id"]]).fetchall()
    users = conn.execute("SELECT id, name, email, role, region FROM users ORDER BY id").fetchall()
    agents = conn.execute("SELECT key, name, role, provider, model, status FROM agents ORDER BY id").fetchall()
    return {"products": products, "users": users, "agents": agents, "emailTransport": "simulated"}


@router.get("/v1/policies")
def policies(conn: Connection = Depends(get_conn)):
    return conn.execute("SELECT kind, version, content, created_at FROM policies WHERE active ORDER BY kind").fetchall()


@router.get("/v1/dashboard")
def dashboard(conn: Connection = Depends(get_conn)):
    grp = lambda col: {r["k"] or "none": r["n"] for r in conn.execute(f"SELECT {col} AS k, count(*) AS n FROM leads GROUP BY 1").fetchall()}  # noqa: E731
    one = lambda sql: conn.execute(sql).fetchone()  # noqa: E731
    return {
        "totalLeads": one("SELECT count(*) AS n FROM leads")["n"],
        "bySource": grp("source"), "byStage": grp("stage"), "byIcpFit": grp("icp_fit"), "byBantStatus": grp("bant_status"),
        "readyForFollowup": one("SELECT count(*) AS n FROM leads WHERE ready_for_followup")["n"],
        "pendingApprovals": one("SELECT count(*) AS n FROM messages WHERE status = 'pending_approval'")["n"],
        "openTasks": one("SELECT (SELECT count(*) FROM next_actions WHERE status='open') + (SELECT count(*) FROM handoffs WHERE status='pending') AS n")["n"],
        "simulatedSends": one("SELECT count(*) AS n FROM messages WHERE status = 'sent_simulated'")["n"],
        "avgFirstResponseSeconds": one("SELECT round(avg(response_time_seconds)) AS v FROM leads")["v"],
    }


@router.get("/v1/accounts")
def accounts(conn: Connection = Depends(get_conn)):
    return conn.execute("""
        SELECT a.*, u.name AS owner,
               (SELECT count(*) FROM leads l WHERE l.account_id = a.id) AS leads,
               (SELECT count(*) FROM opportunities o WHERE o.account_id = a.id AND o.status = 'open') AS open_opportunities
        FROM accounts a LEFT JOIN users u ON u.id = a.owner_id ORDER BY a.name""").fetchall()


@router.get("/v1/events")
def events(after: int = 0, limit: int = Query(100, ge=1, le=500), conn: Connection = Depends(get_conn)):
    return conn.execute("SELECT * FROM audit_log WHERE id > %s ORDER BY id LIMIT %s", [after, limit]).fetchall()


@router.get("/v1/suppressions")
def suppressions(conn: Connection = Depends(get_conn)):
    return conn.execute("SELECT * FROM suppressions ORDER BY id DESC").fetchall()
