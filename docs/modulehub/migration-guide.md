# modulehub — moving it to another system

modulehub is designed to be lifted out of jarvis. What carries over untouched, and what you replace:

| Keep as is (no jarvis knowledge) | Replace (jarvis knowledge) |
|---|---|
| `core/` — toml rewrite, naming, state machine, rules, mirror/backport plans, result parsing | `adapters/` — one file per port |
| `service.py`, `ports.py` — use cases | `models.py` / `adapters/store_sqlalchemy.py` — persistence (or implement `Store` on any DB) |
| `api/router.py` — only needs an object with `.releases`, `.mirror`, `.store`, `.settings` and `request.state.user` | `config.py` — settings source (yaml/env today) |
| `workers/loops.py` — `run_forever` | `__init__.py` `Hub` — the wiring |
| tests under `tests/modulehub/` (except adapter tests) | `main.py` three hooks (models import, `register`, lifespan start/stop) |

## Ports to implement

| Port | jarvis adapter | What a new system needs to provide |
|---|---|---|
| `BuildRunner` | `adapters/jenkins_runner.py` | trigger the kit's `module-publish` job with `REPO, BRANCH, PLATFORMS, MAJOR, DRY_RUN, RESUME`; `status(ref)` → queued/running/success/failure/aborted + last console lines (for `STEP=`) + the `publish-result.json` text on success |
| `ScmHost` | `adapters/github_scm.py` | read a file at a ref; list branches by prefix; create a branch from a tag; "branch contains ref"; commits and changed files between refs; open-or-update a one-file PR; open a backport PR (cherry-pick range, placeholder on conflict); "range already on base" |
| `Notifier` | `adapters/notifier.py` | `notify(text)`; must never raise |
| `Store` | `adapters/store_sqlalchemy.py` | CRUD for `ReleaseRecord`, `find_active(module)` (the lock), `list_in_flight`, `list_recent`, `log_mirror` |
| identity | `api/router.py::_actor` | return the acting user's name/email for `requested_by` |

## Steps

1. Copy `core/`, `ports.py`, `service.py`, `api/`, `workers/`, and `tests/modulehub/` (minus `test_architecture.py`'s jarvis rules).
2. Implement the five adapters above against the new platform. The adapter tests show the expected behaviours (they use
   fakes / `httpx.MockTransport`, so they are good templates).
3. Re-create the `mh_*` tables (see `data-model.md`) or implement `Store` elsewhere; no other table is touched.
4. Wire `Hub`/`register` into the new app; start `run_forever(releases.tick_all, poll)` and `run_forever(mirror.sync, interval)`.
5. Point it at the same Jenkins job and shell repos. **Nothing else changes**: versioning is in the kit, the job contract
   (`docs/orchestrator/publish-job.md`) and result schema are system-independent, and the toml format is owned by the shells.
6. Verify with the kit's acceptance list (spec §8.2 G3): release both platforms from `main` (one bump PR per shell), a
   one-platform release (one bump PR), two releases → the same PRs updated, release-branch patch with backport PR,
   API-break refusal, job runnable without modulehub.

Any scheduler that honours the contracts in module-kit `docs/orchestrator/` is a valid replacement — modulehub is just the first one.
