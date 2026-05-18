#!/usr/bin/env python3
import os
import secrets
import threading
from functools import wraps
from pathlib import Path

import yaml
from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import db
from core.sorter import process_all_accounts

# ── Setup ─────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.yaml"
CREDENTIALS_DIR = BASE_DIR / "credentials"
CLIENT_SECRET_PATH = CREDENTIALS_DIR / "client_secret.json"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive.file",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]


def _secret_key():
    key_file = BASE_DIR / ".secret_key"
    if key_file.exists():
        return key_file.read_text().strip()
    key = secrets.token_hex(32)
    key_file.write_text(key)
    return key


app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or _secret_key()


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f) or {}


def _redirect_uri() -> str:
    return os.environ.get("REDIRECT_URI", "http://localhost:5000/auth/callback")


def _make_flow():
    return Flow.from_client_secrets_file(
        str(CLIENT_SECRET_PATH), scopes=SCOPES, redirect_uri=_redirect_uri()
    )


# ── Auth guard ────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    config = load_config()
    pw = config.get("admin_password") or os.environ.get("ADMIN_PASSWORD", "receipts")
    if request.method == "POST":
        if request.form.get("password") == pw:
            session["logged_in"] = True
            return redirect(request.args.get("next") or "/")
        flash("Wrong password.", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def index():
    job = scheduler.get_job("daily_sort")
    return render_template(
        "index.html",
        accounts=db.get_accounts(),
        recent_runs=db.get_runs(limit=5),
        next_run=job.next_run_time if job else None,
        active_run=db.get_active_run(),
        config=load_config(),
    )


# ── Accounts ──────────────────────────────────────────────────────────────────

@app.route("/accounts")
@login_required
def accounts():
    return render_template("accounts.html", accounts=db.get_accounts())


@app.route("/accounts/connect")
@login_required
def connect_account():
    if not CLIENT_SECRET_PATH.exists():
        flash("credentials/client_secret.json not found — see README for setup.", "warning")
        return redirect(url_for("accounts"))
    if _redirect_uri().startswith("http://"):
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
    flow = _make_flow()
    auth_url, state = flow.authorization_url(
        access_type="offline", prompt="select_account", include_granted_scopes="true"
    )
    session["oauth_state"] = state
    return redirect(auth_url)


@app.route("/auth/callback")
def auth_callback():
    if request.args.get("state") != session.pop("oauth_state", None):
        flash("OAuth state mismatch — please try again.", "danger")
        return redirect(url_for("accounts"))
    if _redirect_uri().startswith("http://"):
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
    try:
        flow = _make_flow()
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials
        gmail = build("gmail", "v1", credentials=creds)
        email = gmail.users().getProfile(userId="me").execute()["emailAddress"]
        db.save_account(email, creds.to_json())
        flash(f"Connected {email}", "success")
    except Exception as e:
        flash(f"Error connecting account: {e}", "danger")
    return redirect(url_for("accounts"))


@app.route("/accounts/<path:email>/toggle", methods=["POST"])
@login_required
def toggle_account(email):
    db.toggle_account(email)
    return redirect(url_for("accounts"))


@app.route("/accounts/<path:email>/disconnect", methods=["POST"])
@login_required
def disconnect_account(email):
    db.delete_account(email)
    flash(f"Disconnected {email}", "info")
    return redirect(url_for("accounts"))


# ── Run ───────────────────────────────────────────────────────────────────────

def _run_job(triggered_by: str = "scheduler"):
    config = load_config()
    run_id = db.create_run(triggered_by)
    try:
        stats = process_all_accounts(config, run_id)
        db.finish_run(run_id, stats)
    except Exception as e:
        db.fail_run(run_id, str(e))


@app.route("/run", methods=["POST"])
@login_required
def run_now():
    if db.get_active_run():
        flash("A sort job is already running.", "warning")
    else:
        threading.Thread(target=_run_job, args=("manual",), daemon=True).start()
        flash("Sort started — refresh in a moment to see results.", "success")
    return redirect(url_for("index"))


@app.route("/api/status")
@login_required
def api_status():
    return jsonify({"active": db.get_active_run() is not None})


# ── History ───────────────────────────────────────────────────────────────────

@app.route("/history")
@login_required
def history():
    return render_template("history.html", runs=db.get_runs(limit=100))


@app.route("/history/<int:run_id>")
@login_required
def run_detail(run_id):
    run = db.get_run(run_id)
    if not run:
        flash("Run not found.", "danger")
        return redirect(url_for("history"))
    return render_template("run_detail.html", run=run, items=db.get_run_items(run_id))


# ── Scheduler ─────────────────────────────────────────────────────────────────

scheduler = BackgroundScheduler(timezone="America/Toronto")


def start_scheduler():
    config = load_config()
    t = config.get("schedule_time", "08:00")
    h, m = t.split(":")
    scheduler.add_job(
        func=_run_job,
        trigger=CronTrigger(hour=int(h), minute=int(m)),
        id="daily_sort",
        replace_existing=True,
    )
    if not scheduler.running:
        scheduler.start()


# ── Init ──────────────────────────────────────────────────────────────────────

db.init()
start_scheduler()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, use_reloader=False)
