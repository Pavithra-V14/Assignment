# Connecting a Google Drive folder as the data source

This lets the PMO/company workflow stay exactly as it is today — project
managers update the project plan workbook that already lives in a shared
Drive folder — and the agent just picks up whatever is current in that
folder on its next run. Nobody downloads or manually attaches a file.

## How it works

- `DATA_SOURCE=drive` and `DRIVE_FOLDER_URL=<the folder link>` are the only
  two settings that change (see `.env.example`).
- Every run, `ingestion/drive_client.py` lists every `.xlsx` file (and any
  native Google Sheet, which it auto-exports to `.xlsx`) directly inside
  that folder, and downloads only the ones that are new or have changed
  since the last run (tracked via Drive's `modifiedTime`, not by re-reading
  file content).
- Files removed from the Drive folder are removed from the local cache too,
  so a report is never generated from a workbook someone deleted.
- Everything downstream (`data_loader.py`, `metrics.py`, the LLM reasoning
  layer) is completely unaware this happened — it only ever sees local file
  paths, exactly as if they'd been placed in `sample_data/` by hand.

## One-time setup (5 minutes)

1. **Create a Google Cloud service account** (this is a "robot" account —
   not a personal Google login — meant for unattended jobs like a cron run):
   - Go to [Google Cloud Console](https://console.cloud.google.com/) →
     IAM & Admin → Service Accounts → *Create Service Account*.
   - Enable the **Google Drive API** for that project
     (APIs & Services → Library → search "Google Drive API" → Enable).
   - Create a JSON key for the service account (*Keys* tab → *Add Key* →
     *JSON*) and download it — this is `service-account.json`.

2. **Share the Drive folder with the service account**, the same way you'd
   share it with a colleague:
   - Open the service account's JSON key file and copy the `client_email`
     value (looks like `agent-reader@your-project.iam.gserviceaccount.com`).
   - In Google Drive, right-click the folder → *Share* → paste that email
     → give it **Viewer** access → Send.

3. **Copy the folder link** (right-click the folder → *Get link* / *Share*).
   It looks like:
   ```
   https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz
   ```
   The agent accepts this exact link, or just the ID at the end
   (`1AbCdEfGhIjKlMnOpQrStUvWxYz`).

4. **Configure the agent** (`.env`):
   ```
   DATA_SOURCE=drive
   DRIVE_FOLDER_URL=https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz
   GOOGLE_SERVICE_ACCOUNT_FILE=/path/to/service-account.json
   ```
   Install the extra dependency once: `pip install ".[drive]"`.

5. Run it: `project-health-weekly`. Logs will show which files were synced.

## Running this in CI / GitHub Actions instead of a local machine

You can't commit `service-account.json` to the repo. Instead, base64-encode
it and store it as a repo secret:

```bash
base64 -w0 service-account.json   # copy the output
```

Add it as a repository secret named `GOOGLE_SERVICE_ACCOUNT_JSON_BASE64`,
and set `DRIVE_FOLDER_URL` and `DATA_SOURCE=drive` as repo variables. The
workflow in `.github/workflows/weekly_report.yml` already wires these
through — see that file for the exact env mapping.

## Switching back to local files

Set `DATA_SOURCE=local` (or delete the line — it's the default). The agent
will read from `LOCAL_DATA_DIR` (default: `sample_data/`) instead, no other
changes needed. This is the recommended mode for local development, demos,
and CI unit tests, so nothing ever needs live Drive access to run.

## Multiple teams / portfolios

One folder = one portfolio in this design. If different business units
should be scored and reported on separately, point separate scheduled runs
at separate folders (different `DRIVE_FOLDER_URL` per run/environment)
rather than mixing plans from multiple teams into one folder.
