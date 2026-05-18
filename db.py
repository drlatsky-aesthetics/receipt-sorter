import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "receipt_sorter.db"


def _conn():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init():
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS accounts (
            email       TEXT PRIMARY KEY,
            token_json  TEXT NOT NULL,
            enabled     INTEGER DEFAULT 1,
            connected_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_run_at  TEXT
        );
        CREATE TABLE IF NOT EXISTS runs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at  TEXT DEFAULT CURRENT_TIMESTAMP,
            finished_at TEXT,
            triggered_by TEXT DEFAULT 'scheduler',
            status      TEXT DEFAULT 'running',
            total_processed INTEGER DEFAULT 0,
            total_skipped   INTEGER DEFAULT 0,
            total_errors    INTEGER DEFAULT 0,
            error_msg   TEXT
        );
        CREATE TABLE IF NOT EXISTS run_items (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id      INTEGER REFERENCES runs(id),
            account_email TEXT,
            email_subject TEXT,
            email_sender  TEXT,
            category    TEXT,
            confidence  TEXT,
            filenames   TEXT,
            status      TEXT,
            error_msg   TEXT
        );
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        """)


# ── Accounts ──────────────────────────────────────────────────────────────────

def get_accounts():
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM accounts ORDER BY email")]


def get_enabled_accounts():
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM accounts WHERE enabled=1")]


def save_account(email: str, token_json: str):
    with _conn() as c:
        c.execute(
            "INSERT INTO accounts(email, token_json) VALUES(?,?) "
            "ON CONFLICT(email) DO UPDATE SET token_json=excluded.token_json, connected_at=CURRENT_TIMESTAMP",
            (email, token_json),
        )


def update_account_token(email: str, token_json: str):
    with _conn() as c:
        c.execute("UPDATE accounts SET token_json=? WHERE email=?", (token_json, email))


def toggle_account(email: str):
    with _conn() as c:
        c.execute("UPDATE accounts SET enabled = 1 - enabled WHERE email=?", (email,))


def delete_account(email: str):
    with _conn() as c:
        c.execute("DELETE FROM accounts WHERE email=?", (email,))


def update_account_last_run(email: str):
    with _conn() as c:
        c.execute("UPDATE accounts SET last_run_at=CURRENT_TIMESTAMP WHERE email=?", (email,))


# ── Runs ──────────────────────────────────────────────────────────────────────

def create_run(triggered_by: str = "scheduler") -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO runs(triggered_by, status) VALUES(?,?)",
            (triggered_by, "running"),
        )
        return cur.lastrowid


def finish_run(run_id: int, stats: dict):
    with _conn() as c:
        c.execute(
            "UPDATE runs SET status='done', finished_at=CURRENT_TIMESTAMP, "
            "total_processed=?, total_skipped=?, total_errors=? WHERE id=?",
            (stats.get("processed", 0), stats.get("skipped", 0), stats.get("errors", 0), run_id),
        )


def fail_run(run_id: int, error_msg: str):
    with _conn() as c:
        c.execute(
            "UPDATE runs SET status='error', finished_at=CURRENT_TIMESTAMP, error_msg=? WHERE id=?",
            (error_msg, run_id),
        )


def get_runs(limit: int = 50):
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        )]


def get_run(run_id: int):
    with _conn() as c:
        row = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None


def get_active_run():
    with _conn() as c:
        row = c.execute("SELECT * FROM runs WHERE status='running' ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None


# ── Settings ──────────────────────────────────────────────────────────────────

def get_setting(key: str, default=None):
    with _conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str):
    with _conn() as c:
        c.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def get_all_settings() -> dict:
    with _conn() as c:
        return {r["key"]: r["value"] for r in c.execute("SELECT key, value FROM settings")}


# ── Run items ─────────────────────────────────────────────────────────────────

def add_run_item(run_id, account_email, subject, sender, category, confidence, filenames, status, error_msg=None):
    with _conn() as c:
        c.execute(
            "INSERT INTO run_items(run_id,account_email,email_subject,email_sender,"
            "category,confidence,filenames,status,error_msg) VALUES(?,?,?,?,?,?,?,?,?)",
            (run_id, account_email, subject, sender, category, confidence, filenames, status, error_msg),
        )


def get_run_items(run_id: int):
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM run_items WHERE run_id=? ORDER BY id", (run_id,)
        )]
