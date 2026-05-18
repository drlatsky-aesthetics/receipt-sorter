import base64
import io
import json
from datetime import datetime, timedelta

import anthropic
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

import db

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive.file",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]


def process_all_accounts(config: dict, run_id: int) -> dict:
    accounts = db.get_enabled_accounts()
    totals = {"processed": 0, "skipped": 0, "errors": 0}
    for account in accounts:
        try:
            stats = process_account(account, config, run_id)
            for k in totals:
                totals[k] += stats[k]
        except Exception as e:
            db.add_run_item(run_id, account["email"], None, None,
                            "account_error", None, None, "error", str(e))
            totals["errors"] += 1
    return totals


def process_account(account: dict, config: dict, run_id: int) -> dict:
    creds = Credentials.from_authorized_user_info(
        json.loads(account["token_json"]), SCOPES
    )
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            db.update_account_token(account["email"], creds.to_json())

    gmail = build("gmail", "v1", credentials=creds)
    drive = build("drive", "v3", credentials=creds)
    client = anthropic.Anthropic(api_key=config["anthropic_api_key"])

    # Overlay folder IDs saved via the web settings page
    db_settings = db.get_all_settings()
    categories = config.get("categories", {})
    for name in categories:
        db_key = f"folder_{name}"
        if db_key in db_settings and db_settings[db_key]:
            categories[name]["folder_id"] = db_settings[db_key]

    days = config.get("days_back", 7)
    emails = _get_emails(gmail, days, config.get("processed_label", "receipt-sorted"))

    stats = {"processed": 0, "skipped": 0, "errors": 0}

    for email in emails:
        result = _categorize(client, email, config["categories"])
        category = result.get("category", "uncategorized")
        filenames = ", ".join(a["filename"] for a in email["attachments"])

        if category not in config["categories"]:
            db.add_run_item(run_id, account["email"], email["subject"],
                            email["sender"], "uncategorized",
                            result.get("confidence"), filenames, "skipped")
            stats["skipped"] += 1
            continue

        folder_id = config["categories"][category]["folder_id"]
        try:
            for att in email["attachments"]:
                _upload(drive, gmail, email, att, folder_id)
            _label(gmail, email["id"], config.get("processed_label", "receipt-sorted"))
            db.add_run_item(run_id, account["email"], email["subject"],
                            email["sender"], category,
                            result.get("confidence"), filenames, "filed")
            stats["processed"] += 1
        except Exception as e:
            db.add_run_item(run_id, account["email"], email["subject"],
                            email["sender"], category,
                            result.get("confidence"), filenames, "error", str(e))
            stats["errors"] += 1

    db.update_account_last_run(account["email"])
    return stats


# ── Gmail helpers ─────────────────────────────────────────────────────────────

def _get_emails(gmail, days: int, label_filter: str) -> list:
    after = (datetime.now() - timedelta(days=days)).strftime("%Y/%m/%d")
    q = f"has:attachment after:{after} -label:{label_filter}"
    results = gmail.users().messages().list(userId="me", q=q).execute()
    messages = results.get("messages", [])
    emails = []
    for msg in messages:
        data = gmail.users().messages().get(userId="me", id=msg["id"], format="full").execute()
        headers = {h["name"]: h["value"] for h in data["payload"]["headers"]}
        parts = data["payload"].get("parts", [])
        attachments = [
            {
                "filename": p["filename"],
                "attachment_id": p["body"]["attachmentId"],
                "mime_type": p.get("mimeType", "application/octet-stream"),
            }
            for p in parts
            if p.get("filename") and p["body"].get("attachmentId")
        ]
        if attachments:
            emails.append({
                "id": msg["id"],
                "subject": headers.get("Subject", "(No Subject)"),
                "sender": headers.get("From", ""),
                "attachments": attachments,
            })
    return emails


def _label(gmail, email_id: str, label_name: str):
    labels = gmail.users().labels().list(userId="me").execute().get("labels", [])
    label_id = next((l["id"] for l in labels if l["name"] == label_name), None)
    if not label_id:
        label_id = gmail.users().labels().create(
            userId="me",
            body={"name": label_name, "labelListVisibility": "labelShow",
                  "messageListVisibility": "show"},
        ).execute()["id"]
    gmail.users().messages().modify(
        userId="me", id=email_id, body={"addLabelIds": [label_id]}
    ).execute()


# ── Drive helper ──────────────────────────────────────────────────────────────

def _upload(drive, gmail, email: dict, att: dict, folder_id: str):
    data = gmail.users().messages().attachments().get(
        userId="me", messageId=email["id"], id=att["attachment_id"]
    ).execute()
    file_bytes = base64.urlsafe_b64decode(data["data"])
    name = f"{datetime.now().strftime('%Y-%m-%d')}_{att['filename']}"
    drive.files().create(
        body={"name": name, "parents": [folder_id]},
        media_body=MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=att["mime_type"], resumable=True),
        fields="id",
    ).execute()


# ── Claude categorization ─────────────────────────────────────────────────────

def _categorize(client, email: dict, categories: dict) -> dict:
    cat_list = "\n".join(f"- {n}: {v['description']}" for n, v in categories.items())
    prompt = f"""You are an expense receipt categorization assistant for a medical aesthetics clinic.

CATEGORIES:
{cat_list}
- uncategorized: Cannot be matched to any category above

EMAIL:
Subject: {email['subject']}
From: {email['sender']}
Attachments: {', '.join(a['filename'] for a in email['attachments'])}

Reply ONLY with a JSON object (no markdown):
{{"category": "exact_category_name", "confidence": "high|medium|low", "reason": "one sentence"}}"""

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{"role": "user", "content": prompt}],
    )
    try:
        return json.loads(resp.content[0].text.strip())
    except Exception:
        return {"category": "uncategorized", "confidence": "low", "reason": "Parse error"}
