# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

### Backend (Python / FastAPI)

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Development server (auto-reload)
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# Or via main.py directly
python -m app.main
```

### Frontend (Next.js)

```bash
cd frontend
npm install
npm run dev       # Development
npm run build     # Production build
npm run lint      # ESLint
npm start         # Production start
```

### Docker (full stack)

```bash
docker compose up -d
docker compose logs -f backend
```

### Rule hot-reload (no restart needed)

```bash
curl -X POST http://localhost:8000/api/rules/reload
```

## Architecture

```
Frontend (Next.js 15 + React 19 + Tailwind CSS 4)
  ↕ REST + SSE
Backend (FastAPI + SQLAlchemy + SQLite)
  ↕ subprocess
Agents (Claude Code CLI / Codex CLI — external binaries)
```

### Configuration Layering

Config is merged in this priority order: **env vars > config.yaml > defaults**

- `.env` — secrets: `FEISHU_APP_ID`, `FEISHU_APP_SECRET`, `LINEAR_API_KEY`, `DATABASE_URL`, etc.
- `config.yaml` — agent selection, routing, concurrency, model names
- `backend/app/config.py` — loads both via `get_settings()` (cached singleton)

### Analysis Pipeline

The core flow lives in `backend/app/workers/analysis_worker.py:run_analysis_pipeline()`:

1. **Fetch issue** — from Feishu API, local DB (`fb_` prefix), or Linear webhook (`lin_` prefix)
2. **Download logs** — Feishu file attachments; results cached in `workspaces/_cache/{issue_id}/`
3. **Decrypt** — `.plaud` proprietary format processed in `services/decrypt.py`
4. **Match rules** — `RuleEngine` matches issue description against Markdown rules in `backend/rules/`
5. **Pre-extract** — grep patterns from matched rules run against log files (L1 extraction)
6. **Build workspace** — `workspaces/{task_id}/` with subdirs: `raw/`, `logs/`, `rules/`, `code/`, `output/`
7. **Run agent** — CLI subprocess (`claude` or `codex`) writes JSON to `output/result.json`
8. **Parse result** — `BaseAgent.parse_result()` reads JSON; falls back to raw stdout parsing

### Issue Sources

| Prefix | Source | How it enters |
|--------|--------|---------------|
| (none) | Feishu | Pulled from Feishu BitTable via API |
| `fb_`  | Local  | Submitted via feedback form UI |
| `lin_` | Linear | Linear webhook (`/api/linear`) + `@ai-agent` comment trigger |

### Agent Abstractions

- `backend/app/agents/base.py` — `BaseAgent` ABC with `analyze()` and static `build_prompt()` / `parse_result()`
- `backend/app/agents/claude_code.py` — runs `claude` CLI as subprocess
- `backend/app/agents/codex.py` — runs `codex` CLI as subprocess
- `backend/app/services/agent_orchestrator.py` — selects agent via routing config, builds prompt, calls agent

Agent routing: `config.yaml agent.routing` maps problem type → agent name. Falls back to `agent.default`.

### Analysis Result Schema

Agents must write `output/result.json` with these fields (all required):
- `problem_type` / `problem_type_en`
- `root_cause` / `root_cause_en`
- `confidence`: `"high" | "medium" | "low"`
- `key_evidence`: list of log lines (max 5)
- `user_reply` / `user_reply_en` — customer-facing reply templates
- `needs_engineer`: boolean
- `fix_suggestion`

### Rules System

Rules live in `backend/rules/` as Markdown files with YAML frontmatter. `RuleEngine`:
- Loads and syncs to DB on startup
- Matches rules against issue descriptions via keywords and regex
- Pre-extracts log patterns (grep) before agent runs
- Hot-reloadable without restart

### Frontend

- Next.js App Router under `frontend/src/app/`
- All API calls in `frontend/src/lib/api.ts`
- i18n: Chinese keys with English translations in `frontend/src/lib/i18n.ts`; use `useT()` hook everywhere
- Real-time progress via SSE (`subscribeTaskProgress` in `api.ts`)

Pages: `/` (main analysis), `/tracking`, `/feedback`, `/oncall`, `/analytics`, `/rules`, `/reports`, `/settings`

### Backend API Prefixes

`/api/issues`, `/api/tasks`, `/api/rules`, `/api/settings`, `/api/reports`, `/api/health`, `/api/local`, `/api/feedback`, `/api/users`, `/api/oncall`, `/api/v1`, `/api/env`, `/api/analytics`, `/api/linear`

API docs available at `http://localhost:8000/docs` when running locally.

### Database

SQLite by default at `data/jarvis.db`. Switch to PostgreSQL by setting `DATABASE_URL=postgresql+asyncpg://...` and uncommenting `asyncpg` in `requirements.txt`.

Zombie task cleanup (tasks stuck in analyzing/queued states) runs automatically on startup.
