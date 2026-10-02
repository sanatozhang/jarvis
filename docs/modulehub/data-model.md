# modulehub — data model

Tables use the `mh_` prefix, are created by `Base.metadata.create_all` at startup (like the other jarvis modules —
`modulehub.models` is imported in `main.py` before `init_db`) and never reference non-`mh_` tables.

## `mh_release` (one row per release or preview)

| column | meaning |
|---|---|
| id | primary key |
| module, platform, branch | `logger`, `android`\|`ios`, `main`\|`release/*` |
| major | major bump requested |
| kind | `release` or `preview` (preview = dry run, takes no lock, opens no PR) |
| state | `pending, building, tagged, pr_opened, backport_opened, done, failed` |
| failed_from | `building` / `uploaded` / `tagged` / `pr_opened` when state = failed; drives resume |
| version, previous_version | released version; version the shell pinned before (backport range start) |
| sha256, coordinate, git_sha | from publish-result.json |
| build_ref, build_url | `<jenkins server>|<queue id>` and the build URL |
| bump_pr_url, backport_pr_url | PRs opened (empty when none was needed) |
| api_changes_json | API changes reported by the job |
| error | last failure message |
| requested_by, resume_count | actor and number of resumes |
| created_at, updated_at | timestamps |

Lock: at most one row with `kind='release'` in an active state (`pending…backport_opened`) per (module, platform);
enforced in `ReleaseService.start/resume` via `Store.find_active` (single-writer deployment; a partial unique index
is the upgrade path if jarvis ever runs several writers).

## `mh_release_event`
`release_id → mh_release.id`, `at`, `state`, `message` — an audit trail of every save.

## `mh_mirror_log`
`module, platform, branch, action (create|verify|no_pin), outcome, at` — result of each reconciliation step.

## Not stored
The module list (read from the shell repos' `modules.versions.toml`), artifacts (Nexus / GitHub), credentials.
