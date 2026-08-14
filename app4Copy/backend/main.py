import csv
import io
import os
import shutil
import tempfile
from datetime import datetime, timedelta
from typing import Optional

import pymysql
from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

import hashlib

# from app4_.backend.config import RECOR, RECORDINGS_PLAYBACK_BASE_URLDINGS_PLAYBACK_BASE_URL
from config import PARAMETERS, RECORDINGS_BASE_URL, RECORDINGS_PLAYBACK_BASE_URL
from db import get_conn, init_db
from pipeline import process_manifest, save_manifest

app = FastAPI(title="Athena QA Dashboard")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------------------------------------------------------
# IMPORTANT FIX: init_db() used to run unguarded at import time. If MySQL
# was unreachable (wrong host/user/password, or the server just wasn't up
# yet), that raised an exception WHILE the module was loading - before
# FastAPI/CORSMiddleware even finished being built. uvicorn's worker
# process died right there, so nothing was listening on the port at all.
#
# That's exactly what showed up in the browser as CORS errors + net::ERR_FAILED:
# there was no server to send a response (let alone CORS headers), so Chrome
# reports it as a blocked/failed request instead of a clean 500.
#
# Fix: catch the startup failure, log it clearly, and let the app come up
# anyway. Every DB-touching request will then fail with a clean 503 JSON
# response (see exception handler below) that DOES carry CORS headers,
# instead of the whole server being unreachable.
# ------------------------------------------------------------------
try:
    init_db()
except Exception as e:
    print("=" * 70)
    print("[STARTUP WARNING] Could not connect to MySQL / initialize schema.")
    print(f"[STARTUP WARNING] {type(e).__name__}: {e}")
    print("[STARTUP WARNING] Check MYSQL_HOST / MYSQL_PORT / MYSQL_USER / "
          "MYSQL_PASSWORD / MYSQL_DATABASE env vars and that MySQL is running.")
    print("[STARTUP WARNING] The API is still starting so you get clean 503s "
          "instead of connection failures - fix your DB config and restart.")
    print("=" * 70)


@app.exception_handler(pymysql.err.Error)
async def mysql_error_handler(request: Request, exc: pymysql.err.Error):
    return JSONResponse(
        status_code=503,
        content={"detail": f"Database unavailable: {exc}"},
    )


def get_db():
    conn = get_conn()
    try:
        yield conn
    finally:
        conn.close()


def hash_pw(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()


# ---------- Schemas ----------
class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str
    username: str
    role: str


# ---------- Auth ----------
@app.post("/api/login", response_model=LoginResponse)
def login(req: LoginRequest, db = Depends(get_db)):
    row = db.execute(
        "SELECT * FROM users WHERE username = ? AND password_hash = ?",
        (req.username, hash_pw(req.password)),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = hashlib.sha256(f"{req.username}{datetime.utcnow()}".encode()).hexdigest()
    return LoginResponse(token=token, username=row["username"], role=row["role"])


class ChangePasswordRequest(BaseModel):
    username: str
    current_password: str
    new_password: str


@app.post("/api/change-password")
def change_password(req: ChangePasswordRequest, db = Depends(get_db)):
    # There's no session/bearer-token check anywhere else in this app yet
    # (login just hands back an unverified token) - so identity here is
    # proven the same way login proves it: username + correct current
    # password. If you add real session auth later, swap this for a
    # Depends(current_user) check instead of trusting the request body.
    if len(req.new_password) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")

    row = db.execute(
        "SELECT * FROM users WHERE username = ? AND password_hash = ?",
        (req.username, hash_pw(req.current_password)),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="Current password is incorrect")

    db.execute(
        "UPDATE users SET password_hash = ? WHERE username = ?",
        (hash_pw(req.new_password), req.username),
    )
    db.commit()
    return {"status": "ok"}


# ---------- Helpers ----------
def date_range_defaults(start: Optional[str], end: Optional[str]):
    if not end:
        end = datetime.utcnow().strftime("%Y-%m-%d")
    if not start:
        start = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
    return start, end


# Whitelist of SQL fragments for the 5 clickable dashboard stat cards (feature 2).
# Kept as a fixed dict (not built from the path param) so there's no injection risk
# even though it gets interpolated straight into the query string below.
CATEGORY_FILTERS = {
    "total": "1=1",
    "audited": "c.status = 'audited'",
    "pending": "c.status IN ('pending','transcribing','analyzing')",
    "positive": "c.sentiment = 'positive'",
    "negative": "c.sentiment = 'negative'",
    # Agent-detail summary cards (feature: clickable Total/Average/Good/Poor).
    # "Total Calls" and "Average Call" both draw from the same audited
    # population that agent_detail()'s stats are computed over, so they
    # share the "audited" filter - only the label shown differs.
    "good": "c.overall_quality = 'good'",
    "poor": "c.overall_quality = 'poor'",
    # Disposition-based (feature: below the funnel). Case-insensitive since
    # the manifest's call_end_type_name casing isn't guaranteed consistent
    # (we've seen "CALLBACK" in real data).
    "disp_not_eligible": "UPPER(c.call_end_type_name) = 'NOT ELIGIBLE'",
    "disp_not_interested": "UPPER(c.call_end_type_name) = 'NOT INTERESTED'",
    "disp_callback": "UPPER(c.call_end_type_name) = 'CALLBACK'",
    "disp_short_hangup": "UPPER(c.call_end_type_name) = 'SHORT HANGUP'",
    "disp_lead": """ UPPER(c.call_end_type_name) IN ('LEAD','INTERESTED') """,
    # "disp_eligible": "UPPER(c.call_end_type_name) = 'ELIGIBLE'",
    "disp_hangup": "UPPER(c.call_end_type_name) = 'HANGUP'",
    # Correct/Wrong: actual (manifest) vs predicted (SLM) disposition, compared
    # once and cached on the row at process time (pipeline.py) rather than
    # here - disposition_match is NULL (excluded from both) for any call
    # missing either side of the comparison, so this filter alone is enough,
    # no extra "and both sides present" clause needed.
    "disp_correct": "c.disposition_match = 1",
    "disp_wrong": "c.disposition_match = 0",
}

# Labels + ordering for the disposition buttons row on the dashboard.
# DISPOSITION_CATEGORIES = [
#     ("disp_not_eligible", "Not Eligible", "NOT ELIGIBLE"),
#     ("disp_not_interested", "Not Interested", "NOT INTERESTED"),
#     ("disp_callback", "Callback", "CALLBACK"),
#     ("disp_short_hangup", "Short Hangup", "SHORT HANGUP"),
#     ("disp_lead", "Lead", "LEAD"),
#     # ("disp_eligible", "Eligible", "ELIGIBLE"),
#     ("disp_hangup", "Hangup", "HANGUP"),
# ]

DISPOSITION_CATEGORIES = [
    ("disp_not_eligible", "Not Eligible", "NOT ELIGIBLE"),
    ("disp_not_interested", "Not Interested", "NOT INTERESTED"),
    ("disp_callback", "Callback", "CALLBACK"),
    # ("disp_short_hangup", "Short Hangup", "SHORT HANGUP"),
    ("disp_short_hangup", "Call disconnected", "SHORT HANGUP"),
    ("disp_lead", "Lead", "LEAD"),
    # ("disp_eligible", "Eligible", "ELIGIBLE"),
    # ("disp_interested", "Interested", "INTERESTED"),
]



def category_filter_sql(category: str) -> str:
    if category not in CATEGORY_FILTERS:
        # if category=="disp_lead":
        raise HTTPException(
            status_code=400,
            detail=f"Unknown category '{category}'. Must be one of: {', '.join(CATEGORY_FILTERS)}",
        )
    return CATEGORY_FILTERS[category]


# ---------- Dashboard summary (Section 2 top cards) ----------
@app.get("/api/dashboard/summary")
def dashboard_summary(
    start: Optional[str] = None,
    end: Optional[str] = None,
    db = Depends(get_db),
):
    start, end = date_range_defaults(start, end)
    row = db.execute(
        """
        SELECT
          COUNT(*) AS total_calls,
          SUM(CASE WHEN status = 'audited' THEN 1 ELSE 0 END) AS audited_calls,
          SUM(CASE WHEN status IN ('pending','transcribing','analyzing') THEN 1 ELSE 0 END) AS pending_calls,
          SUM(CASE WHEN sentiment = 'positive' THEN 1 ELSE 0 END) AS positive_calls,
          SUM(CASE WHEN sentiment = 'negative' THEN 1 ELSE 0 END) AS negative_calls
        FROM calls
        WHERE audited_at IS NOT NULL AND date(audited_at) BETWEEN date(?) AND date(?)
        """,
        (start, end),
    ).fetchone()
    return {
        "start": start,
        "end": end,
        "total_calls": row["total_calls"] or 0,
        "audited_calls": row["audited_calls"] or 0,
        "pending_calls": row["pending_calls"] or 0,
        "positive_calls": row["positive_calls"] or 0,
        "negative_calls": row["negative_calls"] or 0,
    }


# ---------- Disposition counts (feature: buttons below the funnel) ----------
# One grouped query for the manifest-based dispositions (not one query per
# button), plus one small aggregate for Correct/Wrong (disposition_match),
# then mapped onto the known categories. Drill-down reuses the exact same
# /api/dashboard/category/{category}/... endpoints as the 5 stat cards do -
# "disp_not_eligible" etc, and now "disp_correct"/"disp_wrong" too, are just
# more entries in CATEGORY_FILTERS above.

@app.get("/api/dashboard/dispositions")
def dashboard_dispositions(
    start: Optional[str] = None,
    end: Optional[str] = None,
    db = Depends(get_db),
):
    start, end = date_range_defaults(start, end)
    rows = db.execute(
        """
        SELECT UPPER(call_end_type_name) AS disp, COUNT(*) AS n
        FROM calls
        WHERE audited_at IS NOT NULL AND date(audited_at) BETWEEN date(?) AND date(?)
          AND call_end_type_name IS NOT NULL AND call_end_type_name != ''
        GROUP BY UPPER(call_end_type_name)
        """,
        (start, end),
    ).fetchall()
    print("====================dispotositon match"*10)
    print(rows)
    counts = {r["disp"]: r["n"] for r in rows}

    match_row = db.execute(
        """
        SELECT
          SUM(CASE WHEN disposition_match = 1 THEN 1 ELSE 0 END) AS correct_n,
          SUM(CASE WHEN disposition_match = 0 THEN 1 ELSE 0 END) AS wrong_n
        FROM calls
        WHERE audited_at IS NOT NULL AND date(audited_at) BETWEEN date(?) AND date(?)
        """,
        (start, end),
    ).fetchone()

    # dispositions = [
    #     {"category": cat, "label": label, "count": counts.get(match_value, 0)}
    #     for cat, label, match_value in DISPOSITION_CATEGORIES
    # ]
    dispositions = []

    for cat, label, match_value in DISPOSITION_CATEGORIES:

        if cat == "disp_lead":
            count = (
            counts.get("LEAD", 0)
            + counts.get("INTERESTED", 0)
        )
        else:
            count = counts.get(match_value, 0)

        dispositions.append({
        "category": cat,
        "label": label,
        "count": count,
        })
    # dispositions.append({"category": "disp_correct", "label": "Correct", "count": match_row["correct_n"] or 0})
    dispositions.append({"category": "disp_wrong", "label": "Wrong", "count": match_row["wrong_n"] or 0})

    return {
        "start": start,
        "end": end,
        "dispositions": dispositions,
    }


# ---------- Parameter-wise average scores (graph) ----------
@app.get("/api/dashboard/parameters")
def dashboard_parameters(
    start: Optional[str] = None,
    end: Optional[str] = None,
    db = Depends(get_db),
):
    start, end = date_range_defaults(start, end)
    rows = db.execute(
        """
        SELECT p.label AS parameter, ROUND(AVG(s.score), 1) AS avg_score, COUNT(*) AS n
        FROM call_scores s
        JOIN parameters p ON p.id = s.parameter_id
        JOIN calls c ON c.id = s.call_id
        WHERE c.audited_at IS NOT NULL AND date(c.audited_at) BETWEEN date(?) AND date(?)
        GROUP BY p.id, p.label
        ORDER BY p.id
        """,
        (start, end),
    ).fetchall()
    return {
        "start": start,
        "end": end,
        "parameters": [
            {"parameter": r["parameter"], "avg_score": r["avg_score"] or 0, "sample_size": r["n"]}
            for r in rows
        ],
    }


# ---------- Parameter success-rate funnel (feature 1) ----------
# Different from /api/dashboard/parameters above (which averages the 0-100
# normalized score). This instead answers "out of every AUDITED call, what
# % actually PASSED (status=true) this parameter?" - sorted highest-first so
# it reads top-to-bottom like a funnel: parameters almost everyone nails at
# the top, parameters agents struggle with at the bottom.
#
# success_pct = (# audited calls where that parameter's status = TRUE)
#               / (total audited calls in range) * 100
#
# Two queries total (not N+1): one scalar for the constant denominator
# (total audited calls), one GROUP BY for all 16 numerators together.
@app.get("/api/dashboard/parameter-funnel")
def parameter_funnel(
    start: Optional[str] = None,
    end: Optional[str] = None,
    db = Depends(get_db),
):
    start, end = date_range_defaults(start, end)

    total_row = db.execute(
        """
        SELECT COUNT(*) AS total_audited
        FROM calls
        WHERE status = 'audited' AND audited_at IS NOT NULL
          AND date(audited_at) BETWEEN date(?) AND date(?)
        """,
        (start, end),
    ).fetchone()
    total_audited = total_row["total_audited"] or 0

    rows = db.execute(
        """
        SELECT p.id AS parameter_id, p.label AS parameter,
               SUM(CASE WHEN s.status = 1 THEN 1 ELSE 0 END) AS success_count,
               COUNT(*) AS sample_size
        FROM call_scores s
        JOIN parameters p ON p.id = s.parameter_id
        JOIN calls c ON c.id = s.call_id
        WHERE c.status = 'audited' AND c.audited_at IS NOT NULL
          AND date(c.audited_at) BETWEEN date(?) AND date(?)
        GROUP BY p.id, p.label
        ORDER BY p.id
        """,
        (start, end),
    ).fetchall()

    funnel = []
    for r in rows:
        success_count = r["success_count"] or 0
        pct = round((success_count / total_audited) * 100, 1) if total_audited else 0.0
        funnel.append({
            "parameter_id": r["parameter_id"],
            "parameter": r["parameter"],
            "success_count": success_count,
            "sample_size": r["sample_size"],
            "success_pct": pct,
        })

    funnel.sort(key=lambda x: x["success_pct"], reverse=True)

    return {
        "start": start,
        "end": end,
        "total_audited_calls": total_audited,
        "parameters": funnel,
    }



# ---------- Agents belonging to a parameter (click on bar) ----------
@app.get("/api/dashboard/parameters/{parameter_name:path}/agents")
def agents_for_parameter(
    parameter_name: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    db = Depends(get_db),
):
    start, end = date_range_defaults(start, end)
    rows = db.execute(
        """
        SELECT a.id AS agent_id,
               a.name AS agent_name,
               a.team,
               ROUND(AVG(s.score), 1) AS avg_score,
               COUNT(*) AS calls_scored
        FROM call_scores s
        JOIN parameters p ON p.id = s.parameter_id
        JOIN calls c ON c.id = s.call_id
        JOIN agents a ON a.id = c.agent_id
        WHERE p.label = ?
          AND c.audited_at IS NOT NULL
          AND date(c.audited_at) BETWEEN date(?) AND date(?)
        GROUP BY a.id, a.name, a.team
        ORDER BY avg_score ASC
        """,
        (parameter_name, start, end),
    ).fetchall()
    if not rows:
        exists = db.execute("SELECT 1 FROM parameters WHERE label = ?", (parameter_name,)).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="Parameter not found")
    return {
        "parameter": parameter_name,
        "start": start,
        "end": end,
        "agents": [dict(r) for r in rows],
    }


# ---------- Agent list (basic) ----------
@app.get("/api/agents")
def list_agents(db = Depends(get_db)):
    rows = db.execute("SELECT id, name, team FROM agents ORDER BY name").fetchall()
    return {"agents": [dict(r) for r in rows]}


# ---------- Agent detail (Section 3) ----------
@app.get("/api/agents/{agent_id}")
def agent_detail(
    agent_id: int,
    start: Optional[str] = None,
    end: Optional[str] = None,
    db = Depends(get_db),
):
    start, end = date_range_defaults(start, end)
    agent = db.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    calls_row = db.execute(
        """
        SELECT COUNT(*) AS total_calls,
               SUM(CASE WHEN overall_quality = 'good' THEN 1 ELSE 0 END) AS good_calls,
               SUM(CASE WHEN overall_quality = 'poor' THEN 1 ELSE 0 END) AS poor_calls,
               SUM(CASE WHEN overall_quality = 'average' THEN 1 ELSE 0 END) AS average_call_score
        FROM calls
        WHERE agent_id = ? AND status = 'audited'
          AND audited_at IS NOT NULL AND date(audited_at) BETWEEN date(?) AND date(?)
        """,
        (agent_id, start, end),
    ).fetchone()

    param_rows = db.execute(
        """
        SELECT p.label AS parameter, ROUND(AVG(s.score),1) AS avg_score
        FROM call_scores s
        JOIN parameters p ON p.id = s.parameter_id
        JOIN calls c ON c.id = s.call_id
        WHERE c.agent_id = ? AND c.audited_at IS NOT NULL AND date(c.audited_at) BETWEEN date(?) AND date(?)
        GROUP BY p.id, p.label
        ORDER BY p.id
        """,
        (agent_id, start, end),
    ).fetchall()

    params = [dict(r) for r in param_rows]
    if params:
        sorted_params = sorted(params, key=lambda x: x["avg_score"] or 0, reverse=True)
        strengths = [p["parameter"] for p in sorted_params[:3]]
        improvements = [p["parameter"] for p in sorted_params[-3:]]
    else:
        strengths, improvements = [], []

    return {
        "agent": {"id": agent["id"], "name": agent["name"], "team": agent["team"]},
        "start": start,
        "end": end,
        "total_calls": calls_row["total_calls"] or 0,
        "average_call_score": calls_row["average_call_score"] or 0,
        "good_calls": calls_row["good_calls"] or 0,
        "poor_calls": calls_row["poor_calls"] or 0,
        "parameters": params,
        "overall_summary": {
            "strengths": strengths,
            "improvements": improvements,
        },
    }


# ==================================================================
# Feature 2: clickable stat cards -> agents -> that agent's calls.
# category is one of the CATEGORY_FILTERS keys (total/audited/pending/positive/negative),
# matching whichever stat card was clicked on the dashboard.
# ==================================================================

@app.get("/api/dashboard/category/{category}/agents")
def category_agents(
    category: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    db = Depends(get_db),
):
    """2.1 - agents ranked by their average score, scoped to the clicked category."""
    where_clause = category_filter_sql(category)
    start, end = date_range_defaults(start, end)
    rows = db.execute(
        f"""
        SELECT a.id AS agent_id, a.name AS agent_name, a.team,
               ROUND(AVG(c.overall_score), 1) AS avg_score,
               COUNT(*) AS call_count
        FROM calls c
        JOIN agents a ON a.id = c.agent_id
        WHERE {where_clause}
          AND c.audited_at IS NOT NULL AND date(c.audited_at) BETWEEN date(?) AND date(?)
        GROUP BY a.id, a.name, a.team
        ORDER BY avg_score DESC
        """,
        (start, end),
    ).fetchall()
    return {
        "category": category,
        "start": start,
        "end": end,
        "agents": [dict(r) for r in rows],
    }


@app.get("/api/dashboard/category/{category}/agents/{agent_id}/calls")
def category_agent_calls(
    category: str,
    agent_id: int,
    start: Optional[str] = None,
    end: Optional[str] = None,
    db = Depends(get_db),
):
    """2.2 - that agent's individual calls (with score) within the clicked category."""
    where_clause = category_filter_sql(category)
    start, end = date_range_defaults(start, end)
    agent = db.execute("SELECT id, name, team FROM agents WHERE id = ?", (agent_id,)).fetchone()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    calls = db.execute(
        f"""
        SELECT c.id, c.call_number, c.call_start_time, c.audited_at, c.status,
               c.sentiment, c.overall_quality, c.overall_score, c.verdict, c.recording_base
        FROM calls c
        WHERE c.agent_id = ? AND {where_clause}
          AND c.audited_at IS NOT NULL AND date(c.audited_at) BETWEEN date(?) AND date(?)
        ORDER BY c.audited_at DESC, c.id DESC
        """,
        (agent_id, start, end),
    ).fetchall()
    return {
        "category": category,
        "agent": dict(agent),
        "start": start,
        "end": end,
        "calls": [dict(r) for r in calls],
    }


# ==================================================================
# Feature 3: top-10 agents leaderboard (right-hand sidebar box)
# ==================================================================

@app.get("/api/dashboard/top-agents")
def top_agents(
    start: Optional[str] = None,
    end: Optional[str] = None,
    limit: int = 10,
    search: Optional[str] = None,
    db = Depends(get_db),
):
    start, end = date_range_defaults(start, end)
    # a search should be able to surface more than 10 matches; the sidebar's
    # default (unsearched) call always passes limit=10 explicitly, this cap
    # just stops a search from being unbounded.
    limit = max(1, min(limit, 50))

    where = [
        "c.status = 'audited'",
        "c.audited_at IS NOT NULL",
        "date(c.audited_at) BETWEEN date(?) AND date(?)",
    ]
    params = [start, end]
    if search:
        where.append("a.name LIKE ?")
        params.append(f"%{search}%")
    where_sql = " AND ".join(where)

    rows = db.execute(
        f"""
        SELECT a.id AS agent_id, a.name AS agent_name, a.team,
               ROUND(AVG(c.overall_score), 1) AS avg_score,
               COUNT(*) AS call_count
        FROM calls c
        JOIN agents a ON a.id = c.agent_id
        WHERE {where_sql}
        GROUP BY a.id, a.name, a.team
        ORDER BY avg_score DESC
        LIMIT ?
        """,
        tuple(params) + (limit,),
    ).fetchall()
    return {
        "start": start,
        "end": end,
        "search": search,
        "agents": [dict(r) for r in rows],
    }



REPORT_SORTS = {
    "call_date_desc": "c.call_date DESC, c.id DESC",
    "call_date_asc": "c.call_date ASC, c.id ASC",
    "score_desc": "c.overall_score DESC",
    "score_asc": "c.overall_score ASC",
    "agent_name": "a.name ASC, c.call_date DESC",
}


def _report_pivot_cols():
    # One column per parameter via conditional aggregation (MySQL has no PIVOT).
    # param_key values come from our own hardcoded PARAMETERS config, not user
    # input, so interpolating them straight into the SQL text is safe here.
    return ",\n               ".join(
        f"MAX(CASE WHEN p.param_key = '{key}' THEN s.score END) AS `{key}`"
        for key, _ in PARAMETERS
    )


def _report_filters(agent_id, disposition_status, actual_disposition, predicted_disposition, start, end):
    """Builds the WHERE clause + params shared by the report's JSON endpoint
    AND its CSV export below - one place to add a filter so the download can
    never drift out of sync with what's on screen.

    Date filter uses audited_at (WHEN the QA analysis completed), NOT
    call_date (WHEN the call itself happened) - this is the Report page
    specifically asking for that; every other endpoint in the app keeps
    filtering on call_date exactly as before."""
    where = [
        "c.status = 'audited'",
        "c.audited_at IS NOT NULL",
        "date(c.audited_at) BETWEEN date(?) AND date(?)",
    ]
    params = [start, end]
    if agent_id:
        where.append("c.agent_id = ?")
        params.append(agent_id)
    if disposition_status == "correct":
        where.append("c.disposition_match = 1")
    elif disposition_status == "incorrect":
        where.append("c.disposition_match = 0")
    if actual_disposition:
        where.append("UPPER(c.call_end_type_name) = UPPER(?)")
        params.append(actual_disposition)
    if predicted_disposition:
        where.append("UPPER(c.predicted_disposition) = UPPER(?)")
        params.append(predicted_disposition)
    return " AND ".join(where), params


@app.get("/api/reports/agent-scores")
def agent_scores_report(
    agent_id: Optional[int] = None,
    disposition_status: Optional[str] = None,  # "correct" | "incorrect"
    actual_disposition: Optional[str] = None,
    predicted_disposition: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    page: int = 1,
    page_size: int = 25,
    sort: str = "call_date_desc",
    db = Depends(get_db),
):
    start, end = date_range_defaults(start, end)
    page = max(1, page)
    page_size = max(1, min(page_size, 200))  # cap it - this is a report table, not a data dump endpoint
    offset = (page - 1) * page_size
    order_sql = REPORT_SORTS.get(sort, REPORT_SORTS["call_date_desc"])

    where_sql, params = _report_filters(
        agent_id, disposition_status, actual_disposition, predicted_disposition, start, end
    )

    total_row = db.execute(
        f"SELECT COUNT(*) AS n FROM calls c JOIN agents a ON a.id = c.agent_id WHERE {where_sql}",
        tuple(params),
    ).fetchone()
    total = total_row["n"] or 0

    rows = db.execute(
        f"""
        SELECT c.id AS call_id, a.id AS agent_id, a.name AS agent_name,
               c.recording_base, c.call_date, c.overall_score, c.verdict, c.overall_quality,
               c.call_end_type_name AS actual_disposition, c.predicted_disposition, c.disposition_match,c.audited_at,
               {_report_pivot_cols()}
        FROM calls c
        JOIN agents a ON a.id = c.agent_id
        LEFT JOIN call_scores s ON s.call_id = c.id
        LEFT JOIN parameters p ON p.id = s.parameter_id
        WHERE {where_sql}
        GROUP BY c.id, a.id, a.name, c.recording_base, c.call_date,
                 c.overall_score, c.verdict, c.overall_quality,
                 c.call_end_type_name, c.predicted_disposition, c.disposition_match
        ORDER BY {order_sql}
        LIMIT ? OFFSET ?
        """,
        tuple(params) + (page_size, offset),
    ).fetchall()

    return {
        "start": start,
        "end": end,
        "agent_id": agent_id,
        "page": page,
        "page_size": page_size,
        "total": total,
        "columns": [{"key": key, "label": label} for key, label in PARAMETERS],
        "rows": [dict(r) for r in rows],
    }


# Dropdown option sources for the report's disposition filters - distinct
# values actually present in the data (within the date range), not a
# hardcoded list, so anything the manifest or SLM ever produces shows up.
@app.get("/api/reports/disposition-options")
def report_disposition_options(
    start: Optional[str] = None,
    end: Optional[str] = None,
    db = Depends(get_db),
):
    start, end = date_range_defaults(start, end)
    date_filter = "c.call_date IS NOT NULL AND date(c.call_date) BETWEEN date(?) AND date(?)"

    actual_rows = db.execute(
        f"""SELECT DISTINCT call_end_type_name AS v FROM calls c
            WHERE {date_filter} AND call_end_type_name IS NOT NULL AND call_end_type_name != ''
            ORDER BY v""",
        (start, end),
    ).fetchall()
    predicted_rows = db.execute(
        f"""SELECT DISTINCT predicted_disposition AS v FROM calls c
            WHERE {date_filter} AND predicted_disposition IS NOT NULL AND predicted_disposition != ''
            ORDER BY v""",
        (start, end),
    ).fetchall()

    return {
        "actual": [r["v"] for r in actual_rows],
        "predicted": [r["v"] for r in predicted_rows],
    }


# CSV download - same filters/sort as the JSON report above (via the shared
# _report_filters/_report_pivot_cols helpers), but no pagination: exports
# every row matching the current filters, exactly what's "in" the filtered
# report, not just whichever page happens to be on screen.
@app.get("/api/reports/agent-scores/export")
def agent_scores_report_export(
    agent_id: Optional[int] = None,
    disposition_status: Optional[str] = None,
    actual_disposition: Optional[str] = None,
    predicted_disposition: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    sort: str = "call_date_desc",
    db = Depends(get_db),
):
    start, end = date_range_defaults(start, end)
    order_sql = REPORT_SORTS.get(sort, REPORT_SORTS["call_date_desc"])
    where_sql, params = _report_filters(
        agent_id, disposition_status, actual_disposition, predicted_disposition, start, end
    )

    rows = db.execute(
        f"""
        SELECT a.name AS agent_name, c.recording_base, c.call_date, c.overall_score,
               c.verdict, c.overall_quality,
               c.call_end_type_name AS actual_disposition, c.predicted_disposition, c.disposition_match,
               {_report_pivot_cols()}
        FROM calls c
        JOIN agents a ON a.id = c.agent_id
        LEFT JOIN call_scores s ON s.call_id = c.id
        LEFT JOIN parameters p ON p.id = s.parameter_id
        WHERE {where_sql}
        GROUP BY c.id, a.id, a.name, c.recording_base, c.call_date,
                 c.overall_score, c.verdict, c.overall_quality,
                 c.call_end_type_name, c.predicted_disposition, c.disposition_match
        ORDER BY {order_sql}
        """,
        tuple(params),
    ).fetchall()

    def disposition_status_label(match):
        if match == 1:
            return "Correct"
        if match == 0:
            return "Incorrect"
        return ""

    buf = io.StringIO()
    writer = csv.writer(buf)
    header = ["Agent", "Recording", "Call Date", "Overall Score", "Verdict", "Quality",
              "Actual Disposition", "Predicted Disposition", "Disposition Status"]
    header += [label for _, label in PARAMETERS]
    writer.writerow(header)
    for r in rows:
        row = [
            r["agent_name"], r["recording_base"], r["call_date"], r["overall_score"],
            r["verdict"], r["overall_quality"],
            r["actual_disposition"], r["predicted_disposition"], disposition_status_label(r["disposition_match"]),
        ]
        row += [r[key] for key, _ in PARAMETERS]
        writer.writerow(row)

    filename = f"agent-report_{start}_to_{end}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ==================================================================
# Manifest upload + STT/SLM pipeline
# ==================================================================

@app.post("/api/manifest/upload")
async def upload_manifest(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    recording_base_url: Optional[str] = None,
    auto_process: bool = True,
):
    if not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(status_code=400, detail="Please upload an .xlsx manifest file")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name

    try:
        manifest_id, row_count = save_manifest(
            tmp_path, file.filename, recording_base_url or RECORDINGS_BASE_URL
        )
        print(f"Manifest uploaded: {manifest_id}, Rows: {row_count}")
    finally:
        os.unlink(tmp_path)

    if auto_process:
        print("Triggering background processing for manifest:", manifest_id)
        background_tasks.add_task(process_manifest, manifest_id)

    return {"manifest_id": manifest_id, "rows_loaded": row_count, "processing": auto_process}


@app.post("/api/manifest/{manifest_id}/process")
def trigger_processing(manifest_id: int, background_tasks: BackgroundTasks, db = Depends(get_db)):
    row = db.execute("SELECT id FROM manifests WHERE id = ?", (manifest_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Manifest not found")
    background_tasks.add_task(process_manifest, manifest_id)
    return {"manifest_id": manifest_id, "processing": True}


@app.get("/api/manifest")
def list_manifests(db = Depends(get_db)):
    rows = db.execute("SELECT * FROM manifests ORDER BY id DESC").fetchall()
    return {"manifests": [dict(r) for r in rows]}


@app.get("/api/manifest/{manifest_id}")
def manifest_status(manifest_id: int, db = Depends(get_db)):
    manifest = db.execute("SELECT * FROM manifests WHERE id = ?", (manifest_id,)).fetchone()
    if not manifest:
        raise HTTPException(status_code=404, detail="Manifest not found")
    calls = db.execute(
        """SELECT c.id, c.call_number, c.recording_base, c.status, c.error_message,
                  c.overall_score, c.verdict, a.name AS agent_name
           FROM calls c JOIN agents a ON a.id = c.agent_id
           WHERE c.manifest_id = ? ORDER BY c.id""",
        (manifest_id,),
    ).fetchall()
    return {"manifest": dict(manifest), "calls": [dict(r) for r in calls]}


# @app.get("/api/calls/{call_id}/transcript")
# def call_transcript(call_id: int, db = Depends(get_db)):
#     row = db.execute("SELECT * FROM transcripts WHERE call_id = ?", (call_id,)).fetchone()
#     if not row:
#         raise HTTPException(status_code=404, detail="No transcript for this call yet")
#     return dict(row)


# @app.get("/api/calls/{call_id}")
# def call_detail(call_id: int, db = Depends(get_db)):
#     # `c.*` already includes rx_path (IN leg) and tx_path (OUT leg) - the same
#     # recording URLs the STT service was given. The frontend call-detail page
#     # (feature 2.3) plays these directly and seeks to each parameter's
#     # evidence start_time so a reviewer can jump straight to the moment a
#     # score was decided, instead of listening to the whole call.
#     call = db.execute(
#         """SELECT c.*, a.name AS agent_name FROM calls c JOIN agents a ON a.id = c.agent_id
#            WHERE c.id = ?""",
#         (call_id,),
#     ).fetchone()
#     if not call:
#         raise HTTPException(status_code=404, detail="Call not found")
#     scores = db.execute(
#         """SELECT p.label AS parameter, s.status, s.reason, s.evidence, s.start_time, s.end_time,
#                   s.raw_score, s.score
#            FROM call_scores s JOIN parameters p ON p.id = s.parameter_id
#            WHERE s.call_id = ? ORDER BY p.id""",
#         (call_id,),
#     ).fetchall()
#     fatal = db.execute("SELECT * FROM fatal_checks WHERE call_id = ?", (call_id,)).fetchall()
#     return {
#         "call": dict(call),
#         "parameters": [dict(r) for r in scores],
#         "fatal_checks": [dict(r) for r in fatal],
#     }

@app.get("/api/calls/{call_id}/transcript")
def call_transcript(call_id: int, db = Depends(get_db)):
    row = db.execute("SELECT * FROM transcripts WHERE call_id = ?", (call_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="No transcript for this call yet")
    return dict(row)


@app.get("/api/calls/{call_id}")
def call_detail(call_id: int, db = Depends(get_db)):
    print(f"Fetching detail for call ID: {call_id}")
    # `c.*` already includes rx_path (IN leg) and tx_path (OUT leg) - the same
    # recording URLs the STT service was given. The frontend call-detail page
    # (feature 2.3) plays these directly and seeks to each parameter's
    # evidence start_time so a reviewer can jump straight to the moment a
    # score was decided, instead of listening to the whole call.
    call = db.execute(
        """SELECT c.*, a.name AS agent_name FROM calls c JOIN agents a ON a.id = c.agent_id
           WHERE c.id = ?""",
        (call_id,),
    ).fetchone()
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    scores = db.execute(
        """SELECT p.label AS parameter, s.status, s.reason, s.evidence, s.start_time, s.end_time,
                  s.raw_score, s.score
           FROM call_scores s JOIN parameters p ON p.id = s.parameter_id
           WHERE s.call_id = ? ORDER BY p.id""",
        (call_id,),
    ).fetchall()
    fatal = db.execute("SELECT * FROM fatal_checks WHERE call_id = ?", (call_id,)).fetchall()
    # print(dict(call), [dict(r) for r in scores], [dict(r) for r in fatal])
    call_dict = dict(call)
    print(call_dict)
    if call_dict.get("recording_base"):
        call_dict["mono_url"] = f"{RECORDINGS_PLAYBACK_BASE_URL.rstrip('/')}/{call_dict['recording_base']}"
    else:
        call_dict["mono_url"] = None
    print(f" after adding the mono url\n{call_dict}")
    return {
        "call": dict(call_dict),
        "parameters": [dict(r) for r in scores],
        "fatal_checks": [dict(r) for r in fatal],
    }

@app.get("/api/health")
def health(db = Depends(get_db)):
    """Hits the DB too, so this actually tells you whether MySQL is reachable,
    not just whether the FastAPI process is up."""
    db.execute("SELECT 1")
    return {"status": "ok"}

# /////////////////w8/////////////////////////
# import csv
# import io
# import os
# import shutil
# import tempfile
# from datetime import datetime, timedelta
# from typing import Optional

# import pymysql
# from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Request, UploadFile
# from fastapi.middleware.cors import CORSMiddleware
# from fastapi.responses import JSONResponse, StreamingResponse
# from pydantic import BaseModel

# import hashlib

# # from app4_.backend.config import RECOR, RECORDINGS_PLAYBACK_BASE_URLDINGS_PLAYBACK_BASE_URL
# from config import PARAMETERS, RECORDINGS_BASE_URL, RECORDINGS_PLAYBACK_BASE_URL
# from db import get_conn, init_db
# from pipeline import process_manifest, save_manifest

# app = FastAPI(title="Athena QA Dashboard")

# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=[
#         "http://localhost:5173",
#         "http://127.0.0.1:5173",
#     ],
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )

# # ------------------------------------------------------------------
# # IMPORTANT FIX: init_db() used to run unguarded at import time. If MySQL
# # was unreachable (wrong host/user/password, or the server just wasn't up
# # yet), that raised an exception WHILE the module was loading - before
# # FastAPI/CORSMiddleware even finished being built. uvicorn's worker
# # process died right there, so nothing was listening on the port at all.
# #
# # That's exactly what showed up in the browser as CORS errors + net::ERR_FAILED:
# # there was no server to send a response (let alone CORS headers), so Chrome
# # reports it as a blocked/failed request instead of a clean 500.
# #
# # Fix: catch the startup failure, log it clearly, and let the app come up
# # anyway. Every DB-touching request will then fail with a clean 503 JSON
# # response (see exception handler below) that DOES carry CORS headers,
# # instead of the whole server being unreachable.
# # ------------------------------------------------------------------
# try:
#     init_db()
# except Exception as e:
#     print("=" * 70)
#     print("[STARTUP WARNING] Could not connect to MySQL / initialize schema.")
#     print(f"[STARTUP WARNING] {type(e).__name__}: {e}")
#     print("[STARTUP WARNING] Check MYSQL_HOST / MYSQL_PORT / MYSQL_USER / "
#           "MYSQL_PASSWORD / MYSQL_DATABASE env vars and that MySQL is running.")
#     print("[STARTUP WARNING] The API is still starting so you get clean 503s "
#           "instead of connection failures - fix your DB config and restart.")
#     print("=" * 70)


# @app.exception_handler(pymysql.err.Error)
# async def mysql_error_handler(request: Request, exc: pymysql.err.Error):
#     return JSONResponse(
#         status_code=503,
#         content={"detail": f"Database unavailable: {exc}"},
#     )


# def get_db():
#     conn = get_conn()
#     try:
#         yield conn
#     finally:
#         conn.close()


# def hash_pw(pw: str) -> str:
#     return hashlib.sha256(pw.encode()).hexdigest()


# # ---------- Schemas ----------
# class LoginRequest(BaseModel):
#     username: str
#     password: str


# class LoginResponse(BaseModel):
#     token: str
#     username: str
#     role: str


# # ---------- Auth ----------
# @app.post("/api/login", response_model=LoginResponse)
# def login(req: LoginRequest, db = Depends(get_db)):
#     row = db.execute(
#         "SELECT * FROM users WHERE username = ? AND password_hash = ?",
#         (req.username, hash_pw(req.password)),
#     ).fetchone()
#     if not row:
#         raise HTTPException(status_code=401, detail="Invalid username or password")
#     token = hashlib.sha256(f"{req.username}{datetime.utcnow()}".encode()).hexdigest()
#     return LoginResponse(token=token, username=row["username"], role=row["role"])


# class ChangePasswordRequest(BaseModel):
#     username: str
#     current_password: str
#     new_password: str


# @app.post("/api/change-password")
# def change_password(req: ChangePasswordRequest, db = Depends(get_db)):
#     # There's no session/bearer-token check anywhere else in this app yet
#     # (login just hands back an unverified token) - so identity here is
#     # proven the same way login proves it: username + correct current
#     # password. If you add real session auth later, swap this for a
#     # Depends(current_user) check instead of trusting the request body.
#     if len(req.new_password) < 8:
#         raise HTTPException(status_code=400, detail="New password must be at least 8 characters")

#     row = db.execute(
#         "SELECT * FROM users WHERE username = ? AND password_hash = ?",
#         (req.username, hash_pw(req.current_password)),
#     ).fetchone()
#     if not row:
#         raise HTTPException(status_code=401, detail="Current password is incorrect")

#     db.execute(
#         "UPDATE users SET password_hash = ? WHERE username = ?",
#         (hash_pw(req.new_password), req.username),
#     )
#     db.commit()
#     return {"status": "ok"}


# # ---------- Helpers ----------
# def date_range_defaults(start: Optional[str], end: Optional[str]):
#     if not end:
#         end = datetime.utcnow().strftime("%Y-%m-%d")
#     if not start:
#         start = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
#     return start, end


# # Whitelist of SQL fragments for the 5 clickable dashboard stat cards (feature 2).
# # Kept as a fixed dict (not built from the path param) so there's no injection risk
# # even though it gets interpolated straight into the query string below.
# # CATEGORY_FILTERS = {
# #     "total": "1=1",
# #     "audited": "c.status = 'audited'",
# #     "pending": "c.status IN ('pending','transcribing','analyzing')",
# #     "positive": "c.sentiment = 'positive'",
# #     "negative": "c.sentiment = 'negative'",
# #     # Agent-detail summary cards (feature: clickable Total/Average/Good/Poor).
# #     # "Total Calls" and "Average Call" both draw from the same audited
# #     # population that agent_detail()'s stats are computed over, so they
# #     # share the "audited" filter - only the label shown differs.
# #     "good": "c.overall_quality = 'good'",
# #     "poor": "c.overall_quality = 'poor'",
# #     # Disposition-based (feature: below the funnel). Case-insensitive since
# #     # the manifest's call_end_type_name casing isn't guaranteed consistent
# #     # (we've seen "CALLBACK" in real data).
# #     "disp_not_eligible": "UPPER(c.call_end_type_name) = 'NOT ELIGIBLE'",
# #     "disp_not_interested": "UPPER(c.call_end_type_name) = 'NOT INTERESTED'",
# #     "disp_callback": "UPPER(c.call_end_type_name) = 'CALLBACK'",
# #     "disp_short_hangup": "UPPER(c.call_end_type_name) = 'SHORT HANGUP'",
# #     "disp_lead": "UPPER(c.call_end_type_name) = 'LEAD'",
# #     "disp_eligible": "UPPER(c.call_end_type_name) = 'ELIGIBLE'",
# #     "disp_hangup": "UPPER(c.call_end_type_name) = 'HANGUP'",
# #     # Correct/Wrong: actual (manifest) vs predicted (SLM) disposition, compared
# #     # once and cached on the row at process time (pipeline.py) rather than
# #     # here - disposition_match is NULL (excluded from both) for any call
# #     # missing either side of the comparison, so this filter alone is enough,
# #     # no extra "and both sides present" clause needed.
# #     "disp_correct": "c.disposition_match = 1",
# #     "disp_wrong": "c.disposition_match = 0",
# # }

# # Labels + ordering for the disposition buttons row on the dashboard.
# # DISPOSITION_CATEGORIES = [
# #     ("disp_not_eligible", "Not Eligible", "NOT ELIGIBLE"),
# #     ("disp_not_interested", "Not Interested", "NOT INTERESTED"),
# #     ("disp_callback", "Callback", "CALLBACK"),
# #     ("disp_short_hangup", "Short Hangup", "SHORT HANGUP"),
# #     ("disp_lead", "Lead", "LEAD"),
# #     ("disp_eligible", "Eligible", "ELIGIBLE"),
# #     ("disp_hangup", "Hangup", "HANGUP"),
# # ]
# CATEGORY_FILTERS = {
#     "total": "1=1",
#     "audited": "c.status = 'audited'",
#     "pending": "c.status IN ('pending','transcribing','analyzing')",
#     "positive": "c.sentiment = 'positive'",
#     "negative": "c.sentiment = 'negative'",
#     # Agent-detail summary cards (feature: clickable Total/Average/Good/Poor).
#     # "Total Calls" and "Average Call" both draw from the same audited
#     # population that agent_detail()'s stats are computed over, so they
#     # share the "audited" filter - only the label shown differs.
#     "good": "c.overall_quality = 'good'",
#     "poor": "c.overall_quality = 'poor'",
#     # Disposition-based (feature: below the funnel). Case-insensitive since
#     # the manifest's call_end_type_name casing isn't guaranteed consistent
#     # (we've seen "CALLBACK" in real data).
#     "disp_not_eligible": "UPPER(c.call_end_type_name) = 'NOT ELIGIBLE'",
#     "disp_not_interested": "UPPER(c.call_end_type_name) = 'NOT INTERESTED'",
#     "disp_callback": "UPPER(c.call_end_type_name) = 'CALLBACK'",
#     "disp_short_hangup": "UPPER(c.call_end_type_name) = 'SHORT HANGUP'",
#     "disp_lead": "UPPER(c.call_end_type_name) = 'LEAD'",
#     "disp_eligible": "UPPER(c.call_end_type_name) = 'ELIGIBLE'",
#     "disp_interested": "UPPER(c.call_end_type_name) = 'INTERESTED'",
#     # Correct/Wrong: actual (manifest) vs predicted (SLM) disposition, compared
#     # once and cached on the row at process time (pipeline.py) rather than
#     # here - disposition_match is NULL (excluded from both) for any call
#     # missing either side of the comparison, so this filter alone is enough,
#     # no extra "and both sides present" clause needed.
#     "disp_correct": "c.disposition_match = 1",
#     "disp_wrong": "c.disposition_match = 0",
# }

# # Labels + ordering for the disposition buttons row on the dashboard.
# # in a sequesnce the type will be selected disposition_value
# DISPOSITION_CATEGORIES = [
#     ("disp_not_eligible", "Not Eligible", "NOT ELIGIBLE"),
#     ("disp_not_interested", "Not Interested", "NOT INTERESTED"),
#     ("disp_callback", "Callback", "CALLBACK"),
#     # ("disp_short_hangup", "Short Hangup", "SHORT HANGUP"),
#     ("disp_short_hangup", "Call disconnected", "SHORT HANGUP"),
#     ("disp_lead", "Lead", "LEAD"),
#     # ("disp_eligible", "Eligible", "ELIGIBLE"),
#     ("disp_interested", "Interested", "INTERESTED"),
# ]



# def category_filter_sql(category: str) -> str:
#     if category not in CATEGORY_FILTERS:
#         raise HTTPException(
#             status_code=400,
#             detail=f"Unknown category '{category}'. Must be one of: {', '.join(CATEGORY_FILTERS)}",
#         )
#     return CATEGORY_FILTERS[category]


# # ---------- Dashboard summary (Section 2 top cards) ----------
# @app.get("/api/dashboard/summary")
# def dashboard_summary(
#     start: Optional[str] = None,
#     end: Optional[str] = None,
#     db = Depends(get_db),
# ):
#     start, end = date_range_defaults(start, end)
#     row = db.execute(
#         """
#         SELECT
#           COUNT(*) AS total_calls,
#           SUM(CASE WHEN status = 'audited' THEN 1 ELSE 0 END) AS audited_calls,
#           SUM(CASE WHEN status IN ('pending','transcribing','analyzing') THEN 1 ELSE 0 END) AS pending_calls,
#           SUM(CASE WHEN sentiment = 'positive' THEN 1 ELSE 0 END) AS positive_calls,
#           SUM(CASE WHEN sentiment = 'negative' THEN 1 ELSE 0 END) AS negative_calls
#         FROM calls
#         WHERE call_date IS NOT NULL AND date(call_date) BETWEEN date(?) AND date(?)
#         """,
#         (start, end),
#     ).fetchone()
#     return {
#         "start": start,
#         "end": end,
#         "total_calls": row["total_calls"] or 0,
#         "audited_calls": row["audited_calls"] or 0,
#         "pending_calls": row["pending_calls"] or 0,
#         "positive_calls": row["positive_calls"] or 0,
#         "negative_calls": row["negative_calls"] or 0,
#     }


# # ---------- Disposition counts (feature: buttons below the funnel) ----------
# # One grouped query for the manifest-based dispositions (not one query per
# # button), plus one small aggregate for Correct/Wrong (disposition_match),
# # then mapped onto the known categories. Drill-down reuses the exact same
# # /api/dashboard/category/{category}/... endpoints as the 5 stat cards do -
# # "disp_not_eligible" etc, and now "disp_correct"/"disp_wrong" too, are just
# # more entries in CATEGORY_FILTERS above.
# @app.get("/api/dashboard/dispositions")
# def dashboard_dispositions(
#     start: Optional[str] = None,
#     end: Optional[str] = None,
#     db = Depends(get_db),
# ):
#     start, end = date_range_defaults(start, end)
#     rows = db.execute(
#         """
#         SELECT UPPER(call_end_type_name) AS disp, COUNT(*) AS n
#         FROM calls
#         WHERE call_date IS NOT NULL AND date(call_date) BETWEEN date(?) AND date(?)
#           AND call_end_type_name IS NOT NULL AND call_end_type_name != ''
#         GROUP BY UPPER(call_end_type_name)
#         """,
#         (start, end),
#     ).fetchall()
#     counts = {r["disp"]: r["n"] for r in rows}

#     match_row = db.execute(
#         """
#         SELECT
#           SUM(CASE WHEN disposition_match = 1 THEN 1 ELSE 0 END) AS correct_n,
#           SUM(CASE WHEN disposition_match = 0 THEN 1 ELSE 0 END) AS wrong_n
#         FROM calls
#         WHERE call_date IS NOT NULL AND date(call_date) BETWEEN date(?) AND date(?)
#         """,
#         (start, end),
#     ).fetchone()

#     dispositions = [
#         {"category": cat, "label": label, "count": counts.get(match_value, 0)}
#         for cat, label, match_value in DISPOSITION_CATEGORIES
#     ]
#     # dispositions.append({"category": "disp_correct", "label": "Correct", "count": match_row["correct_n"] or 0})
#     dispositions.append({"category": "disp_wrong", "label": "Wrong", "count": match_row["wrong_n"] or 0})

#     return {
#         "start": start,
#         "end": end,
#         "dispositions": dispositions,
#     }


# # ---------- Parameter-wise average scores (graph) ----------
# @app.get("/api/dashboard/parameters")
# def dashboard_parameters(
#     start: Optional[str] = None,
#     end: Optional[str] = None,
#     db = Depends(get_db),
# ):
#     start, end = date_range_defaults(start, end)
#     rows = db.execute(
#         """
#         SELECT p.label AS parameter, ROUND(AVG(s.score), 1) AS avg_score, COUNT(*) AS n
#         FROM call_scores s
#         JOIN parameters p ON p.id = s.parameter_id
#         JOIN calls c ON c.id = s.call_id
#         WHERE c.call_date IS NOT NULL AND date(c.call_date) BETWEEN date(?) AND date(?)
#         GROUP BY p.id, p.label
#         ORDER BY p.id
#         """,
#         (start, end),
#     ).fetchall()
#     return {
#         "start": start,
#         "end": end,
#         "parameters": [
#             {"parameter": r["parameter"], "avg_score": r["avg_score"] or 0, "sample_size": r["n"]}
#             for r in rows
#         ],
#     }


# # ---------- Parameter success-rate funnel (feature 1) ----------
# # Different from /api/dashboard/parameters above (which averages the 0-100
# # normalized score). This instead answers "out of every AUDITED call, what
# # % actually PASSED (status=true) this parameter?" - sorted highest-first so
# # it reads top-to-bottom like a funnel: parameters almost everyone nails at
# # the top, parameters agents struggle with at the bottom.
# #
# # success_pct = (# audited calls where that parameter's status = TRUE)
# #               / (total audited calls in range) * 100
# #
# # Two queries total (not N+1): one scalar for the constant denominator
# # (total audited calls), one GROUP BY for all 16 numerators together.
# @app.get("/api/dashboard/parameter-funnel")
# def parameter_funnel(
#     start: Optional[str] = None,
#     end: Optional[str] = None,
#     db = Depends(get_db),
# ):
#     start, end = date_range_defaults(start, end)

#     total_row = db.execute(
#         """
#         SELECT COUNT(*) AS total_audited
#         FROM calls
#         WHERE status = 'audited' AND call_date IS NOT NULL
#           AND date(call_date) BETWEEN date(?) AND date(?)
#         """,
#         (start, end),
#     ).fetchone()
#     total_audited = total_row["total_audited"] or 0

#     rows = db.execute(
#         """
#         SELECT p.id AS parameter_id, p.label AS parameter,
#                SUM(CASE WHEN s.status = 1 THEN 1 ELSE 0 END) AS success_count,
#                COUNT(*) AS sample_size
#         FROM call_scores s
#         JOIN parameters p ON p.id = s.parameter_id
#         JOIN calls c ON c.id = s.call_id
#         WHERE c.status = 'audited' AND c.call_date IS NOT NULL
#           AND date(c.call_date) BETWEEN date(?) AND date(?)
#         GROUP BY p.id, p.label
#         ORDER BY p.id
#         """,
#         (start, end),
#     ).fetchall()

#     funnel = []
#     for r in rows:
#         success_count = r["success_count"] or 0
#         pct = round((success_count / total_audited) * 100, 1) if total_audited else 0.0
#         funnel.append({
#             "parameter_id": r["parameter_id"],
#             "parameter": r["parameter"],
#             "success_count": success_count,
#             "sample_size": r["sample_size"],
#             "success_pct": pct,
#         })

#     funnel.sort(key=lambda x: x["success_pct"], reverse=True)

#     return {
#         "start": start,
#         "end": end,
#         "total_audited_calls": total_audited,
#         "parameters": funnel,
#     }



# # ---------- Agents belonging to a parameter (click on bar) ----------
# @app.get("/api/dashboard/parameters/{parameter_name}/agents")
# def agents_for_parameter(
#     parameter_name: str,
#     start: Optional[str] = None,
#     end: Optional[str] = None,
#     db = Depends(get_db),
# ):
#     start, end = date_range_defaults(start, end)
#     rows = db.execute(
#         """
#         SELECT a.id AS agent_id,
#                a.name AS agent_name,
#                a.team,
#                ROUND(AVG(s.score), 1) AS avg_score,
#                COUNT(*) AS calls_scored
#         FROM call_scores s
#         JOIN parameters p ON p.id = s.parameter_id
#         JOIN calls c ON c.id = s.call_id
#         JOIN agents a ON a.id = c.agent_id
#         WHERE p.label = ?
#           AND c.call_date IS NOT NULL
#           AND date(c.call_date) BETWEEN date(?) AND date(?)
#         GROUP BY a.id, a.name, a.team
#         ORDER BY avg_score ASC
#         """,
#         (parameter_name, start, end),
#     ).fetchall()
#     if not rows:
#         exists = db.execute("SELECT 1 FROM parameters WHERE label = ?", (parameter_name,)).fetchone()
#         if not exists:
#             raise HTTPException(status_code=404, detail="Parameter not found")
#     return {
#         "parameter": parameter_name,
#         "start": start,
#         "end": end,
#         "agents": [dict(r) for r in rows],
#     }


# # ---------- Agent list (basic) ----------
# @app.get("/api/agents")
# def list_agents(db = Depends(get_db)):
#     rows = db.execute("SELECT id, name, team FROM agents ORDER BY name").fetchall()
#     return {"agents": [dict(r) for r in rows]}


# # ---------- Agent detail (Section 3) ----------
# @app.get("/api/agents/{agent_id}")
# def agent_detail(
#     agent_id: int,
#     start: Optional[str] = None,
#     end: Optional[str] = None,
#     db = Depends(get_db),
# ):
#     start, end = date_range_defaults(start, end)
#     agent = db.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
#     if not agent:
#         raise HTTPException(status_code=404, detail="Agent not found")

#     calls_row = db.execute(
#         """
#         SELECT COUNT(*) AS total_calls,
#                SUM(CASE WHEN overall_quality = 'good' THEN 1 ELSE 0 END) AS good_calls,
#                SUM(CASE WHEN overall_quality = 'poor' THEN 1 ELSE 0 END) AS poor_calls,
#                ROUND(AVG(overall_score), 1) AS average_call_score
#         FROM calls
#         WHERE agent_id = ? AND status = 'audited'
#           AND call_date IS NOT NULL AND date(call_date) BETWEEN date(?) AND date(?)
#         """,
#         (agent_id, start, end),
#     ).fetchone()

#     param_rows = db.execute(
#         """
#         SELECT p.label AS parameter, ROUND(AVG(s.score),1) AS avg_score
#         FROM call_scores s
#         JOIN parameters p ON p.id = s.parameter_id
#         JOIN calls c ON c.id = s.call_id
#         WHERE c.agent_id = ? AND c.call_date IS NOT NULL AND date(c.call_date) BETWEEN date(?) AND date(?)
#         GROUP BY p.id, p.label
#         ORDER BY p.id
#         """,
#         (agent_id, start, end),
#     ).fetchall()

#     params = [dict(r) for r in param_rows]
#     if params:
#         sorted_params = sorted(params, key=lambda x: x["avg_score"] or 0, reverse=True)
#         strengths = [p["parameter"] for p in sorted_params[:3]]
#         improvements = [p["parameter"] for p in sorted_params[-3:]]
#     else:
#         strengths, improvements = [], []

#     return {
#         "agent": {"id": agent["id"], "name": agent["name"], "team": agent["team"]},
#         "start": start,
#         "end": end,
#         "total_calls": calls_row["total_calls"] or 0,
#         "average_call_score": calls_row["average_call_score"] or 0,
#         "good_calls": calls_row["good_calls"] or 0,
#         "poor_calls": calls_row["poor_calls"] or 0,
#         "parameters": params,
#         "overall_summary": {
#             "strengths": strengths,
#             "improvements": improvements,
#         },
#     }


# # ==================================================================
# # Feature 2: clickable stat cards -> agents -> that agent's calls.
# # category is one of the CATEGORY_FILTERS keys (total/audited/pending/positive/negative),
# # matching whichever stat card was clicked on the dashboard.
# # ==================================================================

# @app.get("/api/dashboard/category/{category}/agents")
# def category_agents(
#     category: str,
#     start: Optional[str] = None,
#     end: Optional[str] = None,
#     db = Depends(get_db),
# ):
#     """2.1 - agents ranked by their average score, scoped to the clicked category."""
#     where_clause = category_filter_sql(category)
#     start, end = date_range_defaults(start, end)
#     rows = db.execute(
#         f"""
#         SELECT a.id AS agent_id, a.name AS agent_name, a.team,
#                ROUND(AVG(c.overall_score), 1) AS avg_score,
#                COUNT(*) AS call_count
#         FROM calls c
#         JOIN agents a ON a.id = c.agent_id
#         WHERE {where_clause}
#           AND c.call_date IS NOT NULL AND date(c.call_date) BETWEEN date(?) AND date(?)
#         GROUP BY a.id, a.name, a.team
#         ORDER BY avg_score DESC
#         """,
#         (start, end),
#     ).fetchall()
#     return {
#         "category": category,
#         "start": start,
#         "end": end,
#         "agents": [dict(r) for r in rows],
#     }


# @app.get("/api/dashboard/category/{category}/agents/{agent_id}/calls")
# def category_agent_calls(
#     category: str,
#     agent_id: int,
#     start: Optional[str] = None,
#     end: Optional[str] = None,
#     db = Depends(get_db),
# ):
#     """2.2 - that agent's individual calls (with score) within the clicked category."""
#     where_clause = category_filter_sql(category)
#     start, end = date_range_defaults(start, end)
#     agent = db.execute("SELECT id, name, team FROM agents WHERE id = ?", (agent_id,)).fetchone()
#     if not agent:
#         raise HTTPException(status_code=404, detail="Agent not found")

#     calls = db.execute(
#         f"""
#         SELECT c.id, c.call_number, c.call_start_time, c.call_date, c.status,
#                c.sentiment, c.overall_quality, c.overall_score, c.verdict, c.recording_base
#         FROM calls c
#         WHERE c.agent_id = ? AND {where_clause}
#           AND c.call_date IS NOT NULL AND date(c.call_date) BETWEEN date(?) AND date(?)
#         ORDER BY c.call_date DESC, c.id DESC
#         """,
#         (agent_id, start, end),
#     ).fetchall()
#     return {
#         "category": category,
#         "agent": dict(agent),
#         "start": start,
#         "end": end,
#         "calls": [dict(r) for r in calls],
#     }


# # ==================================================================
# # Feature 3: top-10 agents leaderboard (right-hand sidebar box)
# # ==================================================================

# @app.get("/api/dashboard/top-agents")
# def top_agents(
#     start: Optional[str] = None,
#     end: Optional[str] = None,
#     limit: int = 10,
#     search: Optional[str] = None,
#     db = Depends(get_db),
# ):
#     start, end = date_range_defaults(start, end)
#     # a search should be able to surface more than 10 matches; the sidebar's
#     # default (unsearched) call always passes limit=10 explicitly, this cap
#     # just stops a search from being unbounded.
#     limit = max(1, min(limit, 50))

#     where = [
#         "c.status = 'audited'",
#         "c.call_date IS NOT NULL",
#         "date(c.call_date) BETWEEN date(?) AND date(?)",
#     ]
#     params = [start, end]
#     if search:
#         where.append("a.name LIKE ?")
#         params.append(f"%{search}%")
#     where_sql = " AND ".join(where)

#     rows = db.execute(
#         f"""
#         SELECT a.id AS agent_id, a.name AS agent_name, a.team,
#                ROUND(AVG(c.overall_score), 1) AS avg_score,
#                COUNT(*) AS call_count
#         FROM calls c
#         JOIN agents a ON a.id = c.agent_id
#         WHERE {where_sql}
#         GROUP BY a.id, a.name, a.team
#         ORDER BY avg_score DESC
#         LIMIT ?
#         """,
#         tuple(params) + (limit,),
#     ).fetchall()
#     return {
#         "start": start,
#         "end": end,
#         "search": search,
#         "agents": [dict(r) for r in rows],
#     }



# REPORT_SORTS = {
#     "call_date_desc": "c.call_date DESC, c.id DESC",
#     "call_date_asc": "c.call_date ASC, c.id ASC",
#     "score_desc": "c.overall_score DESC",
#     "score_asc": "c.overall_score ASC",
#     "agent_name": "a.name ASC, c.call_date DESC",
# }


# def _report_pivot_cols():
#     # One column per parameter via conditional aggregation (MySQL has no PIVOT).
#     # param_key values come from our own hardcoded PARAMETERS config, not user
#     # input, so interpolating them straight into the SQL text is safe here.
#     return ",\n               ".join(
#         f"MAX(CASE WHEN p.param_key = '{key}' THEN s.score END) AS `{key}`"
#         for key, _ in PARAMETERS
#     )


# def _report_filters(agent_id, disposition_status, actual_disposition, predicted_disposition, start, end):
#     """Builds the WHERE clause + params shared by the report's JSON endpoint
#     AND its CSV export below - one place to add a filter so the download can
#     never drift out of sync with what's on screen."""
#     where = [
#         "c.status = 'audited'",
#         "c.call_date IS NOT NULL",
#         "date(c.call_date) BETWEEN date(?) AND date(?)",
#     ]
#     params = [start, end]
#     if agent_id:
#         where.append("c.agent_id = ?")
#         params.append(agent_id)
#     if disposition_status == "correct":
#         where.append("c.disposition_match = 1")
#     elif disposition_status == "incorrect":
#         where.append("c.disposition_match = 0")
#     if actual_disposition:
#         where.append("UPPER(c.call_end_type_name) = UPPER(?)")
#         params.append(actual_disposition)
#     if predicted_disposition:
#         where.append("UPPER(c.predicted_disposition) = UPPER(?)")
#         params.append(predicted_disposition)
#     return " AND ".join(where), params


# @app.get("/api/reports/agent-scores")
# def agent_scores_report(
#     agent_id: Optional[int] = None,
#     disposition_status: Optional[str] = None,  # "correct" | "incorrect"
#     actual_disposition: Optional[str] = None,
#     predicted_disposition: Optional[str] = None,
#     start: Optional[str] = None,
#     end: Optional[str] = None,
#     page: int = 1,
#     page_size: int = 25,
#     sort: str = "call_date_desc",
#     db = Depends(get_db),
# ):
#     start, end = date_range_defaults(start, end)
#     page = max(1, page)
#     page_size = max(1, min(page_size, 200))  # cap it - this is a report table, not a data dump endpoint
#     offset = (page - 1) * page_size
#     order_sql = REPORT_SORTS.get(sort, REPORT_SORTS["call_date_desc"])

#     where_sql, params = _report_filters(
#         agent_id, disposition_status, actual_disposition, predicted_disposition, start, end
#     )

#     total_row = db.execute(
#         f"SELECT COUNT(*) AS n FROM calls c JOIN agents a ON a.id = c.agent_id WHERE {where_sql}",
#         tuple(params),
#     ).fetchone()
#     total = total_row["n"] or 0

#     rows = db.execute(
#         f"""
#         SELECT c.id AS call_id, a.id AS agent_id, a.name AS agent_name,
#                c.recording_base, c.call_date, c.overall_score, c.verdict, c.overall_quality,
#                c.call_end_type_name AS actual_disposition, c.predicted_disposition, c.disposition_match,
#                {_report_pivot_cols()}
#         FROM calls c
#         JOIN agents a ON a.id = c.agent_id
#         LEFT JOIN call_scores s ON s.call_id = c.id
#         LEFT JOIN parameters p ON p.id = s.parameter_id
#         WHERE {where_sql}
#         GROUP BY c.id, a.id, a.name, c.recording_base, c.call_date,
#                  c.overall_score, c.verdict, c.overall_quality,
#                  c.call_end_type_name, c.predicted_disposition, c.disposition_match
#         ORDER BY {order_sql}
#         LIMIT ? OFFSET ?
#         """,
#         tuple(params) + (page_size, offset),
#     ).fetchall()

#     return {
#         "start": start,
#         "end": end,
#         "agent_id": agent_id,
#         "page": page,
#         "page_size": page_size,
#         "total": total,
#         "columns": [{"key": key, "label": label} for key, label in PARAMETERS],
#         "rows": [dict(r) for r in rows],
#     }


# # Dropdown option sources for the report's disposition filters - distinct
# # values actually present in the data (within the date range), not a
# # hardcoded list, so anything the manifest or SLM ever produces shows up.
# @app.get("/api/reports/disposition-options")
# def report_disposition_options(
#     start: Optional[str] = None,
#     end: Optional[str] = None,
#     db = Depends(get_db),
# ):
#     start, end = date_range_defaults(start, end)
#     date_filter = "c.call_date IS NOT NULL AND date(c.call_date) BETWEEN date(?) AND date(?)"

#     actual_rows = db.execute(
#         f"""SELECT DISTINCT call_end_type_name AS v FROM calls c
#             WHERE {date_filter} AND call_end_type_name IS NOT NULL AND call_end_type_name != ''
#             ORDER BY v""",
#         (start, end),
#     ).fetchall()
#     predicted_rows = db.execute(
#         f"""SELECT DISTINCT predicted_disposition AS v FROM calls c
#             WHERE {date_filter} AND predicted_disposition IS NOT NULL AND predicted_disposition != ''
#             ORDER BY v""",
#         (start, end),
#     ).fetchall()

#     return {
#         "actual": [r["v"] for r in actual_rows],
#         "predicted": [r["v"] for r in predicted_rows],
#     }


# # CSV download - same filters/sort as the JSON report above (via the shared
# # _report_filters/_report_pivot_cols helpers), but no pagination: exports
# # every row matching the current filters, exactly what's "in" the filtered
# # report, not just whichever page happens to be on screen.
# @app.get("/api/reports/agent-scores/export")
# def agent_scores_report_export(
#     agent_id: Optional[int] = None,
#     disposition_status: Optional[str] = None,
#     actual_disposition: Optional[str] = None,
#     predicted_disposition: Optional[str] = None,
#     start: Optional[str] = None,
#     end: Optional[str] = None,
#     sort: str = "call_date_desc",
#     db = Depends(get_db),
# ):
#     start, end = date_range_defaults(start, end)
#     order_sql = REPORT_SORTS.get(sort, REPORT_SORTS["call_date_desc"])
#     where_sql, params = _report_filters(
#         agent_id, disposition_status, actual_disposition, predicted_disposition, start, end
#     )

#     rows = db.execute(
#         f"""
#         SELECT a.name AS agent_name, c.recording_base, c.call_date, c.overall_score,
#                c.verdict, c.overall_quality,
#                c.call_end_type_name AS actual_disposition, c.predicted_disposition, c.disposition_match,
#                {_report_pivot_cols()}
#         FROM calls c
#         JOIN agents a ON a.id = c.agent_id
#         LEFT JOIN call_scores s ON s.call_id = c.id
#         LEFT JOIN parameters p ON p.id = s.parameter_id
#         WHERE {where_sql}
#         GROUP BY c.id, a.id, a.name, c.recording_base, c.call_date,
#                  c.overall_score, c.verdict, c.overall_quality,
#                  c.call_end_type_name, c.predicted_disposition, c.disposition_match
#         ORDER BY {order_sql}
#         """,
#         tuple(params),
#     ).fetchall()

#     def disposition_status_label(match):
#         if match == 1:
#             return "Correct"
#         if match == 0:
#             return "Incorrect"
#         return ""

#     buf = io.StringIO()
#     writer = csv.writer(buf)
#     header = ["Agent", "Recording", "Call Date", "Overall Score", "Verdict", "Quality",
#               "Actual Disposition", "Predicted Disposition", "Disposition Status"]
#     header += [label for _, label in PARAMETERS]
#     writer.writerow(header)
#     for r in rows:
#         row = [
#             r["agent_name"], r["recording_base"], r["call_date"], r["overall_score"],
#             r["verdict"], r["overall_quality"],
#             r["actual_disposition"], r["predicted_disposition"], disposition_status_label(r["disposition_match"]),
#         ]
#         row += [r[key] for key, _ in PARAMETERS]
#         writer.writerow(row)

#     filename = f"agent-report_{start}_to_{end}.csv"
#     return StreamingResponse(
#         iter([buf.getvalue()]),
#         media_type="text/csv",
#         headers={"Content-Disposition": f'attachment; filename="{filename}"'},
#     )


# # ==================================================================
# # Manifest upload + STT/SLM pipeline
# # ==================================================================

# @app.post("/api/manifest/upload")
# async def upload_manifest(
#     background_tasks: BackgroundTasks,
#     file: UploadFile = File(...),
#     recording_base_url: Optional[str] = None,
#     auto_process: bool = True,
# ):
#     if not file.filename.lower().endswith((".xlsx", ".xlsm")):
#         raise HTTPException(status_code=400, detail="Please upload an .xlsx manifest file")

#     with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
#         shutil.copyfileobj(file.file, tmp)
#         tmp_path = tmp.name

#     try:
#         manifest_id, row_count = save_manifest(
#             tmp_path, file.filename, recording_base_url or RECORDINGS_BASE_URL
#         )
#         print(f"Manifest uploaded: {manifest_id}, Rows: {row_count}")
#     finally:
#         os.unlink(tmp_path)

#     if auto_process:
#         print("Triggering background processing for manifest:", manifest_id)
#         background_tasks.add_task(process_manifest, manifest_id)

#     return {"manifest_id": manifest_id, "rows_loaded": row_count, "processing": auto_process}


# @app.post("/api/manifest/{manifest_id}/process")
# def trigger_processing(manifest_id: int, background_tasks: BackgroundTasks, db = Depends(get_db)):
#     row = db.execute("SELECT id FROM manifests WHERE id = ?", (manifest_id,)).fetchone()
#     if not row:
#         raise HTTPException(status_code=404, detail="Manifest not found")
#     background_tasks.add_task(process_manifest, manifest_id)
#     return {"manifest_id": manifest_id, "processing": True}


# @app.get("/api/manifest")
# def list_manifests(db = Depends(get_db)):
#     rows = db.execute("SELECT * FROM manifests ORDER BY id DESC").fetchall()
#     return {"manifests": [dict(r) for r in rows]}


# @app.get("/api/manifest/{manifest_id}")
# def manifest_status(manifest_id: int, db = Depends(get_db)):
#     manifest = db.execute("SELECT * FROM manifests WHERE id = ?", (manifest_id,)).fetchone()
#     if not manifest:
#         raise HTTPException(status_code=404, detail="Manifest not found")
#     calls = db.execute(
#         """SELECT c.id, c.call_number, c.recording_base, c.status, c.error_message,
#                   c.overall_score, c.verdict, a.name AS agent_name
#            FROM calls c JOIN agents a ON a.id = c.agent_id
#            WHERE c.manifest_id = ? ORDER BY c.id""",
#         (manifest_id,),
#     ).fetchall()
#     return {"manifest": dict(manifest), "calls": [dict(r) for r in calls]}


# # @app.get("/api/calls/{call_id}/transcript")
# # def call_transcript(call_id: int, db = Depends(get_db)):
# #     row = db.execute("SELECT * FROM transcripts WHERE call_id = ?", (call_id,)).fetchone()
# #     if not row:
# #         raise HTTPException(status_code=404, detail="No transcript for this call yet")
# #     return dict(row)


# # @app.get("/api/calls/{call_id}")
# # def call_detail(call_id: int, db = Depends(get_db)):
# #     # `c.*` already includes rx_path (IN leg) and tx_path (OUT leg) - the same
# #     # recording URLs the STT service was given. The frontend call-detail page
# #     # (feature 2.3) plays these directly and seeks to each parameter's
# #     # evidence start_time so a reviewer can jump straight to the moment a
# #     # score was decided, instead of listening to the whole call.
# #     call = db.execute(
# #         """SELECT c.*, a.name AS agent_name FROM calls c JOIN agents a ON a.id = c.agent_id
# #            WHERE c.id = ?""",
# #         (call_id,),
# #     ).fetchone()
# #     if not call:
# #         raise HTTPException(status_code=404, detail="Call not found")
# #     scores = db.execute(
# #         """SELECT p.label AS parameter, s.status, s.reason, s.evidence, s.start_time, s.end_time,
# #                   s.raw_score, s.score
# #            FROM call_scores s JOIN parameters p ON p.id = s.parameter_id
# #            WHERE s.call_id = ? ORDER BY p.id""",
# #         (call_id,),
# #     ).fetchall()
# #     fatal = db.execute("SELECT * FROM fatal_checks WHERE call_id = ?", (call_id,)).fetchall()
# #     return {
# #         "call": dict(call),
# #         "parameters": [dict(r) for r in scores],
# #         "fatal_checks": [dict(r) for r in fatal],
# #     }

# @app.get("/api/calls/{call_id}/transcript")
# def call_transcript(call_id: int, db = Depends(get_db)):
#     row = db.execute("SELECT * FROM transcripts WHERE call_id = ?", (call_id,)).fetchone()
#     if not row:
#         raise HTTPException(status_code=404, detail="No transcript for this call yet")
#     return dict(row)


# @app.get("/api/calls/{call_id}")
# def call_detail(call_id: int, db = Depends(get_db)):
#     print(f"Fetching detail for call ID: {call_id}")
#     # `c.*` already includes rx_path (IN leg) and tx_path (OUT leg) - the same
#     # recording URLs the STT service was given. The frontend call-detail page
#     # (feature 2.3) plays these directly and seeks to each parameter's
#     # evidence start_time so a reviewer can jump straight to the moment a
#     # score was decided, instead of listening to the whole call.
#     call = db.execute(
#         """SELECT c.*, a.name AS agent_name FROM calls c JOIN agents a ON a.id = c.agent_id
#            WHERE c.id = ?""",
#         (call_id,),
#     ).fetchone()
#     if not call:
#         raise HTTPException(status_code=404, detail="Call not found")
#     scores = db.execute(
#         """SELECT p.label AS parameter, s.status, s.reason, s.evidence, s.start_time, s.end_time,
#                   s.raw_score, s.score
#            FROM call_scores s JOIN parameters p ON p.id = s.parameter_id
#            WHERE s.call_id = ? ORDER BY p.id""",
#         (call_id,),
#     ).fetchall()
#     fatal = db.execute("SELECT * FROM fatal_checks WHERE call_id = ?", (call_id,)).fetchall()
#     # print(dict(call), [dict(r) for r in scores], [dict(r) for r in fatal])
#     call_dict = dict(call)
#     print(call_dict)
#     if call_dict.get("recording_base"):
#         call_dict["mono_url"] = f"{RECORDINGS_PLAYBACK_BASE_URL.rstrip('/')}/{call_dict['recording_base']}"
#     else:
#         call_dict["mono_url"] = None
#     print(f" after adding the mono url\n{call_dict}")
#     return {
#         "call": dict(call_dict),
#         "parameters": [dict(r) for r in scores],
#         "fatal_checks": [dict(r) for r in fatal],
#     }

# @app.get("/api/health")
# def health(db = Depends(get_db)):
#     """Hits the DB too, so this actually tells you whether MySQL is reachable,
#     not just whether the FastAPI process is up."""
#     db.execute("SELECT 1")
#     return {"status": "ok"}

# *********************************w7********************************

# import csv
# import io
# import os
# import shutil
# import tempfile
# from datetime import datetime, timedelta
# from typing import Optional

# import pymysql
# from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Request, UploadFile
# from fastapi.middleware.cors import CORSMiddleware
# from fastapi.responses import JSONResponse, StreamingResponse
# from pydantic import BaseModel

# import hashlib

# # from app4_.backend.config import RECOR, RECORDINGS_PLAYBACK_BASE_URLDINGS_PLAYBACK_BASE_URL
# from config import PARAMETERS, RECORDINGS_BASE_URL, RECORDINGS_PLAYBACK_BASE_URL
# from db import get_conn, init_db
# from pipeline import process_manifest, save_manifest

# app = FastAPI(title="Athena QA Dashboard")

# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=[
#         "http://localhost:5173",
#         "http://127.0.0.1:5173",
#     ],
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )

# # ------------------------------------------------------------------
# # IMPORTANT FIX: init_db() used to run unguarded at import time. If MySQL
# # was unreachable (wrong host/user/password, or the server just wasn't up
# # yet), that raised an exception WHILE the module was loading - before
# # FastAPI/CORSMiddleware even finished being built. uvicorn's worker
# # process died right there, so nothing was listening on the port at all.
# #
# # That's exactly what showed up in the browser as CORS errors + net::ERR_FAILED:
# # there was no server to send a response (let alone CORS headers), so Chrome
# # reports it as a blocked/failed request instead of a clean 500.
# #
# # Fix: catch the startup failure, log it clearly, and let the app come up
# # anyway. Every DB-touching request will then fail with a clean 503 JSON
# # response (see exception handler below) that DOES carry CORS headers,
# # instead of the whole server being unreachable.
# # ------------------------------------------------------------------
# try:
#     init_db()
# except Exception as e:
#     print("=" * 70)
#     print("[STARTUP WARNING] Could not connect to MySQL / initialize schema.")
#     print(f"[STARTUP WARNING] {type(e).__name__}: {e}")
#     print("[STARTUP WARNING] Check MYSQL_HOST / MYSQL_PORT / MYSQL_USER / "
#           "MYSQL_PASSWORD / MYSQL_DATABASE env vars and that MySQL is running.")
#     print("[STARTUP WARNING] The API is still starting so you get clean 503s "
#           "instead of connection failures - fix your DB config and restart.")
#     print("=" * 70)


# @app.exception_handler(pymysql.err.Error)
# async def mysql_error_handler(request: Request, exc: pymysql.err.Error):
#     return JSONResponse(
#         status_code=503,
#         content={"detail": f"Database unavailable: {exc}"},
#     )


# def get_db():
#     conn = get_conn()
#     try:
#         yield conn
#     finally:
#         conn.close()


# def hash_pw(pw: str) -> str:
#     return hashlib.sha256(pw.encode()).hexdigest()


# # ---------- Schemas ----------
# class LoginRequest(BaseModel):
#     username: str
#     password: str


# class LoginResponse(BaseModel):
#     token: str
#     username: str
#     role: str


# # ---------- Auth ----------
# @app.post("/api/login", response_model=LoginResponse)
# def login(req: LoginRequest, db = Depends(get_db)):
#     row = db.execute(
#         "SELECT * FROM users WHERE username = ? AND password_hash = ?",
#         (req.username, hash_pw(req.password)),
#     ).fetchone()
#     if not row:
#         raise HTTPException(status_code=401, detail="Invalid username or password")
#     token = hashlib.sha256(f"{req.username}{datetime.utcnow()}".encode()).hexdigest()
#     return LoginResponse(token=token, username=row["username"], role=row["role"])


# class ChangePasswordRequest(BaseModel):
#     username: str
#     current_password: str
#     new_password: str


# @app.post("/api/change-password")
# def change_password(req: ChangePasswordRequest, db = Depends(get_db)):
#     # There's no session/bearer-token check anywhere else in this app yet
#     # (login just hands back an unverified token) - so identity here is
#     # proven the same way login proves it: username + correct current
#     # password. If you add real session auth later, swap this for a
#     # Depends(current_user) check instead of trusting the request body.
#     if len(req.new_password) < 8:
#         raise HTTPException(status_code=400, detail="New password must be at least 8 characters")

#     row = db.execute(
#         "SELECT * FROM users WHERE username = ? AND password_hash = ?",
#         (req.username, hash_pw(req.current_password)),
#     ).fetchone()
#     if not row:
#         raise HTTPException(status_code=401, detail="Current password is incorrect")

#     db.execute(
#         "UPDATE users SET password_hash = ? WHERE username = ?",
#         (hash_pw(req.new_password), req.username),
#     )
#     db.commit()
#     return {"status": "ok"}


# # ---------- Helpers ----------
# def date_range_defaults(start: Optional[str], end: Optional[str]):
#     if not end:
#         end = datetime.utcnow().strftime("%Y-%m-%d")
#     if not start:
#         start = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
#     return start, end


# # Whitelist of SQL fragments for the 5 clickable dashboard stat cards (feature 2).
# # Kept as a fixed dict (not built from the path param) so there's no injection risk
# # even though it gets interpolated straight into the query string below.
# CATEGORY_FILTERS = {
#     "total": "1=1",
#     "audited": "c.status = 'audited'",
#     "pending": "c.status IN ('pending','transcribing','analyzing')",
#     "positive": "c.sentiment = 'positive'",
#     "negative": "c.sentiment = 'negative'",
#     # Agent-detail summary cards (feature: clickable Total/Average/Good/Poor).
#     # "Total Calls" and "Average Call" both draw from the same audited
#     # population that agent_detail()'s stats are computed over, so they
#     # share the "audited" filter - only the label shown differs.
#     "good": "c.overall_quality = 'good'",
#     "poor": "c.overall_quality = 'poor'",
#     # Disposition-based (feature: below the funnel). Case-insensitive since
#     # the manifest's call_end_type_name casing isn't guaranteed consistent
#     # (we've seen "CALLBACK" in real data).
#     "disp_not_eligible": "UPPER(c.call_end_type_name) = 'NOT ELIGIBLE'",
#     "disp_not_interested": "UPPER(c.call_end_type_name) = 'NOT INTERESTED'",
#     "disp_callback": "UPPER(c.call_end_type_name) = 'CALLBACK'",
#     "disp_short_hangup": "UPPER(c.call_end_type_name) = 'SHORT HANGUP'",
#     "disp_lead": "UPPER(c.call_end_type_name) = 'LEAD'",
#     "disp_eligible": "UPPER(c.call_end_type_name) = 'ELIGIBLE'",
#     "disp_hangup": "UPPER(c.call_end_type_name) = 'HANGUP'",
#     # Correct/Wrong: actual (manifest) vs predicted (SLM) disposition, compared
#     # once and cached on the row at process time (pipeline.py) rather than
#     # here - disposition_match is NULL (excluded from both) for any call
#     # missing either side of the comparison, so this filter alone is enough,
#     # no extra "and both sides present" clause needed.
#     "disp_correct": "c.disposition_match = 1",
#     "disp_wrong": "c.disposition_match = 0",
# }

# # Labels + ordering for the disposition buttons row on the dashboard.
# DISPOSITION_CATEGORIES = [
#     ("disp_not_eligible", "Not Eligible", "NOT ELIGIBLE"),
#     ("disp_not_interested", "Not Interested", "NOT INTERESTED"),
#     ("disp_callback", "Callback", "CALLBACK"),
#     ("disp_short_hangup", "Short Hangup", "SHORT HANGUP"),
#     ("disp_lead", "Lead", "LEAD"),
#     ("disp_eligible", "Eligible", "ELIGIBLE"),
#     ("disp_hangup", "Hangup", "HANGUP"),
# ]


# def category_filter_sql(category: str) -> str:
#     if category not in CATEGORY_FILTERS:
#         raise HTTPException(
#             status_code=400,
#             detail=f"Unknown category '{category}'. Must be one of: {', '.join(CATEGORY_FILTERS)}",
#         )
#     return CATEGORY_FILTERS[category]


# # ---------- Dashboard summary (Section 2 top cards) ----------
# @app.get("/api/dashboard/summary")
# def dashboard_summary(
#     start: Optional[str] = None,
#     end: Optional[str] = None,
#     db = Depends(get_db),
# ):
#     start, end = date_range_defaults(start, end)
#     row = db.execute(
#         """
#         SELECT
#           COUNT(*) AS total_calls,
#           SUM(CASE WHEN status = 'audited' THEN 1 ELSE 0 END) AS audited_calls,
#           SUM(CASE WHEN status IN ('pending','transcribing','analyzing') THEN 1 ELSE 0 END) AS pending_calls,
#           SUM(CASE WHEN sentiment = 'positive' THEN 1 ELSE 0 END) AS positive_calls,
#           SUM(CASE WHEN sentiment = 'negative' THEN 1 ELSE 0 END) AS negative_calls
#         FROM calls
#         WHERE call_date IS NOT NULL AND date(call_date) BETWEEN date(?) AND date(?)
#         """,
#         (start, end),
#     ).fetchone()
#     return {
#         "start": start,
#         "end": end,
#         "total_calls": row["total_calls"] or 0,
#         "audited_calls": row["audited_calls"] or 0,
#         "pending_calls": row["pending_calls"] or 0,
#         "positive_calls": row["positive_calls"] or 0,
#         "negative_calls": row["negative_calls"] or 0,
#     }


# # ---------- Disposition counts (feature: buttons below the funnel) ----------
# # One grouped query for the manifest-based dispositions (not one query per
# # button), plus one small aggregate for Correct/Wrong (disposition_match),
# # then mapped onto the known categories. Drill-down reuses the exact same
# # /api/dashboard/category/{category}/... endpoints as the 5 stat cards do -
# # "disp_not_eligible" etc, and now "disp_correct"/"disp_wrong" too, are just
# # more entries in CATEGORY_FILTERS above.
# @app.get("/api/dashboard/dispositions")
# def dashboard_dispositions(
#     start: Optional[str] = None,
#     end: Optional[str] = None,
#     db = Depends(get_db),
# ):
#     start, end = date_range_defaults(start, end)
#     rows = db.execute(
#         """
#         SELECT UPPER(call_end_type_name) AS disp, COUNT(*) AS n
#         FROM calls
#         WHERE call_date IS NOT NULL AND date(call_date) BETWEEN date(?) AND date(?)
#           AND call_end_type_name IS NOT NULL AND call_end_type_name != ''
#         GROUP BY UPPER(call_end_type_name)
#         """,
#         (start, end),
#     ).fetchall()
#     counts = {r["disp"]: r["n"] for r in rows}

#     match_row = db.execute(
#         """
#         SELECT
#           SUM(CASE WHEN disposition_match = 1 THEN 1 ELSE 0 END) AS correct_n,
#           SUM(CASE WHEN disposition_match = 0 THEN 1 ELSE 0 END) AS wrong_n
#         FROM calls
#         WHERE call_date IS NOT NULL AND date(call_date) BETWEEN date(?) AND date(?)
#         """,
#         (start, end),
#     ).fetchone()

#     dispositions = [
#         {"category": cat, "label": label, "count": counts.get(match_value, 0)}
#         for cat, label, match_value in DISPOSITION_CATEGORIES
#     ]
#     dispositions.append({"category": "disp_correct", "label": "Correct", "count": match_row["correct_n"] or 0})
#     dispositions.append({"category": "disp_wrong", "label": "Wrong", "count": match_row["wrong_n"] or 0})

#     return {
#         "start": start,
#         "end": end,
#         "dispositions": dispositions,
#     }


# # ---------- Parameter-wise average scores (graph) ----------
# @app.get("/api/dashboard/parameters")
# def dashboard_parameters(
#     start: Optional[str] = None,
#     end: Optional[str] = None,
#     db = Depends(get_db),
# ):
#     start, end = date_range_defaults(start, end)
#     rows = db.execute(
#         """
#         SELECT p.label AS parameter, ROUND(AVG(s.score), 1) AS avg_score, COUNT(*) AS n
#         FROM call_scores s
#         JOIN parameters p ON p.id = s.parameter_id
#         JOIN calls c ON c.id = s.call_id
#         WHERE c.call_date IS NOT NULL AND date(c.call_date) BETWEEN date(?) AND date(?)
#         GROUP BY p.id, p.label
#         ORDER BY p.id
#         """,
#         (start, end),
#     ).fetchall()
#     return {
#         "start": start,
#         "end": end,
#         "parameters": [
#             {"parameter": r["parameter"], "avg_score": r["avg_score"] or 0, "sample_size": r["n"]}
#             for r in rows
#         ],
#     }


# # ---------- Parameter success-rate funnel (feature 1) ----------
# # Different from /api/dashboard/parameters above (which averages the 0-100
# # normalized score). This instead answers "out of every AUDITED call, what
# # % actually PASSED (status=true) this parameter?" - sorted highest-first so
# # it reads top-to-bottom like a funnel: parameters almost everyone nails at
# # the top, parameters agents struggle with at the bottom.
# #
# # success_pct = (# audited calls where that parameter's status = TRUE)
# #               / (total audited calls in range) * 100
# #
# # Two queries total (not N+1): one scalar for the constant denominator
# # (total audited calls), one GROUP BY for all 16 numerators together.
# @app.get("/api/dashboard/parameter-funnel")
# def parameter_funnel(
#     start: Optional[str] = None,
#     end: Optional[str] = None,
#     db = Depends(get_db),
# ):
#     start, end = date_range_defaults(start, end)

#     total_row = db.execute(
#         """
#         SELECT COUNT(*) AS total_audited
#         FROM calls
#         WHERE status = 'audited' AND call_date IS NOT NULL
#           AND date(call_date) BETWEEN date(?) AND date(?)
#         """,
#         (start, end),
#     ).fetchone()
#     total_audited = total_row["total_audited"] or 0

#     rows = db.execute(
#         """
#         SELECT p.id AS parameter_id, p.label AS parameter,
#                SUM(CASE WHEN s.status = 1 THEN 1 ELSE 0 END) AS success_count,
#                COUNT(*) AS sample_size
#         FROM call_scores s
#         JOIN parameters p ON p.id = s.parameter_id
#         JOIN calls c ON c.id = s.call_id
#         WHERE c.status = 'audited' AND c.call_date IS NOT NULL
#           AND date(c.call_date) BETWEEN date(?) AND date(?)
#         GROUP BY p.id, p.label
#         ORDER BY p.id
#         """,
#         (start, end),
#     ).fetchall()

#     funnel = []
#     for r in rows:
#         success_count = r["success_count"] or 0
#         pct = round((success_count / total_audited) * 100, 1) if total_audited else 0.0
#         funnel.append({
#             "parameter_id": r["parameter_id"],
#             "parameter": r["parameter"],
#             "success_count": success_count,
#             "sample_size": r["sample_size"],
#             "success_pct": pct,
#         })

#     funnel.sort(key=lambda x: x["success_pct"], reverse=True)

#     return {
#         "start": start,
#         "end": end,
#         "total_audited_calls": total_audited,
#         "parameters": funnel,
#     }



# # ---------- Agents belonging to a parameter (click on bar) ----------
# @app.get("/api/dashboard/parameters/{parameter_name}/agents")
# def agents_for_parameter(
#     parameter_name: str,
#     start: Optional[str] = None,
#     end: Optional[str] = None,
#     db = Depends(get_db),
# ):
#     start, end = date_range_defaults(start, end)
#     rows = db.execute(
#         """
#         SELECT a.id AS agent_id,
#                a.name AS agent_name,
#                a.team,
#                ROUND(AVG(s.score), 1) AS avg_score,
#                COUNT(*) AS calls_scored
#         FROM call_scores s
#         JOIN parameters p ON p.id = s.parameter_id
#         JOIN calls c ON c.id = s.call_id
#         JOIN agents a ON a.id = c.agent_id
#         WHERE p.label = ?
#           AND c.call_date IS NOT NULL
#           AND date(c.call_date) BETWEEN date(?) AND date(?)
#         GROUP BY a.id, a.name, a.team
#         ORDER BY avg_score ASC
#         """,
#         (parameter_name, start, end),
#     ).fetchall()
#     if not rows:
#         exists = db.execute("SELECT 1 FROM parameters WHERE label = ?", (parameter_name,)).fetchone()
#         if not exists:
#             raise HTTPException(status_code=404, detail="Parameter not found")
#     return {
#         "parameter": parameter_name,
#         "start": start,
#         "end": end,
#         "agents": [dict(r) for r in rows],
#     }


# # ---------- Agent list (basic) ----------
# @app.get("/api/agents")
# def list_agents(db = Depends(get_db)):
#     rows = db.execute("SELECT id, name, team FROM agents ORDER BY name").fetchall()
#     return {"agents": [dict(r) for r in rows]}


# # ---------- Agent detail (Section 3) ----------
# @app.get("/api/agents/{agent_id}")
# def agent_detail(
#     agent_id: int,
#     start: Optional[str] = None,
#     end: Optional[str] = None,
#     db = Depends(get_db),
# ):
#     start, end = date_range_defaults(start, end)
#     agent = db.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
#     if not agent:
#         raise HTTPException(status_code=404, detail="Agent not found")

#     calls_row = db.execute(
#         """
#         SELECT COUNT(*) AS total_calls,
#                SUM(CASE WHEN overall_quality = 'good' THEN 1 ELSE 0 END) AS good_calls,
#                SUM(CASE WHEN overall_quality = 'poor' THEN 1 ELSE 0 END) AS poor_calls,
#                ROUND(AVG(overall_score), 1) AS average_call_score
#         FROM calls
#         WHERE agent_id = ? AND status = 'audited'
#           AND call_date IS NOT NULL AND date(call_date) BETWEEN date(?) AND date(?)
#         """,
#         (agent_id, start, end),
#     ).fetchone()

#     param_rows = db.execute(
#         """
#         SELECT p.label AS parameter, ROUND(AVG(s.score),1) AS avg_score
#         FROM call_scores s
#         JOIN parameters p ON p.id = s.parameter_id
#         JOIN calls c ON c.id = s.call_id
#         WHERE c.agent_id = ? AND c.call_date IS NOT NULL AND date(c.call_date) BETWEEN date(?) AND date(?)
#         GROUP BY p.id, p.label
#         ORDER BY p.id
#         """,
#         (agent_id, start, end),
#     ).fetchall()

#     params = [dict(r) for r in param_rows]
#     if params:
#         sorted_params = sorted(params, key=lambda x: x["avg_score"] or 0, reverse=True)
#         strengths = [p["parameter"] for p in sorted_params[:3]]
#         improvements = [p["parameter"] for p in sorted_params[-3:]]
#     else:
#         strengths, improvements = [], []

#     return {
#         "agent": {"id": agent["id"], "name": agent["name"], "team": agent["team"]},
#         "start": start,
#         "end": end,
#         "total_calls": calls_row["total_calls"] or 0,
#         "average_call_score": calls_row["average_call_score"] or 0,
#         "good_calls": calls_row["good_calls"] or 0,
#         "poor_calls": calls_row["poor_calls"] or 0,
#         "parameters": params,
#         "overall_summary": {
#             "strengths": strengths,
#             "improvements": improvements,
#         },
#     }


# # ==================================================================
# # Feature 2: clickable stat cards -> agents -> that agent's calls.
# # category is one of the CATEGORY_FILTERS keys (total/audited/pending/positive/negative),
# # matching whichever stat card was clicked on the dashboard.
# # ==================================================================

# @app.get("/api/dashboard/category/{category}/agents")
# def category_agents(
#     category: str,
#     start: Optional[str] = None,
#     end: Optional[str] = None,
#     db = Depends(get_db),
# ):
#     """2.1 - agents ranked by their average score, scoped to the clicked category."""
#     where_clause = category_filter_sql(category)
#     start, end = date_range_defaults(start, end)
#     rows = db.execute(
#         f"""
#         SELECT a.id AS agent_id, a.name AS agent_name, a.team,
#                ROUND(AVG(c.overall_score), 1) AS avg_score,
#                COUNT(*) AS call_count
#         FROM calls c
#         JOIN agents a ON a.id = c.agent_id
#         WHERE {where_clause}
#           AND c.call_date IS NOT NULL AND date(c.call_date) BETWEEN date(?) AND date(?)
#         GROUP BY a.id, a.name, a.team
#         ORDER BY avg_score DESC
#         """,
#         (start, end),
#     ).fetchall()
#     return {
#         "category": category,
#         "start": start,
#         "end": end,
#         "agents": [dict(r) for r in rows],
#     }


# @app.get("/api/dashboard/category/{category}/agents/{agent_id}/calls")
# def category_agent_calls(
#     category: str,
#     agent_id: int,
#     start: Optional[str] = None,
#     end: Optional[str] = None,
#     db = Depends(get_db),
# ):
#     """2.2 - that agent's individual calls (with score) within the clicked category."""
#     where_clause = category_filter_sql(category)
#     start, end = date_range_defaults(start, end)
#     agent = db.execute("SELECT id, name, team FROM agents WHERE id = ?", (agent_id,)).fetchone()
#     if not agent:
#         raise HTTPException(status_code=404, detail="Agent not found")

#     calls = db.execute(
#         f"""
#         SELECT c.id, c.call_number, c.call_start_time, c.call_date, c.status,
#                c.sentiment, c.overall_quality, c.overall_score, c.verdict, c.recording_base
#         FROM calls c
#         WHERE c.agent_id = ? AND {where_clause}
#           AND c.call_date IS NOT NULL AND date(c.call_date) BETWEEN date(?) AND date(?)
#         ORDER BY c.call_date DESC, c.id DESC
#         """,
#         (agent_id, start, end),
#     ).fetchall()
#     return {
#         "category": category,
#         "agent": dict(agent),
#         "start": start,
#         "end": end,
#         "calls": [dict(r) for r in calls],
#     }


# # ==================================================================
# # Feature 3: top-10 agents leaderboard (right-hand sidebar box)
# # ==================================================================

# @app.get("/api/dashboard/top-agents")
# def top_agents(
#     start: Optional[str] = None,
#     end: Optional[str] = None,
#     limit: int = 10,
#     search: Optional[str] = None,
#     db = Depends(get_db),
# ):
#     start, end = date_range_defaults(start, end)
#     # a search should be able to surface more than 10 matches; the sidebar's
#     # default (unsearched) call always passes limit=10 explicitly, this cap
#     # just stops a search from being unbounded.
#     limit = max(1, min(limit, 50))

#     where = [
#         "c.status = 'audited'",
#         "c.call_date IS NOT NULL",
#         "date(c.call_date) BETWEEN date(?) AND date(?)",
#     ]
#     params = [start, end]
#     if search:
#         where.append("a.name LIKE ?")
#         params.append(f"%{search}%")
#     where_sql = " AND ".join(where)

#     rows = db.execute(
#         f"""
#         SELECT a.id AS agent_id, a.name AS agent_name, a.team,
#                ROUND(AVG(c.overall_score), 1) AS avg_score,
#                COUNT(*) AS call_count
#         FROM calls c
#         JOIN agents a ON a.id = c.agent_id
#         WHERE {where_sql}
#         GROUP BY a.id, a.name, a.team
#         ORDER BY avg_score DESC
#         LIMIT ?
#         """,
#         tuple(params) + (limit,),
#     ).fetchall()
#     return {
#         "start": start,
#         "end": end,
#         "search": search,
#         "agents": [dict(r) for r in rows],
#     }



# REPORT_SORTS = {
#     "call_date_desc": "c.call_date DESC, c.id DESC",
#     "call_date_asc": "c.call_date ASC, c.id ASC",
#     "score_desc": "c.overall_score DESC",
#     "score_asc": "c.overall_score ASC",
#     "agent_name": "a.name ASC, c.call_date DESC",
# }


# def _report_pivot_cols():
#     # One column per parameter via conditional aggregation (MySQL has no PIVOT).
#     # param_key values come from our own hardcoded PARAMETERS config, not user
#     # input, so interpolating them straight into the SQL text is safe here.
#     return ",\n               ".join(
#         f"MAX(CASE WHEN p.param_key = '{key}' THEN s.score END) AS `{key}`"
#         for key, _ in PARAMETERS
#     )


# def _report_filters(agent_id, disposition_status, actual_disposition, predicted_disposition, start, end):
#     """Builds the WHERE clause + params shared by the report's JSON endpoint
#     AND its CSV export below - one place to add a filter so the download can
#     never drift out of sync with what's on screen."""
#     where = [
#         "c.status = 'audited'",
#         "c.call_date IS NOT NULL",
#         "date(c.call_date) BETWEEN date(?) AND date(?)",
#     ]
#     params = [start, end]
#     if agent_id:
#         where.append("c.agent_id = ?")
#         params.append(agent_id)
#     if disposition_status == "correct":
#         where.append("c.disposition_match = 1")
#     elif disposition_status == "incorrect":
#         where.append("c.disposition_match = 0")
#     if actual_disposition:
#         where.append("UPPER(c.call_end_type_name) = UPPER(?)")
#         params.append(actual_disposition)
#     if predicted_disposition:
#         where.append("UPPER(c.predicted_disposition) = UPPER(?)")
#         params.append(predicted_disposition)
#     return " AND ".join(where), params


# @app.get("/api/reports/agent-scores")
# def agent_scores_report(
#     agent_id: Optional[int] = None,
#     disposition_status: Optional[str] = None,  # "correct" | "incorrect"
#     actual_disposition: Optional[str] = None,
#     predicted_disposition: Optional[str] = None,
#     start: Optional[str] = None,
#     end: Optional[str] = None,
#     page: int = 1,
#     page_size: int = 25,
#     sort: str = "call_date_desc",
#     db = Depends(get_db),
# ):
#     start, end = date_range_defaults(start, end)
#     page = max(1, page)
#     page_size = max(1, min(page_size, 200))  # cap it - this is a report table, not a data dump endpoint
#     offset = (page - 1) * page_size
#     order_sql = REPORT_SORTS.get(sort, REPORT_SORTS["call_date_desc"])

#     where_sql, params = _report_filters(
#         agent_id, disposition_status, actual_disposition, predicted_disposition, start, end
#     )

#     total_row = db.execute(
#         f"SELECT COUNT(*) AS n FROM calls c JOIN agents a ON a.id = c.agent_id WHERE {where_sql}",
#         tuple(params),
#     ).fetchone()
#     total = total_row["n"] or 0

#     rows = db.execute(
#         f"""
#         SELECT c.id AS call_id, a.id AS agent_id, a.name AS agent_name,
#                c.recording_base, c.call_date, c.overall_score, c.verdict, c.overall_quality,
#                c.call_end_type_name AS actual_disposition, c.predicted_disposition, c.disposition_match,
#                {_report_pivot_cols()}
#         FROM calls c
#         JOIN agents a ON a.id = c.agent_id
#         LEFT JOIN call_scores s ON s.call_id = c.id
#         LEFT JOIN parameters p ON p.id = s.parameter_id
#         WHERE {where_sql}
#         GROUP BY c.id, a.id, a.name, c.recording_base, c.call_date,
#                  c.overall_score, c.verdict, c.overall_quality,
#                  c.call_end_type_name, c.predicted_disposition, c.disposition_match
#         ORDER BY {order_sql}
#         LIMIT ? OFFSET ?
#         """,
#         tuple(params) + (page_size, offset),
#     ).fetchall()

#     return {
#         "start": start,
#         "end": end,
#         "agent_id": agent_id,
#         "page": page,
#         "page_size": page_size,
#         "total": total,
#         "columns": [{"key": key, "label": label} for key, label in PARAMETERS],
#         "rows": [dict(r) for r in rows],
#     }


# # Dropdown option sources for the report's disposition filters - distinct
# # values actually present in the data (within the date range), not a
# # hardcoded list, so anything the manifest or SLM ever produces shows up.
# @app.get("/api/reports/disposition-options")
# def report_disposition_options(
#     start: Optional[str] = None,
#     end: Optional[str] = None,
#     db = Depends(get_db),
# ):
#     start, end = date_range_defaults(start, end)
#     date_filter = "c.call_date IS NOT NULL AND date(c.call_date) BETWEEN date(?) AND date(?)"

#     actual_rows = db.execute(
#         f"""SELECT DISTINCT call_end_type_name AS v FROM calls c
#             WHERE {date_filter} AND call_end_type_name IS NOT NULL AND call_end_type_name != ''
#             ORDER BY v""",
#         (start, end),
#     ).fetchall()
#     predicted_rows = db.execute(
#         f"""SELECT DISTINCT predicted_disposition AS v FROM calls c
#             WHERE {date_filter} AND predicted_disposition IS NOT NULL AND predicted_disposition != ''
#             ORDER BY v""",
#         (start, end),
#     ).fetchall()

#     return {
#         "actual": [r["v"] for r in actual_rows],
#         "predicted": [r["v"] for r in predicted_rows],
#     }


# # CSV download - same filters/sort as the JSON report above (via the shared
# # _report_filters/_report_pivot_cols helpers), but no pagination: exports
# # every row matching the current filters, exactly what's "in" the filtered
# # report, not just whichever page happens to be on screen.
# @app.get("/api/reports/agent-scores/export")
# def agent_scores_report_export(
#     agent_id: Optional[int] = None,
#     disposition_status: Optional[str] = None,
#     actual_disposition: Optional[str] = None,
#     predicted_disposition: Optional[str] = None,
#     start: Optional[str] = None,
#     end: Optional[str] = None,
#     sort: str = "call_date_desc",
#     db = Depends(get_db),
# ):
#     start, end = date_range_defaults(start, end)
#     order_sql = REPORT_SORTS.get(sort, REPORT_SORTS["call_date_desc"])
#     where_sql, params = _report_filters(
#         agent_id, disposition_status, actual_disposition, predicted_disposition, start, end
#     )

#     rows = db.execute(
#         f"""
#         SELECT a.name AS agent_name, c.recording_base, c.call_date, c.overall_score,
#                c.verdict, c.overall_quality,
#                c.call_end_type_name AS actual_disposition, c.predicted_disposition, c.disposition_match,
#                {_report_pivot_cols()}
#         FROM calls c
#         JOIN agents a ON a.id = c.agent_id
#         LEFT JOIN call_scores s ON s.call_id = c.id
#         LEFT JOIN parameters p ON p.id = s.parameter_id
#         WHERE {where_sql}
#         GROUP BY c.id, a.id, a.name, c.recording_base, c.call_date,
#                  c.overall_score, c.verdict, c.overall_quality,
#                  c.call_end_type_name, c.predicted_disposition, c.disposition_match
#         ORDER BY {order_sql}
#         """,
#         tuple(params),
#     ).fetchall()

#     def disposition_status_label(match):
#         if match == 1:
#             return "Correct"
#         if match == 0:
#             return "Incorrect"
#         return ""

#     buf = io.StringIO()
#     writer = csv.writer(buf)
#     header = ["Agent", "Recording", "Call Date", "Overall Score", "Verdict", "Quality",
#               "Actual Disposition", "Predicted Disposition", "Disposition Status"]
#     header += [label for _, label in PARAMETERS]
#     writer.writerow(header)
#     for r in rows:
#         row = [
#             r["agent_name"], r["recording_base"], r["call_date"], r["overall_score"],
#             r["verdict"], r["overall_quality"],
#             r["actual_disposition"], r["predicted_disposition"], disposition_status_label(r["disposition_match"]),
#         ]
#         row += [r[key] for key, _ in PARAMETERS]
#         writer.writerow(row)

#     filename = f"agent-report_{start}_to_{end}.csv"
#     return StreamingResponse(
#         iter([buf.getvalue()]),
#         media_type="text/csv",
#         headers={"Content-Disposition": f'attachment; filename="{filename}"'},
#     )


# # ==================================================================
# # Manifest upload + STT/SLM pipeline
# # ==================================================================

# @app.post("/api/manifest/upload")
# async def upload_manifest(
#     background_tasks: BackgroundTasks,
#     file: UploadFile = File(...),
#     recording_base_url: Optional[str] = None,
#     auto_process: bool = True,
# ):
#     if not file.filename.lower().endswith((".xlsx", ".xlsm")):
#         raise HTTPException(status_code=400, detail="Please upload an .xlsx manifest file")

#     with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
#         shutil.copyfileobj(file.file, tmp)
#         tmp_path = tmp.name

#     try:
#         manifest_id, row_count = save_manifest(
#             tmp_path, file.filename, recording_base_url or RECORDINGS_BASE_URL
#         )
#         print(f"Manifest uploaded: {manifest_id}, Rows: {row_count}")
#     finally:
#         os.unlink(tmp_path)

#     if auto_process:
#         print("Triggering background processing for manifest:", manifest_id)
#         background_tasks.add_task(process_manifest, manifest_id)

#     return {"manifest_id": manifest_id, "rows_loaded": row_count, "processing": auto_process}


# @app.post("/api/manifest/{manifest_id}/process")
# def trigger_processing(manifest_id: int, background_tasks: BackgroundTasks, db = Depends(get_db)):
#     row = db.execute("SELECT id FROM manifests WHERE id = ?", (manifest_id,)).fetchone()
#     if not row:
#         raise HTTPException(status_code=404, detail="Manifest not found")
#     background_tasks.add_task(process_manifest, manifest_id)
#     return {"manifest_id": manifest_id, "processing": True}


# @app.get("/api/manifest")
# def list_manifests(db = Depends(get_db)):
#     rows = db.execute("SELECT * FROM manifests ORDER BY id DESC").fetchall()
#     return {"manifests": [dict(r) for r in rows]}


# @app.get("/api/manifest/{manifest_id}")
# def manifest_status(manifest_id: int, db = Depends(get_db)):
#     manifest = db.execute("SELECT * FROM manifests WHERE id = ?", (manifest_id,)).fetchone()
#     if not manifest:
#         raise HTTPException(status_code=404, detail="Manifest not found")
#     calls = db.execute(
#         """SELECT c.id, c.call_number, c.recording_base, c.status, c.error_message,
#                   c.overall_score, c.verdict, a.name AS agent_name
#            FROM calls c JOIN agents a ON a.id = c.agent_id
#            WHERE c.manifest_id = ? ORDER BY c.id""",
#         (manifest_id,),
#     ).fetchall()
#     return {"manifest": dict(manifest), "calls": [dict(r) for r in calls]}


# # @app.get("/api/calls/{call_id}/transcript")
# # def call_transcript(call_id: int, db = Depends(get_db)):
# #     row = db.execute("SELECT * FROM transcripts WHERE call_id = ?", (call_id,)).fetchone()
# #     if not row:
# #         raise HTTPException(status_code=404, detail="No transcript for this call yet")
# #     return dict(row)


# # @app.get("/api/calls/{call_id}")
# # def call_detail(call_id: int, db = Depends(get_db)):
# #     # `c.*` already includes rx_path (IN leg) and tx_path (OUT leg) - the same
# #     # recording URLs the STT service was given. The frontend call-detail page
# #     # (feature 2.3) plays these directly and seeks to each parameter's
# #     # evidence start_time so a reviewer can jump straight to the moment a
# #     # score was decided, instead of listening to the whole call.
# #     call = db.execute(
# #         """SELECT c.*, a.name AS agent_name FROM calls c JOIN agents a ON a.id = c.agent_id
# #            WHERE c.id = ?""",
# #         (call_id,),
# #     ).fetchone()
# #     if not call:
# #         raise HTTPException(status_code=404, detail="Call not found")
# #     scores = db.execute(
# #         """SELECT p.label AS parameter, s.status, s.reason, s.evidence, s.start_time, s.end_time,
# #                   s.raw_score, s.score
# #            FROM call_scores s JOIN parameters p ON p.id = s.parameter_id
# #            WHERE s.call_id = ? ORDER BY p.id""",
# #         (call_id,),
# #     ).fetchall()
# #     fatal = db.execute("SELECT * FROM fatal_checks WHERE call_id = ?", (call_id,)).fetchall()
# #     return {
# #         "call": dict(call),
# #         "parameters": [dict(r) for r in scores],
# #         "fatal_checks": [dict(r) for r in fatal],
# #     }

# @app.get("/api/calls/{call_id}/transcript")
# def call_transcript(call_id: int, db = Depends(get_db)):
#     row = db.execute("SELECT * FROM transcripts WHERE call_id = ?", (call_id,)).fetchone()
#     if not row:
#         raise HTTPException(status_code=404, detail="No transcript for this call yet")
#     return dict(row)


# @app.get("/api/calls/{call_id}")
# def call_detail(call_id: int, db = Depends(get_db)):
#     print(f"Fetching detail for call ID: {call_id}")
#     # `c.*` already includes rx_path (IN leg) and tx_path (OUT leg) - the same
#     # recording URLs the STT service was given. The frontend call-detail page
#     # (feature 2.3) plays these directly and seeks to each parameter's
#     # evidence start_time so a reviewer can jump straight to the moment a
#     # score was decided, instead of listening to the whole call.
#     call = db.execute(
#         """SELECT c.*, a.name AS agent_name FROM calls c JOIN agents a ON a.id = c.agent_id
#            WHERE c.id = ?""",
#         (call_id,),
#     ).fetchone()
#     if not call:
#         raise HTTPException(status_code=404, detail="Call not found")
#     scores = db.execute(
#         """SELECT p.label AS parameter, s.status, s.reason, s.evidence, s.start_time, s.end_time,
#                   s.raw_score, s.score
#            FROM call_scores s JOIN parameters p ON p.id = s.parameter_id
#            WHERE s.call_id = ? ORDER BY p.id""",
#         (call_id,),
#     ).fetchall()
#     fatal = db.execute("SELECT * FROM fatal_checks WHERE call_id = ?", (call_id,)).fetchall()
#     # print(dict(call), [dict(r) for r in scores], [dict(r) for r in fatal])
#     call_dict = dict(call)
#     print(call_dict)
#     if call_dict.get("recording_base"):
#         call_dict["mono_url"] = f"{RECORDINGS_PLAYBACK_BASE_URL.rstrip('/')}/{call_dict['recording_base']}"
#     else:
#         call_dict["mono_url"] = None
#     print(f" after adding the mono url\n{call_dict}")
#     return {
#         "call": dict(call_dict),
#         "parameters": [dict(r) for r in scores],
#         "fatal_checks": [dict(r) for r in fatal],
#     }

# @app.get("/api/health")
# def health(db = Depends(get_db)):
#     """Hits the DB too, so this actually tells you whether MySQL is reachable,
#     not just whether the FastAPI process is up."""
#     db.execute("SELECT 1")
#     return {"status": "ok"}

# ===============================w6==========================================
# import io
# import os
# import shutil
# import tempfile
# from datetime import datetime, timedelta
# from typing import Optional

# import pymysql
# from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Request, UploadFile
# from fastapi.middleware.cors import CORSMiddleware
# from fastapi.responses import JSONResponse, StreamingResponse
# from pydantic import BaseModel

# import hashlib

# # from app4_.backend.config import RECOR, RECORDINGS_PLAYBACK_BASE_URLDINGS_PLAYBACK_BASE_URL
# from config import PARAMETERS, RECORDINGS_BASE_URL, RECORDINGS_PLAYBACK_BASE_URL, SLM_API_URL
# from db import get_conn, init_db
# from pipeline import process_manifest, save_manifest

# app = FastAPI(title="Athena QA Dashboard")

# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=[
#         "http://localhost:5173",
#         "http://127.0.0.1:5173",
#     ],
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )

# # ------------------------------------------------------------------
# # IMPORTANT FIX: init_db() used to run unguarded at import time. If MySQL
# # was unreachable (wrong host/user/password, or the server just wasn't up
# # yet), that raised an exception WHILE the module was loading - before
# # FastAPI/CORSMiddleware even finished being built. uvicorn's worker
# # process died right there, so nothing was listening on the port at all.
# #
# # That's exactly what showed up in the browser as CORS errors + net::ERR_FAILED:
# # there was no server to send a response (let alone CORS headers), so Chrome
# # reports it as a blocked/failed request instead of a clean 500.
# #
# # Fix: catch the startup failure, log it clearly, and let the app come up
# # anyway. Every DB-touching request will then fail with a clean 503 JSON
# # response (see exception handler below) that DOES carry CORS headers,
# # instead of the whole server being unreachable.
# # ------------------------------------------------------------------
# try:
#     init_db()
# except Exception as e:
#     print("=" * 70)
#     print("[STARTUP WARNING] Could not connect to MySQL / initialize schema.")
#     print(f"[STARTUP WARNING] {type(e).__name__}: {e}")
#     print("[STARTUP WARNING] Check MYSQL_HOST / MYSQL_PORT / MYSQL_USER / "
#           "MYSQL_PASSWORD / MYSQL_DATABASE env vars and that MySQL is running.")
#     print("[STARTUP WARNING] The API is still starting so you get clean 503s "
#           "instead of connection failures - fix your DB config and restart.")
#     print("=" * 70)


# @app.exception_handler(pymysql.err.Error)
# async def mysql_error_handler(request: Request, exc: pymysql.err.Error):
#     return JSONResponse(
#         status_code=503,
#         content={"detail": f"Database unavailable: {exc}"},
#     )


# def get_db():
#     conn = get_conn()
#     try:
#         yield conn
#     finally:
#         conn.close()


# def hash_pw(pw: str) -> str:
#     return hashlib.sha256(pw.encode()).hexdigest()


# # ---------- Schemas ----------
# class LoginRequest(BaseModel):
#     username: str
#     password: str


# class LoginResponse(BaseModel):
#     token: str
#     username: str
#     role: str


# # ---------- Auth ----------
# @app.post("/api/login", response_model=LoginResponse)
# def login(req: LoginRequest, db = Depends(get_db)):
#     row = db.execute(
#         "SELECT * FROM users WHERE username = ? AND password_hash = ?",
#         (req.username, hash_pw(req.password)),
#     ).fetchone()
#     if not row:
#         raise HTTPException(status_code=401, detail="Invalid username or password")
#     token = hashlib.sha256(f"{req.username}{datetime.utcnow()}".encode()).hexdigest()
#     return LoginResponse(token=token, username=row["username"], role=row["role"])


# class ChangePasswordRequest(BaseModel):
#     username: str
#     current_password: str
#     new_password: str


# @app.post("/api/change-password")
# def change_password(req: ChangePasswordRequest, db = Depends(get_db)):
#     # There's no session/bearer-token check anywhere else in this app yet
#     # (login just hands back an unverified token) - so identity here is
#     # proven the same way login proves it: username + correct current
#     # password. If you add real session auth later, swap this for a
#     # Depends(current_user) check instead of trusting the request body.
#     if len(req.new_password) < 8:
#         raise HTTPException(status_code=400, detail="New password must be at least 8 characters")

#     row = db.execute(
#         "SELECT * FROM users WHERE username = ? AND password_hash = ?",
#         (req.username, hash_pw(req.current_password)),
#     ).fetchone()
#     if not row:
#         raise HTTPException(status_code=401, detail="Current password is incorrect")

#     db.execute(
#         "UPDATE users SET password_hash = ? WHERE username = ?",
#         (hash_pw(req.new_password), req.username),
#     )
#     db.commit()
#     return {"status": "ok"}


# # ---------- Helpers ----------
# def date_range_defaults(start: Optional[str], end: Optional[str]):
#     if not end:
#         end = datetime.utcnow().strftime("%Y-%m-%d")
#     if not start:
#         start = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
#     return start, end

# w6
# # Whitelist of SQL fragments for the 5 clickable dashboard stat cards (feature 2).
# # Kept as a fixed dict (not built from the path param) so there's no injection risk
# # even though it gets interpolated straight into the query string below.
# CATEGORY_FILTERS = {
#     "total": "1=1",
#     "audited": "c.status = 'audited'",
#     "pending": "c.status IN ('pending','transcribing','analyzing')",
#     "positive": "c.sentiment = 'positive'",
#     "negative": "c.sentiment = 'negative'",
#     # Agent-detail summary cards (feature: clickable Total/Average/Good/Poor).
#     # "Total Calls" and "Average Call" both draw from the same audited
#     # population that agent_detail()'s stats are computed over, so they
#     # share the "audited" filter - only the label shown differs.
#     "good": "c.overall_quality = 'good'",
#     "poor": "c.overall_quality = 'poor'",
#     # Disposition-based (feature: below the funnel). Case-insensitive since
#     # the manifest's call_end_type_name casing isn't guaranteed consistent
#     # (we've seen "CALLBACK" in real data).
#     "disp_not_eligible": "UPPER(c.call_end_type_name) = 'NOT ELIGIBLE'",
#     "disp_not_interested": "UPPER(c.call_end_type_name) = 'NOT INTERESTED'",
#     "disp_callback": "UPPER(c.call_end_type_name) = 'CALLBACK'",
#     "disp_short_hangup": "UPPER(c.call_end_type_name) = 'SHORT HANGUP'",
#     "disp_lead": "UPPER(c.call_end_type_name) = 'LEAD'",
#     "disp_eligible": "UPPER(c.call_end_type_name) = 'ELIGIBLE'",
#     "disp_interested": "UPPER(c.call_end_type_name) = 'INTERESTED'",
#     # Correct/Wrong: actual (manifest) vs predicted (SLM) disposition, compared
#     # once and cached on the row at process time (pipeline.py) rather than
#     # here - disposition_match is NULL (excluded from both) for any call
#     # missing either side of the comparison, so this filter alone is enough,
#     # no extra "and both sides present" clause needed.
#     "disp_correct": "c.disposition_match = 1",
#     "disp_wrong": "c.disposition_match = 0",
# }

# # Labels + ordering for the disposition buttons row on the dashboard.
# # in a sequesnce the type will be selected disposition_value
# DISPOSITION_CATEGORIES = [
#     ("disp_not_eligible", "Not Eligible", "NOT ELIGIBLE"),
#     ("disp_not_interested", "Not Interested", "NOT INTERESTED"),
#     ("disp_callback", "Callback", "CALLBACK"),
#     # ("disp_short_hangup", "Short Hangup", "SHORT HANGUP"),
#     ("disp_short_hangup", "Call disconnected", "SHORT HANGUP"),
#     ("disp_lead", "Lead", "LEAD"),
#     # ("disp_eligible", "Eligible", "ELIGIBLE"),
#     ("disp_interested", "Interested", "INTERESTED"),
# ]


# def category_filter_sql(category: str) -> str:
#     if category not in CATEGORY_FILTERS:
#         raise HTTPException(
#             status_code=400,
#             detail=f"Unknown category '{category}'. Must be one of: {', '.join(CATEGORY_FILTERS)}",
#         )
#     return CATEGORY_FILTERS[category]


# # ---------- Dashboard summary (Section 2 top cards) ----------
# @app.get("/api/dashboard/summary")
# def dashboard_summary(
#     start: Optional[str] = None,
#     end: Optional[str] = None,
#     db = Depends(get_db),
# ):
#     start, end = date_range_defaults(start, end)
#     row = db.execute(
#         """
#         SELECT
#           COUNT(*) AS total_calls,
#           SUM(CASE WHEN status = 'audited' THEN 1 ELSE 0 END) AS audited_calls,
#           SUM(CASE WHEN status IN ('pending','transcribing','analyzing') THEN 1 ELSE 0 END) AS pending_calls,
#           SUM(CASE WHEN sentiment = 'positive' THEN 1 ELSE 0 END) AS positive_calls,
#           SUM(CASE WHEN sentiment = 'negative' THEN 1 ELSE 0 END) AS negative_calls
#         FROM calls
#         WHERE call_date IS NOT NULL AND date(call_date) BETWEEN date(?) AND date(?)
#         """,
#         (start, end),
#     ).fetchone()
#     return {
#         "start": start,
#         "end": end,
#         "total_calls": row["total_calls"] or 0,
#         "audited_calls": row["audited_calls"] or 0,
#         "pending_calls": row["pending_calls"] or 0,
#         "positive_calls": row["positive_calls"] or 0,
#         "negative_calls": row["negative_calls"] or 0,
#     }


# # ---------- Disposition counts (feature: buttons below the funnel) ----------
# # One grouped query for the manifest-based dispositions (not one query per
# # button), plus one small aggregate for Correct/Wrong (disposition_match),
# # then mapped onto the known categories. Drill-down reuses the exact same
# # /api/dashboard/category/{category}/... endpoints as the 5 stat cards do -
# # "disp_not_eligible" etc, and now "disp_correct"/"disp_wrong" too, are just
# # more entries in CATEGORY_FILTERS above.
# @app.get("/api/dashboard/dispositions")
# def dashboard_dispositions(
#     start: Optional[str] = None,
#     end: Optional[str] = None,
#     db = Depends(get_db),
# ):
#     start, end = date_range_defaults(start, end)
#     rows = db.execute(
#         """
#         SELECT UPPER(call_end_type_name) AS disp, COUNT(*) AS n
#         FROM calls
#         WHERE call_date IS NOT NULL AND date(call_date) BETWEEN date(?) AND date(?)
#           AND call_end_type_name IS NOT NULL AND call_end_type_name != ''
#         GROUP BY UPPER(call_end_type_name)
#         """,
#         (start, end),
#     ).fetchall()
#     counts = {r["disp"]: r["n"] for r in rows}

#     match_row = db.execute(
#         """
#         SELECT
#           SUM(CASE WHEN disposition_match = 1 THEN 1 ELSE 0 END) AS correct_n,
#           SUM(CASE WHEN disposition_match = 0 THEN 1 ELSE 0 END) AS wrong_n
#         FROM calls
#         WHERE call_date IS NOT NULL AND date(call_date) BETWEEN date(?) AND date(?)
#         """,
#         (start, end),
#     ).fetchone()

#     dispositions = [
#         {"category": cat, "label": label, "count": counts.get(match_value, 0)}
#         for cat, label, match_value in DISPOSITION_CATEGORIES
#     ]
#     # dispositions.append({"category": "disp_correct", "label": "Correct", "count": match_row["correct_n"] or 0})
#     dispositions.append({"category": "disp_wrong", "label": "Wrong", "count": match_row["wrong_n"] or 0})

#     return {
#         "start": start,
#         "end": end,
#         "dispositions": dispositions,
#     }


# # ---------- Parameter-wise average scores (graph) ----------
# @app.get("/api/dashboard/parameters")
# def dashboard_parameters(
#     start: Optional[str] = None,
#     end: Optional[str] = None,
#     db = Depends(get_db),
# ):
#     start, end = date_range_defaults(start, end)
#     rows = db.execute(
#         """
#         SELECT p.label AS parameter, ROUND(AVG(s.score), 1) AS avg_score, COUNT(*) AS n
#         FROM call_scores s
#         JOIN parameters p ON p.id = s.parameter_id
#         JOIN calls c ON c.id = s.call_id
#         WHERE c.call_date IS NOT NULL AND date(c.call_date) BETWEEN date(?) AND date(?)
#         GROUP BY p.id, p.label
#         ORDER BY p.id
#         """,
#         (start, end),
#     ).fetchall()
#     return {
#         "start": start,
#         "end": end,
#         "parameters": [
#             {"parameter": r["parameter"], "avg_score": r["avg_score"] or 0, "sample_size": r["n"]}
#             for r in rows
#         ],
#     }


# # ---------- Parameter success-rate funnel (feature 1) ----------
# # Different from /api/dashboard/parameters above (which averages the 0-100
# # normalized score). This instead answers "out of every AUDITED call, what
# # % actually PASSED (status=true) this parameter?" - sorted highest-first so
# # it reads top-to-bottom like a funnel: parameters almost everyone nails at
# # the top, parameters agents struggle with at the bottom.
# #
# # success_pct = (# audited calls where that parameter's status = TRUE)
# #               / (total audited calls in range) * 100
# #
# # Two queries total (not N+1): one scalar for the constant denominator
# # (total audited calls), one GROUP BY for all 16 numerators together.
# @app.get("/api/dashboard/parameter-funnel")
# def parameter_funnel(
#     start: Optional[str] = None,
#     end: Optional[str] = None,
#     db = Depends(get_db),
# ):
#     start, end = date_range_defaults(start, end)

#     total_row = db.execute(
#         """
#         SELECT COUNT(*) AS total_audited
#         FROM calls
#         WHERE status = 'audited' AND call_date IS NOT NULL
#           AND date(call_date) BETWEEN date(?) AND date(?)
#         """,
#         (start, end),
#     ).fetchone()
#     total_audited = total_row["total_audited"] or 0

#     rows = db.execute(
#         """
#         SELECT p.id AS parameter_id, p.label AS parameter,
#                SUM(CASE WHEN s.status = 1 THEN 1 ELSE 0 END) AS success_count,
#                COUNT(*) AS sample_size
#         FROM call_scores s
#         JOIN parameters p ON p.id = s.parameter_id
#         JOIN calls c ON c.id = s.call_id
#         WHERE c.status = 'audited' AND c.call_date IS NOT NULL
#           AND date(c.call_date) BETWEEN date(?) AND date(?)
#         GROUP BY p.id, p.label
#         ORDER BY p.id
#         """,
#         (start, end),
#     ).fetchall()

#     funnel = []
#     for r in rows:
#         success_count = r["success_count"] or 0
#         pct = round((success_count / total_audited) * 100, 1) if total_audited else 0.0
#         funnel.append({
#             "parameter_id": r["parameter_id"],
#             "parameter": r["parameter"],
#             "success_count": success_count,
#             "sample_size": r["sample_size"],
#             "success_pct": pct,
#         })

#     funnel.sort(key=lambda x: x["success_pct"], reverse=True)
#     print("*funnel printin"*10)
#     print(funnel)

#     return {
#         "start": start,
#         "end": end,
#         "total_audited_calls": total_audited,
#         "parameters": funnel,
#     }


# # @app.get("/api/dashboard/parameters/{parameter_name:path}/agents")
# # ---------- Agents belonging to a parameter (click on bar) ----------
# @app.get("/api/dashboard/parameters/{parameter_name:path}/agents")
# def agents_for_parameter(
#     parameter_name: str,
#     start: Optional[str] = None,
#     end: Optional[str] = None,
#     db = Depends(get_db),
# ):
#     start, end = date_range_defaults(start, end)
#     rows = db.execute(
#         """
#         SELECT a.id AS agent_id,
#                a.name AS agent_name,
#                a.team,
#                ROUND(AVG(s.score), 1) AS avg_score,
#                COUNT(*) AS calls_scored
#         FROM call_scores s
#         JOIN parameters p ON p.id = s.parameter_id
#         JOIN calls c ON c.id = s.call_id
#         JOIN agents a ON a.id = c.agent_id
#         WHERE p.label = ?
#           AND c.call_date IS NOT NULL
#           AND date(c.call_date) BETWEEN date(?) AND date(?)
#         GROUP BY a.id, a.name, a.team
#         ORDER BY avg_score ASC
#         """,
#         (parameter_name, start, end),
#     ).fetchall()
#     if not rows:
#         exists = db.execute("SELECT 1 FROM parameters WHERE label = ?", (parameter_name,)).fetchone()
#         if not exists:
#             raise HTTPException(status_code=404, detail="Parameter not found")
#     return {
#         "parameter": parameter_name,
#         "start": start,
#         "end": end,
#         "agents": [dict(r) for r in rows],
#     }


# # ---------- Agent list (basic) ----------
# @app.get("/api/agents")
# def list_agents(db = Depends(get_db)):
#     rows = db.execute("SELECT id, name, team FROM agents ORDER BY name").fetchall()
#     return {"agents": [dict(r) for r in rows]}


# # ---------- Agent detail (Section 3) ----------
# @app.get("/api/agents/{agent_id}")
# def agent_detail(
#     agent_id: int,
#     start: Optional[str] = None,
#     end: Optional[str] = None,
#     db = Depends(get_db),
# ):
#     start, end = date_range_defaults(start, end)
#     agent = db.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
#     if not agent:
#         raise HTTPException(status_code=404, detail="Agent not found")
# # ROUND(AVG(overall_score), 1) AS average_call_score
#     calls_row = db.execute(
#         """
#         SELECT COUNT(*) AS total_calls,
#                SUM(CASE WHEN overall_quality = 'good' THEN 1 ELSE 0 END) AS good_calls,
#                SUM(CASE WHEN overall_quality = 'poor' THEN 1 ELSE 0 END) AS poor_calls,
#                SUM(CASE WHEN overall_quality = 'average' THEN 1 ELSE 0 END) AS average_call_score
               
#         FROM calls
#         WHERE agent_id = ? AND status = 'audited'
#           AND call_date IS NOT NULL AND date(call_date) BETWEEN date(?) AND date(?)
#         """,
#         (agent_id, start, end),
#     ).fetchone()

#     param_rows = db.execute(
#         """
#         SELECT p.label AS parameter, ROUND(AVG(s.score),1) AS avg_score
#         FROM call_scores s
#         JOIN parameters p ON p.id = s.parameter_id
#         JOIN calls c ON c.id = s.call_id
#         WHERE c.agent_id = ? AND c.call_date IS NOT NULL AND date(c.call_date) BETWEEN date(?) AND date(?)
#         GROUP BY p.id, p.label
#         ORDER BY p.id
#         """,
#         (agent_id, start, end),
#     ).fetchall()

#     params = [dict(r) for r in param_rows]
#     if params:
#         sorted_params = sorted(params, key=lambda x: x["avg_score"] or 0, reverse=True)
#         strengths = [p["parameter"] for p in sorted_params[:3]]
#         improvements = [p["parameter"] for p in sorted_params[-3:]]
#     else:
#         strengths, improvements = [], []

#     return {
#         "agent": {"id": agent["id"], "name": agent["name"], "team": agent["team"]},
#         "start": start,
#         "end": end,
#         "total_calls": calls_row["total_calls"] or 0,
#         "average_call_score": calls_row["average_call_score"] or 0,
#         "good_calls": calls_row["good_calls"] or 0,
#         "poor_calls": calls_row["poor_calls"] or 0,
#         "parameters": params,
#         "overall_summary": {
#             "strengths": strengths,
#             "improvements": improvements,
#         },
#     }


# # ==================================================================
# # Feature 2: clickable stat cards -> agents -> that agent's calls.
# # category is one of the CATEGORY_FILTERS keys (total/audited/pending/positive/negative),
# # matching whichever stat card was clicked on the dashboard.
# # ==================================================================

# @app.get("/api/dashboard/category/{category}/agents")
# def category_agents(
#     category: str,
#     start: Optional[str] = None,
#     end: Optional[str] = None,
#     db = Depends(get_db),
# ):
#     """2.1 - agents ranked by their average score, scoped to the clicked category."""
#     where_clause = category_filter_sql(category)
#     start, end = date_range_defaults(start, end)
#     rows = db.execute(
#         f"""
#         SELECT a.id AS agent_id, a.name AS agent_name, a.team,
#                ROUND(AVG(c.overall_score), 1) AS avg_score,
#                COUNT(*) AS call_count
#         FROM calls c
#         JOIN agents a ON a.id = c.agent_id
#         WHERE {where_clause}
#           AND c.call_date IS NOT NULL AND date(c.call_date) BETWEEN date(?) AND date(?)
#         GROUP BY a.id, a.name, a.team
#         ORDER BY avg_score DESC
#         """,
#         (start, end),
#     ).fetchall()
#     return {
#         "category": category,
#         "start": start,
#         "end": end,
#         "agents": [dict(r) for r in rows],
#     }


# @app.get("/api/dashboard/category/{category}/agents/{agent_id}/calls")
# def category_agent_calls(
#     category: str,
#     agent_id: int,
#     start: Optional[str] = None,
#     end: Optional[str] = None,
#     db = Depends(get_db),
# ):
#     """2.2 - that agent's individual calls (with score) within the clicked category."""
#     where_clause = category_filter_sql(category)
#     start, end = date_range_defaults(start, end)
#     agent = db.execute("SELECT id, name, team FROM agents WHERE id = ?", (agent_id,)).fetchone()
#     if not agent:
#         raise HTTPException(status_code=404, detail="Agent not found")

#     calls = db.execute(
#         f"""
#         SELECT c.id, c.call_number, c.call_start_time, c.call_date, c.status,
#                c.sentiment, c.overall_quality, c.overall_score, c.verdict, c.recording_base
#         FROM calls c
#         WHERE c.agent_id = ? AND {where_clause}
#           AND c.call_date IS NOT NULL AND date(c.call_date) BETWEEN date(?) AND date(?)
#         ORDER BY c.call_date DESC, c.id DESC
#         """,
#         (agent_id, start, end),
#     ).fetchall()
#     return {
#         "category": category,
#         "agent": dict(agent),
#         "start": start,
#         "end": end,
#         "calls": [dict(r) for r in calls],
#     }


# # # ==================================================================
# # # Feature 3: top-10 agents leaderboard (right-hand sidebar box)
# # # ==================================================================

# @app.get("/api/dashboard/top-agents")
# def top_agents(
#     start: Optional[str] = None,
#     end: Optional[str] = None,
#     limit: int = 10,
#     search: Optional[str] = None,
#     db = Depends(get_db),
# ):
#     start, end = date_range_defaults(start, end)
#     # a search should be able to surface more than 10 matches; the sidebar's
#     # default (unsearched) call always passes limit=10 explicitly, this cap
#     # just stops a search from being unbounded.
#     limit = max(1, min(limit, 50))

#     where = [
#         "c.status = 'audited'",
#         "c.call_date IS NOT NULL",
#         "date(c.call_date) BETWEEN date(?) AND date(?)",
#     ]
#     params = [start, end]
#     if search:
#         where.append("a.name LIKE ?")
#         params.append(f"%{search}%")
#     where_sql = " AND ".join(where)

#     rows = db.execute(
#         f"""
#         SELECT a.id AS agent_id, a.name AS agent_name, a.team,
#                ROUND(AVG(c.overall_score), 1) AS avg_score,
#                COUNT(*) AS call_count
#         FROM calls c
#         JOIN agents a ON a.id = c.agent_id
#         WHERE {where_sql}
#         GROUP BY a.id, a.name, a.team
#         ORDER BY avg_score DESC
#         LIMIT ?
#         """,
#         tuple(params) + (limit,),
#     ).fetchall()
#     return {
#         "start": start,
#         "end": end,
#         "search": search,
#         "agents": [dict(r) for r in rows],
#     }



# REPORT_SORTS = {
#     "call_date_desc": "c.call_date DESC, c.id DESC",
#     "call_date_asc": "c.call_date ASC, c.id ASC",
#     "score_desc": "c.overall_score DESC",
#     "score_asc": "c.overall_score ASC",
#     "agent_name": "a.name ASC, c.call_date DESC",
# }


# def _report_pivot_cols():
#     # One column per parameter via conditional aggregation (MySQL has no PIVOT).
#     # param_key values come from our own hardcoded PARAMETERS config, not user
#     # input, so interpolating them straight into the SQL text is safe here.
#     return ",\n               ".join(
#         f"MAX(CASE WHEN p.param_key = '{key}' THEN s.score END) AS `{key}`"
#         for key, _ in PARAMETERS
#     )


# def _report_filters(agent_id, disposition_status, actual_disposition, predicted_disposition, start, end):
#     """Builds the WHERE clause + params shared by the report's JSON endpoint
#     AND its CSV export below - one place to add a filter so the download can
#     never drift out of sync with what's on screen."""
#     where = [
#         "c.status = 'audited'",
#         "c.call_date IS NOT NULL",
#         "date(c.call_date) BETWEEN date(?) AND date(?)",
#     ]
#     params = [start, end]
#     if agent_id:
#         where.append("c.agent_id = ?")
#         params.append(agent_id)
#     if disposition_status == "correct":
#         where.append("c.disposition_match = 1")
#     elif disposition_status == "incorrect":
#         where.append("c.disposition_match = 0")
#     if actual_disposition:
#         where.append("UPPER(c.call_end_type_name) = UPPER(?)")
#         params.append(actual_disposition)
#     if predicted_disposition:
#         where.append("UPPER(c.predicted_disposition) = UPPER(?)")
#         params.append(predicted_disposition)
#     return " AND ".join(where), params


# @app.get("/api/reports/agent-scores")
# def agent_scores_report(
#     agent_id: Optional[int] = None,
#     disposition_status: Optional[str] = None,  # "correct" | "incorrect"
#     actual_disposition: Optional[str] = None,
#     predicted_disposition: Optional[str] = None,
#     start: Optional[str] = None,
#     end: Optional[str] = None,
#     page: int = 1,
#     page_size: int = 25,
#     sort: str = "call_date_desc",
#     db = Depends(get_db),
# ):
#     start, end = date_range_defaults(start, end)
#     page = max(1, page)
#     page_size = max(1, min(page_size, 200))  # cap it - this is a report table, not a data dump endpoint
#     offset = (page - 1) * page_size
#     order_sql = REPORT_SORTS.get(sort, REPORT_SORTS["call_date_desc"])

#     where_sql, params = _report_filters(
#         agent_id, disposition_status, actual_disposition, predicted_disposition, start, end
#     )

#     total_row = db.execute(
#         f"SELECT COUNT(*) AS n FROM calls c JOIN agents a ON a.id = c.agent_id WHERE {where_sql}",
#         tuple(params),
#     ).fetchone()
#     total = total_row["n"] or 0

#     rows = db.execute(
#         f"""
#         SELECT c.id AS call_id, a.id AS agent_id, a.name AS agent_name,
#                c.recording_base, c.call_date, c.overall_score, c.verdict, c.overall_quality,
#                c.call_end_type_name AS actual_disposition, c.predicted_disposition, c.disposition_match,
#                {_report_pivot_cols()}
#         FROM calls c
#         JOIN agents a ON a.id = c.agent_id
#         LEFT JOIN call_scores s ON s.call_id = c.id
#         LEFT JOIN parameters p ON p.id = s.parameter_id
#         WHERE {where_sql}
#         GROUP BY c.id, a.id, a.name, c.recording_base, c.call_date,
#                  c.overall_score, c.verdict, c.overall_quality,
#                  c.call_end_type_name, c.predicted_disposition, c.disposition_match
#         ORDER BY {order_sql}
#         LIMIT ? OFFSET ?
#         """,
#         tuple(params) + (page_size, offset),
#     ).fetchall()

#     return {
#         "start": start,
#         "end": end,
#         "agent_id": agent_id,
#         "page": page,
#         "page_size": page_size,
#         "total": total,
#         "columns": [{"key": key, "label": label} for key, label in PARAMETERS],
#         "rows": [dict(r) for r in rows],
#     }


# # Dropdown option sources for the report's disposition filters - distinct
# # values actually present in the data (within the date range), not a
# # hardcoded list, so anything the manifest or SLM ever produces shows up.
# @app.get("/api/reports/disposition-options")
# def report_disposition_options(
#     start: Optional[str] = None,
#     end: Optional[str] = None,
#     db = Depends(get_db),
# ):
#     start, end = date_range_defaults(start, end)
#     date_filter = "c.call_date IS NOT NULL AND date(c.call_date) BETWEEN date(?) AND date(?)"

#     actual_rows = db.execute(
#         f"""SELECT DISTINCT call_end_type_name AS v FROM calls c
#             WHERE {date_filter} AND call_end_type_name IS NOT NULL AND call_end_type_name != ''
#             ORDER BY v""",
#         (start, end),
#     ).fetchall()
#     predicted_rows = db.execute(
#         f"""SELECT DISTINCT predicted_disposition AS v FROM calls c
#             WHERE {date_filter} AND predicted_disposition IS NOT NULL AND predicted_disposition != ''
#             ORDER BY v""",
#         (start, end),
#     ).fetchall()

#     return {
#         "actual": [r["v"] for r in actual_rows],
#         "predicted": [r["v"] for r in predicted_rows],
#     }


# # CSV download - same filters/sort as the JSON report above (via the shared
# # _report_filters/_report_pivot_cols helpers), but no pagination: exports
# # every row matching the current filters, exactly what's "in" the filtered
# # report, not just whichever page happens to be on screen.
# @app.get("/api/reports/agent-scores/export")
# def agent_scores_report_export(
#     agent_id: Optional[int] = None,
#     disposition_status: Optional[str] = None,
#     actual_disposition: Optional[str] = None,
#     predicted_disposition: Optional[str] = None,
#     start: Optional[str] = None,
#     end: Optional[str] = None,
#     sort: str = "call_date_desc",
#     db = Depends(get_db),
# ):
#     start, end = date_range_defaults(start, end)
#     order_sql = REPORT_SORTS.get(sort, REPORT_SORTS["call_date_desc"])
#     where_sql, params = _report_filters(
#         agent_id, disposition_status, actual_disposition, predicted_disposition, start, end
#     )

#     rows = db.execute(
#         f"""
#         SELECT a.name AS agent_name, c.recording_base, c.call_date, c.overall_score,
#                c.verdict, c.overall_quality,
#                c.call_end_type_name AS actual_disposition, c.predicted_disposition, c.disposition_match,
#                {_report_pivot_cols()}
#         FROM calls c
#         JOIN agents a ON a.id = c.agent_id
#         LEFT JOIN call_scores s ON s.call_id = c.id
#         LEFT JOIN parameters p ON p.id = s.parameter_id
#         WHERE {where_sql}
#         GROUP BY c.id, a.id, a.name, c.recording_base, c.call_date,
#                  c.overall_score, c.verdict, c.overall_quality,
#                  c.call_end_type_name, c.predicted_disposition, c.disposition_match
#         ORDER BY {order_sql}
#         """,
#         tuple(params),
#     ).fetchall()

#     def disposition_status_label(match):
#         if match == 1:
#             return "Correct"
#         if match == 0:
#             return "Incorrect"
#         return ""

#     buf = io.StringIO()
#     writer = csv.writer(buf)
#     header = ["Agent", "Recording", "Call Date", "Overall Score", "Verdict", "Quality",
#               "Actual Disposition", "Predicted Disposition", "Disposition Status"]
#     header += [label for _, label in PARAMETERS]
#     writer.writerow(header)
#     for r in rows:
#         row = [
#             r["agent_name"], r["recording_base"], r["call_date"], r["overall_score"],
#             r["verdict"], r["overall_quality"],
#             r["actual_disposition"], r["predicted_disposition"], disposition_status_label(r["disposition_match"]),
#         ]
#         row += [r[key] for key, _ in PARAMETERS]
#         writer.writerow(row)

#     filename = f"agent-report_{start}_to_{end}.csv"
#     return StreamingResponse(
#         iter([buf.getvalue()]),
#         media_type="text/csv",
#         headers={"Content-Disposition": f'attachment; filename="{filename}"'},
#     )


# # ==================================================================
# # Manifest upload + STT/SLM pipeline
# # ==================================================================

# @app.post("/api/manifest/upload")
# async def upload_manifest(
#     background_tasks: BackgroundTasks,
#     file: UploadFile = File(...),
#     recording_base_url: Optional[str] = None,
#     auto_process: bool = True,
# ):
#     if not file.filename.lower().endswith((".xlsx", ".xlsm")):
#         raise HTTPException(status_code=400, detail="Please upload an .xlsx manifest file")

#     with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
#         shutil.copyfileobj(file.file, tmp)
#         tmp_path = tmp.name

#     try:
#         manifest_id, row_count = save_manifest(
#             tmp_path, file.filename, recording_base_url or RECORDINGS_BASE_URL
#         )
#         print(f"Manifest uploaded: {manifest_id}, Rows: {row_count}")
#     finally:
#         os.unlink(tmp_path)

#     if auto_process:
#         print("Triggering background processing for manifest:", manifest_id)
#         background_tasks.add_task(process_manifest, manifest_id)

#     return {"manifest_id": manifest_id, "rows_loaded": row_count, "processing": auto_process}


# @app.post("/api/manifest/{manifest_id}/process")
# def trigger_processing(manifest_id: int, background_tasks: BackgroundTasks, db = Depends(get_db)):
#     row = db.execute("SELECT id FROM manifests WHERE id = ?", (manifest_id,)).fetchone()
#     if not row:
#         raise HTTPException(status_code=404, detail="Manifest not found")
#     background_tasks.add_task(process_manifest, manifest_id)
#     return {"manifest_id": manifest_id, "processing": True}


# @app.get("/api/manifest")
# def list_manifests(db = Depends(get_db)):
#     rows = db.execute("SELECT * FROM manifests ORDER BY id DESC").fetchall()
#     return {"manifests": [dict(r) for r in rows]}


# @app.get("/api/manifest/{manifest_id}")
# def manifest_status(manifest_id: int, db = Depends(get_db)):
#     manifest = db.execute("SELECT * FROM manifests WHERE id = ?", (manifest_id,)).fetchone()
#     if not manifest:
#         raise HTTPException(status_code=404, detail="Manifest not found")
#     calls = db.execute(
#         """SELECT c.id, c.call_number, c.recording_base, c.status, c.error_message,
#                   c.overall_score, c.verdict, a.name AS agent_name
#            FROM calls c JOIN agents a ON a.id = c.agent_id
#            WHERE c.manifest_id = ? ORDER BY c.id""",
#         (manifest_id,),
#     ).fetchall()
#     return {"manifest": dict(manifest), "calls": [dict(r) for r in calls]}


# # @app.get("/api/calls/{call_id}/transcript")
# # def call_transcript(call_id: int, db = Depends(get_db)):
# #     row = db.execute("SELECT * FROM transcripts WHERE call_id = ?", (call_id,)).fetchone()
# #     if not row:
# #         raise HTTPException(status_code=404, detail="No transcript for this call yet")
# #     return dict(row)


# # @app.get("/api/calls/{call_id}")
# # def call_detail(call_id: int, db = Depends(get_db)):
# #     # `c.*` already includes rx_path (IN leg) and tx_path (OUT leg) - the same
# #     # recording URLs the STT service was given. The frontend call-detail page
# #     # (feature 2.3) plays these directly and seeks to each parameter's
# #     # evidence start_time so a reviewer can jump straight to the moment a
# #     # score was decided, instead of listening to the whole call.
# #     call = db.execute(
# #         """SELECT c.*, a.name AS agent_name FROM calls c JOIN agents a ON a.id = c.agent_id
# #            WHERE c.id = ?""",
# #         (call_id,),
# #     ).fetchone()
# #     if not call:
# #         raise HTTPException(status_code=404, detail="Call not found")
# #     scores = db.execute(
# #         """SELECT p.label AS parameter, s.status, s.reason, s.evidence, s.start_time, s.end_time,
# #                   s.raw_score, s.score
# #            FROM call_scores s JOIN parameters p ON p.id = s.parameter_id
# #            WHERE s.call_id = ? ORDER BY p.id""",
# #         (call_id,),
# #     ).fetchall()
# #     fatal = db.execute("SELECT * FROM fatal_checks WHERE call_id = ?", (call_id,)).fetchall()
# #     return {
# #         "call": dict(call),
# #         "parameters": [dict(r) for r in scores],
# #         "fatal_checks": [dict(r) for r in fatal],
# #     }

# @app.get("/api/calls/{call_id}/transcript")
# def call_transcript(call_id: int, db = Depends(get_db)):
#     row = db.execute("SELECT * FROM transcripts WHERE call_id = ?", (call_id,)).fetchone()
#     if not row:
#         raise HTTPException(status_code=404, detail="No transcript for this call yet")
#     return dict(row)


# @app.get("/api/calls/{call_id}")
# def call_detail(call_id: int, db = Depends(get_db)):
#     print(f"Fetching detail for call ID: {call_id}")
#     # `c.*` already includes rx_path (IN leg) and tx_path (OUT leg) - the same
#     # recording URLs the STT service was given. The frontend call-detail page
#     # (feature 2.3) plays these directly and seeks to each parameter's
#     # evidence start_time so a reviewer can jump straight to the moment a
#     # score was decided, instead of listening to the whole call.
#     call = db.execute(
#         """SELECT c.*, a.name AS agent_name FROM calls c JOIN agents a ON a.id = c.agent_id
#            WHERE c.id = ?""",
#         (call_id,),
#     ).fetchone()
#     if not call:
#         raise HTTPException(status_code=404, detail="Call not found")
#     scores = db.execute(
#         """SELECT p.label AS parameter, s.status, s.reason, s.evidence, s.start_time, s.end_time,
#                   s.raw_score, s.score
#            FROM call_scores s JOIN parameters p ON p.id = s.parameter_id
#            WHERE s.call_id = ? ORDER BY p.id""",
#         (call_id,),
#     ).fetchall()
#     fatal = db.execute("SELECT * FROM fatal_checks WHERE call_id = ?", (call_id,)).fetchall()
#     # print(dict(call), [dict(r) for r in scores], [dict(r) for r in fatal])
#     call_dict = dict(call)
#     print(call_dict)
#     if call_dict.get("recording_base"):
#         call_dict["mono_url"] = f"{RECORDINGS_PLAYBACK_BASE_URL.rstrip('/')}/{call_dict['recording_base']}"
#     else:
#         call_dict["mono_url"] = None
#     print(f" after adding the mono url\n{call_dict}")
#     return {
#         "call": dict(call_dict),
#         "parameters": [dict(r) for r in scores],
#         "fatal_checks": [dict(r) for r in fatal],
#     }

# @app.get("/api/health")
# def health(db = Depends(get_db)):
#     """Hits the DB too, so this actually tells you whether MySQL is reachable,
#     not just whether the FastAPI process is up."""
#     db.execute("SELECT 1")
#     return {"status": "ok"}





# import requests
# class QARequest(BaseModel):
#     transcript: str


# # for testing the SLM api integration
# @app.post("/test-slm")
# def test_slm(req: QARequest):
#     try:
#         response = requests.post(
#             SLM_API_URL,
#             json={
#                 "transcript": req.transcript
#             },
#             timeout=300
#         )

#         response.raise_for_status()

#         return response.json()

#     except requests.exceptions.RequestException as e:
#         raise HTTPException(
#             status_code=500,
#             detail=f"SLM API Error: {str(e)}"
#         )
# # import os
# # import shutil
# # import tempfile
# # from datetime import datetime, timedelta
# # from typing import Optional

# # import pymysql
# # from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Request, UploadFile
# # from fastapi.middleware.cors import CORSMiddleware
# # from fastapi.responses import JSONResponse
# # from pydantic import BaseModel

# # import hashlib

# # # from app4_.backend.config import RECOR, RECORDINGS_PLAYBACK_BASE_URLDINGS_PLAYBACK_BASE_URL
# # from config import PARAMETERS, RECORDINGS_BASE_URL, RECORDINGS_PLAYBACK_BASE_URL, SLM_API_URL
# # from db import get_conn, init_db
# # from pipeline import process_manifest, save_manifest

# # app = FastAPI(title="Athena QA Dashboard")

# # app.add_middleware(
# #     CORSMiddleware,
# #     allow_origins=[
# #         "http://localhost:5173",
# #         "http://127.0.0.1:5173",
# #     ],
# #     allow_credentials=True,
# #     allow_methods=["*"],
# #     allow_headers=["*"],
# # )

# # # ------------------------------------------------------------------
# # # IMPORTANT FIX: init_db() used to run unguarded at import time. If MySQL
# # # was unreachable (wrong host/user/password, or the server just wasn't up
# # # yet), that raised an exception WHILE the module was loading - before
# # # FastAPI/CORSMiddleware even finished being built. uvicorn's worker
# # # process died right there, so nothing was listening on the port at all.
# # #
# # # That's exactly what showed up in the browser as CORS errors + net::ERR_FAILED:
# # # there was no server to send a response (let alone CORS headers), so Chrome
# # # reports it as a blocked/failed request instead of a clean 500.
# # #
# # # Fix: catch the startup failure, log it clearly, and let the app come up
# # # anyway. Every DB-touching request will then fail with a clean 503 JSON
# # # response (see exception handler below) that DOES carry CORS headers,
# # # instead of the whole server being unreachable.
# # # ------------------------------------------------------------------
# # try:
# #     init_db()
# # except Exception as e:
# #     print("=" * 70)
# #     print("[STARTUP WARNING] Could not connect to MySQL / initialize schema.")
# #     print(f"[STARTUP WARNING] {type(e).__name__}: {e}")
# #     print("[STARTUP WARNING] Check MYSQL_HOST / MYSQL_PORT / MYSQL_USER / "
# #           "MYSQL_PASSWORD / MYSQL_DATABASE env vars and that MySQL is running.")
# #     print("[STARTUP WARNING] The API is still starting so you get clean 503s "
# #           "instead of connection failures - fix your DB config and restart.")
# #     print("=" * 70)


# # @app.exception_handler(pymysql.err.Error)
# # async def mysql_error_handler(request: Request, exc: pymysql.err.Error):
# #     return JSONResponse(
# #         status_code=503,
# #         content={"detail": f"Database unavailable: {exc}"},
# #     )


# # def get_db():
# #     conn = get_conn()
# #     try:
# #         yield conn
# #     finally:
# #         conn.close()


# # def hash_pw(pw: str) -> str:
# #     return hashlib.sha256(pw.encode()).hexdigest()


# # # ---------- Schemas ----------
# # class LoginRequest(BaseModel):
# #     username: str
# #     password: str


# # class LoginResponse(BaseModel):
# #     token: str
# #     username: str
# #     role: str


# # # ---------- Auth ----------
# # @app.post("/api/login", response_model=LoginResponse)
# # def login(req: LoginRequest, db = Depends(get_db)):
# #     row = db.execute(
# #         "SELECT * FROM users WHERE username = ? AND password_hash = ?",
# #         (req.username, hash_pw(req.password)),
# #     ).fetchone()
# #     if not row:
# #         raise HTTPException(status_code=401, detail="Invalid username or password")
# #     token = hashlib.sha256(f"{req.username}{datetime.utcnow()}".encode()).hexdigest()
# #     return LoginResponse(token=token, username=row["username"], role=row["role"])


# # class ChangePasswordRequest(BaseModel):
# #     username: str
# #     current_password: str
# #     new_password: str


# # @app.post("/api/change-password")
# # def change_password(req: ChangePasswordRequest, db = Depends(get_db)):
# #     # There's no session/bearer-token check anywhere else in this app yet
# #     # (login just hands back an unverified token) - so identity here is
# #     # proven the same way login proves it: username + correct current
# #     # password. If you add real session auth later, swap this for a
# #     # Depends(current_user) check instead of trusting the request body.
# #     if len(req.new_password) < 8:
# #         raise HTTPException(status_code=400, detail="New password must be at least 8 characters")

# #     row = db.execute(
# #         "SELECT * FROM users WHERE username = ? AND password_hash = ?",
# #         (req.username, hash_pw(req.current_password)),
# #     ).fetchone()
# #     if not row:
# #         raise HTTPException(status_code=401, detail="Current password is incorrect")

# #     db.execute(
# #         "UPDATE users SET password_hash = ? WHERE username = ?",
# #         (hash_pw(req.new_password), req.username),
# #     )
# #     db.commit()
# #     return {"status": "ok"}


# # # ---------- Helpers ----------
# # def date_range_defaults(start: Optional[str], end: Optional[str]):
# #     if not end:
# #         end = datetime.utcnow().strftime("%Y-%m-%d")
# #     if not start:
# #         start = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
# #     return start, end


# # # Whitelist of SQL fragments for the 5 clickable dashboard stat cards (feature 2).
# # # Kept as a fixed dict (not built from the path param) so there's no injection risk
# # # even though it gets interpolated straight into the query string below.
# # CATEGORY_FILTERS = {
# #     "total": "1=1",
# #     "audited": "c.status = 'audited'",
# #     "pending": "c.status IN ('pending','transcribing','analyzing')",
# #     "positive": "c.sentiment = 'positive'",
# #     "negative": "c.sentiment = 'negative'",
# #     # Agent-detail summary cards (feature: clickable Total/Average/Good/Poor).
# #     # "Total Calls" and "Average Call" both draw from the same audited
# #     # population that agent_detail()'s stats are computed over, so they
# #     # share the "audited" filter - only the label shown differs.
# #     "good": "c.overall_quality = 'good'",
# #     "poor": "c.overall_quality = 'poor'",
# #     # Disposition-based (feature: below the funnel). Case-insensitive since
# #     # the manifest's call_end_type_name casing isn't guaranteed consistent
# #     # (we've seen "CALLBACK" in real data). "Correct"/"Wrong" pair (actual vs
# #     # predicted disposition) is intentionally not wired up yet - needs a
# #     # ground-truth "actual disposition" field the pipeline doesn't capture yet.
# #     "disp_not_eligible": "UPPER(c.call_end_type_name) = 'NOT ELIGIBLE'",
# #     "disp_not_interested": "UPPER(c.call_end_type_name) = 'NOT INTERESTED'",
# #     "disp_callback": "UPPER(c.call_end_type_name) = 'CALLBACK'",
# # }

# # # Labels + ordering for the disposition buttons row on the dashboard.
# # DISPOSITION_CATEGORIES = [
# # ("disp_interested", "Interested", "INTERESTED"),
# # ("disp_not_interested", "Not Interested", "NOT INTERESTED"),
# # ("disp_callback", "Callback", "CALLBACK")

# # ]


# # def category_filter_sql(category: str) -> str:
# #     if category not in CATEGORY_FILTERS:
# #         raise HTTPException(
# #             status_code=400,
# #             detail=f"Unknown category '{category}'. Must be one of: {', '.join(CATEGORY_FILTERS)}",
# #         )
# #     return CATEGORY_FILTERS[category]


# # # ---------- Dashboard summary (Section 2 top cards) ----------
# # @app.get("/api/dashboard/summary")
# # def dashboard_summary(
# #     start: Optional[str] = None,
# #     end: Optional[str] = None,
# #     db = Depends(get_db),
# # ):
# #     start, end = date_range_defaults(start, end)
# #     row = db.execute(
# #         """
# #         SELECT
# #           COUNT(*) AS total_calls,
# #           SUM(CASE WHEN status = 'audited' THEN 1 ELSE 0 END) AS audited_calls,
# #           SUM(CASE WHEN status IN ('pending','transcribing','analyzing') THEN 1 ELSE 0 END) AS pending_calls,
# #           SUM(CASE WHEN sentiment = 'positive' THEN 1 ELSE 0 END) AS positive_calls,
# #           SUM(CASE WHEN sentiment = 'negative' THEN 1 ELSE 0 END) AS negative_calls
# #         FROM calls
# #         WHERE call_date IS NOT NULL AND date(call_date) BETWEEN date(?) AND date(?)
# #         """,
# #         (start, end),
# #     ).fetchone()
# #     return {
# #         "start": start,
# #         "end": end,
# #         "total_calls": row["total_calls"] or 0,
# #         "audited_calls": row["audited_calls"] or 0,
# #         "pending_calls": row["pending_calls"] or 0,
# #         "positive_calls": row["positive_calls"] or 0,
# #         "negative_calls": row["negative_calls"] or 0,
# #     }


# # # ---------- Disposition counts (feature: buttons below the funnel) ----------
# # # One grouped query for all dispositions (not one query per button), then map
# # # onto the 3 known categories. Drill-down reuses the exact same
# # # /api/dashboard/category/{category}/... endpoints as the 5 stat cards do -
# # # "disp_not_eligible" etc are just more entries in CATEGORY_FILTERS above.
# # @app.get("/api/dashboard/dispositions")
# # def dashboard_dispositions(
# #     start: Optional[str] = None,
# #     end: Optional[str] = None,
# #     db = Depends(get_db),
# # ):
# #     start, end = date_range_defaults(start, end)
# #     rows = db.execute(
# #         """
# #         SELECT UPPER(call_end_type_name) AS disp, COUNT(*) AS n
# #         FROM calls
# #         WHERE call_date IS NOT NULL AND date(call_date) BETWEEN date(?) AND date(?)
# #           AND call_end_type_name IS NOT NULL AND call_end_type_name != ''
# #         GROUP BY UPPER(call_end_type_name)
# #         """,
# #         (start, end),
# #     ).fetchall()

# #     print(rows)
# #     counts = {r["disp"]: r["n"] for r in rows}

# #     return {
# #         "start": start,
# #         "end": end,
# #         "dispositions": [
# #             {"category": cat, "label": label, "count": counts.get(match_value, 0)}
# #             for cat, label, match_value in DISPOSITION_CATEGORIES
# #         ],
# #     }


# # # ---------- Parameter-wise average scores (graph) ----------
# # @app.get("/api/dashboard/parameters")
# # def dashboard_parameters(
# #     start: Optional[str] = None,
# #     end: Optional[str] = None,
# #     db = Depends(get_db),
# # ):
# #     start, end = date_range_defaults(start, end)
# #     rows = db.execute(
# #         """
# #         SELECT p.label AS parameter, ROUND(AVG(s.score), 1) AS avg_score, COUNT(*) AS n
# #         FROM call_scores s
# #         JOIN parameters p ON p.id = s.parameter_id
# #         JOIN calls c ON c.id = s.call_id
# #         WHERE c.call_date IS NOT NULL AND date(c.call_date) BETWEEN date(?) AND date(?)
# #         GROUP BY p.id, p.label
# #         ORDER BY p.id
# #         """,
# #         (start, end),
# #     ).fetchall()
# #     return {
# #         "start": start,
# #         "end": end,
# #         "parameters": [
# #             {"parameter": r["parameter"], "avg_score": r["avg_score"] or 0, "sample_size": r["n"]}
# #             for r in rows
# #         ],
# #     }


# # # ---------- Parameter success-rate funnel (feature 1) ----------
# # # Different from /api/dashboard/parameters above (which averages the 0-100
# # # normalized score). This instead answers "out of every AUDITED call, what
# # # % actually PASSED (status=true) this parameter?" - sorted highest-first so
# # # it reads top-to-bottom like a funnel: parameters almost everyone nails at
# # # the top, parameters agents struggle with at the bottom.
# # #
# # # success_pct = (# audited calls where that parameter's status = TRUE)
# # #               / (total audited calls in range) * 100
# # #
# # # Two queries total (not N+1): one scalar for the constant denominator
# # # (total audited calls), one GROUP BY for all 16 numerators together.
# # @app.get("/api/dashboard/parameter-funnel")
# # def parameter_funnel(
# #     start: Optional[str] = None,
# #     end: Optional[str] = None,
# #     db = Depends(get_db),
# # ):
# #     start, end = date_range_defaults(start, end)

# #     total_row = db.execute(
# #         """
# #         SELECT COUNT(*) AS total_audited
# #         FROM calls
# #         WHERE status = 'audited' AND call_date IS NOT NULL
# #           AND date(call_date) BETWEEN date(?) AND date(?)
# #         """,
# #         (start, end),
# #     ).fetchone()
# #     total_audited = total_row["total_audited"] or 0

# #     rows = db.execute(
# #         """
# #         SELECT p.id AS parameter_id, p.label AS parameter,
# #                SUM(CASE WHEN s.status = 1 THEN 1 ELSE 0 END) AS success_count,
# #                COUNT(*) AS sample_size
# #         FROM call_scores s
# #         JOIN parameters p ON p.id = s.parameter_id
# #         JOIN calls c ON c.id = s.call_id
# #         WHERE c.status = 'audited' AND c.call_date IS NOT NULL
# #           AND date(c.call_date) BETWEEN date(?) AND date(?)
# #         GROUP BY p.id, p.label
# #         ORDER BY p.id
# #         """,
# #         (start, end),
# #     ).fetchall()

# #     funnel = []
# #     for r in rows:
# #         success_count = r["success_count"] or 0
# #         pct = round((success_count / total_audited) * 100, 1) if total_audited else 0.0
# #         funnel.append({
# #             "parameter_id": r["parameter_id"],
# #             "parameter": r["parameter"],
# #             "success_count": success_count,
# #             "sample_size": r["sample_size"],
# #             "success_pct": pct,
# #         })

# #     funnel.sort(key=lambda x: x["success_pct"], reverse=True)

# #     return {
# #         "start": start,
# #         "end": end,
# #         "total_audited_calls": total_audited,
# #         "parameters": funnel,
# #     }



# # # ---------- Agents belonging to a parameter (click on bar) ----------
# # @app.get("/api/dashboard/parameters/{parameter_name}/agents")
# # def agents_for_parameter(
# #     parameter_name: str,
# #     start: Optional[str] = None,
# #     end: Optional[str] = None,
# #     db = Depends(get_db),
# # ):
# #     start, end = date_range_defaults(start, end)
# #     rows = db.execute(
# #         """
# #         SELECT a.id AS agent_id,
# #                a.name AS agent_name,
# #                a.team,
# #                ROUND(AVG(s.score), 1) AS avg_score,
# #                COUNT(*) AS calls_scored
# #         FROM call_scores s
# #         JOIN parameters p ON p.id = s.parameter_id
# #         JOIN calls c ON c.id = s.call_id
# #         JOIN agents a ON a.id = c.agent_id
# #         WHERE p.label = ?
# #           AND c.call_date IS NOT NULL
# #           AND date(c.call_date) BETWEEN date(?) AND date(?)
# #         GROUP BY a.id, a.name, a.team
# #         ORDER BY avg_score ASC
# #         """,
# #         (parameter_name, start, end),
# #     ).fetchall()
# #     if not rows:
# #         exists = db.execute("SELECT 1 FROM parameters WHERE label = ?", (parameter_name,)).fetchone()
# #         if not exists:
# #             raise HTTPException(status_code=404, detail="Parameter not found")
# #     return {
# #         "parameter": parameter_name,
# #         "start": start,
# #         "end": end,
# #         "agents": [dict(r) for r in rows],
# #     }


# # # ---------- Agent list (basic) ----------
# # @app.get("/api/agents")
# # def list_agents(db = Depends(get_db)):
# #     rows = db.execute("SELECT id, name, team FROM agents ORDER BY name").fetchall()
# #     return {"agents": [dict(r) for r in rows]}


# # # ---------- Agent detail (Section 3) ----------
# # @app.get("/api/agents/{agent_id}")
# # def agent_detail(
# #     agent_id: int,
# #     start: Optional[str] = None,
# #     end: Optional[str] = None,
# #     db = Depends(get_db),
# # ):
# #     start, end = date_range_defaults(start, end)
# #     agent = db.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
# #     if not agent:
# #         raise HTTPException(status_code=404, detail="Agent not found")

# #     calls_row = db.execute(
# #         """
# #         SELECT COUNT(*) AS total_calls,
# #                SUM(CASE WHEN overall_quality = 'good' THEN 1 ELSE 0 END) AS good_calls,
# #                SUM(CASE WHEN overall_quality = 'poor' THEN 1 ELSE 0 END) AS poor_calls,
# #                ROUND(AVG(overall_score), 1) AS average_call_score
# #         FROM calls
# #         WHERE agent_id = ? AND status = 'audited'
# #           AND call_date IS NOT NULL AND date(call_date) BETWEEN date(?) AND date(?)
# #         """,
# #         (agent_id, start, end),
# #     ).fetchone()

# #     param_rows = db.execute(
# #         """
# #         SELECT p.label AS parameter, ROUND(AVG(s.score),1) AS avg_score
# #         FROM call_scores s
# #         JOIN parameters p ON p.id = s.parameter_id
# #         JOIN calls c ON c.id = s.call_id
# #         WHERE c.agent_id = ? AND c.call_date IS NOT NULL AND date(c.call_date) BETWEEN date(?) AND date(?)
# #         GROUP BY p.id, p.label
# #         ORDER BY p.id
# #         """,
# #         (agent_id, start, end),
# #     ).fetchall()

# #     params = [dict(r) for r in param_rows]
# #     if params:
# #         sorted_params = sorted(params, key=lambda x: x["avg_score"] or 0, reverse=True)
# #         strengths = [p["parameter"] for p in sorted_params[:3]]
# #         improvements = [p["parameter"] for p in sorted_params[-3:]]
# #     else:
# #         strengths, improvements = [], []

# #     return {
# #         "agent": {"id": agent["id"], "name": agent["name"], "team": agent["team"]},
# #         "start": start,
# #         "end": end,
# #         "total_calls": calls_row["total_calls"] or 0,
# #         "average_call_score": calls_row["average_call_score"] or 0,
# #         "good_calls": calls_row["good_calls"] or 0,
# #         "poor_calls": calls_row["poor_calls"] or 0,
# #         "parameters": params,
# #         "overall_summary": {
# #             "strengths": strengths,
# #             "improvements": improvements,
# #         },
# #     }


# # # ==================================================================
# # # Feature 2: clickable stat cards -> agents -> that agent's calls.
# # # category is one of the CATEGORY_FILTERS keys (total/audited/pending/positive/negative),
# # # matching whichever stat card was clicked on the dashboard.
# # # ==================================================================

# # @app.get("/api/dashboard/category/{category}/agents")
# # def category_agents(
# #     category: str,
# #     start: Optional[str] = None,
# #     end: Optional[str] = None,
# #     db = Depends(get_db),
# # ):
# #     """2.1 - agents ranked by their average score, scoped to the clicked category."""
# #     where_clause = category_filter_sql(category)
# #     start, end = date_range_defaults(start, end)
# #     rows = db.execute(
# #         f"""
# #         SELECT a.id AS agent_id, a.name AS agent_name, a.team,
# #                ROUND(AVG(c.overall_score), 1) AS avg_score,
# #                COUNT(*) AS call_count
# #         FROM calls c
# #         JOIN agents a ON a.id = c.agent_id
# #         WHERE {where_clause}
# #           AND c.call_date IS NOT NULL AND date(c.call_date) BETWEEN date(?) AND date(?)
# #         GROUP BY a.id, a.name, a.team
# #         ORDER BY avg_score DESC
# #         """,
# #         (start, end),
# #     ).fetchall()
# #     return {
# #         "category": category,
# #         "start": start,
# #         "end": end,
# #         "agents": [dict(r) for r in rows],
# #     }


# # @app.get("/api/dashboard/category/{category}/agents/{agent_id}/calls")
# # def category_agent_calls(
# #     category: str,
# #     agent_id: int,
# #     start: Optional[str] = None,
# #     end: Optional[str] = None,
# #     db = Depends(get_db),
# # ):
# #     """2.2 - that agent's individual calls (with score) within the clicked category."""
# #     where_clause = category_filter_sql(category)
# #     start, end = date_range_defaults(start, end)
# #     agent = db.execute("SELECT id, name, team FROM agents WHERE id = ?", (agent_id,)).fetchone()
# #     if not agent:
# #         raise HTTPException(status_code=404, detail="Agent not found")

# #     calls = db.execute(
# #         f"""
# #         SELECT c.id, c.call_number, c.call_start_time, c.call_date, c.status,
# #                c.sentiment, c.overall_quality, c.overall_score, c.verdict, c.recording_base
# #         FROM calls c
# #         WHERE c.agent_id = ? AND {where_clause}
# #           AND c.call_date IS NOT NULL AND date(c.call_date) BETWEEN date(?) AND date(?)
# #         ORDER BY c.call_date DESC, c.id DESC
# #         """,
# #         (agent_id, start, end),
# #     ).fetchall()
# #     return {
# #         "category": category,
# #         "agent": dict(agent),
# #         "start": start,
# #         "end": end,
# #         "calls": [dict(r) for r in calls],
# #     }


# # # ==================================================================
# # # Feature 3: top-10 agents leaderboard (right-hand sidebar box)
# # # ==================================================================

# # @app.get("/api/dashboard/top-agents")
# # def top_agents(
# #     start: Optional[str] = None,
# #     end: Optional[str] = None,
# #     limit: int = 10,
# #     search: Optional[str] = None,
# #     db = Depends(get_db),
# # ):
# #     start, end = date_range_defaults(start, end)
# #     # a search should be able to surface more than 10 matches; the sidebar's
# #     # default (unsearched) call always passes limit=10 explicitly, this cap
# #     # just stops a search from being unbounded.
# #     limit = max(1, min(limit, 50))

# #     where = [
# #         "c.status = 'audited'",
# #         "c.call_date IS NOT NULL",
# #         "date(c.call_date) BETWEEN date(?) AND date(?)",
# #     ]
# #     params = [start, end]
# #     if search:
# #         where.append("a.name LIKE ?")
# #         params.append(f"%{search}%")
# #     where_sql = " AND ".join(where)

# #     rows = db.execute(
# #         f"""
# #         SELECT a.id AS agent_id, a.name AS agent_name, a.team,
# #                ROUND(AVG(c.overall_score), 1) AS avg_score,
# #                COUNT(*) AS call_count
# #         FROM calls c
# #         JOIN agents a ON a.id = c.agent_id
# #         WHERE {where_sql}
# #         GROUP BY a.id, a.name, a.team
# #         ORDER BY avg_score DESC
# #         LIMIT ?
# #         """,
# #         tuple(params) + (limit,),
# #     ).fetchall()
# #     return {
# #         "start": start,
# #         "end": end,
# #         "search": search,
# #         "agents": [dict(r) for r in rows],
# #     }



# # REPORT_SORTS = {
# #     "call_date_desc": "c.call_date DESC, c.id DESC",
# #     "call_date_asc": "c.call_date ASC, c.id ASC",
# #     "score_desc": "c.overall_score DESC",
# #     "score_asc": "c.overall_score ASC",
# #     "agent_name": "a.name ASC, c.call_date DESC",
# # }

# # @app.get("/api/reports/agent-scores")
# # def agent_scores_report(
# #     agent_id: Optional[int] = None,
# #     start: Optional[str] = None,
# #     end: Optional[str] = None,
# #     page: int = 1,
# #     page_size: int = 25,
# #     sort: str = "call_date_desc",
# #     db = Depends(get_db),
# # ):
# #     start, end = date_range_defaults(start, end)
# #     page = max(1, page)
# #     page_size = max(1, min(page_size, 200))  # cap it - this is a report table, not a data dump endpoint
# #     offset = (page - 1) * page_size
# #     order_sql = REPORT_SORTS.get(sort, REPORT_SORTS["call_date_desc"])

# #     where = [
# #         "c.status = 'audited'",
# #         "c.call_date IS NOT NULL",
# #         "date(c.call_date) BETWEEN date(?) AND date(?)",
# #     ]
# #     params = [start, end]
# #     if agent_id:
# #         where.append("c.agent_id = ?")
# #         params.append(agent_id)
# #     where_sql = " AND ".join(where)

# #     total_row = db.execute(
# #         f"SELECT COUNT(*) AS n FROM calls c JOIN agents a ON a.id = c.agent_id WHERE {where_sql}",
# #         tuple(params),
# #     ).fetchone()
# #     total = total_row["n"] or 0

# #     # One column per parameter via conditional aggregation (MySQL has no PIVOT).
# #     # param_key values come from our own hardcoded PARAMETERS config, not user
# #     # input, so interpolating them straight into the SQL text is safe here.
# #     pivot_cols = ",\n               ".join(
# #         f"MAX(CASE WHEN p.param_key = '{key}' THEN s.score END) AS `{key}`"
# #         for key, _ in PARAMETERS
# #     )

# #     rows = db.execute(
# #         f"""
# #         SELECT c.id AS call_id, a.id AS agent_id, a.name AS agent_name,
# #                c.recording_base, c.call_date, c.overall_score, c.verdict, c.overall_quality,
# #                {pivot_cols}
# #         FROM calls c
# #         JOIN agents a ON a.id = c.agent_id
# #         LEFT JOIN call_scores s ON s.call_id = c.id
# #         LEFT JOIN parameters p ON p.id = s.parameter_id
# #         WHERE {where_sql}
# #         GROUP BY c.id, a.id, a.name, c.recording_base, c.call_date,
# #                  c.overall_score, c.verdict, c.overall_quality
# #         ORDER BY {order_sql}
# #         LIMIT ? OFFSET ?
# #         """,
# #         tuple(params) + (page_size, offset),
# #     ).fetchall()

# #     return {
# #         "start": start,
# #         "end": end,
# #         "agent_id": agent_id,
# #         "page": page,
# #         "page_size": page_size,
# #         "total": total,
# #         "columns": [{"key": key, "label": label} for key, label in PARAMETERS],
# #         "rows": [dict(r) for r in rows],
# #     }

# # # ==================================================================
# # # Manifest upload + STT/SLM pipeline
# # # ==================================================================

# # @app.post("/api/manifest/upload")
# # async def upload_manifest(
# #     background_tasks: BackgroundTasks,
# #     file: UploadFile = File(...),
# #     recording_base_url: Optional[str] = None,
# #     auto_process: bool = True,
# # ):
# #     if not file.filename.lower().endswith((".xlsx", ".xlsm")):
# #         raise HTTPException(status_code=400, detail="Please upload an .xlsx manifest file")

# #     with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
# #         shutil.copyfileobj(file.file, tmp)
# #         tmp_path = tmp.name

# #     try:
# #         manifest_id, row_count = save_manifest(
# #             tmp_path, file.filename, recording_base_url or RECORDINGS_BASE_URL
# #         )
# #         print(f"Manifest uploaded: {manifest_id}, Rows: {row_count}")
# #     finally:
# #         os.unlink(tmp_path)

# #     if auto_process:
# #         print("Triggering background processing for manifest:", manifest_id)
# #         background_tasks.add_task(process_manifest, manifest_id)

# #     return {"manifest_id": manifest_id, "rows_loaded": row_count, "processing": auto_process}


# # @app.post("/api/manifest/{manifest_id}/process")
# # def trigger_processing(manifest_id: int, background_tasks: BackgroundTasks, db = Depends(get_db)):
# #     row = db.execute("SELECT id FROM manifests WHERE id = ?", (manifest_id,)).fetchone()
# #     if not row:
# #         raise HTTPException(status_code=404, detail="Manifest not found")
# #     background_tasks.add_task(process_manifest, manifest_id)
# #     return {"manifest_id": manifest_id, "processing": True}


# # @app.get("/api/manifest")
# # def list_manifests(db = Depends(get_db)):
# #     rows = db.execute("SELECT * FROM manifests ORDER BY id DESC").fetchall()
# #     return {"manifests": [dict(r) for r in rows]}


# # @app.get("/api/manifest/{manifest_id}")
# # def manifest_status(manifest_id: int, db = Depends(get_db)):
# #     manifest = db.execute("SELECT * FROM manifests WHERE id = ?", (manifest_id,)).fetchone()
# #     if not manifest:
# #         raise HTTPException(status_code=404, detail="Manifest not found")
# #     calls = db.execute(
# #         """SELECT c.id, c.call_number, c.recording_base, c.status, c.error_message,
# #                   c.overall_score, c.verdict, a.name AS agent_name
# #            FROM calls c JOIN agents a ON a.id = c.agent_id
# #            WHERE c.manifest_id = ? ORDER BY c.id""",
# #         (manifest_id,),
# #     ).fetchall()
# #     return {"manifest": dict(manifest), "calls": [dict(r) for r in calls]}


# # # @app.get("/api/calls/{call_id}/transcript")
# # # def call_transcript(call_id: int, db = Depends(get_db)):
# # #     row = db.execute("SELECT * FROM transcripts WHERE call_id = ?", (call_id,)).fetchone()
# # #     if not row:
# # #         raise HTTPException(status_code=404, detail="No transcript for this call yet")
# # #     return dict(row)


# # # @app.get("/api/calls/{call_id}")
# # # def call_detail(call_id: int, db = Depends(get_db)):
# # #     # `c.*` already includes rx_path (IN leg) and tx_path (OUT leg) - the same
# # #     # recording URLs the STT service was given. The frontend call-detail page
# # #     # (feature 2.3) plays these directly and seeks to each parameter's
# # #     # evidence start_time so a reviewer can jump straight to the moment a
# # #     # score was decided, instead of listening to the whole call.
# # #     call = db.execute(
# # #         """SELECT c.*, a.name AS agent_name FROM calls c JOIN agents a ON a.id = c.agent_id
# # #            WHERE c.id = ?""",
# # #         (call_id,),
# # #     ).fetchone()
# # #     if not call:
# # #         raise HTTPException(status_code=404, detail="Call not found")
# # #     scores = db.execute(
# # #         """SELECT p.label AS parameter, s.status, s.reason, s.evidence, s.start_time, s.end_time,
# # #                   s.raw_score, s.score
# # #            FROM call_scores s JOIN parameters p ON p.id = s.parameter_id
# # #            WHERE s.call_id = ? ORDER BY p.id""",
# # #         (call_id,),
# # #     ).fetchall()
# # #     fatal = db.execute("SELECT * FROM fatal_checks WHERE call_id = ?", (call_id,)).fetchall()
# # #     return {
# # #         "call": dict(call),
# # #         "parameters": [dict(r) for r in scores],
# # #         "fatal_checks": [dict(r) for r in fatal],
# # #     }

# # @app.get("/api/calls/{call_id}/transcript")
# # def call_transcript(call_id: int, db = Depends(get_db)):
# #     row = db.execute("SELECT * FROM transcripts WHERE call_id = ?", (call_id,)).fetchone()
# #     if not row:
# #         raise HTTPException(status_code=404, detail="No transcript for this call yet")
# #     return dict(row)


# # @app.get("/api/calls/{call_id}")
# # def call_detail(call_id: int, db = Depends(get_db)):
# #     print(f"Fetching detail for call ID: {call_id}")
# #     # `c.*` already includes rx_path (IN leg) and tx_path (OUT leg) - the same
# #     # recording URLs the STT service was given. The frontend call-detail page
# #     # (feature 2.3) plays these directly and seeks to each parameter's
# #     # evidence start_time so a reviewer can jump straight to the moment a
# #     # score was decided, instead of listening to the whole call.
# #     call = db.execute(
# #         """SELECT c.*, a.name AS agent_name FROM calls c JOIN agents a ON a.id = c.agent_id
# #            WHERE c.id = ?""",
# #         (call_id,),
# #     ).fetchone()
# #     if not call:
# #         raise HTTPException(status_code=404, detail="Call not found")
# #     scores = db.execute(
# #         """SELECT p.label AS parameter, s.status, s.reason, s.evidence, s.start_time, s.end_time,
# #                   s.raw_score, s.score
# #            FROM call_scores s JOIN parameters p ON p.id = s.parameter_id
# #            WHERE s.call_id = ? ORDER BY p.id""",
# #         (call_id,),
# #     ).fetchall()
# #     fatal = db.execute("SELECT * FROM fatal_checks WHERE call_id = ?", (call_id,)).fetchall()
# #     # print(dict(call), [dict(r) for r in scores], [dict(r) for r in fatal])
# #     call_dict = dict(call)
# #     print(call_dict)
# #     if call_dict.get("recording_base"):
# #         call_dict["mono_url"] = f"{RECORDINGS_PLAYBACK_BASE_URL.rstrip('/')}/{call_dict['recording_base']}"
# #     else:
# #         call_dict["mono_url"] = None
# #     print(f" after adding the mono url\n{call_dict}")
# #     return {
# #         "call": dict(call_dict),
# #         "parameters": [dict(r) for r in scores],
# #         "fatal_checks": [dict(r) for r in fatal],
# #     }

# # import requests
# # class QARequest(BaseModel):
# #     transcript: str


# # for testing the SLM api integration
# # @app.post("/test-slm")
# # def test_slm(req: QARequest):
# #     try:
# #         response = requests.post(
# #             SLM_API_URL,
# #             json={
# #                 "transcript": req.transcript
# #             },
# #             timeout=300
# #         )

# #         response.raise_for_status()

# #         return response.json()

# #     except requests.exceptions.RequestException as e:
# #         raise HTTPException(
# #             status_code=500,
# #             detail=f"SLM API Error: {str(e)}"
# #         )
# # @app.get("/api/health")
# # def health(db = Depends(get_db)):
# #     """Hits the DB too, so this actually tells you whether MySQL is reachable,
# #     not just whether the FastAPI process is up."""
# #     db.execute("SELECT 1")
# #     return {"status": "ok"} 

# # # import os
# # # import shutil
# # # import tempfile
# # # from datetime import datetime, timedelta
# # # from typing import Optional

# # # import pymysql
# # # from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Request, UploadFile
# # # from fastapi.middleware.cors import CORSMiddleware
# # # from fastapi.responses import JSONResponse
# # # from pydantic import BaseModel

# # # import hashlib

# # # from config import RECORDINGS_BASE_URL, RECORDINGS_PLAYBACK_BASE_URL
# # # from db import get_conn, init_db
# # # from pipeline import process_manifest, save_manifest

# # # app = FastAPI(title="Athena QA Dashboard")

# # # app.add_middleware(
# # #     CORSMiddleware,
# # #     allow_origins=[
# # #         "http://localhost:5173",
# # #         "http://127.0.0.1:5173",
# # #     ],
# # #     allow_credentials=True,
# # #     allow_methods=["*"],
# # #     allow_headers=["*"],
# # # )

# # # # ------------------------------------------------------------------
# # # # IMPORTANT FIX: init_db() used to run unguarded at import time. If MySQL
# # # # was unreachable (wrong host/user/password, or the server just wasn't up
# # # # yet), that raised an exception WHILE the module was loading - before
# # # # FastAPI/CORSMiddleware even finished being built. uvicorn's worker
# # # # process died right there, so nothing was listening on the port at all.
# # # #
# # # # That's exactly what showed up in the browser as CORS errors + net::ERR_FAILED:
# # # # there was no server to send a response (let alone CORS headers), so Chrome
# # # # reports it as a blocked/failed request instead of a clean 500.
# # # #
# # # # Fix: catch the startup failure, log it clearly, and let the app come up
# # # # anyway. Every DB-touching request will then fail with a clean 503 JSON
# # # # response (see exception handler below) that DOES carry CORS headers,
# # # # instead of the whole server being unreachable.
# # # # ------------------------------------------------------------------
# # # try:
# # #     init_db()
# # # except Exception as e:
# # #     print("=" * 70)
# # #     print("[STARTUP WARNING] Could not connect to MySQL / initialize schema.")
# # #     print(f"[STARTUP WARNING] {type(e).__name__}: {e}")
# # #     print("[STARTUP WARNING] Check MYSQL_HOST / MYSQL_PORT / MYSQL_USER / "
# # #           "MYSQL_PASSWORD / MYSQL_DATABASE env vars and that MySQL is running.")
# # #     print("[STARTUP WARNING] The API is still starting so you get clean 503s "
# # #           "instead of connection failures - fix your DB config and restart.")
# # #     print("=" * 70)


# # # @app.exception_handler(pymysql.err.Error)
# # # async def mysql_error_handler(request: Request, exc: pymysql.err.Error):
# # #     return JSONResponse(
# # #         status_code=503,
# # #         content={"detail": f"Database unavailable: {exc}"},
# # #     )


# # # def get_db():
# # #     conn = get_conn()
# # #     try:
# # #         yield conn
# # #     finally:
# # #         conn.close()


# # # def hash_pw(pw: str) -> str:
# # #     return hashlib.sha256(pw.encode()).hexdigest()


# # # # ---------- Schemas ----------
# # # class LoginRequest(BaseModel):
# # #     username: str
# # #     password: str


# # # class LoginResponse(BaseModel):
# # #     token: str
# # #     username: str
# # #     role: str


# # # # ---------- Auth ----------
# # # @app.post("/api/login", response_model=LoginResponse)
# # # def login(req: LoginRequest, db = Depends(get_db)):
# # #     row = db.execute(
# # #         "SELECT * FROM users WHERE username = ? AND password_hash = ?",
# # #         (req.username, hash_pw(req.password)),
# # #     ).fetchone()
# # #     if not row:
# # #         raise HTTPException(status_code=401, detail="Invalid username or password")
# # #     token = hashlib.sha256(f"{req.username}{datetime.utcnow()}".encode()).hexdigest()
# # #     return LoginResponse(token=token, username=row["username"], role=row["role"])


# # # # ---------- Helpers ----------
# # # def date_range_defaults(start: Optional[str], end: Optional[str]):
# # #     if not end:
# # #         end = datetime.utcnow().strftime("%Y-%m-%d")
# # #     if not start:
# # #         start = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
# # #     return start, end


# # # # Whitelist of SQL fragments for the 5 clickable dashboard stat cards (feature 2).
# # # # Kept as a fixed dict (not built from the path param) so there's no injection risk
# # # # even though it gets interpolated straight into the query string below.
# # # CATEGORY_FILTERS = {
# # #     "total": "1=1",
# # #     "audited": "c.status = 'audited'",
# # #     "pending": "c.status IN ('pending','transcribing','analyzing')",
# # #     "positive": "c.sentiment = 'positive'",
# # #     "negative": "c.sentiment = 'negative'",
# # # }


# # # def category_filter_sql(category: str) -> str:
# # #     if category not in CATEGORY_FILTERS:
# # #         raise HTTPException(
# # #             status_code=400,
# # #             detail=f"Unknown category '{category}'. Must be one of: {', '.join(CATEGORY_FILTERS)}",
# # #         )
# # #     return CATEGORY_FILTERS[category]


# # # # ---------- Dashboard summary (Section 2 top cards) ----------
# # # @app.get("/api/dashboard/summary")
# # # def dashboard_summary(
# # #     start: Optional[str] = None,
# # #     end: Optional[str] = None,
# # #     db = Depends(get_db),
# # # ):
# # #     start, end = date_range_defaults(start, end)
# # #     row = db.execute(
# # #         """
# # #         SELECT
# # #           COUNT(*) AS total_calls,
# # #           SUM(CASE WHEN status = 'audited' THEN 1 ELSE 0 END) AS audited_calls,
# # #           SUM(CASE WHEN status IN ('pending','transcribing','analyzing') THEN 1 ELSE 0 END) AS pending_calls,
# # #           SUM(CASE WHEN sentiment = 'positive' THEN 1 ELSE 0 END) AS positive_calls,
# # #           SUM(CASE WHEN sentiment = 'negative' THEN 1 ELSE 0 END) AS negative_calls
# # #         FROM calls
# # #         WHERE call_date IS NOT NULL AND date(call_date) BETWEEN date(?) AND date(?)
# # #         """,
# # #         (start, end),
# # #     ).fetchone()
# # #     return {
# # #         "start": start,
# # #         "end": end,
# # #         "total_calls": row["total_calls"] or 0,
# # #         "audited_calls": row["audited_calls"] or 0,
# # #         "pending_calls": row["pending_calls"] or 0,
# # #         "positive_calls": row["positive_calls"] or 0,
# # #         "negative_calls": row["negative_calls"] or 0,
# # #     }


# # # # ---------- Parameter-wise average scores (graph) ----------
# # # @app.get("/api/dashboard/parameters")
# # # def dashboard_parameters(
# # #     start: Optional[str] = None,
# # #     end: Optional[str] = None,
# # #     db = Depends(get_db),
# # # ):
# # #     start, end = date_range_defaults(start, end)
# # #     rows = db.execute(
# # #         """
# # #         SELECT p.label AS parameter, ROUND(AVG(s.score), 1) AS avg_score, COUNT(*) AS n
# # #         FROM call_scores s
# # #         JOIN parameters p ON p.id = s.parameter_id
# # #         JOIN calls c ON c.id = s.call_id
# # #         WHERE c.call_date IS NOT NULL AND date(c.call_date) BETWEEN date(?) AND date(?)
# # #         GROUP BY p.label
# # #         ORDER BY p.id
# # #         """,
# # #         (start, end),
# # #     ).fetchall()
# # #     return {
# # #         "start": start,
# # #         "end": end,
# # #         "parameters": [
# # #             {"parameter": r["parameter"], "avg_score": r["avg_score"] or 0, "sample_size": r["n"]}
# # #             for r in rows
# # #         ],
# # #     }


# # # # ---------- Parameter success-rate funnel (feature 1) ----------
# # # # Different from /api/dashboard/parameters above (which averages the 0-100
# # # # normalized score). This instead answers "out of every audited call, what
# # # # % actually PASSED (status=true) this parameter?" - sorted highest-first so
# # # # it reads top-to-bottom like a funnel: parameters almost everyone nails at
# # # # the top, parameters agents struggle with at the bottom.
# # # @app.get("/api/dashboard/parameter-funnel")
# # # def parameter_funnel(
# # #     start: Optional[str] = None,
# # #     end: Optional[str] = None,
# # #     db = Depends(get_db),
# # # ):
# # #     start, end = date_range_defaults(start, end)
# # #     rows = db.execute(
# # #         """
# # #         SELECT p.label AS parameter,
# # #                ROUND(SUM(s.status) / COUNT(*) * 100, 1) AS success_pct,
# # #                COUNT(*) AS sample_size
# # #         FROM call_scores s
# # #         JOIN parameters p ON p.id = s.parameter_id
# # #         JOIN calls c ON c.id = s.call_id
# # #         WHERE c.call_date IS NOT NULL AND date(c.call_date) BETWEEN date(?) AND date(?)
# # #         GROUP BY p.label
# # #         ORDER BY success_pct DESC
# # #         """,
# # #         (start, end),
# # #     ).fetchall()
# # #     return {
# # #         "start": start,
# # #         "end": end,
# # #         "parameters": [
# # #             {"parameter": r["parameter"], "success_pct": r["success_pct"] or 0, "sample_size": r["sample_size"]}
# # #             for r in rows
# # #         ],
# # #     }


# # # # ---------- Agents belonging to a parameter (click on bar) ----------
# # # @app.get("/api/dashboard/parameters/{parameter_name}/agents")
# # # def agents_for_parameter(
# # #     parameter_name: str,
# # #     start: Optional[str] = None,
# # #     end: Optional[str] = None,
# # #     db = Depends(get_db),
# # # ):
# # #     start, end = date_range_defaults(start, end)
# # #     rows = db.execute(
# # #         """
# # #         SELECT a.id AS agent_id,
# # #                a.name AS agent_name,
# # #                a.team,
# # #                ROUND(AVG(s.score), 1) AS avg_score,
# # #                COUNT(*) AS calls_scored
# # #         FROM call_scores s
# # #         JOIN parameters p ON p.id = s.parameter_id
# # #         JOIN calls c ON c.id = s.call_id
# # #         JOIN agents a ON a.id = c.agent_id
# # #         WHERE p.label = ?
# # #           AND c.call_date IS NOT NULL
# # #           AND date(c.call_date) BETWEEN date(?) AND date(?)
# # #         GROUP BY a.id
# # #         ORDER BY avg_score ASC
# # #         """,
# # #         (parameter_name, start, end),
# # #     ).fetchall()
# # #     if not rows:
# # #         exists = db.execute("SELECT 1 FROM parameters WHERE label = ?", (parameter_name,)).fetchone()
# # #         if not exists:
# # #             raise HTTPException(status_code=404, detail="Parameter not found")
# # #     return {
# # #         "parameter": parameter_name,
# # #         "start": start,
# # #         "end": end,
# # #         "agents": [dict(r) for r in rows],
# # #     }


# # # # ---------- Agent list (basic) ----------
# # # @app.get("/api/agents")
# # # def list_agents(db = Depends(get_db)):
# # #     rows = db.execute("SELECT id, name, team FROM agents ORDER BY name").fetchall()
# # #     return {"agents": [dict(r) for r in rows]}


# # # # ---------- Agent detail (Section 3) ----------
# # # @app.get("/api/agents/{agent_id}")
# # # def agent_detail(
# # #     agent_id: int,
# # #     start: Optional[str] = None,
# # #     end: Optional[str] = None,
# # #     db = Depends(get_db),
# # # ):
# # #     start, end = date_range_defaults(start, end)
# # #     agent = db.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
# # #     if not agent:
# # #         raise HTTPException(status_code=404, detail="Agent not found")

# # #     calls_row = db.execute(
# # #         """
# # #         SELECT COUNT(*) AS total_calls,
# # #                SUM(CASE WHEN overall_quality = 'good' THEN 1 ELSE 0 END) AS good_calls,
# # #                SUM(CASE WHEN overall_quality = 'poor' THEN 1 ELSE 0 END) AS poor_calls,
# # #                ROUND(AVG(overall_score), 1) AS average_call_score
# # #         FROM calls
# # #         WHERE agent_id = ? AND status = 'audited'
# # #           AND call_date IS NOT NULL AND date(call_date) BETWEEN date(?) AND date(?)
# # #         """,
# # #         (agent_id, start, end),
# # #     ).fetchone()

# # #     param_rows = db.execute(
# # #         """
# # #         SELECT p.label AS parameter, ROUND(AVG(s.score),1) AS avg_score
# # #         FROM call_scores s
# # #         JOIN parameters p ON p.id = s.parameter_id
# # #         JOIN calls c ON c.id = s.call_id
# # #         WHERE c.agent_id = ? AND c.call_date IS NOT NULL AND date(c.call_date) BETWEEN date(?) AND date(?)
# # #         GROUP BY p.label
# # #         ORDER BY p.id
# # #         """,
# # #         (agent_id, start, end),
# # #     ).fetchall()

# # #     params = [dict(r) for r in param_rows]
# # #     if params:
# # #         sorted_params = sorted(params, key=lambda x: x["avg_score"] or 0, reverse=True)
# # #         strengths = [p["parameter"] for p in sorted_params[:3]]
# # #         improvements = [p["parameter"] for p in sorted_params[-3:]]
# # #     else:
# # #         strengths, improvements = [], []

# # #     return {
# # #         "agent": {"id": agent["id"], "name": agent["name"], "team": agent["team"]},
# # #         "start": start,
# # #         "end": end,
# # #         "total_calls": calls_row["total_calls"] or 0,
# # #         "average_call_score": calls_row["average_call_score"] or 0,
# # #         "good_calls": calls_row["good_calls"] or 0,
# # #         "poor_calls": calls_row["poor_calls"] or 0,
# # #         "parameters": params,
# # #         "overall_summary": {
# # #             "strengths": strengths,
# # #             "improvements": improvements,
# # #         },
# # #     }


# # # # ==================================================================
# # # # Feature 2: clickable stat cards -> agents -> that agent's calls.
# # # # category is one of the CATEGORY_FILTERS keys (total/audited/pending/positive/negative),
# # # # matching whichever stat card was clicked on the dashboard.
# # # # ==================================================================

# # # @app.get("/api/dashboard/category/{category}/agents")
# # # def category_agents(
# # #     category: str,
# # #     start: Optional[str] = None,
# # #     end: Optional[str] = None,
# # #     db = Depends(get_db),
# # # ):
# # #     """2.1 - agents ranked by their average score, scoped to the clicked category."""
# # #     where_clause = category_filter_sql(category)
# # #     start, end = date_range_defaults(start, end)
# # #     rows = db.execute(
# # #         f"""
# # #         SELECT a.id AS agent_id, a.name AS agent_name, a.team,
# # #                ROUND(AVG(c.overall_score), 1) AS avg_score,
# # #                COUNT(*) AS call_count
# # #         FROM calls c
# # #         JOIN agents a ON a.id = c.agent_id
# # #         WHERE {where_clause}
# # #           AND c.call_date IS NOT NULL AND date(c.call_date) BETWEEN date(?) AND date(?)
# # #         GROUP BY a.id
# # #         ORDER BY avg_score DESC
# # #         """,
# # #         (start, end),
# # #     ).fetchall()
# # #     return {
# # #         "category": category,
# # #         "start": start,
# # #         "end": end,
# # #         "agents": [dict(r) for r in rows],
# # #     }


# # # @app.get("/api/dashboard/category/{category}/agents/{agent_id}/calls")
# # # def category_agent_calls(
# # #     category: str,
# # #     agent_id: int,
# # #     start: Optional[str] = None,
# # #     end: Optional[str] = None,
# # #     db = Depends(get_db),
# # # ):
# # #     """2.2 - that agent's individual calls (with score) within the clicked category."""
# # #     where_clause = category_filter_sql(category)
# # #     start, end = date_range_defaults(start, end)
# # #     agent = db.execute("SELECT id, name, team FROM agents WHERE id = ?", (agent_id,)).fetchone()
# # #     if not agent:
# # #         raise HTTPException(status_code=404, detail="Agent not found")

# # #     calls = db.execute(
# # #         f"""
# # #         SELECT c.id, c.call_number, c.call_start_time, c.call_date, c.status,
# # #                c.sentiment, c.overall_quality, c.overall_score, c.verdict, c.recording_base
# # #         FROM calls c
# # #         WHERE c.agent_id = ? AND {where_clause}
# # #           AND c.call_date IS NOT NULL AND date(c.call_date) BETWEEN date(?) AND date(?)
# # #         ORDER BY c.call_date DESC, c.id DESC 
# # #         """,
# # #         (agent_id, start, end),
# # #     ).fetchall()
# # #     return {
# # #         "category": category,
# # #         "agent": dict(agent),
# # #         "start": start,
# # #         "end": end,
# # #         "calls": [dict(r) for r in calls],
# # #     }


# # # # ==================================================================
# # # # Feature 3: top-10 agents leaderboard (right-hand sidebar box)
# # # # ==================================================================

# # # # @app.get("/api/dashboard/top-agents")
# # # # def top_agents(
# # # #     start: Optional[str] = None,
# # # #     end: Optional[str] = None,
# # # #     limit: int = 10,
# # # #     db = Depends(get_db),
# # # # ):
# # # #     start, end = date_range_defaults(start, end)
# # # #     limit = max(1, min(limit, 50))  # sane bounds, this only ever backs a small sidebar widget
# # # #     rows = db.execute(
# # # #         """
# # # #         SELECT a.id AS agent_id, a.name AS agent_name, a.team,
# # # #                ROUND(AVG(c.overall_score), 1) AS avg_score,
# # # #                COUNT(*) AS call_count
# # # #         FROM calls c
# # # #         JOIN agents a ON a.id = c.agent_id
# # # #         WHERE c.status = 'audited'
# # # #           AND c.call_date IS NOT NULL AND date(c.call_date) BETWEEN date(?) AND date(?)
# # # #         GROUP BY a.id
# # # #         ORDER BY avg_score DESC
# # # #         LIMIT ?
# # # #         """,
# # # #         (start, end, limit),
# # # #     ).fetchall()
# # # #     return {
# # # #         "start": start,
# # # #         "end": end,
# # # #         "agents": [dict(r) for r in rows],
# # # #     }


# # # @app.get("/api/dashboard/top-agents")
# # # def top_agents(
# # #     start: Optional[str] = None,
# # #     end: Optional[str] = None,
# # #     limit: int = 10,
# # #     db = Depends(get_db),
# # # ):
# # #     start, end = date_range_defaults(start, end)
# # #     limit = max(1, min(limit, 50))  # sane bounds, this only ever backs a small sidebar widget
# # #     rows = db.execute(
# # #         """
# # #         SELECT a.id AS agent_id, a.name AS agent_name, a.team,
# # #                ROUND(AVG(c.overall_score), 1) AS avg_score,
# # #                COUNT(*) AS call_count
# # #         FROM calls c
# # #         JOIN agents a ON a.id = c.agent_id
# # #         WHERE c.status = 'audited'
# # #           AND c.call_date IS NOT NULL AND date(c.call_date) BETWEEN date(?) AND date(?)
# # #         GROUP BY a.id, a.name, a.team
# # #         ORDER BY avg_score DESC
# # #         LIMIT ?
# # #         """,
# # #         (start, end, limit),
# # #     ).fetchall()
# # #     return {
# # #         "start": start,
# # #         "end": end,
# # #         "agents": [dict(r) for r in rows],
# # #     }

# # # # ==================================================================
# # # # Manifest upload + STT/SLM pipeline
# # # # ==================================================================

# # # @app.post("/api/manifest/upload")
# # # async def upload_manifest(
# # #     background_tasks: BackgroundTasks,
# # #     file: UploadFile = File(...),
# # #     recording_base_url: Optional[str] = None,
# # #     auto_process: bool = True,
# # # ):
# # #     if not file.filename.lower().endswith((".xlsx", ".xlsm")):
# # #         raise HTTPException(status_code=400, detail="Please upload an .xlsx manifest file")

# # #     with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
# # #         shutil.copyfileobj(file.file, tmp)
# # #         tmp_path = tmp.name

# # #     try:
# # #         manifest_id, row_count = save_manifest(
# # #             tmp_path, file.filename, recording_base_url or RECORDINGS_BASE_URL
# # #         )
# # #         print(f"Manifest uploaded: {manifest_id}, Rows: {row_count}")
# # #     finally:
# # #         os.unlink(tmp_path)

# # #     if auto_process:
# # #         print("Triggering background processing for manifest:", manifest_id)
# # #         background_tasks.add_task(process_manifest, manifest_id)

# # #     return {"manifest_id": manifest_id, "rows_loaded": row_count, "processing": auto_process}


# # # @app.post("/api/manifest/{manifest_id}/process")
# # # def trigger_processing(manifest_id: int, background_tasks: BackgroundTasks, db = Depends(get_db)):
# # #     row = db.execute("SELECT id FROM manifests WHERE id = ?", (manifest_id,)).fetchone()
# # #     if not row:
# # #         raise HTTPException(status_code=404, detail="Manifest not found")
# # #     background_tasks.add_task(process_manifest, manifest_id)
# # #     return {"manifest_id": manifest_id, "processing": True}


# # # @app.get("/api/manifest")
# # # def list_manifests(db = Depends(get_db)):
# # #     rows = db.execute("SELECT * FROM manifests ORDER BY id DESC").fetchall()
# # #     return {"manifests": [dict(r) for r in rows]}


# # # @app.get("/api/manifest/{manifest_id}")
# # # def manifest_status(manifest_id: int, db = Depends(get_db)):
# # #     manifest = db.execute("SELECT * FROM manifests WHERE id = ?", (manifest_id,)).fetchone()
# # #     if not manifest:
# # #         raise HTTPException(status_code=404, detail="Manifest not found")
# # #     calls = db.execute(
# # #         """SELECT c.id, c.call_number, c.recording_base, c.status, c.error_message,
# # #                   c.overall_score, c.verdict, a.name AS agent_name
# # #            FROM calls c JOIN agents a ON a.id = c.agent_id
# # #            WHERE c.manifest_id = ? ORDER BY c.id""",
# # #         (manifest_id,), 
# # #     ).fetchall() 
# # #     return {"manifest": dict(manifest), "calls": [dict(r) for r in calls]} 


# # # @app.get("/api/calls/{call_id}/transcript")
# # # def call_transcript(call_id: int, db = Depends(get_db)):
# # #     row = db.execute("SELECT * FROM transcripts WHERE call_id = ?", (call_id,)).fetchone()
# # #     if not row:
# # #         raise HTTPException(status_code=404, detail="No transcript for this call yet")
# # #     return dict(row)


# # # @app.get("/api/calls/{call_id}")
# # # def call_detail(call_id: int, db = Depends(get_db)):
# # #     print(f"Fetching detail for call ID: {call_id}")
# # #     # `c.*` already includes rx_path (IN leg) and tx_path (OUT leg) - the same
# # #     # recording URLs the STT service was given. The frontend call-detail page
# # #     # (feature 2.3) plays these directly and seeks to each parameter's
# # #     # evidence start_time so a reviewer can jump straight to the moment a
# # #     # score was decided, instead of listening to the whole call.
# # #     call = db.execute(
# # #         """SELECT c.*, a.name AS agent_name FROM calls c JOIN agents a ON a.id = c.agent_id
# # #            WHERE c.id = ?""",
# # #         (call_id,),
# # #     ).fetchone()
# # #     if not call:
# # #         raise HTTPException(status_code=404, detail="Call not found")
# # #     scores = db.execute(
# # #         """SELECT p.label AS parameter, s.status, s.reason, s.evidence, s.start_time, s.end_time,
# # #                   s.raw_score, s.score
# # #            FROM call_scores s JOIN parameters p ON p.id = s.parameter_id
# # #            WHERE s.call_id = ? ORDER BY p.id""",
# # #         (call_id,),
# # #     ).fetchall()
# # #     fatal = db.execute("SELECT * FROM fatal_checks WHERE call_id = ?", (call_id,)).fetchall()
# # #     # print(dict(call), [dict(r) for r in scores], [dict(r) for r in fatal])
# # #     call_dict = dict(call)
# # #     print(call_dict)
# # #     if call_dict.get("recording_base"):
# # #         call_dict["mono_url"] = f"{RECORDINGS_PLAYBACK_BASE_URL.rstrip('/')}/{call_dict['recording_base']}"
# # #     else:
# # #         call_dict["mono_url"] = None
# # #     print(f" after adding the mono url\n{call_dict}")
# # #     return {
# # #         "call": dict(call_dict),
# # #         "parameters": [dict(r) for r in scores],
# # #         "fatal_checks": [dict(r) for r in fatal],
# # #     }


# # # @app.get("/api/health")
# # # def health(db = Depends(get_db)):
# # #     """Hits the DB too, so this actually tells you whether MySQL is reachable,
# # #     not just whether the FastAPI process is up."""
# # #     db.execute("SELECT 1")
# # #     return {"status": "ok"}


# # # # to change the password as well 
# # # class ChangePasswordRequest(BaseModel):
# # #     username: str
# # #     current_password: str
# # #     new_password: str

# # # @app.post("/api/change-password")
# # # def change_password(req: ChangePasswordRequest, db = Depends(get_db)):
# # #     row = db.execute(
# # #         "SELECT * FROM users WHERE username = ? AND password_hash = ?",
# # #         (req.username, hash_pw(req.current_password)),
# # #     ).fetchone()
# # #     if not row:
# # #         raise HTTPException(status_code=401, detail="Current password is incorrect")
# # #     if len(req.new_password) < 8:
# # #         raise HTTPException(status_code=400, detail="New password must be at least 8 characters")
# # #     db.execute("UPDATE users SET password_hash = ? WHERE username = ?", (hash_pw(req.new_password), req.username))
# # #     db.commit()
# # #     return {"status": "ok"}
# # # *******************************w4*************************************************
# # # import os
# # # import shutil
# # # import tempfile
# # # from datetime import datetime, timedelta
# # # from typing import Optional

# # # import pymysql
# # # from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Request, UploadFile
# # # from fastapi.middleware.cors import CORSMiddleware
# # # from fastapi.responses import JSONResponse
# # # from pydantic import BaseModel

# # # import hashlib

# # # # from app4_.backend.config import RECOR, RECORDINGS_PLAYBACK_BASE_URLDINGS_PLAYBACK_BASE_URL
# # # from config import PARAMETERS, RECORDINGS_BASE_URL, RECORDINGS_PLAYBACK_BASE_URL
# # # from db import get_conn, init_db
# # # from pipeline import process_manifest, save_manifest

# # # app = FastAPI(title="Athena QA Dashboard")

# # # app.add_middleware(
# # #     CORSMiddleware,
# # #     allow_origins=[
# # #         "http://localhost:5173",
# # #         "http://127.0.0.1:5173",
# # #     ],
# # #     allow_credentials=True,
# # #     allow_methods=["*"],
# # #     allow_headers=["*"],
# # # )

# # # # ------------------------------------------------------------------
# # # # IMPORTANT FIX: init_db() used to run unguarded at import time. If MySQL
# # # # was unreachable (wrong host/user/password, or the server just wasn't up
# # # # yet), that raised an exception WHILE the module was loading - before
# # # # FastAPI/CORSMiddleware even finished being built. uvicorn's worker
# # # # process died right there, so nothing was listening on the port at all.
# # # #
# # # # That's exactly what showed up in the browser as CORS errors + net::ERR_FAILED:
# # # # there was no server to send a response (let alone CORS headers), so Chrome
# # # # reports it as a blocked/failed request instead of a clean 500.
# # # #
# # # # Fix: catch the startup failure, log it clearly, and let the app come up
# # # # anyway. Every DB-touching request will then fail with a clean 503 JSON
# # # # response (see exception handler below) that DOES carry CORS headers,
# # # # instead of the whole server being unreachable.
# # # # ------------------------------------------------------------------
# # # try:
# # #     init_db()
# # # except Exception as e:
# # #     print("=" * 70)
# # #     print("[STARTUP WARNING] Could not connect to MySQL / initialize schema.")
# # #     print(f"[STARTUP WARNING] {type(e).__name__}: {e}")
# # #     print("[STARTUP WARNING] Check MYSQL_HOST / MYSQL_PORT / MYSQL_USER / "
# # #           "MYSQL_PASSWORD / MYSQL_DATABASE env vars and that MySQL is running.")
# # #     print("[STARTUP WARNING] The API is still starting so you get clean 503s "
# # #           "instead of connection failures - fix your DB config and restart.")
# # #     print("=" * 70)


# # # @app.exception_handler(pymysql.err.Error)
# # # async def mysql_error_handler(request: Request, exc: pymysql.err.Error):
# # #     return JSONResponse(
# # #         status_code=503,
# # #         content={"detail": f"Database unavailable: {exc}"},
# # #     )


# # # def get_db():
# # #     conn = get_conn()
# # #     try:
# # #         yield conn
# # #     finally:
# # #         conn.close()


# # # def hash_pw(pw: str) -> str:
# # #     return hashlib.sha256(pw.encode()).hexdigest()


# # # # ---------- Schemas ----------
# # # class LoginRequest(BaseModel):
# # #     username: str
# # #     password: str


# # # class LoginResponse(BaseModel):
# # #     token: str
# # #     username: str
# # #     role: str


# # # # ---------- Auth ----------
# # # @app.post("/api/login", response_model=LoginResponse)
# # # def login(req: LoginRequest, db = Depends(get_db)):
# # #     row = db.execute(
# # #         "SELECT * FROM users WHERE username = ? AND password_hash = ?",
# # #         (req.username, hash_pw(req.password)),
# # #     ).fetchone()
# # #     if not row:
# # #         raise HTTPException(status_code=401, detail="Invalid username or password")
# # #     token = hashlib.sha256(f"{req.username}{datetime.utcnow()}".encode()).hexdigest()
# # #     return LoginResponse(token=token, username=row["username"], role=row["role"])


# # # class ChangePasswordRequest(BaseModel):
# # #     username: str
# # #     current_password: str
# # #     new_password: str


# # # @app.post("/api/change-password")
# # # def change_password(req: ChangePasswordRequest, db = Depends(get_db)):
# # #     # There's no session/bearer-token check anywhere else in this app yet
# # #     # (login just hands back an unverified token) - so identity here is
# # #     # proven the same way login proves it: username + correct current
# # #     # password. If you add real session auth later, swap this for a
# # #     # Depends(current_user) check instead of trusting the request body.
# # #     if len(req.new_password) < 8:
# # #         raise HTTPException(status_code=400, detail="New password must be at least 8 characters")

# # #     row = db.execute(
# # #         "SELECT * FROM users WHERE username = ? AND password_hash = ?",
# # #         (req.username, hash_pw(req.current_password)),
# # #     ).fetchone()
# # #     if not row:
# # #         raise HTTPException(status_code=401, detail="Current password is incorrect")

# # #     db.execute(
# # #         "UPDATE users SET password_hash = ? WHERE username = ?",
# # #         (hash_pw(req.new_password), req.username),
# # #     )
# # #     db.commit()
# # #     return {"status": "ok"}


# # # # ---------- Helpers ----------
# # # def date_range_defaults(start: Optional[str], end: Optional[str]):
# # #     if not end:
# # #         end = datetime.utcnow().strftime("%Y-%m-%d")
# # #     if not start:
# # #         start = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
# # #     return start, end


# # # # Whitelist of SQL fragments for the 5 clickable dashboard stat cards (feature 2).
# # # # Kept as a fixed dict (not built from the path param) so there's no injection risk
# # # # even though it gets interpolated straight into the query string below.
# # # CATEGORY_FILTERS = {
# # #     "total": "1=1",
# # #     "audited": "c.status = 'audited'",
# # #     "pending": "c.status IN ('pending','transcribing','analyzing')",
# # #     "positive": "c.sentiment = 'positive'",
# # #     "negative": "c.sentiment = 'negative'",
# # #     # Disposition-based (feature: below the funnel). Case-insensitive since
# # #     # the manifest's call_end_type_name casing isn't guaranteed consistent
# # #     # (we've seen "CALLBACK" in real data). "Correct"/"Wrong" pair (actual vs
# # #     # predicted disposition) is intentionally not wired up yet - needs a
# # #     # ground-truth "actual disposition" field the pipeline doesn't capture yet.
# # #     "disp_not_eligible": "UPPER(c.call_end_type_name) = 'NOT ELIGIBLE'",
# # #     "disp_not_interested": "UPPER(c.call_end_type_name) = 'NOT INTERESTED'",
# # #     "disp_callback": "UPPER(c.call_end_type_name) = 'CALLBACK'",
# # # }

# # # # Labels + ordering for the disposition buttons row on the dashboard.
# # # DISPOSITION_CATEGORIES = [
# # #     ("disp_not_eligible", "Not Eligible", "NOT ELIGIBLE"),
# # #     ("disp_not_interested", "Not Interested", "NOT INTERESTED"),
# # #     ("disp_callback", "Callback", "CALLBACK"),
# # # ]


# # # def category_filter_sql(category: str) -> str:
# # #     if category not in CATEGORY_FILTERS:
# # #         raise HTTPException(
# # #             status_code=400,
# # #             detail=f"Unknown category '{category}'. Must be one of: {', '.join(CATEGORY_FILTERS)}",
# # #         )
# # #     return CATEGORY_FILTERS[category]


# # # # ---------- Dashboard summary (Section 2 top cards) ----------
# # # @app.get("/api/dashboard/summary")
# # # def dashboard_summary(
# # #     start: Optional[str] = None,
# # #     end: Optional[str] = None,
# # #     db = Depends(get_db),
# # # ):
# # #     start, end = date_range_defaults(start, end)
# # #     row = db.execute(
# # #         """
# # #         SELECT
# # #           COUNT(*) AS total_calls,
# # #           SUM(CASE WHEN status = 'audited' THEN 1 ELSE 0 END) AS audited_calls,
# # #           SUM(CASE WHEN status IN ('pending','transcribing','analyzing') THEN 1 ELSE 0 END) AS pending_calls,
# # #           SUM(CASE WHEN sentiment = 'positive' THEN 1 ELSE 0 END) AS positive_calls,
# # #           SUM(CASE WHEN sentiment = 'negative' THEN 1 ELSE 0 END) AS negative_calls
# # #         FROM calls
# # #         WHERE call_date IS NOT NULL AND date(call_date) BETWEEN date(?) AND date(?)
# # #         """,
# # #         (start, end),
# # #     ).fetchone()
# # #     return {
# # #         "start": start,
# # #         "end": end,
# # #         "total_calls": row["total_calls"] or 0,
# # #         "audited_calls": row["audited_calls"] or 0,
# # #         "pending_calls": row["pending_calls"] or 0,
# # #         "positive_calls": row["positive_calls"] or 0,
# # #         "negative_calls": row["negative_calls"] or 0,
# # #     }


# # # # ---------- Disposition counts (feature: buttons below the funnel) ----------
# # # # One grouped query for all dispositions (not one query per button), then map
# # # # onto the 3 known categories. Drill-down reuses the exact same
# # # # /api/dashboard/category/{category}/... endpoints as the 5 stat cards do -
# # # # "disp_not_eligible" etc are just more entries in CATEGORY_FILTERS above.
# # # @app.get("/api/dashboard/dispositions")
# # # def dashboard_dispositions(
# # #     start: Optional[str] = None,
# # #     end: Optional[str] = None,
# # #     db = Depends(get_db),
# # # ):
# # #     start, end = date_range_defaults(start, end)
# # #     rows = db.execute(
# # #         """
# # #         SELECT UPPER(call_end_type_name) AS disp, COUNT(*) AS n
# # #         FROM calls
# # #         WHERE call_date IS NOT NULL AND date(call_date) BETWEEN date(?) AND date(?)
# # #           AND call_end_type_name IS NOT NULL AND call_end_type_name != ''
# # #         GROUP BY UPPER(call_end_type_name)
# # #         """,
# # #         (start, end),
# # #     ).fetchall()

# # #     print(rows)
# # #     counts = {r["disp"]: r["n"] for r in rows}

# # #     return {
# # #         "start": start,
# # #         "end": end,
# # #         "dispositions": [
# # #             {"category": cat, "label": label, "count": counts.get(match_value, 0)}
# # #             for cat, label, match_value in DISPOSITION_CATEGORIES
# # #         ],
# # #     }


# # # # ---------- Parameter-wise average scores (graph) ----------
# # # @app.get("/api/dashboard/parameters")
# # # def dashboard_parameters(
# # #     start: Optional[str] = None,
# # #     end: Optional[str] = None,
# # #     db = Depends(get_db),
# # # ):
# # #     start, end = date_range_defaults(start, end)
# # #     rows = db.execute(
# # #         """
# # #         SELECT p.label AS parameter, ROUND(AVG(s.score), 1) AS avg_score, COUNT(*) AS n
# # #         FROM call_scores s
# # #         JOIN parameters p ON p.id = s.parameter_id
# # #         JOIN calls c ON c.id = s.call_id
# # #         WHERE c.call_date IS NOT NULL AND date(c.call_date) BETWEEN date(?) AND date(?)
# # #         GROUP BY p.id, p.label
# # #         ORDER BY p.id
# # #         """,
# # #         (start, end),
# # #     ).fetchall()
# # #     return {
# # #         "start": start,
# # #         "end": end,
# # #         "parameters": [
# # #             {"parameter": r["parameter"], "avg_score": r["avg_score"] or 0, "sample_size": r["n"]}
# # #             for r in rows
# # #         ],
# # #     }


# # # # ---------- Parameter success-rate funnel (feature 1) ----------
# # # # Different from /api/dashboard/parameters above (which averages the 0-100
# # # # normalized score). This instead answers "out of every AUDITED call, what
# # # # % actually PASSED (status=true) this parameter?" - sorted highest-first so
# # # # it reads top-to-bottom like a funnel: parameters almost everyone nails at
# # # # the top, parameters agents struggle with at the bottom.
# # # #
# # # # success_pct = (# audited calls where that parameter's status = TRUE)
# # # #               / (total audited calls in range) * 100
# # # #
# # # # Two queries total (not N+1): one scalar for the constant denominator
# # # # (total audited calls), one GROUP BY for all 16 numerators together.
# # # @app.get("/api/dashboard/parameter-funnel")
# # # def parameter_funnel(
# # #     start: Optional[str] = None,
# # #     end: Optional[str] = None,
# # #     db = Depends(get_db),
# # # ):
# # #     start, end = date_range_defaults(start, end)

# # #     total_row = db.execute(
# # #         """
# # #         SELECT COUNT(*) AS total_audited
# # #         FROM calls
# # #         WHERE status = 'audited' AND call_date IS NOT NULL
# # #           AND date(call_date) BETWEEN date(?) AND date(?)
# # #         """,
# # #         (start, end),
# # #     ).fetchone()
# # #     total_audited = total_row["total_audited"] or 0

# # #     rows = db.execute(
# # #         """
# # #         SELECT p.id AS parameter_id, p.label AS parameter,
# # #                SUM(CASE WHEN s.status = 1 THEN 1 ELSE 0 END) AS success_count,
# # #                COUNT(*) AS sample_size
# # #         FROM call_scores s
# # #         JOIN parameters p ON p.id = s.parameter_id
# # #         JOIN calls c ON c.id = s.call_id
# # #         WHERE c.status = 'audited' AND c.call_date IS NOT NULL
# # #           AND date(c.call_date) BETWEEN date(?) AND date(?)
# # #         GROUP BY p.id, p.label
# # #         ORDER BY p.id
# # #         """,
# # #         (start, end),
# # #     ).fetchall()

# # #     funnel = []
# # #     for r in rows:
# # #         success_count = r["success_count"] or 0
# # #         pct = round((success_count / total_audited) * 100, 1) if total_audited else 0.0
# # #         funnel.append({
# # #             "parameter_id": r["parameter_id"],
# # #             "parameter": r["parameter"],
# # #             "success_count": success_count,
# # #             "sample_size": r["sample_size"],
# # #             "success_pct": pct,
# # #         })

# # #     funnel.sort(key=lambda x: x["success_pct"], reverse=True)

# # #     return {
# # #         "start": start,
# # #         "end": end,
# # #         "total_audited_calls": total_audited,
# # #         "parameters": funnel,
# # #     }



# # # # ---------- Agents belonging to a parameter (click on bar) ----------
# # # @app.get("/api/dashboard/parameters/{parameter_name}/agents")
# # # def agents_for_parameter(
# # #     parameter_name: str,
# # #     start: Optional[str] = None,
# # #     end: Optional[str] = None,
# # #     db = Depends(get_db),
# # # ):
# # #     start, end = date_range_defaults(start, end)
# # #     rows = db.execute(
# # #         """
# # #         SELECT a.id AS agent_id,
# # #                a.name AS agent_name,
# # #                a.team,
# # #                ROUND(AVG(s.score), 1) AS avg_score,
# # #                COUNT(*) AS calls_scored
# # #         FROM call_scores s
# # #         JOIN parameters p ON p.id = s.parameter_id
# # #         JOIN calls c ON c.id = s.call_id
# # #         JOIN agents a ON a.id = c.agent_id
# # #         WHERE p.label = ?
# # #           AND c.call_date IS NOT NULL
# # #           AND date(c.call_date) BETWEEN date(?) AND date(?)
# # #         GROUP BY a.id, a.name, a.team
# # #         ORDER BY avg_score ASC
# # #         """,
# # #         (parameter_name, start, end),
# # #     ).fetchall()
# # #     if not rows:
# # #         exists = db.execute("SELECT 1 FROM parameters WHERE label = ?", (parameter_name,)).fetchone()
# # #         if not exists:
# # #             raise HTTPException(status_code=404, detail="Parameter not found")
# # #     return {
# # #         "parameter": parameter_name,
# # #         "start": start,
# # #         "end": end,
# # #         "agents": [dict(r) for r in rows],
# # #     }


# # # # ---------- Agent list (basic) ----------
# # # @app.get("/api/agents")
# # # def list_agents(db = Depends(get_db)):
# # #     rows = db.execute("SELECT id, name, team FROM agents ORDER BY name").fetchall()
# # #     return {"agents": [dict(r) for r in rows]}


# # # # ---------- Agent detail (Section 3) ----------
# # # @app.get("/api/agents/{agent_id}")
# # # def agent_detail(
# # #     agent_id: int,
# # #     start: Optional[str] = None,
# # #     end: Optional[str] = None,
# # #     db = Depends(get_db),
# # # ):
# # #     start, end = date_range_defaults(start, end)
# # #     agent = db.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
# # #     if not agent:
# # #         raise HTTPException(status_code=404, detail="Agent not found")

# # #     calls_row = db.execute(
# # #         """
# # #         SELECT COUNT(*) AS total_calls,
# # #                SUM(CASE WHEN overall_quality = 'good' THEN 1 ELSE 0 END) AS good_calls,
# # #                SUM(CASE WHEN overall_quality = 'poor' THEN 1 ELSE 0 END) AS poor_calls,
# # #                ROUND(AVG(overall_score), 1) AS average_call_score
# # #         FROM calls
# # #         WHERE agent_id = ? AND status = 'audited'
# # #           AND call_date IS NOT NULL AND date(call_date) BETWEEN date(?) AND date(?)
# # #         """,
# # #         (agent_id, start, end),
# # #     ).fetchone()

# # #     param_rows = db.execute(
# # #         """
# # #         SELECT p.label AS parameter, ROUND(AVG(s.score),1) AS avg_score
# # #         FROM call_scores s
# # #         JOIN parameters p ON p.id = s.parameter_id
# # #         JOIN calls c ON c.id = s.call_id
# # #         WHERE c.agent_id = ? AND c.call_date IS NOT NULL AND date(c.call_date) BETWEEN date(?) AND date(?)
# # #         GROUP BY p.id, p.label
# # #         ORDER BY p.id
# # #         """,
# # #         (agent_id, start, end),
# # #     ).fetchall()

# # #     params = [dict(r) for r in param_rows]
# # #     if params:
# # #         sorted_params = sorted(params, key=lambda x: x["avg_score"] or 0, reverse=True)
# # #         strengths = [p["parameter"] for p in sorted_params[:3]]
# # #         improvements = [p["parameter"] for p in sorted_params[-3:]]
# # #     else:
# # #         strengths, improvements = [], []

# # #     return {
# # #         "agent": {"id": agent["id"], "name": agent["name"], "team": agent["team"]},
# # #         "start": start,
# # #         "end": end,
# # #         "total_calls": calls_row["total_calls"] or 0,
# # #         "average_call_score": calls_row["average_call_score"] or 0,
# # #         "good_calls": calls_row["good_calls"] or 0,
# # #         "poor_calls": calls_row["poor_calls"] or 0,
# # #         "parameters": params,
# # #         "overall_summary": {
# # #             "strengths": strengths,
# # #             "improvements": improvements,
# # #         },
# # #     }


# # # # ==================================================================
# # # # Feature 2: clickable stat cards -> agents -> that agent's calls.
# # # # category is one of the CATEGORY_FILTERS keys (total/audited/pending/positive/negative),
# # # # matching whichever stat card was clicked on the dashboard.
# # # # ==================================================================

# # # @app.get("/api/dashboard/category/{category}/agents")
# # # def category_agents(
# # #     category: str,
# # #     start: Optional[str] = None,
# # #     end: Optional[str] = None,
# # #     db = Depends(get_db),
# # # ):
# # #     """2.1 - agents ranked by their average score, scoped to the clicked category."""
# # #     where_clause = category_filter_sql(category)
# # #     start, end = date_range_defaults(start, end)
# # #     rows = db.execute(
# # #         f"""
# # #         SELECT a.id AS agent_id, a.name AS agent_name, a.team,
# # #                ROUND(AVG(c.overall_score), 1) AS avg_score,
# # #                COUNT(*) AS call_count
# # #         FROM calls c
# # #         JOIN agents a ON a.id = c.agent_id
# # #         WHERE {where_clause}
# # #           AND c.call_date IS NOT NULL AND date(c.call_date) BETWEEN date(?) AND date(?)
# # #         GROUP BY a.id, a.name, a.team
# # #         ORDER BY avg_score DESC
# # #         """,
# # #         (start, end),
# # #     ).fetchall()
# # #     return {
# # #         "category": category,
# # #         "start": start,
# # #         "end": end,
# # #         "agents": [dict(r) for r in rows],
# # #     }


# # # @app.get("/api/dashboard/category/{category}/agents/{agent_id}/calls")
# # # def category_agent_calls(
# # #     category: str,
# # #     agent_id: int,
# # #     start: Optional[str] = None,
# # #     end: Optional[str] = None,
# # #     db = Depends(get_db),
# # # ):
# # #     """2.2 - that agent's individual calls (with score) within the clicked category."""
# # #     where_clause = category_filter_sql(category)
# # #     start, end = date_range_defaults(start, end)
# # #     agent = db.execute("SELECT id, name, team FROM agents WHERE id = ?", (agent_id,)).fetchone()
# # #     if not agent:
# # #         raise HTTPException(status_code=404, detail="Agent not found")

# # #     calls = db.execute(
# # #         f"""
# # #         SELECT c.id, c.call_number, c.call_start_time, c.call_date, c.status,
# # #                c.sentiment, c.overall_quality, c.overall_score, c.verdict, c.recording_base
# # #         FROM calls c
# # #         WHERE c.agent_id = ? AND {where_clause}
# # #           AND c.call_date IS NOT NULL AND date(c.call_date) BETWEEN date(?) AND date(?)
# # #         ORDER BY c.call_date DESC, c.id DESC
# # #         """,
# # #         (agent_id, start, end),
# # #     ).fetchall()
# # #     return {
# # #         "category": category,
# # #         "agent": dict(agent),
# # #         "start": start,
# # #         "end": end,
# # #         "calls": [dict(r) for r in calls],
# # #     }


# # # # ==================================================================
# # # # Feature 3: top-10 agents leaderboard (right-hand sidebar box)
# # # # ==================================================================

# # # @app.get("/api/dashboard/top-agents")
# # # def top_agents(
# # #     start: Optional[str] = None,
# # #     end: Optional[str] = None,
# # #     limit: int = 10,
# # #     db = Depends(get_db),
# # # ):
# # #     start, end = date_range_defaults(start, end)
# # #     limit = max(1, min(limit, 50))  # sane bounds, this only ever backs a small sidebar widget
# # #     rows = db.execute(
# # #         """
# # #         SELECT a.id AS agent_id, a.name AS agent_name, a.team,
# # #                ROUND(AVG(c.overall_score), 1) AS avg_score,
# # #                COUNT(*) AS call_count
# # #         FROM calls c
# # #         JOIN agents a ON a.id = c.agent_id
# # #         WHERE c.status = 'audited'
# # #           AND c.call_date IS NOT NULL AND date(c.call_date) BETWEEN date(?) AND date(?)
# # #         GROUP BY a.id, a.name, a.team
# # #         ORDER BY avg_score DESC
# # #         LIMIT ?
# # #         """,
# # #         (start, end, limit),
# # #     ).fetchall()
# # #     return {
# # #         "start": start,
# # #         "end": end,
# # #         "agents": [dict(r) for r in rows],
# # #     }



# # # REPORT_SORTS = {
# # #     "call_date_desc": "c.call_date DESC, c.id DESC",
# # #     "call_date_asc": "c.call_date ASC, c.id ASC",
# # #     "score_desc": "c.overall_score DESC",
# # #     "score_asc": "c.overall_score ASC",
# # #     "agent_name": "a.name ASC, c.call_date DESC",
# # # }

# # # @app.get("/api/reports/agent-scores")
# # # def agent_scores_report(
# # #     agent_id: Optional[int] = None,
# # #     start: Optional[str] = None,
# # #     end: Optional[str] = None,
# # #     page: int = 1,
# # #     page_size: int = 25,
# # #     sort: str = "call_date_desc",
# # #     db = Depends(get_db),
# # # ):
# # #     start, end = date_range_defaults(start, end)
# # #     page = max(1, page)
# # #     page_size = max(1, min(page_size, 200))  # cap it - this is a report table, not a data dump endpoint
# # #     offset = (page - 1) * page_size
# # #     order_sql = REPORT_SORTS.get(sort, REPORT_SORTS["call_date_desc"])

# # #     where = [
# # #         "c.status = 'audited'",
# # #         "c.call_date IS NOT NULL",
# # #         "date(c.call_date) BETWEEN date(?) AND date(?)",
# # #     ]
# # #     params = [start, end]
# # #     if agent_id:
# # #         where.append("c.agent_id = ?")
# # #         params.append(agent_id)
# # #     where_sql = " AND ".join(where)

# # #     total_row = db.execute(
# # #         f"SELECT COUNT(*) AS n FROM calls c JOIN agents a ON a.id = c.agent_id WHERE {where_sql}",
# # #         tuple(params),
# # #     ).fetchone()
# # #     total = total_row["n"] or 0

# # #     # One column per parameter via conditional aggregation (MySQL has no PIVOT).
# # #     # param_key values come from our own hardcoded PARAMETERS config, not user
# # #     # input, so interpolating them straight into the SQL text is safe here.
# # #     pivot_cols = ",\n               ".join(
# # #         f"MAX(CASE WHEN p.param_key = '{key}' THEN s.score END) AS `{key}`"
# # #         for key, _ in PARAMETERS
# # #     )

# # #     rows = db.execute(
# # #         f"""
# # #         SELECT c.id AS call_id, a.id AS agent_id, a.name AS agent_name,
# # #                c.recording_base, c.call_date, c.overall_score, c.verdict, c.overall_quality,
# # #                {pivot_cols}
# # #         FROM calls c
# # #         JOIN agents a ON a.id = c.agent_id
# # #         LEFT JOIN call_scores s ON s.call_id = c.id
# # #         LEFT JOIN parameters p ON p.id = s.parameter_id
# # #         WHERE {where_sql}
# # #         GROUP BY c.id, a.id, a.name, c.recording_base, c.call_date,
# # #                  c.overall_score, c.verdict, c.overall_quality
# # #         ORDER BY {order_sql}
# # #         LIMIT ? OFFSET ?
# # #         """,
# # #         tuple(params) + (page_size, offset),
# # #     ).fetchall()

# # #     return {
# # #         "start": start,
# # #         "end": end,
# # #         "agent_id": agent_id,
# # #         "page": page,
# # #         "page_size": page_size,
# # #         "total": total,
# # #         "columns": [{"key": key, "label": label} for key, label in PARAMETERS],
# # #         "rows": [dict(r) for r in rows],
# # #     }

# # # # ==================================================================
# # # # Manifest upload + STT/SLM pipeline
# # # # ==================================================================

# # # @app.post("/api/manifest/upload")
# # # async def upload_manifest(
# # #     background_tasks: BackgroundTasks,
# # #     file: UploadFile = File(...),
# # #     recording_base_url: Optional[str] = None,
# # #     auto_process: bool = True,
# # # ):
# # #     if not file.filename.lower().endswith((".xlsx", ".xlsm")):
# # #         raise HTTPException(status_code=400, detail="Please upload an .xlsx manifest file")

# # #     with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
# # #         shutil.copyfileobj(file.file, tmp)
# # #         tmp_path = tmp.name

# # #     try:
# # #         manifest_id, row_count = save_manifest(
# # #             tmp_path, file.filename, recording_base_url or RECORDINGS_BASE_URL
# # #         )
# # #         print(f"Manifest uploaded: {manifest_id}, Rows: {row_count}")
# # #     finally:
# # #         os.unlink(tmp_path)

# # #     if auto_process:
# # #         print("Triggering background processing for manifest:", manifest_id)
# # #         background_tasks.add_task(process_manifest, manifest_id)

# # #     return {"manifest_id": manifest_id, "rows_loaded": row_count, "processing": auto_process}


# # # @app.post("/api/manifest/{manifest_id}/process")
# # # def trigger_processing(manifest_id: int, background_tasks: BackgroundTasks, db = Depends(get_db)):
# # #     row = db.execute("SELECT id FROM manifests WHERE id = ?", (manifest_id,)).fetchone()
# # #     if not row:
# # #         raise HTTPException(status_code=404, detail="Manifest not found")
# # #     background_tasks.add_task(process_manifest, manifest_id)
# # #     return {"manifest_id": manifest_id, "processing": True}


# # # @app.get("/api/manifest")
# # # def list_manifests(db = Depends(get_db)):
# # #     rows = db.execute("SELECT * FROM manifests ORDER BY id DESC").fetchall()
# # #     return {"manifests": [dict(r) for r in rows]}


# # # @app.get("/api/manifest/{manifest_id}")
# # # def manifest_status(manifest_id: int, db = Depends(get_db)):
# # #     manifest = db.execute("SELECT * FROM manifests WHERE id = ?", (manifest_id,)).fetchone()
# # #     if not manifest:
# # #         raise HTTPException(status_code=404, detail="Manifest not found")
# # #     calls = db.execute(
# # #         """SELECT c.id, c.call_number, c.recording_base, c.status, c.error_message,
# # #                   c.overall_score, c.verdict, a.name AS agent_name
# # #            FROM calls c JOIN agents a ON a.id = c.agent_id
# # #            WHERE c.manifest_id = ? ORDER BY c.id""",
# # #         (manifest_id,),
# # #     ).fetchall()
# # #     return {"manifest": dict(manifest), "calls": [dict(r) for r in calls]}


# # # # @app.get("/api/calls/{call_id}/transcript")
# # # # def call_transcript(call_id: int, db = Depends(get_db)):
# # # #     row = db.execute("SELECT * FROM transcripts WHERE call_id = ?", (call_id,)).fetchone()
# # # #     if not row:
# # # #         raise HTTPException(status_code=404, detail="No transcript for this call yet")
# # # #     return dict(row)


# # # # @app.get("/api/calls/{call_id}")
# # # # def call_detail(call_id: int, db = Depends(get_db)):
# # # #     # `c.*` already includes rx_path (IN leg) and tx_path (OUT leg) - the same
# # # #     # recording URLs the STT service was given. The frontend call-detail page
# # # #     # (feature 2.3) plays these directly and seeks to each parameter's
# # # #     # evidence start_time so a reviewer can jump straight to the moment a
# # # #     # score was decided, instead of listening to the whole call.
# # # #     call = db.execute(
# # # #         """SELECT c.*, a.name AS agent_name FROM calls c JOIN agents a ON a.id = c.agent_id
# # # #            WHERE c.id = ?""",
# # # #         (call_id,),
# # # #     ).fetchone()
# # # #     if not call:
# # # #         raise HTTPException(status_code=404, detail="Call not found")
# # # #     scores = db.execute(
# # # #         """SELECT p.label AS parameter, s.status, s.reason, s.evidence, s.start_time, s.end_time,
# # # #                   s.raw_score, s.score
# # # #            FROM call_scores s JOIN parameters p ON p.id = s.parameter_id
# # # #            WHERE s.call_id = ? ORDER BY p.id""",
# # # #         (call_id,),
# # # #     ).fetchall()
# # # #     fatal = db.execute("SELECT * FROM fatal_checks WHERE call_id = ?", (call_id,)).fetchall()
# # # #     return {
# # # #         "call": dict(call),
# # # #         "parameters": [dict(r) for r in scores],
# # # #         "fatal_checks": [dict(r) for r in fatal],
# # # #     }

# # # @app.get("/api/calls/{call_id}/transcript")
# # # def call_transcript(call_id: int, db = Depends(get_db)):
# # #     row = db.execute("SELECT * FROM transcripts WHERE call_id = ?", (call_id,)).fetchone()
# # #     if not row:
# # #         raise HTTPException(status_code=404, detail="No transcript for this call yet")
# # #     return dict(row)


# # # @app.get("/api/calls/{call_id}")
# # # def call_detail(call_id: int, db = Depends(get_db)):
# # #     print(f"Fetching detail for call ID: {call_id}")
# # #     # `c.*` already includes rx_path (IN leg) and tx_path (OUT leg) - the same
# # #     # recording URLs the STT service was given. The frontend call-detail page
# # #     # (feature 2.3) plays these directly and seeks to each parameter's
# # #     # evidence start_time so a reviewer can jump straight to the moment a
# # #     # score was decided, instead of listening to the whole call.
# # #     call = db.execute(
# # #         """SELECT c.*, a.name AS agent_name FROM calls c JOIN agents a ON a.id = c.agent_id
# # #            WHERE c.id = ?""",
# # #         (call_id,),
# # #     ).fetchone()
# # #     if not call:
# # #         raise HTTPException(status_code=404, detail="Call not found")
# # #     scores = db.execute(
# # #         """SELECT p.label AS parameter, s.status, s.reason, s.evidence, s.start_time, s.end_time,
# # #                   s.raw_score, s.score
# # #            FROM call_scores s JOIN parameters p ON p.id = s.parameter_id
# # #            WHERE s.call_id = ? ORDER BY p.id""",
# # #         (call_id,),
# # #     ).fetchall()
# # #     fatal = db.execute("SELECT * FROM fatal_checks WHERE call_id = ?", (call_id,)).fetchall()
# # #     # print(dict(call), [dict(r) for r in scores], [dict(r) for r in fatal])
# # #     call_dict = dict(call)
# # #     print(call_dict)
# # #     if call_dict.get("recording_base"):
# # #         call_dict["mono_url"] = f"{RECORDINGS_PLAYBACK_BASE_URL.rstrip('/')}/{call_dict['recording_base']}"
# # #     else:
# # #         call_dict["mono_url"] = None
# # #     print(f" after adding the mono url\n{call_dict}")
# # #     return {
# # #         "call": dict(call_dict),
# # #         "parameters": [dict(r) for r in scores],
# # #         "fatal_checks": [dict(r) for r in fatal],
# # #     }

# # # @app.get("/api/health")
# # # def health(db = Depends(get_db)):
# # #     """Hits the DB too, so this actually tells you whether MySQL is reachable,
# # #     not just whether the FastAPI process is up."""
# # #     db.execute("SELECT 1")
# # #     return {"status": "ok"}

# # # # import os
# # # # import shutil
# # # # import tempfile
# # # # from datetime import datetime, timedelta
# # # # from typing import Optional

# # # # import pymysql
# # # # from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Request, UploadFile
# # # # from fastapi.middleware.cors import CORSMiddleware
# # # # from fastapi.responses import JSONResponse
# # # # from pydantic import BaseModel

# # # # import hashlib

# # # # from config import RECORDINGS_BASE_URL, RECORDINGS_PLAYBACK_BASE_URL
# # # # from db import get_conn, init_db
# # # # from pipeline import process_manifest, save_manifest

# # # # app = FastAPI(title="Athena QA Dashboard")

# # # # app.add_middleware(
# # # #     CORSMiddleware,
# # # #     allow_origins=[
# # # #         "http://localhost:5173",
# # # #         "http://127.0.0.1:5173",
# # # #     ],
# # # #     allow_credentials=True,
# # # #     allow_methods=["*"],
# # # #     allow_headers=["*"],
# # # # )

# # # # # ------------------------------------------------------------------
# # # # # IMPORTANT FIX: init_db() used to run unguarded at import time. If MySQL
# # # # # was unreachable (wrong host/user/password, or the server just wasn't up
# # # # # yet), that raised an exception WHILE the module was loading - before
# # # # # FastAPI/CORSMiddleware even finished being built. uvicorn's worker
# # # # # process died right there, so nothing was listening on the port at all.
# # # # #
# # # # # That's exactly what showed up in the browser as CORS errors + net::ERR_FAILED:
# # # # # there was no server to send a response (let alone CORS headers), so Chrome
# # # # # reports it as a blocked/failed request instead of a clean 500.
# # # # #
# # # # # Fix: catch the startup failure, log it clearly, and let the app come up
# # # # # anyway. Every DB-touching request will then fail with a clean 503 JSON
# # # # # response (see exception handler below) that DOES carry CORS headers,
# # # # # instead of the whole server being unreachable.
# # # # # ------------------------------------------------------------------
# # # # try:
# # # #     init_db()
# # # # except Exception as e:
# # # #     print("=" * 70)
# # # #     print("[STARTUP WARNING] Could not connect to MySQL / initialize schema.")
# # # #     print(f"[STARTUP WARNING] {type(e).__name__}: {e}")
# # # #     print("[STARTUP WARNING] Check MYSQL_HOST / MYSQL_PORT / MYSQL_USER / "
# # # #           "MYSQL_PASSWORD / MYSQL_DATABASE env vars and that MySQL is running.")
# # # #     print("[STARTUP WARNING] The API is still starting so you get clean 503s "
# # # #           "instead of connection failures - fix your DB config and restart.")
# # # #     print("=" * 70)


# # # # @app.exception_handler(pymysql.err.Error)
# # # # async def mysql_error_handler(request: Request, exc: pymysql.err.Error):
# # # #     return JSONResponse(
# # # #         status_code=503,
# # # #         content={"detail": f"Database unavailable: {exc}"},
# # # #     )


# # # # def get_db():
# # # #     conn = get_conn()
# # # #     try:
# # # #         yield conn
# # # #     finally:
# # # #         conn.close()


# # # # def hash_pw(pw: str) -> str:
# # # #     return hashlib.sha256(pw.encode()).hexdigest()


# # # # # ---------- Schemas ----------
# # # # class LoginRequest(BaseModel):
# # # #     username: str
# # # #     password: str


# # # # class LoginResponse(BaseModel):
# # # #     token: str
# # # #     username: str
# # # #     role: str


# # # # # ---------- Auth ----------
# # # # @app.post("/api/login", response_model=LoginResponse)
# # # # def login(req: LoginRequest, db = Depends(get_db)):
# # # #     row = db.execute(
# # # #         "SELECT * FROM users WHERE username = ? AND password_hash = ?",
# # # #         (req.username, hash_pw(req.password)),
# # # #     ).fetchone()
# # # #     if not row:
# # # #         raise HTTPException(status_code=401, detail="Invalid username or password")
# # # #     token = hashlib.sha256(f"{req.username}{datetime.utcnow()}".encode()).hexdigest()
# # # #     return LoginResponse(token=token, username=row["username"], role=row["role"])


# # # # # ---------- Helpers ----------
# # # # def date_range_defaults(start: Optional[str], end: Optional[str]):
# # # #     if not end:
# # # #         end = datetime.utcnow().strftime("%Y-%m-%d")
# # # #     if not start:
# # # #         start = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
# # # #     return start, end


# # # # # Whitelist of SQL fragments for the 5 clickable dashboard stat cards (feature 2).
# # # # # Kept as a fixed dict (not built from the path param) so there's no injection risk
# # # # # even though it gets interpolated straight into the query string below.
# # # # CATEGORY_FILTERS = {
# # # #     "total": "1=1",
# # # #     "audited": "c.status = 'audited'",
# # # #     "pending": "c.status IN ('pending','transcribing','analyzing')",
# # # #     "positive": "c.sentiment = 'positive'",
# # # #     "negative": "c.sentiment = 'negative'",
# # # # }


# # # # def category_filter_sql(category: str) -> str:
# # # #     if category not in CATEGORY_FILTERS:
# # # #         raise HTTPException(
# # # #             status_code=400,
# # # #             detail=f"Unknown category '{category}'. Must be one of: {', '.join(CATEGORY_FILTERS)}",
# # # #         )
# # # #     return CATEGORY_FILTERS[category]


# # # # # ---------- Dashboard summary (Section 2 top cards) ----------
# # # # @app.get("/api/dashboard/summary")
# # # # def dashboard_summary(
# # # #     start: Optional[str] = None,
# # # #     end: Optional[str] = None,
# # # #     db = Depends(get_db),
# # # # ):
# # # #     start, end = date_range_defaults(start, end)
# # # #     row = db.execute(
# # # #         """
# # # #         SELECT
# # # #           COUNT(*) AS total_calls,
# # # #           SUM(CASE WHEN status = 'audited' THEN 1 ELSE 0 END) AS audited_calls,
# # # #           SUM(CASE WHEN status IN ('pending','transcribing','analyzing') THEN 1 ELSE 0 END) AS pending_calls,
# # # #           SUM(CASE WHEN sentiment = 'positive' THEN 1 ELSE 0 END) AS positive_calls,
# # # #           SUM(CASE WHEN sentiment = 'negative' THEN 1 ELSE 0 END) AS negative_calls
# # # #         FROM calls
# # # #         WHERE call_date IS NOT NULL AND date(call_date) BETWEEN date(?) AND date(?)
# # # #         """,
# # # #         (start, end),
# # # #     ).fetchone()
# # # #     return {
# # # #         "start": start,
# # # #         "end": end,
# # # #         "total_calls": row["total_calls"] or 0,
# # # #         "audited_calls": row["audited_calls"] or 0,
# # # #         "pending_calls": row["pending_calls"] or 0,
# # # #         "positive_calls": row["positive_calls"] or 0,
# # # #         "negative_calls": row["negative_calls"] or 0,
# # # #     }


# # # # # ---------- Parameter-wise average scores (graph) ----------
# # # # @app.get("/api/dashboard/parameters")
# # # # def dashboard_parameters(
# # # #     start: Optional[str] = None,
# # # #     end: Optional[str] = None,
# # # #     db = Depends(get_db),
# # # # ):
# # # #     start, end = date_range_defaults(start, end)
# # # #     rows = db.execute(
# # # #         """
# # # #         SELECT p.label AS parameter, ROUND(AVG(s.score), 1) AS avg_score, COUNT(*) AS n
# # # #         FROM call_scores s
# # # #         JOIN parameters p ON p.id = s.parameter_id
# # # #         JOIN calls c ON c.id = s.call_id
# # # #         WHERE c.call_date IS NOT NULL AND date(c.call_date) BETWEEN date(?) AND date(?)
# # # #         GROUP BY p.label
# # # #         ORDER BY p.id
# # # #         """,
# # # #         (start, end),
# # # #     ).fetchall()
# # # #     return {
# # # #         "start": start,
# # # #         "end": end,
# # # #         "parameters": [
# # # #             {"parameter": r["parameter"], "avg_score": r["avg_score"] or 0, "sample_size": r["n"]}
# # # #             for r in rows
# # # #         ],
# # # #     }


# # # # # ---------- Parameter success-rate funnel (feature 1) ----------
# # # # # Different from /api/dashboard/parameters above (which averages the 0-100
# # # # # normalized score). This instead answers "out of every audited call, what
# # # # # % actually PASSED (status=true) this parameter?" - sorted highest-first so
# # # # # it reads top-to-bottom like a funnel: parameters almost everyone nails at
# # # # # the top, parameters agents struggle with at the bottom.
# # # # @app.get("/api/dashboard/parameter-funnel")
# # # # def parameter_funnel(
# # # #     start: Optional[str] = None,
# # # #     end: Optional[str] = None,
# # # #     db = Depends(get_db),
# # # # ):
# # # #     start, end = date_range_defaults(start, end)
# # # #     rows = db.execute(
# # # #         """
# # # #         SELECT p.label AS parameter,
# # # #                ROUND(SUM(s.status) / COUNT(*) * 100, 1) AS success_pct,
# # # #                COUNT(*) AS sample_size
# # # #         FROM call_scores s
# # # #         JOIN parameters p ON p.id = s.parameter_id
# # # #         JOIN calls c ON c.id = s.call_id
# # # #         WHERE c.call_date IS NOT NULL AND date(c.call_date) BETWEEN date(?) AND date(?)
# # # #         GROUP BY p.label
# # # #         ORDER BY success_pct DESC
# # # #         """,
# # # #         (start, end),
# # # #     ).fetchall()
# # # #     return {
# # # #         "start": start,
# # # #         "end": end,
# # # #         "parameters": [
# # # #             {"parameter": r["parameter"], "success_pct": r["success_pct"] or 0, "sample_size": r["sample_size"]}
# # # #             for r in rows
# # # #         ],
# # # #     }


# # # # # ---------- Agents belonging to a parameter (click on bar) ----------
# # # # @app.get("/api/dashboard/parameters/{parameter_name}/agents")
# # # # def agents_for_parameter(
# # # #     parameter_name: str,
# # # #     start: Optional[str] = None,
# # # #     end: Optional[str] = None,
# # # #     db = Depends(get_db),
# # # # ):
# # # #     start, end = date_range_defaults(start, end)
# # # #     rows = db.execute(
# # # #         """
# # # #         SELECT a.id AS agent_id,
# # # #                a.name AS agent_name,
# # # #                a.team,
# # # #                ROUND(AVG(s.score), 1) AS avg_score,
# # # #                COUNT(*) AS calls_scored
# # # #         FROM call_scores s
# # # #         JOIN parameters p ON p.id = s.parameter_id
# # # #         JOIN calls c ON c.id = s.call_id
# # # #         JOIN agents a ON a.id = c.agent_id
# # # #         WHERE p.label = ?
# # # #           AND c.call_date IS NOT NULL
# # # #           AND date(c.call_date) BETWEEN date(?) AND date(?)
# # # #         GROUP BY a.id
# # # #         ORDER BY avg_score ASC
# # # #         """,
# # # #         (parameter_name, start, end),
# # # #     ).fetchall()
# # # #     if not rows:
# # # #         exists = db.execute("SELECT 1 FROM parameters WHERE label = ?", (parameter_name,)).fetchone()
# # # #         if not exists:
# # # #             raise HTTPException(status_code=404, detail="Parameter not found")
# # # #     return {
# # # #         "parameter": parameter_name,
# # # #         "start": start,
# # # #         "end": end,
# # # #         "agents": [dict(r) for r in rows],
# # # #     }


# # # # # ---------- Agent list (basic) ----------
# # # # @app.get("/api/agents")
# # # # def list_agents(db = Depends(get_db)):
# # # #     rows = db.execute("SELECT id, name, team FROM agents ORDER BY name").fetchall()
# # # #     return {"agents": [dict(r) for r in rows]}


# # # # # ---------- Agent detail (Section 3) ----------
# # # # @app.get("/api/agents/{agent_id}")
# # # # def agent_detail(
# # # #     agent_id: int,
# # # #     start: Optional[str] = None,
# # # #     end: Optional[str] = None,
# # # #     db = Depends(get_db),
# # # # ):
# # # #     start, end = date_range_defaults(start, end)
# # # #     agent = db.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
# # # #     if not agent:
# # # #         raise HTTPException(status_code=404, detail="Agent not found")

# # # #     calls_row = db.execute(
# # # #         """
# # # #         SELECT COUNT(*) AS total_calls,
# # # #                SUM(CASE WHEN overall_quality = 'good' THEN 1 ELSE 0 END) AS good_calls,
# # # #                SUM(CASE WHEN overall_quality = 'poor' THEN 1 ELSE 0 END) AS poor_calls,
# # # #                ROUND(AVG(overall_score), 1) AS average_call_score
# # # #         FROM calls
# # # #         WHERE agent_id = ? AND status = 'audited'
# # # #           AND call_date IS NOT NULL AND date(call_date) BETWEEN date(?) AND date(?)
# # # #         """,
# # # #         (agent_id, start, end),
# # # #     ).fetchone()

# # # #     param_rows = db.execute(
# # # #         """
# # # #         SELECT p.label AS parameter, ROUND(AVG(s.score),1) AS avg_score
# # # #         FROM call_scores s
# # # #         JOIN parameters p ON p.id = s.parameter_id
# # # #         JOIN calls c ON c.id = s.call_id
# # # #         WHERE c.agent_id = ? AND c.call_date IS NOT NULL AND date(c.call_date) BETWEEN date(?) AND date(?)
# # # #         GROUP BY p.label
# # # #         ORDER BY p.id
# # # #         """,
# # # #         (agent_id, start, end),
# # # #     ).fetchall()

# # # #     params = [dict(r) for r in param_rows]
# # # #     if params:
# # # #         sorted_params = sorted(params, key=lambda x: x["avg_score"] or 0, reverse=True)
# # # #         strengths = [p["parameter"] for p in sorted_params[:3]]
# # # #         improvements = [p["parameter"] for p in sorted_params[-3:]]
# # # #     else:
# # # #         strengths, improvements = [], []

# # # #     return {
# # # #         "agent": {"id": agent["id"], "name": agent["name"], "team": agent["team"]},
# # # #         "start": start,
# # # #         "end": end,
# # # #         "total_calls": calls_row["total_calls"] or 0,
# # # #         "average_call_score": calls_row["average_call_score"] or 0,
# # # #         "good_calls": calls_row["good_calls"] or 0,
# # # #         "poor_calls": calls_row["poor_calls"] or 0,
# # # #         "parameters": params,
# # # #         "overall_summary": {
# # # #             "strengths": strengths,
# # # #             "improvements": improvements,
# # # #         },
# # # #     }


# # # # # ==================================================================
# # # # # Feature 2: clickable stat cards -> agents -> that agent's calls.
# # # # # category is one of the CATEGORY_FILTERS keys (total/audited/pending/positive/negative),
# # # # # matching whichever stat card was clicked on the dashboard.
# # # # # ==================================================================

# # # # @app.get("/api/dashboard/category/{category}/agents")
# # # # def category_agents(
# # # #     category: str,
# # # #     start: Optional[str] = None,
# # # #     end: Optional[str] = None,
# # # #     db = Depends(get_db),
# # # # ):
# # # #     """2.1 - agents ranked by their average score, scoped to the clicked category."""
# # # #     where_clause = category_filter_sql(category)
# # # #     start, end = date_range_defaults(start, end)
# # # #     rows = db.execute(
# # # #         f"""
# # # #         SELECT a.id AS agent_id, a.name AS agent_name, a.team,
# # # #                ROUND(AVG(c.overall_score), 1) AS avg_score,
# # # #                COUNT(*) AS call_count
# # # #         FROM calls c
# # # #         JOIN agents a ON a.id = c.agent_id
# # # #         WHERE {where_clause}
# # # #           AND c.call_date IS NOT NULL AND date(c.call_date) BETWEEN date(?) AND date(?)
# # # #         GROUP BY a.id
# # # #         ORDER BY avg_score DESC
# # # #         """,
# # # #         (start, end),
# # # #     ).fetchall()
# # # #     return {
# # # #         "category": category,
# # # #         "start": start,
# # # #         "end": end,
# # # #         "agents": [dict(r) for r in rows],
# # # #     }


# # # # @app.get("/api/dashboard/category/{category}/agents/{agent_id}/calls")
# # # # def category_agent_calls(
# # # #     category: str,
# # # #     agent_id: int,
# # # #     start: Optional[str] = None,
# # # #     end: Optional[str] = None,
# # # #     db = Depends(get_db),
# # # # ):
# # # #     """2.2 - that agent's individual calls (with score) within the clicked category."""
# # # #     where_clause = category_filter_sql(category)
# # # #     start, end = date_range_defaults(start, end)
# # # #     agent = db.execute("SELECT id, name, team FROM agents WHERE id = ?", (agent_id,)).fetchone()
# # # #     if not agent:
# # # #         raise HTTPException(status_code=404, detail="Agent not found")

# # # #     calls = db.execute(
# # # #         f"""
# # # #         SELECT c.id, c.call_number, c.call_start_time, c.call_date, c.status,
# # # #                c.sentiment, c.overall_quality, c.overall_score, c.verdict, c.recording_base
# # # #         FROM calls c
# # # #         WHERE c.agent_id = ? AND {where_clause}
# # # #           AND c.call_date IS NOT NULL AND date(c.call_date) BETWEEN date(?) AND date(?)
# # # #         ORDER BY c.call_date DESC, c.id DESC 
# # # #         """,
# # # #         (agent_id, start, end),
# # # #     ).fetchall()
# # # #     return {
# # # #         "category": category,
# # # #         "agent": dict(agent),
# # # #         "start": start,
# # # #         "end": end,
# # # #         "calls": [dict(r) for r in calls],
# # # #     }


# # # # # ==================================================================
# # # # # Feature 3: top-10 agents leaderboard (right-hand sidebar box)
# # # # # ==================================================================

# # # # # @app.get("/api/dashboard/top-agents")
# # # # # def top_agents(
# # # # #     start: Optional[str] = None,
# # # # #     end: Optional[str] = None,
# # # # #     limit: int = 10,
# # # # #     db = Depends(get_db),
# # # # # ):
# # # # #     start, end = date_range_defaults(start, end)
# # # # #     limit = max(1, min(limit, 50))  # sane bounds, this only ever backs a small sidebar widget
# # # # #     rows = db.execute(
# # # # #         """
# # # # #         SELECT a.id AS agent_id, a.name AS agent_name, a.team,
# # # # #                ROUND(AVG(c.overall_score), 1) AS avg_score,
# # # # #                COUNT(*) AS call_count
# # # # #         FROM calls c
# # # # #         JOIN agents a ON a.id = c.agent_id
# # # # #         WHERE c.status = 'audited'
# # # # #           AND c.call_date IS NOT NULL AND date(c.call_date) BETWEEN date(?) AND date(?)
# # # # #         GROUP BY a.id
# # # # #         ORDER BY avg_score DESC
# # # # #         LIMIT ?
# # # # #         """,
# # # # #         (start, end, limit),
# # # # #     ).fetchall()
# # # # #     return {
# # # # #         "start": start,
# # # # #         "end": end,
# # # # #         "agents": [dict(r) for r in rows],
# # # # #     }


# # # # @app.get("/api/dashboard/top-agents")
# # # # def top_agents(
# # # #     start: Optional[str] = None,
# # # #     end: Optional[str] = None,
# # # #     limit: int = 10,
# # # #     db = Depends(get_db),
# # # # ):
# # # #     start, end = date_range_defaults(start, end)
# # # #     limit = max(1, min(limit, 50))  # sane bounds, this only ever backs a small sidebar widget
# # # #     rows = db.execute(
# # # #         """
# # # #         SELECT a.id AS agent_id, a.name AS agent_name, a.team,
# # # #                ROUND(AVG(c.overall_score), 1) AS avg_score,
# # # #                COUNT(*) AS call_count
# # # #         FROM calls c
# # # #         JOIN agents a ON a.id = c.agent_id
# # # #         WHERE c.status = 'audited'
# # # #           AND c.call_date IS NOT NULL AND date(c.call_date) BETWEEN date(?) AND date(?)
# # # #         GROUP BY a.id, a.name, a.team
# # # #         ORDER BY avg_score DESC
# # # #         LIMIT ?
# # # #         """,
# # # #         (start, end, limit),
# # # #     ).fetchall()
# # # #     return {
# # # #         "start": start,
# # # #         "end": end,
# # # #         "agents": [dict(r) for r in rows],
# # # #     }

# # # # # ==================================================================
# # # # # Manifest upload + STT/SLM pipeline
# # # # # ==================================================================

# # # # @app.post("/api/manifest/upload")
# # # # async def upload_manifest(
# # # #     background_tasks: BackgroundTasks,
# # # #     file: UploadFile = File(...),
# # # #     recording_base_url: Optional[str] = None,
# # # #     auto_process: bool = True,
# # # # ):
# # # #     if not file.filename.lower().endswith((".xlsx", ".xlsm")):
# # # #         raise HTTPException(status_code=400, detail="Please upload an .xlsx manifest file")

# # # #     with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
# # # #         shutil.copyfileobj(file.file, tmp)
# # # #         tmp_path = tmp.name

# # # #     try:
# # # #         manifest_id, row_count = save_manifest(
# # # #             tmp_path, file.filename, recording_base_url or RECORDINGS_BASE_URL
# # # #         )
# # # #         print(f"Manifest uploaded: {manifest_id}, Rows: {row_count}")
# # # #     finally:
# # # #         os.unlink(tmp_path)

# # # #     if auto_process:
# # # #         print("Triggering background processing for manifest:", manifest_id)
# # # #         background_tasks.add_task(process_manifest, manifest_id)

# # # #     return {"manifest_id": manifest_id, "rows_loaded": row_count, "processing": auto_process}


# # # # @app.post("/api/manifest/{manifest_id}/process")
# # # # def trigger_processing(manifest_id: int, background_tasks: BackgroundTasks, db = Depends(get_db)):
# # # #     row = db.execute("SELECT id FROM manifests WHERE id = ?", (manifest_id,)).fetchone()
# # # #     if not row:
# # # #         raise HTTPException(status_code=404, detail="Manifest not found")
# # # #     background_tasks.add_task(process_manifest, manifest_id)
# # # #     return {"manifest_id": manifest_id, "processing": True}


# # # # @app.get("/api/manifest")
# # # # def list_manifests(db = Depends(get_db)):
# # # #     rows = db.execute("SELECT * FROM manifests ORDER BY id DESC").fetchall()
# # # #     return {"manifests": [dict(r) for r in rows]}


# # # # @app.get("/api/manifest/{manifest_id}")
# # # # def manifest_status(manifest_id: int, db = Depends(get_db)):
# # # #     manifest = db.execute("SELECT * FROM manifests WHERE id = ?", (manifest_id,)).fetchone()
# # # #     if not manifest:
# # # #         raise HTTPException(status_code=404, detail="Manifest not found")
# # # #     calls = db.execute(
# # # #         """SELECT c.id, c.call_number, c.recording_base, c.status, c.error_message,
# # # #                   c.overall_score, c.verdict, a.name AS agent_name
# # # #            FROM calls c JOIN agents a ON a.id = c.agent_id
# # # #            WHERE c.manifest_id = ? ORDER BY c.id""",
# # # #         (manifest_id,), 
# # # #     ).fetchall() 
# # # #     return {"manifest": dict(manifest), "calls": [dict(r) for r in calls]} 


# # # # @app.get("/api/calls/{call_id}/transcript")
# # # # def call_transcript(call_id: int, db = Depends(get_db)):
# # # #     row = db.execute("SELECT * FROM transcripts WHERE call_id = ?", (call_id,)).fetchone()
# # # #     if not row:
# # # #         raise HTTPException(status_code=404, detail="No transcript for this call yet")
# # # #     return dict(row)


# # # # @app.get("/api/calls/{call_id}")
# # # # def call_detail(call_id: int, db = Depends(get_db)):
# # # #     print(f"Fetching detail for call ID: {call_id}")
# # # #     # `c.*` already includes rx_path (IN leg) and tx_path (OUT leg) - the same
# # # #     # recording URLs the STT service was given. The frontend call-detail page
# # # #     # (feature 2.3) plays these directly and seeks to each parameter's
# # # #     # evidence start_time so a reviewer can jump straight to the moment a
# # # #     # score was decided, instead of listening to the whole call.
# # # #     call = db.execute(
# # # #         """SELECT c.*, a.name AS agent_name FROM calls c JOIN agents a ON a.id = c.agent_id
# # # #            WHERE c.id = ?""",
# # # #         (call_id,),
# # # #     ).fetchone()
# # # #     if not call:
# # # #         raise HTTPException(status_code=404, detail="Call not found")
# # # #     scores = db.execute(
# # # #         """SELECT p.label AS parameter, s.status, s.reason, s.evidence, s.start_time, s.end_time,
# # # #                   s.raw_score, s.score
# # # #            FROM call_scores s JOIN parameters p ON p.id = s.parameter_id
# # # #            WHERE s.call_id = ? ORDER BY p.id""",
# # # #         (call_id,),
# # # #     ).fetchall()
# # # #     fatal = db.execute("SELECT * FROM fatal_checks WHERE call_id = ?", (call_id,)).fetchall()
# # # #     # print(dict(call), [dict(r) for r in scores], [dict(r) for r in fatal])
# # # #     call_dict = dict(call)
# # # #     print(call_dict)
# # # #     if call_dict.get("recording_base"):
# # # #         call_dict["mono_url"] = f"{RECORDINGS_PLAYBACK_BASE_URL.rstrip('/')}/{call_dict['recording_base']}"
# # # #     else:
# # # #         call_dict["mono_url"] = None
# # # #     print(f" after adding the mono url\n{call_dict}")
# # # #     return {
# # # #         "call": dict(call_dict),
# # # #         "parameters": [dict(r) for r in scores],
# # # #         "fatal_checks": [dict(r) for r in fatal],
# # # #     }


# # # # @app.get("/api/health")
# # # # def health(db = Depends(get_db)):
# # # #     """Hits the DB too, so this actually tells you whether MySQL is reachable,
# # # #     not just whether the FastAPI process is up."""
# # # #     db.execute("SELECT 1")
# # # #     return {"status": "ok"}


# # # # # to change the password as well 
# # # # class ChangePasswordRequest(BaseModel):
# # # #     username: str
# # # #     current_password: str
# # # #     new_password: str

# # # # @app.post("/api/change-password")
# # # # def change_password(req: ChangePasswordRequest, db = Depends(get_db)):
# # # #     row = db.execute(
# # # #         "SELECT * FROM users WHERE username = ? AND password_hash = ?",
# # # #         (req.username, hash_pw(req.current_password)),
# # # #     ).fetchone()
# # # #     if not row:
# # # #         raise HTTPException(status_code=401, detail="Current password is incorrect")
# # # #     if len(req.new_password) < 8:
# # # #         raise HTTPException(status_code=400, detail="New password must be at least 8 characters")
# # # #     db.execute("UPDATE users SET password_hash = ? WHERE username = ?", (hash_pw(req.new_password), req.username))
# # # #     db.commit()
# # # #     return {"status": "ok"}