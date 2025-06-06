# Sheet-Sync-Demo

This project implements **sub-1-minute, serverless change detection** on a Google Sheet (linked to a Form) by using **Drive push notifications** and a Python **Cloud Function** for diff + external API calls, with **Firestore** storing the last snapshot.  

---

## Repository Structure

sheet-sync-demo/
├── .github/
│ └── workflows/
│ └── deploy.yml # GitHub Actions CI/CD pipeline
├── main.py # Python Cloud Function code
├── requirements.txt # Python dependencies
└── README.md # Setup & usage instructions

---

## How It Works

1.  **Drive Watch**  
    - We register a "watch" on the Google Sheet file. When the sheet changes (e.g., a new Form response), Drive sends an HTTP POST (webhook) to our Cloud Function.

2.  **Cloud Function (`main.py`)**  
    - Validates the notification (checks `X-Goog-Resource-State` header).  
    - Reads **all rows** from the sheet (`Form Responses!A1:Z`).  
    - Loads the **previous snapshot** from Firestore (`FIRESTORE_DOC`).  
    - Diffs current vs. previous rows; identifies only new or changed rows.  
    - Sends those rows as a JSON **list** to the external API (`API_ENDPOINT`).  
    - Overwrites Firestore snapshot with the latest entire sheet.

3.  **Firestore**  
    - Holds a single document at `FIRESTORE_DOC` (e.g. `sheet_snapshots/latest`) with a field `snapshot` that maps each row's "unique ID" (timestamp) to its row data.  
    - Each function run reads that doc, diffs, and writes back the new state.

4.  **CI/CD (GitHub Actions)**  
    - On every push to `main`, `.github/workflows/deploy.yml` automatically redeploys the Cloud Function with updated code.

---

## Prerequisites

1.  **GitHub Repo** (you can fork or create new from these files)  
2.  **Google Cloud Project** (with billing enabled)  
3.  **Service Account (`sheet-sync-sa`)** with:
    - **Firestore → Cloud Datastore User**  
    - **Sheets API → Viewer**  
    - **Cloud Functions → Cloud Functions Invoker**  
    - **Cloud Scheduler → Cloud Scheduler Service Agent** (for OIDC if used)  
4.  **Firestore** (Native mode)  
5.  **Google Sheet** (linked to a Form) with header row  
6.  **GitHub Secrets**:
    - `GCP_PROJECT` → your GCP Project ID  
    - `GCP_SA_KEY` → entire JSON contents of `sheet-sync-sa-key.json`  
    - `SHEET_ID` → Google Sheet ID (from the URL)  
    - `API_ENDPOINT` → e.g. `https://your.api/endpoint`  
    - `FIRESTORE_DOC` → e.g. `sheet_snapshots/latest`  

Note: For testing you can set API_ENDPOINT=https://httpbin.org/post to inspect incoming requests.

---

## Step-by-Step Setup

### 1) Clone this repository

```bash
git clone https://github.com/your-username/sheet-sync-demo.git
cd sheet-sync-demo
```
2) Enable required GCP APIs
```bash
gcloud config set project YOUR_PROJECT_ID

gcloud services enable \
  drive.googleapis.com \
  sheets.googleapis.com \
  firestore.googleapis.com \
  cloudfunctions.googleapis.com \
  cloudscheduler.googleapis.com \
  cloudbuild.googleapis.com
```
3) Create Firestore database
In Cloud Console → Firestore → Create database → Native mode → pick a region → Enable.

4) Create Service Account & Key
In Cloud Console → IAM & Admin → Service Accounts → Create SA "sheet-sync-sa"

Grant roles:

*   Firestore → Cloud Datastore User
*   Sheets API → Viewer
*   Cloud Functions → Cloud Functions Invoker
*   Cloud Scheduler → Cloud Scheduler Service Agent (if you plan to secure the function)

Generate a JSON key → download → name it sheet-sync-sa-key.json.

5) Share your Sheet with the SA
Open the Google Sheet → Share → Add sheet-sync-sa@YOUR_PROJECT_ID.iam.gserviceaccount.com as Viewer.

6) Add GitHub Secrets
In your GitHub repo → Settings → Secrets → Actions → New Repository Secret:

| Name          | Value                                            |
|---------------|--------------------------------------------------|
| `GCP_PROJECT`   | YOUR_PROJECT_ID                                  |
| `GCP_SA_KEY`    | Paste entire JSON of sheet-sync-sa-key.json    |
| `SHEET_ID`      | <YOUR_SHEET_ID>                                  |
| `API_ENDPOINT`  | `https://your.api/endpoint`                    |
| `FIRESTORE_DOC` | `sheet_snapshots/latest`                       |

7) Commit & Push to main
```bash
git add .
git commit -m "Initial commit"
git push origin main
```
This triggers GitHub Actions. If your secrets are set correctly, you'll see the "Deploy Cloud Function" job succeed and your function `sheet_webhook` will be live.

8) Manually Set Up Drive "Watch" on the Sheet
Use Cloud Shell or your local terminal (with gcloud auth done):

```bash
gcloud auth application-default login
```
Then run:

```bash
PROJECT_ID="YOUR_PROJECT_ID"
SHEET_ID="YOUR_SHEET_ID"
FUNCTION_URL="https://us-central1-${PROJECT_ID}.cloudfunctions.net/sheet_webhook"  # adjust if your region differs

curl -X POST \
  -H "Authorization: Bearer $(gcloud auth application-default print-access-token)" \
  -H "Content-Type: application/json" \
  -d '{ \
    "id": "sheet-watch-channel-123", \
    "type": "web_hook", \
    "address": "'"${FUNCTION_URL}"'" \
  }' \
  "https://www.googleapis.com/drive/v3/files/${SHEET_ID}/watch"
```
Replace "sheet-watch-channel-123" with a unique string (UUID or any unique channel ID).

After a successful call, Drive will respond with:

```json
{
  "kind": "api#channel",
  "id": "sheet-watch-channel-123",
  "resourceId": "...",
  "resourceUri": "...",
  "token": "...",
  "expiration": "YYYY-MM-DDThh:mm:ss.000Z"
}
```
Keep the returned "resourceId" and "expiration" if you want to explicitly stop the watch later. The watch expires in ~7 days; you must renew it periodically (see Step 10).

Now whenever the Sheet changes (Form submission or manual edit), Drive sends an HTTP POST to your function's URL immediately (latency typically under a few seconds). 