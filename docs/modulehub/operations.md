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
| `MODULEHUB_GITHUB_TOKEN` (optional; falls back to the server's `gh auth token` login, **never** to `GH_TOKEN` / `GITHUB_TOKEN` — personal PATs get rejected by the Plaud-AI org 90-day policy) | GitHub REST + git push: read shell/module repos, open PRs, create branches, push backport branches. Needs contents + pull-requests write on the shell and module repos. On 102 the `gh` login is `appbot-ctrl` (the same one crashguard opens PRs with) |
| `JENKINS_*` (existing) | Jenkins servers/credentials, reused from the release automation |

The Jenkins job `module-publish` (kit `jenkins/Jenkinsfile`) holds the Nexus and mirror credentials; modulehub never sees them.

## Authentication

Requests need a logged-in user (SSO middleware sets `request.state.user`); the actor is recorded in `requested_by`.
`modulehub.allow_anonymous: true` skips the check — development only.

## API (see module-kit `docs/orchestrator/api.md`)

```
GET  /api/modulehub/modules
GET  /api/modulehub/releases?limit=50
POST /api/modulehub/releases:preview    {"module":"logger","platforms":["android","ios"],"branch":"main","major":false}
POST /api/modulehub/releases            same body; 201, state=building ("platforms" defaults to both; ["ios"] ships iOS only)
GET  /api/modulehub/releases/{id}
POST /api/modulehub/releases/{id}:resume
POST /api/modulehub/mirror:sync
```
Errors: 400 invalid request, 401 not logged in, 404 unknown release, 409 another release active / not resumable.

## Runbook

| Symptom | Meaning | Action |
|---|---|---|
| release `failed`, `failed_from=building` | the job failed before anything was uploaded (preflight/test/build/API check) | read `build_url`, fix, start a new release |
| `failed_from=uploaded` | some artifacts of the version exist (a platform or the tag step failed after an upload) | fix the cause, `POST …:resume` (reruns the job with `RESUME=true`: uploaded platforms are kept, the rest is published, then tagged) |
| `failed_from=tagged` / `pr_opened` | a bump / backport PR step failed (GitHub error, conflict, token) | fix the cause, `POST …:resume` (only the missing PRs are opened) |
| `409` on start | an active release of that module (any platform) | wait, or resume/finish the active one |
| mirror log `DIVERGED` | the shells pin different versions on a release branch and the older platform changed in between | cut the module branch by hand; next time ship both platforms before release branches are cut |
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
so a UI can be added as an isolated `frontend/src/app/modulehub/` directory that only calls `/api/modulehub`. The release form
needs: module, branch, an Android and an iOS checkbox (both ticked by default; at least one), major, and Preview / Release
buttons; the release page shows the version, one artifact and one bump PR per platform, and a Resume button when `failedFrom`
allows it.

## Schema change (2026-10-02)

`mh_release` moved from one row per (module, platform) to one row per module release (`platforms`, `artifacts_json`,
`previous_versions_json`, `bump_prs_json`); `mh_mirror_log` lost `platform`. modulehub has never been deployed, so there is
no migration: `create_all` builds the new tables. If an environment ever created the old ones, drop the three `mh_*` tables.
