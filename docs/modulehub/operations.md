# modulehub — operations

## Enable

modulehub is **off by default**: the API is mounted, workers do not start. Configure and enable:

```yaml
# config.local.yaml (per server, not in git)
modulehub:
  enabled: true
  jenkins_job: module-publish
  poll_interval_seconds: 30
  mirror_interval_minutes: 10
  shell_repo_android: Plaud-AI/plaud-native-android
  shell_repo_ios: Plaud-AI/plaud-native-ios
  notify_emails: [someone@plaud.ai]
  base_url: https://jarvis.example      # deep links in PR bodies
```

Every key can be overridden by env `MODULEHUB_<KEY>` (env wins). Secrets are env only:

| env | purpose |
|---|---|
| `MODULEHUB_GITHUB_TOKEN` (falls back to `GH_TOKEN` / `GITHUB_TOKEN`) | GitHub REST + git push: read shell/module repos, open PRs, create branches, push backport branches. Needs contents + pull-requests write on the shell and module repos |
| `JENKINS_*` (existing) | Jenkins servers/credentials, reused from the release automation |

The Jenkins job `module-publish` (kit `jenkins/Jenkinsfile`) holds the Nexus and mirror credentials; modulehub never sees them.

## Authentication

Requests need a logged-in user (SSO middleware sets `request.state.user`); the actor is recorded in `requested_by`.
`modulehub.allow_anonymous: true` skips the check — development only.

## API (see module-kit `docs/orchestrator/api.md`)

```
GET  /api/modulehub/modules
GET  /api/modulehub/releases?limit=50
POST /api/modulehub/releases:preview    {"module":"logger","platform":"android","branch":"main","major":false}
POST /api/modulehub/releases            same body; 201, state=building
GET  /api/modulehub/releases/{id}
POST /api/modulehub/releases/{id}:resume
POST /api/modulehub/mirror:sync
```
Errors: 400 invalid request, 401 not logged in, 404 unknown release, 409 another release active / not resumable.

## Runbook

| Symptom | Meaning | Action |
|---|---|---|
| release `failed`, `failed_from=building` | the job failed before anything was uploaded (preflight/test/build/API check) | read `build_url`, fix, start a new release |
| `failed_from=uploaded` | artifacts exist, tag step failed | `POST …:resume` (reruns the job with `RESUME=true`, which only tags) |
| `failed_from=tagged` / `pr_opened` | bump / backport PR step failed (GitHub error, conflict, token) | fix the cause, `POST …:resume` |
| `409` on start | active release for that module+platform | wait, or resume/finish the active one |
| backport PR titled `[CONFLICT]` | cherry-pick conflicts | resolve the placeholder PR by hand |
| mirror log `MISMATCH` | module `release/x` exists but lacks the shell's pinned tag | a human decides; modulehub never rewrites existing branches |
| release stuck in `building` | Jenkins build gone or poller disabled | check `modulehub.enabled`, `JENKINS_*`; the queue item disappearing marks the release failed |

## Tests

```bash
cd backend && pytest tests/modulehub -v          # unit + adapters (fakes/mocks only, no network)
pytest tests/modulehub/test_architecture.py      # isolation contract
```
Python 3.12 (the Dockerfile's version) is required for the whole app; `core/` itself runs on 3.9+.

## Frontend

Not implemented: the repo's frontend pattern needs edits to shared files (sidebar, `lib/api.ts`, `i18n.ts`). The API is complete,
so a UI can be added as an isolated `frontend/src/app/modulehub/` directory that only calls `/api/modulehub`.
