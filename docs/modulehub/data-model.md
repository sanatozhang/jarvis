# modulehub — data model

Tables use the `mh_` prefix, are created by `Base.metadata.create_all` at startup (like the other jarvis modules —
`modulehub.models` is imported in `main.py` before `init_db`) and never reference non-`mh_` tables.

## `mh_release` (one row per release or preview of one module repo)

| column | meaning |
|---|---|
| id | primary key |
| module, platforms, branch | `logger`; `android,ios` \| `android` \| `ios` (shipped as one version); `main` \| `release/*` |
| major | major bump requested |
| kind | `release` or `preview` (preview = dry run, takes no lock, opens no PR) |
| state | `pending, building, tagged, pr_opened, backport_opened, done, failed` |
| failed_from | `building` / `uploaded` / `tagged` / `pr_opened` when state = failed; drives resume |
| version, git_sha | released version (one tag `v<version>`) and commit, from publish-result.json |
| artifacts_json | platform -> `{coordinate, sha256, apiChanges}` from publish-result.json |
| previous_versions_json | platform -> version that platform's shell pinned before (bump PR title; the newest one starts the backport range) |
| bump_prs_json | platform -> bump PR url (`""` when the shell already pinned the version); platforms missing here are retried on resume |
| build_ref, build_url | `<jenkins server>|<queue id>` and the build URL |
| backport_pr_url | backport PR (empty when none was needed) |
| error | last failure message |
| requested_by, resume_count | actor and number of resumes |
| created_at, updated_at | timestamps |

Lock: at most one row with `kind='release'` in an active state (`pending…backport_opened`) per module (all its platforms
share one version line); enforced in `ReleaseService.start/resume` via `Store.find_active` (single-writer deployment; a
partial unique index is the upgrade path if jarvis ever runs several writers).

## `mh_release_event`
`release_id → mh_release.id`, `at`, `state`, `message` — an audit trail of every save.

## `mh_mirror_log`
`module, branch, action (create|verify|no_pin), outcome, at` — result of each reconciliation step (outcome carries
`ok`, `created from vX`, `MISMATCH: …`, `DIVERGED: …` or `error: …`).

## Not stored
The module list (read from the shell repos' `modules.versions.toml`), artifacts (Nexus / GitHub), credentials.
