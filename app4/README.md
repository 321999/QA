# QA Pulse — BPO Call Audit Dashboard

A full-stack app matching the wireframe: login page → dashboard with 5 summary
cards + 16-point QA checklist graph (click a parameter to see agents) →
agent scorecard (total/average/good/poor calls + strengths & improvement areas).

- **Backend:** Python (FastAPI) + MySQL
- **Frontend:** React (Vite)

## Project structure

```
app/
  backend/
    main.py          # API endpoints
    seed.py           # creates + seeds qa_dashboard.db with sample data
    requirements.txt
  frontend/
    src/
      pages/           # Login, Dashboard, ParameterAgents, AgentDetail
      components/       # DateRangePicker
      api.js            # fetch wrapper for the backend
```

## 1. Backend setup

Requires a running MySQL (or MariaDB) server reachable from the backend.

```bash
cd backend
pip install -r requirements.txt

export MYSQL_HOST=localhost
export MYSQL_PORT=3306
export MYSQL_USER=root
export MYSQL_PASSWORD=yourpassword
export MYSQL_DATABASE=qa_dashboard   # created automatically if it doesn't exist

python seed.py             # creates the schema + ~60 days of sample calls
uvicorn main:app --reload --port 8000
```

API will be live at `http://localhost:8000` (interactive docs at `/docs`).

Demo login: **admin / admin123** (also **manager / manager123**)

### Key endpoints
| Endpoint | Purpose |
|---|---|
| `POST /api/login` | authenticate |
| `GET /api/dashboard/summary?start=&end=` | total / audited / pending / positive / negative calls |
| `GET /api/dashboard/parameters?start=&end=` | avg score per QA parameter |
| `GET /api/dashboard/parameter-funnel?start=&end=` | **(new)** % of calls that *passed* each parameter, sorted highest→lowest |
| `GET /api/dashboard/parameters/{name}/agents?start=&end=` | agents scored on that parameter, worst → best |
| `GET /api/dashboard/category/{category}/agents?start=&end=` | **(new)** agents + avg score for one stat-card category (`total`/`audited`/`pending`/`positive`/`negative`) |
| `GET /api/dashboard/category/{category}/agents/{agent_id}/calls?start=&end=` | **(new)** that agent's individual calls within the category |
| `GET /api/dashboard/top-agents?start=&end=&limit=` | **(new)** leaderboard: top N agents by avg score |
| `GET /api/agents/{id}?start=&end=` | agent scorecard: totals, good/poor calls, per-parameter scores, strengths/improvements |
| `GET /api/calls/{id}` | full scorecard for one call - parameters (with reason/evidence/timestamps), fatal checks, **and `rx_path`/`tx_path` recording URLs** |

Date range defaults to the **last 30 days** if `start`/`end` aren't passed — matching the brief.

## 2. Frontend setup

```bash
cd frontend
npm install
npm run dev
```

Opens at `http://localhost:5173`. It talks to the backend at the URL in
`frontend/.env` (`VITE_API_URL`, defaults to `http://localhost:8000`).

## How it maps to the sketch

1. **Login page** — left panel branding, right panel the sign-in form.
2. **Dashboard (page 2)**
   - 5 stat cards: Total calls, Audited calls, Pending calls, Positive calls, Negative calls
   - Date range selector, defaults to last 30 days, fully custom start/end also supported
   - The 16 QA parameters rendered as a horizontal bar checklist (numbered, like the audit form) — clicking any row goes to the agent list for that parameter
3. **Agent list** — agents tied to the clicked parameter, sorted worst-scoring first, with a score chip
4. **Agent scorecard (page 3)** — clicking an agent shows Total calls / Average call score / Good calls / Poor calls, the full parameter breakdown, and an auto-generated Overall strengths / Overall improvement areas summary (top 3 / bottom 3 scoring parameters)

## 3. Manifest upload → STT → SLM pipeline

Admins/managers can upload the dialer's Excel manifest (columns: `agent_name`,
`call_number`, `cti_call_number`, `call_start_time`, `call_service_name`,
`call_lead_id`, `call_end_type_name`, `call_talk_duration`,
`call_trunk_duration`, `Recordings`). For every row the backend:

1. Pulls the shared recording base name out of the `Recordings` cell
   (`7002_..._18-11-32.wav16` → `7002_..._18-11-32`) and builds the IN/OUT leg
   URLs: `{RECORDINGS_BASE_URL}/{agent_name}/{base}-IN.wav16` / `-OUT.wav16`.
2. Calls the speech-to-text service (`STT_API_URL`, same contract as your
   `/transcribe` curl example: `{"rx_path":..., "tx_path":...}` in, diarized
   `utterances` + `conversation` text out) and stores the transcript.
3. Sends that conversation to the SLM QA service (`SLM_API_URL`, your `/qa`
   endpoint) and stores the 16-point checklist scores, fatal checks
   (OTP/CVV/PIN/password/card-number asks), summary, and verdict.
4. Marks the call `audited` so it immediately shows up on the dashboard,
   parameter graph, agent scorecards, and a new per-call transcript view.

Configure the three external services via environment variables before
starting the backend (defaults match what you provided):

```bash
export STT_API_URL=http://192.168.11.253:9000/transcribe
export SLM_API_URL=http://localhost:8000/qa
export RECORDINGS_BASE_URL=http://192.168.10.189/qa_upload
uvicorn main:app --reload --port 8000
```

`RECORDINGS_BASE_URL` can also be overridden per-upload from the "Upload
manifest" screen if different manifests live on different recording servers.

Endpoints added for this:
| Endpoint | Purpose |
|---|---|
| `POST /api/manifest/upload` (multipart `file`) | parse manifest, create one `pending` call per row, kick off processing |
| `POST /api/manifest/{id}/process` | re-run processing for any rows still pending/failed |
| `GET /api/manifest` / `GET /api/manifest/{id}` | list manifests / poll a manifest's row-by-row status |
| `GET /api/calls/{id}` | full scorecard for one call (parameters + fatal checks + summary) |
| `GET /api/calls/{id}/transcript` | stored diarized transcript for that call |

Row statuses: `pending → transcribing → analyzing → audited` (or `failed`,
with `error_message` set — e.g. recording legs not found, STT/SLM
unreachable). Failed rows can be retried by hitting "process" again.

Note: the raw per-parameter scores your SLM returns use mixed max-points
(e.g. 4, 2, 10 — see the sample response). `config.py` has a
`PARAMETER_MAX_POINTS` map used to normalize each into a 0–100 score so
dashboard bars/averages stay comparable; adjust it to match your actual
rubric weights.

## 4. Dashboard drill-down: funnel, clickable stats, leaderboard, audio evidence

Three additions on top of the dashboard, all reading from the same `calls` /
`call_scores` tables the manifest pipeline fills in - no new tables needed.

**Parameter success funnel.** Below the stat cards, the 16 QA parameters are
now ranked by *pass rate* (`% of audited calls where that parameter's
status was true`) instead of average score, highest first - so it reads
top-to-bottom like a funnel: what agents nail almost every time at the top,
what they struggle with at the bottom. Backed by
`GET /api/dashboard/parameter-funnel`. Clicking a row still drills into the
agent list for that parameter (unchanged, same as the old avg-score bars).

**Clickable stat cards → agent list → call list → call detail.** All 5 top
cards (Total/Audited/Pending/Positive/Negative) are now buttons:

```
stat card (e.g. "Positive calls")
  -> agents ranked by avg score within that category      [CategoryAgents.jsx]
     -> that agent's individual calls, with score          [AgentCalls.jsx]
        -> full call detail: parameter marks + audio        [CallDetail.jsx]
```

The category is just a whitelisted SQL filter (`CATEGORY_FILTERS` in
`main.py`) - `total` has no filter, `audited`/`pending` filter on `status`,
`positive`/`negative` filter on `sentiment`.

**Audio evidence on the call detail page.** Every parameter row already
carries the SLM's `reason`, `evidence` quote, and `start_time`/`end_time`
(from the sample output you gave, e.g. `"greeted_professionally": {"status":
true, "evidence": "गुड आफ्टरनून सर", "start_time": 8.94, "end_time": 9.57}`).
The call detail page now plays the call's `rx_path`/`tx_path` recordings
(same URLs sent to the STT service) directly in an `<audio>` player, and a
"▶ Listen" button next to each parameter seeks straight to its `start_time`
so you can hear exactly why that score was given instead of scrubbing
through the whole call. A toggle switches between the agent leg (OUT) and
customer leg (IN) - both share the same wall-clock timeline so a timestamp
seeks correctly on either.

**Top-10 leaderboard.** Right-hand sidebar box on the dashboard, agents
ranked by average score over audited calls in the selected range. Backed by
`GET /api/dashboard/top-agents`. Clicking an agent opens their full
scorecard (the existing `AgentDetail` page).

## Extending with real data

`seed.py` is just demo data — delete/ignore it once you're driving the
dashboard from the manifest pipeline above. The schema (`agents`, `calls`,
`parameters`, `call_scores`, `fatal_checks`, `transcripts`, `manifests`)
covers both paths.

#### where is my loc 
```
dir /s /b Scripts
```
<p>command to know where is my Scripts folder<span>this i have used to locate my vevn</span></p>