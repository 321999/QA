import pymysql
import pymysql.cursors

from config import (
    PARAMETERS,
    MYSQL_HOST,
    MYSQL_PORT,
    MYSQL_USER,
    MYSQL_PASSWORD,
    MYSQL_DATABASE,
)


class Cursor:
    """Wraps a pymysql DictCursor so call sites keep using .fetchone()/.fetchall()/.lastrowid
    exactly like they did with sqlite3."""

    def __init__(self, raw_cursor):
        self._c = raw_cursor

    def fetchone(self):
        return self._c.fetchone()

    def fetchall(self):
        return self._c.fetchall()

    @property
    def lastrowid(self):
        return self._c.lastrowid


class Connection:
    """Thin wrapper so the rest of the app can keep calling conn.execute(sql, params)
    with '?' placeholders and dict-style rows, backed by MySQL instead of sqlite3."""

    def __init__(self, raw_conn):
        self._conn = raw_conn

    def execute(self, sql, params=()):
        cur = self._conn.cursor()
        cur.execute(sql.replace("?", "%s"), params)
        return Cursor(cur)

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def get_conn(use_database=True):
    kwargs = dict(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
        charset="utf8mb4",
    )
    if use_database:
        kwargs["database"] = MYSQL_DATABASE
    return Connection(pymysql.connect(**kwargs))


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INT AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(191) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    role VARCHAR(32) NOT NULL DEFAULT 'admin'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS agents (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(191) UNIQUE NOT NULL,
    team VARCHAR(128)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS parameters (
    id INT AUTO_INCREMENT PRIMARY KEY,
    param_key VARCHAR(191) UNIQUE NOT NULL,
    label VARCHAR(255) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS manifests (
    id INT AUTO_INCREMENT PRIMARY KEY,
    filename VARCHAR(255) NOT NULL,
    recording_base_url VARCHAR(500),
    uploaded_at VARCHAR(64) NOT NULL,
    total_rows INT DEFAULT 0,
    processed_rows INT DEFAULT 0,
    failed_rows INT DEFAULT 0,
    status VARCHAR(32) NOT NULL DEFAULT 'uploaded'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS calls (
    id INT AUTO_INCREMENT PRIMARY KEY,
    manifest_id INT,
    agent_id INT,
    call_number VARCHAR(64),
    cti_call_number VARCHAR(64),
    call_start_time VARCHAR(64),
    call_date VARCHAR(10),
    call_service_name VARCHAR(128),
    call_lead_id VARCHAR(64),
    call_end_type_name VARCHAR(64),
    call_talk_duration INT,
    call_trunk_duration INT,
    recording_base VARCHAR(255),
    rx_path VARCHAR(500),
    tx_path VARCHAR(500),
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    error_message TEXT,
    sentiment VARCHAR(16),
    overall_quality VARCHAR(16),
    overall_score DOUBLE,
    verdict VARCHAR(32),
    summary TEXT,
    fatal_error TINYINT DEFAULT 0,
    INDEX idx_calls_date (call_date),
    INDEX idx_calls_agent (agent_id),
    INDEX idx_calls_manifest (manifest_id),
    FOREIGN KEY (manifest_id) REFERENCES manifests(id) ON DELETE SET NULL,
    FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS transcripts (
    id INT AUTO_INCREMENT PRIMARY KEY,
    call_id INT NOT NULL,
    conversation_text TEXT,
    utterances_json TEXT,
    raw_json TEXT,
    FOREIGN KEY (call_id) REFERENCES calls(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS call_scores (
    id INT AUTO_INCREMENT PRIMARY KEY,
    call_id INT NOT NULL,
    parameter_id INT NOT NULL,
    status TINYINT NOT NULL DEFAULT 0,
    reason TEXT,
    evidence TEXT,
    start_time DOUBLE,
    end_time DOUBLE,
    raw_score DOUBLE,
    score DOUBLE,
    INDEX idx_scores_call (call_id),
    INDEX idx_scores_param (parameter_id),
    FOREIGN KEY (call_id) REFERENCES calls(id) ON DELETE CASCADE,
    FOREIGN KEY (parameter_id) REFERENCES parameters(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS fatal_checks (
    id INT AUTO_INCREMENT PRIMARY KEY,
    call_id INT NOT NULL,
    check_key VARCHAR(64) NOT NULL,
    status TINYINT NOT NULL DEFAULT 0,
    reason TEXT,
    evidence TEXT,
    start_time DOUBLE,
    end_time DOUBLE,
    INDEX idx_fatal_call (call_id),
    FOREIGN KEY (call_id) REFERENCES calls(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


def init_db():
    # create the database itself if it doesn't exist yet
    boot = get_conn(use_database=False)
    boot.execute(f"CREATE DATABASE IF NOT EXISTS `{MYSQL_DATABASE}` CHARACTER SET utf8mb4")
    boot.commit()
    boot.close()

    conn = get_conn()
    for stmt in [s.strip() for s in SCHEMA.strip().split(";") if s.strip()]:
        conn.execute(stmt)
    for key, label in PARAMETERS:
        conn.execute("INSERT IGNORE INTO parameters (param_key, label) VALUES (?, ?)", (key, label))
    conn.commit()
    conn.close()


def get_or_create_agent(conn, name, team=None):
    row = conn.execute("SELECT id FROM agents WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute("INSERT INTO agents (name, team) VALUES (?, ?)", (name, team))
    return cur.lastrowid
