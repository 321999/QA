"""
Initializes the MySQL database (via db.init_db, same schema main.py uses) and
fills it with ~60 days of sample calls so the dashboard has data out of the box.
This is independent of the manifest/STT/SLM pipeline - it's just demo data.

Requires a reachable MySQL server - set MYSQL_HOST/PORT/USER/PASSWORD/DATABASE
env vars first if not using the localhost/root defaults in config.py.

Run: python seed.py
"""
import random
import hashlib
from datetime import datetime, timedelta

from db import get_conn, init_db, get_or_create_agent
from config import PARAMETER_MAX_POINTS, DEFAULT_PARAMETER_MAX_POINTS

AGENTS = [
    ("Aarav Shah", "Team Alpha"),
    ("Priya Nair", "Team Alpha"),
    ("Rohan Verma", "Team Beta"),
    ("Sneha Iyer", "Team Beta"),
    ("Karan Mehta", "Team Gamma"),
    ("Ananya Rao", "Team Gamma"),
    ("Vikram Singh", "Team Alpha"),
    ("Neha Kapoor", "Team Beta"),
]


def hash_pw(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()


def build():
    init_db()  # creates the database + tables if missing, seeds the 16 canonical parameters

    conn = get_conn()

    # wipe any previous demo/real data so this script is safely re-runnable
    # for table in ["fatal_checks", "call_scores", "transcripts", "calls", "manifests", "agents", "users"]:
    #     conn.execute(f"DELETE FROM {table}")
    # conn.commit()

    # users
    conn.execute("INSERT INTO users (username, password_hash, role) VALUES (?,?,?)",
                 ("admin", hash_pw("admin123"), "admin"))
    conn.execute("INSERT INTO users (username, password_hash, role) VALUES (?,?,?)",
                 ("manager", hash_pw("manager123"), "manager"))

    # agents
    agent_ids = [get_or_create_agent(conn, name, team) for name, team in AGENTS]

    param_rows = conn.execute("SELECT id, param_key FROM parameters ORDER BY id").fetchall()

    random.seed(42)
    today = datetime.utcnow()
    for day_offset in range(60):
        call_date = today - timedelta(days=day_offset)
        for _ in range(random.randint(3, 8)):
            agent_id = random.choice(agent_ids)
            status = random.choices(["audited", "pending"], weights=[80, 20])[0]
            sentiment = random.choices(["positive", "negative", "neutral"], weights=[55, 20, 25])[0]

            scores = {}
            if status == "audited":
                for p in param_rows:
                    max_pts = PARAMETER_MAX_POINTS.get(p["param_key"], DEFAULT_PARAMETER_MAX_POINTS)
                    raw = max(0, min(max_pts, round(random.gauss(max_pts * 0.78, max_pts * 0.18))))
                    norm = round(raw / max_pts * 100, 1) if max_pts else 0
                    scores[p["id"]] = (raw, norm)

            overall_score = round(sum(n for _, n in scores.values()) / len(scores), 1) if scores else None
            overall_quality = None
            if overall_score is not None:
                overall_quality = "good" if overall_score >= 80 else ("poor" if overall_score < 60 else "average")

            cur = conn.execute(
                """INSERT INTO calls (
                    agent_id, call_start_time, call_date, call_service_name, status,
                    sentiment, overall_quality, overall_score, verdict, summary, fatal_error
                ) VALUES (?,?,?,?,?,?,?,?,?,?,0)""",
                (
                    agent_id, call_date.strftime("%Y-%m-%d %H:%M"), call_date.strftime("%Y-%m-%d"),
                    "Sample QA", status, sentiment, overall_quality, overall_score,
                    overall_quality, "Seeded sample call for demo purposes." if status == "audited" else None,
                ),
            )
            call_id = cur.lastrowid
            for pid, (raw, norm) in scores.items():
                conn.execute(
                    """INSERT INTO call_scores (call_id, parameter_id, status, reason, evidence, raw_score, score)
                       VALUES (?,?,?,?,?,?,?)""",
                    (call_id, pid, int(norm >= 60), "Sample seeded score.", "", raw, norm),
                )

    conn.commit()
    conn.close()
    print("Seeded MySQL database:", __import__("config").MYSQL_DATABASE)


if __name__ == "__main__":
    build()
