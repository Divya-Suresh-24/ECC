# ECC Protocol Assistant

A retrieval-augmented (RAG) chatbot for Iowa State University's Emergency & Critical Care residents, interns and house officers. Ask a question in plain language and get a short answer drawn **only** from ISU ECC protocols, SOPs, the intern handbook and teaching files. Every statement carries a numbered citation you can click to see the exact passage, open the full document, or download the original. Faculty manage the document library, see what people ask, and manage accounts.

## What's in it

**Residents and interns**
- **Ask:** chat-style Q&A with follow-up questions. Answers cite sources inline (`[1]`, `[2]`). Clicking a citation opens a side panel with the cited lines highlighted, plus *Open in document*, *View PDF page* and *Download*.
- If the documents don't cover a question, the assistant says so ("I couldn't find this in the ECC documents") instead of guessing. It never supplies doses from general knowledge.
- **Library:** browse by category or search the full text, read any document in the app, download it (if faculty allow), or click *Ask* to ask questions about that one document only.
- 👍/👎 feedback on every answer. A 👎 asks what was wrong, and that note goes to faculty.
- Conversation history, and copying an answer together with its sources.

**Faculty**
- **Documents:** drag and drop many files at once (PDF, Word .docx, PowerPoint .pptx, .txt, .md). The category is auto-detected and you can change it. You can also set an effective date and a per-document **downloadable** switch (off = view-only for residents and interns).
- **Replace file** updates a protocol in place. Old versions are archived (and can be downloaded from *Edit*), and the assistant switches to the new text immediately.
- **Retire** removes a document from answers without deleting it. **Search keywords** help a document match how people actually phrase questions (e.g. "blocked cat").
- **Questions & feedback:** a review queue of unanswered questions (likely gaps in the documents) and 👎 answers, with full answer and sources. You can also export everything to CSV.
- **Users:** add one account or paste a class list (`netid, Full Name`), set roles, disable accounts and reset passwords. Temporary passwords must be changed at first sign-in.
- **Activity log** of every document and account change.

## How answers are produced

1. **Search.** Each document is split into passages tagged with their page, slide or section. A fast model (Claude Haiku) rewrites the question into search keywords, expanding ECC shorthand like UO, GDV, IVDD, abx and torb. SQLite full-text search (BM25) then ranks the passages. No vector database and no GPU are needed.
2. **Answer.** The top passages go to Claude (Sonnet) as `search_result` blocks with **citations enabled**. Claude is instructed to answer only from them, to copy doses and thresholds exactly, to put SOPs ahead of lecture slides, and to say when something isn't covered.
3. **Verify.** The API returns citations tied to specific lines of specific passages. The app shows those lines highlighted, so every claim can be checked against the source.

Everything except the Claude API call runs on your server. Only the question, recent conversation turns and the matching passages are sent to Anthropic.

## Quick start (local test)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # add ANTHROPIC_API_KEY, or set MOCK_LLM=1 to try it without a key
python -m app.cli create-user yourname --name "Your Name" --faculty
python -m app.cli import-folder /path/to/ecc-documents
uvicorn app.main:app --reload --port 8010
# open http://localhost:8010
```

`MOCK_LLM=1` runs the whole app without an API key. Answers then show the matching passages instead of a written answer, which is useful for checking that search finds the right documents.

## Deploying on an ISU VM (same pattern as the ECG tutor)

These steps assume a Linux VM with Python 3.10+ and nginx or Apache already serving HTTPS.

```bash
sudo useradd -r -m -d /srv/ecc-assistant ecc
sudo -u ecc git clone <this repo> /srv/ecc-assistant/app
sudo -u ecc python3 -m venv /srv/ecc-assistant/venv
sudo -u ecc /srv/ecc-assistant/venv/bin/pip install -r /srv/ecc-assistant/app/requirements.txt
sudo -u ecc mkdir -p /srv/ecc-assistant/data
sudo -u ecc cp /srv/ecc-assistant/app/.env.example /srv/ecc-assistant/app/.env
sudo -u ecc nano /srv/ecc-assistant/app/.env     # API key, DATA_DIR=/srv/ecc-assistant/data, ROOT_PATH, COOKIE_SECURE=1, first admin
sudo chmod 600 /srv/ecc-assistant/app/.env

# load the documents (put all the PDFs / .docx / .pptx in one folder first)
cd /srv/ecc-assistant/app && sudo -u ecc /srv/ecc-assistant/venv/bin/python -m app.cli import-folder /path/to/ecc-docs

sudo cp deploy/ecc-assistant.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now ecc-assistant
```

Then add the reverse-proxy snippet from `deploy/nginx.conf` or `deploy/apache.conf`. To serve it at `https://<host>/ecc-assistant/`, set `ROOT_PATH=/ecc-assistant`. The proxy strips that prefix before passing requests to the app. Check that it's running at `/healthz`.

Optional: run `sudo apt install tesseract-ocr` (or `dnf install tesseract`) so scanned PDF pages are read with OCR. If you prefer containers, a `Dockerfile` is included.

**Backups:** everything (database and uploaded files, including old versions) lives in `DATA_DIR`. Back up that folder.

**Updating the code:** `git pull`, then `pip install -r requirements.txt`, then `sudo systemctl restart ecc-assistant`.

## Loading the initial documents

Put all the original files in one folder and run `python -m app.cli import-folder <folder>`. That includes the Word and PowerPoint files (handbook, overnight guidelines, triage, lecture decks) and the PDFs. Identical files are skipped automatically. Faculty can also drag and drop everything into **Faculty → Documents**.

Use the original `.docx` and `.pptx` files rather than copies or exports, so citations point to the right section or slide and downloads give people the real file.

## Configuration

All settings are environment variables, usually in `.env`. See `.env.example`.

| Variable | Default | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | (none) | Required for real answers |
| `ANSWER_MODEL` | `claude-sonnet-5-5` | Writes the cited answer |
| `REWRITE_MODEL` | `claude-haiku-5-5` | Turns questions into search keywords (cheap and fast) |
| `MOCK_LLM` | `0` | `1` = no API calls (testing) |
| `DATA_DIR` | `./data` | Database and files. Keep it outside the git checkout. |
| `ROOT_PATH` | (empty) | Sub-path, e.g. `/ecc-assistant` |
| `COOKIE_SECURE` | `0` | Set `1` behind HTTPS |
| `INITIAL_ADMIN_USERNAME` / `_PASSWORD` | (none) | Creates the first faculty account on first start |
| `TOP_K` | `8` | Passages sent to the model per question |
| `DAILY_QUESTION_LIMIT` | `150` | Per user, per rolling 24 h |
| `MAX_UPLOAD_MB` | `50` | Per file |

## Command-line tools

```bash
python -m app.cli create-user <username> --name "Full Name" [--faculty]
python -m app.cli import-folder <folder> [--category "Protocols & SOPs"] [--view-only]
python -m app.cli reset-password <username>
python -m app.cli list-users
python -m app.cli reindex          # re-extract every file (after changing CHUNK_CHARS)
```

## Moving to NetID sign-in (Microsoft Entra ID) later

Sign-in is isolated in `app/auth.py`. The rest of the app only uses `current_user` and `require_faculty`. To switch to ISU NetID:

1. Ask ISU IT to register an Entra ID (Azure AD) app with redirect URI `https://<host>/ecc-assistant/auth/callback`. You'll get a tenant ID, client ID and secret.
2. Add an OpenID Connect login route (for example with `msal` or `authlib`) that, after Microsoft sign-in, looks up the user by NetID (`username`) or email. On first login it can create them as `student`.
3. Keep faculty status as a role in the `users` table, which is already editable on the Users page.
4. Set `AUTH_MODE=entra` and hide the password form.

If accounts are created with NetIDs as usernames now, they carry over with their history and roles.

## Privacy and safety notes

- The app shows a standing reminder that it is a reference tool and that users should not enter client names or record numbers. Questions are logged, with the asker, so faculty can review answers and find documentation gaps. Tell users this.
- Check with ISU IT Security that hospital SOPs are OK to send to the Anthropic API under your data-classification rules before going live.
- Sessions use signed HttpOnly cookies. Passwords are hashed with scrypt, failed sign-ins are rate-limited, and state-changing requests require a same-origin header.

## Development

```bash
MOCK_LLM=1 python -m pytest -q
```

Code map: `app/ingest.py` (text extraction and chunking), `app/search.py` (BM25 retrieval and abbreviations), `app/llm.py` (Claude calls and citation parsing), `app/documents.py` (library operations), `app/main.py` (routes), `static/` (front end, no build step).
