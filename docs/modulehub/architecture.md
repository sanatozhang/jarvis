# modulehub — architecture

modulehub releases independent native modules (Android AAR / iOS XCFramework) on behalf of engineers. A module is one
repository (the `repo` both shells' `modules.versions.toml` name, e.g. `Plaud-AI/mobile_logger`) holding `android/` and/or `ios/`; one release ships the platforms the engineer ticks
(both by default) as one version with one tag. It is a **pure scheduler**: it triggers one Jenkins job, waits, opens PRs, reconciles branches and notifies. It does not
compute versions, build, sign or upload anything — that is all in `module-kit` (the job it triggers).

Contracts it implements against (owned by the kit repo, `docs/orchestrator/`): `publish-job.md`,
`publish-result.schema.json`, `versioning.md`, `bump-pr.md`, `backport-pr.md`, `branch-mirror.md`, `api.md`.

## Layout (hexagonal)

```
backend/app/modulehub/
├── core/          pure logic, stdlib only
│   ├── versions_toml.py   read / surgically rewrite one [module] table of modules.versions.toml
│   ├── naming.py          bump / backport branch names, PR titles and bodies
│   ├── states.py          release state machine + resume rules
│   ├── release_rules.py   request validation (branch, major, platforms), repo naming, semver order
│   ├── mirror.py          release-branch reconciliation plan (both shells' pins -> one module branch)
│   ├── backport.py        release -> main backport plan
│   └── result.py          publish-result.json (v2, one entry per platform) parsing, STEP= marker parsing
├── ports.py       BuildRunner / ScmHost / Notifier / Store protocols + ReleaseRecord
├── service.py     ReleaseService (start/tick/resume), MirrorService (sync) — depends on core + ports only
├── adapters/      the ONLY place that knows jarvis: Jenkins client, GitHub REST+git, Feishu, SQLAlchemy
├── models.py      mh_* tables
├── config.py      settings (yaml `modulehub:` < env MODULEHUB_*)
├── api/router.py  /api/modulehub
├── workers/loops.py   run_forever(step, interval)
└── __init__.py    Hub (lazy wiring) and register(app) — the single mount point
```

## Isolation rules (enforced by `tests/modulehub/test_architecture.py`)

1. modulehub never imports `crashguard`, `coreguard`, `graygate`, `platform_tickets`, `app.api.release`, `app.workers`.
2. None of those (or any other package) imports modulehub. `app/main.py` may import only `app.modulehub` (register) and
   `app.modulehub.models` (table registration before `init_db`).
3. `core/` imports nothing but the stdlib and other `core` modules; `service.py` and `ports.py` import only `core` and `ports`.
4. Only `adapters/`, `config.py`, `models.py` and the package root touch jarvis infrastructure (`app.config`, `app.db`, `app.services`).
5. Tables are prefixed `mh_`, foreign keys point only to `mh_` tables; the startup DB check (`main.py`) includes the `mh_` prefix.

The Jenkins client in `app/services/jenkins_client.py` gained two additive methods (`fetch_console_tail`,
`fetch_artifact_text`) because the publish job's STEP= markers and `publish-result.json` are read from the build.

## Release flow

```
POST /releases ──► validate (branch, major, platforms; module listed in every selected shell's modules.versions.toml on that
                    branch; one active release per module) ──► Jenkins module-publish (REPO, BRANCH, PLATFORMS, MAJOR, DRY_RUN, RESUME)
                                                                    │ poll every poll_interval_seconds
   building ──job ok + publish-result.json covers exactly the selected platforms──► tagged
        │        ──one bump PR per shipped platform's shell (update if one is open)──► pr_opened
        │                                                     └─ release/*: one backport PR in the module repo (conflict => placeholder PR + notify)
        └─job failed── failed_from = uploaded if the last STEP was `upload`/`tag`, else building     └──► done
```

State machine: `pending → building → tagged → pr_opened → (backport_opened) → done | failed`. `failed` remembers where
it failed (`failed_from`); resume is possible when artifacts exist (`uploaded` → rerun job with `RESUME=true`, which keeps
the uploaded platforms, publishes the rest and tags; `tagged` → open the bump PRs still missing; `pr_opened` → reopen the
backport PR). A failure before any upload needs a fresh release.

The lock is per module, not per platform: all platforms share one version line, so two concurrent releases of one module
would race for the same version.

`POST /releases:preview` runs the same job with `DRY_RUN=true`, never takes the lock and never opens PRs; the version in
its result is the version the real release would get.

## Branch reconciliation

Every `mirror_interval_minutes` (default 10) and on `POST /mirror:sync`: for each module listed in either shell's
`modules.versions.toml`, collect what both shells pin on each `release/*` branch and compare with the module repo's
`release/*`. A missing module branch is created from the newest pinned tag; an existing one is verified to contain every
pinned tag (mismatch → alert, no change). When the shells pin different versions (after one-platform releases), the newest
tag is used only if it contains the older one and the older platform's directory did not change in between; otherwise
the branch is reported `DIVERGED` and a human cuts it. A branch that pins no such module is logged as `no_pin`. Results go
to `mh_mirror_log`.

## Known limits

- `range_applied_on` treats a range as already on `main` only if its head tag is an ancestor of `main`
  (cherry-picked duplicates are not detected by patch-id).
- Backport conflicts open a `[CONFLICT]` placeholder PR (empty commit); the owner resolves it by hand.
- Shell repos come from config; the module repo is the `repo` of the module's table in the shells' toml (both shells must agree, otherwise the release is refused and the mirror reports it).
- The divergence check reads GitHub's compare file list, which is capped at 300 files.
