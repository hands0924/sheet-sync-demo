import os
import json
import requests
from flask import Request, jsonify, abort
from google.oauth2 import service_account
from google.cloud import firestore
from googleapiclient.discovery import build

# ─── Environment Variables ─────────────────────────────────────────────────────
# Set these via Cloud Function's "--set-env-vars" (or via GitHub Actions)
SHEET_ID       = os.getenv("SHEET_ID")         # e.g. "1AbCdEfGhIjKlMnOpQrStUvWxYz"
API_ENDPOINT   = os.getenv("API_ENDPOINT")     # e.g. "https://your.api/endpoint"
FIRESTORE_DOC  = os.getenv("FIRESTORE_DOC")    # e.g. "sheet_snapshots/latest"
# Path to the service account JSON key is provided by GOOGLE_APPLICATION_CREDENTIALS

if not (SHEET_ID and API_ENDPOINT and FIRESTORE_DOC):
    raise EnvironmentError("One or more required env vars (SHEET_ID, API_ENDPOINT, FIRESTORE_DOC) are missing.")

# ─── Initialize Firestore client ────────────────────────────────────────────────
# The Cloud Function runtime sets GOOGLE_APPLICATION_CREDENTIALS to the SA key automatically.
firestore_client = firestore.Client()

# ─── Initialize Sheets API client ───────────────────────────────────────────────
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
credentials = service_account.Credentials.from_service_account_file(
    os.getenv("GOOGLE_APPLICATION_CREDENTIALS"), scopes=SCOPES
)
sheets_service = build("sheets", "v4", credentials=credentials)


def sheet_webhook(request: Request):
    """
    Cloud Function triggered by a Drive push notification.
    1) Validates the notification headers (optional).
    2) Reads the entire Google Sheet.
    3) Loads last snapshot from Firestore.
    4) Compares and finds only new/changed rows.
    5) Sends those rows (as a list) to the external API.
    6) Writes the new snapshot back to Firestore.
    """

    # 1) Basic check: Ensure this is a Drive notification (check X-Goog-Resource-State)
    resource_state = request.headers.get("X-Goog-Resource-State")
    if resource_state not in ("update", "add"):
        # Ignore other states like "exists", "sync", etc.
        return ("Ignored notification", 200)

    # 2) Fetch all values from the sheet
    sheet_range = "Form Responses!A1:Z"  # Adjust columns as needed
    try:
        sheet = sheets_service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID,
            range=sheet_range
        ).execute()
    except Exception as e:
        print("Error reading sheet:", e)
        return abort(500, "Failed to read sheet")

    all_values = sheet.get("values", [])
    if len(all_values) < 2:
        # Only header or no data
        return jsonify({"status": "no data", "rows_total": 0})

    header = all_values[0]         # E.g. ["Timestamp", "Name", "Email", ...]
    data_rows = all_values[1:]     # All rows below header

    # 3) Build a dictionary for current rows: { unique_id (timestamp) : {col:val, ...} }
    current_dict = {}
    for row in data_rows:
        # Pad row to match header length if some cells are missing
        row += [""] * (len(header) - len(row))
        row_data = {header[i]: row[i] for i in range(len(header))}
        unique_id = row[0]  # We assume column A ("Timestamp") is unique
        current_dict[unique_id] = row_data

    # 4) Load last snapshot from Firestore
    doc_ref = firestore_client.document(FIRESTORE_DOC)
    try:
        previous_snapshot = doc_ref.get().to_dict() or {}
    except Exception:
        previous_snapshot = {}
    prev_dict = previous_snapshot.get("snapshot", {})

    # 5) Compare and collect new/changed rows
    changed_rows = []
    for uid, row_data in current_dict.items():
        prev_data = prev_dict.get(uid)
        if prev_data is None or prev_data != row_data:
            changed_rows.append(row_data)

    # 6) Send changed rows to external API (only if there are any)
    if changed_rows:
        payload = {"rows": changed_rows}
        try:
            resp = requests.post(API_ENDPOINT, json=payload, timeout=10)
            resp.raise_for_status()
        except Exception as e:
            print("Error sending to external API:", e)
            # Even if API call fails, we still update Firestore snapshot below to avoid resending old rows in a loop.

    # 7) Overwrite Firestore snapshot with current_dict
    try:
        doc_ref.set({"snapshot": current_dict})
    except Exception as e:
        print("Error writing snapshot to Firestore:", e)

    return jsonify({
        "status": "completed",
        "rows_total": len(data_rows),
        "rows_changed": len(changed_rows),
    }) 