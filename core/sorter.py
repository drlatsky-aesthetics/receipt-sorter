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
    "https://www.googleapis.com/auth/drive",
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

    parent_id = db.get_setting("parent_folder_id", "").strip()
    if not parent_id:
        raise ValueError("No Google Drive folder configured. Go to Drive Folders in the nav.")

    # Build folder path: parent → Year → Account email → Category
    year = datetime.now().strftime("%Y")
    folder_cache = {}

    def get_folder(name: str, parent: str) -> str:
        key = (name, parent)
        if key not in folder_cache:
            folder_cache[key] = _get_or_create_folder(drive, name, parent)
        return folder_cache[key]

    year_folder   = get_folder(year, parent_id)
    account_folder = get_folder(account["email"], year_folder)

    categories = config.get("categories", {})
    days = config.get("days_back", 7)
    emails = _get_emails(gmail, days, config.get("processed_label", "receipt-sorted"))

    stats = {"processed": 0, "skipped": 0, "errors": 0}

    for email in emails:
        result = _categorize(client, email, categories)
        category = result.get("category", "uncategorized")
        filenames = ", ".join(a["filename"] for a in email["attachments"])

        if category not in categories:
            db.add_run_item(run_id, account["email"], email["subject"],
                            email["sender"], "uncategorized",
                            result.get("confidence"), filenames, "skipped")
            stats["skipped"] += 1
            continue

        # e.g. treasury_devices → Treasury Devices
        cat_display = category.replace("_", " ").title()
        cat_folder = get_folder(cat_display, account_folder)

        try:
            for att in email["attachments"]:
                _upload(drive, gmail, email, att, cat_folder)
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


# ── Drive folder helpers ──────────────────────────────────────────────────────

def _get_or_create_folder(drive, name: str, parent_id: str) -> str:
    safe = name.replace("'", "\\'")
    q = (
        f"name='{safe}' and '{parent_id}' in parents "
        f"and mimeType='application/vnd.google-apps.folder' and trashed=false"
    )
    results = drive.files().list(q=q, fields="files(id)", spaces="drive").execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]
    folder = drive.files().create(
        body={
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        },
        fields="id",
    ).execute()
    return folder["id"]


def _upload(drive, gmail, email: dict, att: dict, folder_id: str):
    data = gmail.users().messages().attachments().get(
        userId="me", messageId=email["id"], id=att["attachment_id"]
    ).execute()
    file_bytes = base64.urlsafe_b64decode(data["data"])
    name = f"{datetime.now().strftime('%Y-%m-%d')}_{att['filename']}"
    drive.files().create(
        body={"name": name, "parents": [folder_id]},
        media_body=MediaIoBaseUpload(
            io.BytesIO(file_bytes), mimetype=att["mime_type"], resumable=True
        ),
        fields="id",
    ).execute()


# ── Gmail helpers ─────────────────────────────────────────────────────────────

def _get_emails(gmail, days: int, label_filter: str) -> list:
    after = (datetime.now() - timedelta(days=days)).strftime("%Y/%m/%d")
    q = f"has:attachment after:{after} -label:{label_filter}"
    results = gmail.users().messages().list(userId="me", q=q).execute()
    emails = []
    for msg in results.get("messages", []):
        data = gmail.users().messages().get(
            userId="me", id=msg["id"], format="full"
        ).execute()
        headers = {h["name"]: h["value"] for h in data["payload"]["headers"]}
        attachments = [
            {
                "filename": p["filename"],
                "attachment_id": p["body"]["attachmentId"],
                "mime_type": p.get("mimeType", "application/octet-stream"),
            }
            for p in data["payload"].get("parts", [])
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
