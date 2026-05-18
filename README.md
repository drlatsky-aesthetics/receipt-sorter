# Receipt Sorter

Web-based Gmail → Google Drive receipt filer, powered by Claude AI.

- Connects **multiple Google accounts** (drjlatsky@gmail.com, drlatsky@treasuryhealth.ca, aesthetics@treasuryhealth.ca)
- **Runs daily at 8am** automatically (configurable)
- **Mobile-friendly** web dashboard — access from phone or browser anywhere
- AI categorizes each receipt by subject line, sender, and filename
- Files attachments into the correct Google Drive expense folder
- Labels processed emails so they're never double-filed

---

## One-time Setup (15 minutes)

### 1. Google Cloud Console

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a new project (e.g. "Receipt Sorter")
3. Enable these APIs:
   - **Gmail API**
   - **Google Drive API**
4. Go to **APIs & Services → OAuth consent screen**
   - User type: External
   - Add your 3 email addresses as **Test users**
5. Go to **APIs & Services → Credentials → Create Credentials → OAuth client ID**
   - Application type: **Web application**
   - Authorized redirect URIs:
     - `http://localhost:5000/auth/callback` (for local dev)
     - `https://YOUR-APP.onrender.com/auth/callback` (for cloud deployment)
6. Download the JSON → save as `credentials/client_secret.json`

### 2. Config

```bash
cp config.example.yaml config.yaml
```

Edit `config.yaml`:
- `anthropic_api_key`: Get from [console.anthropic.com](https://console.anthropic.com)
- `admin_password`: Change this to something strong
- `folder_id` for each category: Open the folder in Google Drive, copy the ID from the URL:
  `drive.google.com/drive/folders/`**`THIS_PART_IS_THE_ID`**

### 3. Run Locally

```bash
pip install -r requirements.txt
python app.py
```

Open [http://localhost:5000](http://localhost:5000) — log in, then go to **Accounts → Add Account** to connect each Gmail account.

---

## Cloud Deployment (Render — free tier)

Render gives you a free always-on web server, so the daily schedule runs 24/7 without your computer being on.

1. Push this repo to GitHub
2. Go to [render.com](https://render.com) → New → Web Service → connect your repo
3. Settings:
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn app:app -w 1 --bind 0.0.0.0:$PORT`
4. Add environment variables:
   - `ANTHROPIC_API_KEY` — your key
   - `ADMIN_PASSWORD` — your chosen password
   - `REDIRECT_URI` — `https://YOUR-APP.onrender.com/auth/callback`
   - `SECRET_KEY` — any random string (e.g. run `python -c "import secrets; print(secrets.token_hex(32))"`)
5. Upload `credentials/client_secret.json` as a Secret File at path `/etc/secrets/client_secret.json`, then add env var:
   - `GOOGLE_CLIENT_SECRET_PATH` = `/etc/secrets/client_secret.json`

**Important**: On Render, the SQLite database resets on each deploy. For persistent storage, upgrade to a paid plan with a disk, or use a PostgreSQL database (open an issue for help).

---

## How It Works

1. **Daily at 8am (Eastern)** the scheduler wakes up
2. For each connected, enabled Google account it:
   - Searches Gmail for emails with attachments from the past 7 days (not yet labelled `receipt-sorted`)
   - Sends the subject line, sender, and filename to Claude for categorization
   - Uploads the attachment to the matching Google Drive folder
   - Labels the email `receipt-sorted` in Gmail
3. Results are logged and visible in the **History** tab

---

## Adding / Changing Categories

Edit `config.yaml` → `categories`. Each entry needs:
- `description`: Plain-English description of what goes here (Claude reads this)
- `folder_id`: The Google Drive folder ID to file into

Restart the app after changes.
