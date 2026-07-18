# main 2
import os
import shutil
import tempfile
from datetime import datetime, timedelta
from typing import Optional

import pymysql
from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import hashlib

from config import RECORDINGS_BASE_URL
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


# ---------- Helpers ----------
def date_range_defaults(start: Optional[str], end: Optional[str]):
    if not end:
        end = datetime.utcnow().strftime("%Y-%m-%d")
    if not start:
        start = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
    return start, end


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
        WHERE call_date IS NOT NULL AND date(call_date) BETWEEN date(?) AND date(?)
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
    SELECT
        p.label AS parameter,
        ROUND(AVG(s.score), 1) AS avg_score,
        COUNT(*) AS n
    FROM call_scores s
    JOIN parameters p ON p.id = s.parameter_id
    JOIN calls c ON c.id = s.call_id
    WHERE c.call_date IS NOT NULL
      AND DATE(c.call_date) BETWEEN DATE(%s) AND DATE(%s)
    GROUP BY
        p.id,
        p.label
    ORDER BY
        p.id
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


# ---------- Agents belonging to a parameter (click on bar) ----------
@app.get("/api/dashboard/parameters/{parameter_name}/agents")
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
          AND c.call_date IS NOT NULL
          AND date(c.call_date) BETWEEN date(?) AND date(?)
        GROUP BY a.id
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
               ROUND(AVG(overall_score), 1) AS average_call_score
        FROM calls
        WHERE agent_id = ? AND status = 'audited'
          AND call_date IS NOT NULL AND date(call_date) BETWEEN date(?) AND date(?)
        """,
        (agent_id, start, end),
    ).fetchone()
    param_rows = db.execute(
    """
    SELECT
        p.id,
        p.label AS parameter,
        ROUND(AVG(s.score),1) AS avg_score
    FROM call_scores s
    JOIN parameters p ON p.id = s.parameter_id
    JOIN calls c ON c.id = s.call_id
    WHERE c.agent_id = %s
      AND c.call_date IS NOT NULL
      AND DATE(c.call_date) BETWEEN DATE(%s) AND DATE(%s)
    GROUP BY
        p.id,
        p.label
    ORDER BY
        p.id
    """,
    (agent_id, start, end),
).fetchall()
    # param_rows = db.execute(
    #     """
    #     SELECT p.label AS parameter, ROUND(AVG(s.score),1) AS avg_score
    #     FROM call_scores s
    #     JOIN parameters p ON p.id = s.parameter_id
    #     JOIN calls c ON c.id = s.call_id
    #     WHERE c.agent_id = ? AND c.call_date IS NOT NULL AND date(c.call_date) BETWEEN date(?) AND date(?)
    #     GROUP BY p.label
    #     ORDER BY p.id
    #     """,
    #     (agent_id, start, end),
    # ).fetchall()

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


@app.get("/api/calls/{call_id}/transcript")
def call_transcript(call_id: int, db = Depends(get_db)):
    row = db.execute("SELECT * FROM transcripts WHERE call_id = ?", (call_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="No transcript for this call yet")
    return dict(row)


@app.get("/api/calls/{call_id}")
def call_detail(call_id: int, db = Depends(get_db)):
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
    return {
        "call": dict(call),
        "parameters": [dict(r) for r in scores],
        "fatal_checks": [dict(r) for r in fatal],
    }


@app.get("/api/health")
def health(db = Depends(get_db)):
    """Hits the DB too, so this actually tells you whether MySQL is reachable,
    not just whether the FastAPI process is up."""
    db.execute("SELECT 1")
    return {"status": "ok"}

# import os
# import shutil
# import tempfile
# from datetime import datetime, timedelta
# from typing import Optional

# from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, UploadFile
# from fastapi.middleware.cors import CORSMiddleware
# from pydantic import BaseModel

# import hashlib

# from config import RECORDINGS_BASE_URL
# from db import get_conn, init_db
# from pipeline import process_manifest, save_manifest

# init_db()

# app = FastAPI(title="Athena QA Dashboard")

# # app.add_middleware(
# #     CORSMiddleware,
# #     allow_origins=["http://localhost:5173","http://127.0.0.1:5173"],
# #     allow_credentials=True,
# #     allow_methods=["*"],
# #     allow_headers=["*"],
# # )
# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=[
#         "http://localhost:5173",
#         "http://127.0.0.1:5173"
#     ],
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )

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


# # ---------- Helpers ----------
# def date_range_defaults(start: Optional[str], end: Optional[str]):
#     if not end:
#         end = datetime.utcnow().strftime("%Y-%m-%d")
#     if not start:
#         start = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
#     return start, end


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
#     j={
#         "start": start,
#         "end": end,
#         "total_calls": row["total_calls"] or 0,
#         "audited_calls": row["audited_calls"] or 0,
#         "pending_calls": row["pending_calls"] or 0,
#         "positive_calls": row["positive_calls"] or 0,
#         "negative_calls": row["negative_calls"] or 0,
#     }
#     print("it is working",j)
#     return {
#         "start": start,
#         "end": end,
#         "total_calls": row["total_calls"] or 0,
#         "audited_calls": row["audited_calls"] or 0,
#         "pending_calls": row["pending_calls"] or 0,
#         "positive_calls": row["positive_calls"] or 0,
#         "negative_calls": row["negative_calls"] or 0,
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
#         GROUP BY p.label
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
#         SELECT a.id AS agent_id, a.name AS agent_name, a.team,
#                ROUND(AVG(s.score), 1) AS avg_score, COUNT(*) AS calls_scored
#         FROM call_scores s
#         JOIN parameters p ON p.id = s.parameter_id
#         JOIN calls c ON c.id = s.call_id
#         JOIN agents a ON a.id = c.agent_id
#         WHERE p.label = ? AND c.call_date IS NOT NULL AND date(c.call_date) BETWEEN date(?) AND date(?)
#         GROUP BY a.id
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
#         GROUP BY p.label
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
#         manifest_id, row_count = save_manifest(tmp_path, file.filename, RECORDINGS_BASE_URL if not recording_base_url else recording_base_url)
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


# @app.get("/api/calls/{call_id}/transcript")
# def call_transcript(call_id: int, db = Depends(get_db)):
#     row = db.execute("SELECT * FROM transcripts WHERE call_id = ?", (call_id,)).fetchone()
#     if not row:
#         raise HTTPException(status_code=404, detail="No transcript for this call yet")
#     return dict(row)


# @app.get("/api/calls/{call_id}")
# def call_detail(call_id: int, db = Depends(get_db)):
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


# @app.get("/api/health")
# def health():
#     return {"status": "ok"}

