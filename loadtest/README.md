# Synodic Load Tester

A standalone Locust-based load-testing harness for the Synodic backend. Lives at the repo root in `loadtest/` and is decoupled from `backend/` — install it in its own venv and point it at any running backend (local, staging, prod).

Used by the perf plan ([../docs/audits/](../docs/audits/) / `~/.claude/plans/`) to validate each slice against measurable SLOs. Generic enough to extend for any future perf work — add scenarios as new modules under `scenarios/`.

## Layout

```
loadtest/
├── README.md                # this file
├── Makefile                 # smoke-run targets — `make smoke` runs each scenario + mixed
├── requirements.txt         # locust >= 2.27 (no backend deps)
├── locustfile.py            # default entry point — composes the plan's production mix
├── config.py                # env-driven settings (host, auth, think time)
├── lib/
│   ├── auth.py              # bearer-token or cookie-login auth
│   ├── data.py              # workspace/datasource ID discovery + pool
│   └── slo.py               # post-run SLO check (standalone script; `--smoke` flag)
├── runners/
│   ├── views.py             # HttpUser wrapper around ViewsTasks (for `-f`)
│   ├── cached_stats.py      # HttpUser wrapper around CachedStatsTasks
│   └── announcements.py     # HttpUser wrapper around AnnouncementsTasks
└── scenarios/
    ├── views.py             # GET /views/ + /views/popular
    ├── workspaces.py        # GET /admin/workspaces/.../cached-stats
    └── announcements.py     # GET /announcements
```

## Install

```bash
cd loadtest
python -m venv .venv
.venv/bin/pip install -r requirements.txt
```

The harness has **one** dependency (Locust). It does not import anything from `backend/`.

## Configure

All knobs are env vars — no config files. The full set:

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `SYNODIC_HOST` | yes | `http://localhost:8000` | Backend base URL |
| `SYNODIC_BEARER_TOKEN` | one of | — | Pre-issued JWT/service token (sent as `Authorization: Bearer ...`) |
| `SYNODIC_USER` + `SYNODIC_PASSWORD` | one of | — | Cookie-login credentials (calls `/api/v1/auth/login`) |
| `SYNODIC_ID_POOL_LIMIT` | no | `50` | How many workspace IDs to discover per user |
| `SYNODIC_THINK_MIN` / `SYNODIC_THINK_MAX` | no | `0.5` / `2.5` | Per-task think time range (seconds) |
| `SYNODIC_FALLBACK_WS_ID` / `SYNODIC_FALLBACK_DS_ID` | no | — | Static ID list when admin discovery is denied (comma-separated) |
| `SYNODIC_LOG_FAILURES` | no | `false` | If `true`, log every non-2xx response (chatty) |

Auth picks bearer first, falls back to cookie login, and aborts if neither is set.

## Run

### Plan-mix workload (the production simulation)

```bash
.venv/bin/locust -f locustfile.py --headless \
    --users 200 --spawn-rate 20 --run-time 5m \
    --csv results/run --html results/run.html
```

- `--users` = peak concurrent VUs
- `--spawn-rate` = users/sec ramp-up
- `--run-time` = total duration
- `--csv` writes `<prefix>_stats.csv`, `<prefix>_stats_history.csv`, `<prefix>_failures.csv` — consumed by the SLO checker
- `--html` writes a self-contained report you can attach to a PR

### A single scenario (focused validation)

Each scenario module is self-contained — point Locust at it directly to drive one endpoint group only. Useful for validating a single workstream's success gate.

```bash
# Validate WS-1: hammer /views/?limit=20
.venv/bin/locust -f scenarios/views.py --headless \
    --users 200 --spawn-rate 50 --run-time 2m \
    --csv results/views

# Validate WS-2 / WS-7: hammer /cached-stats
.venv/bin/locust -f scenarios/workspaces.py --headless \
    --users 200 --spawn-rate 50 --run-time 2m \
    --csv results/cached_stats
```

Note: standalone scenario files don't include the `SynodicUser` class. To run a scenario solo, you can either (a) use the `locustfile.py` entry point and adjust the `MIXED_TASKS` dict, or (b) wrap the scenario in a thin `HttpUser` in a separate file. The plan-mix entry point is the common case; standalone wrappers are a 10-line copy of `locustfile.py` with `tasks = {ViewsTasks: 1}`.

### Web UI (interactive)

Omit `--headless` to get the Locust web UI at `http://localhost:8089`:

```bash
.venv/bin/locust -f locustfile.py
```

## Smoke runs

For a quick local check that every scenario actually drives traffic against the running backend, use the Makefile. Each target invokes Locust headlessly for a short run, then runs `lib.slo --smoke` to gate pass/fail with relaxed thresholds suitable for a dev backend.

```bash
# Bring up the backend yourself first (docker-compose, uvicorn, whatever),
# then set auth env vars in the same shell:
export SYNODIC_HOST=http://localhost:8000
export SYNODIC_USER=admin@synodic.local
export SYNODIC_PASSWORD=changeme

cd loadtest
make help        # list targets and current settings
make smoke       # run all four: announcements → views → cached-stats → mixed
```

Run a single scenario:

```bash
make smoke-announcements
make smoke-views
make smoke-cached-stats   # will surface as failed SLO if no workspaces are seeded
make smoke-mixed
```

Override defaults via env:

```bash
SMOKE_USERS=20 SMOKE_RUN_TIME=1m make smoke-mixed
SMOKE_HOST=http://localhost:8000 make smoke-views
PYTHON=.venv/bin/python LOCUST=.venv/bin/locust make smoke   # if using the venv
```

CSVs land under `results/smoke/<scenario>/` so you can inspect a run after the fact. `make smoke-clean` wipes the directory.

Smoke mode uses the relaxed `SMOKE_SLOS` from [lib/slo.py](lib/slo.py): per-endpoint `p95 < 1000–1500 ms`, aggregate failure rate `< 5%`, and `min_request_count = 1` (we just want to confirm the endpoint was actually hit). The full production SLOs in `DEFAULT_SLOS` are used by the unflagged `python -m lib.slo` invocation below.

### Troubleshooting: 429 / 500 from `/auth/login`

The backend rate-limits `POST /api/v1/auth/login` at 10 requests/minute (slowapi `@limiter.limit("10/minute")`). The harness compensates by doing **one shared login per Locust process** and copying the resulting cookies + CSRF header into every spawned user ([lib/auth.py](lib/auth.py)) — so a 200-user smoke run still only fires one login, well under the limit.

Separately, if you see HTTP 500 on `/auth/login` with `AttributeError: 'State' object has no attribute 'limiter'` in the backend traceback, that's a backend bug: slowapi's exception handler requires `app.state.limiter = limiter` to be registered during FastAPI startup. Fix it in `backend/app/main.py` near where the limiter is created — once that's wired the backend correctly returns 429 instead of 500.

### Troubleshooting: 401 on every admin call

The backend ships with `AUTH_COOKIE_SECURE=true` by default, so the `nx_access` JWT cookie is set with the `Secure` flag. Over plain `http://localhost:…`, Python's `requests` cookie jar stores those cookies but refuses to resend them, and every authenticated call 401s. The harness compensates by stripping the `Secure` flag after a successful cookie login on `http://` hosts ([lib/auth.py](lib/auth.py)) — you'll see an `INFO` line `Stripped Secure flag from N session cookie(s)…` when it kicks in. To avoid the workaround entirely, run the backend with `AUTH_COOKIE_SECURE=false` for local dev (see [backend/auth_service/cookies.py](../backend/auth_service/cookies.py)).

## Validate the run — SLO check

After a run, validate against the plan's thresholds:

```bash
.venv/bin/python -m lib.slo results/run_stats.csv
```

Exit code `0` = all SLOs met, `1` = at least one violation (with details on stderr).

Default SLOs (defined in [lib/slo.py](lib/slo.py)):

| Endpoint | p95 SLO | Notes |
|---|---|---|
| Aggregated (all) | < 500 ms | Plan's end-to-end gate |
| `views:list` | < 150 ms | WS-1 success gate (down from ~5s baseline) |
| `views:popular` | < 150 ms | Same N+1 fix |
| `cached-stats:get` | < 300 ms | WS-2 JSONB + ETag |
| `announcements:list` | < 100 ms | Trivial endpoint, no excuses |

Aggregate failure rate must be `< 0.1%` (no 5xx storm).

Edit `DEFAULT_SLOS` in `lib/slo.py` to tighten/relax targets for a specific run.

## Extending

To add a new endpoint scenario:

1. Create `scenarios/<thing>.py` with a `TaskSet` subclass and `@task`-decorated methods. Use stable `name=` kwargs on every `client.get/post` so stats group correctly.
2. Import it in `locustfile.py`, add to `MIXED_TASKS` with a weight.
3. Add an `SLO(name="thing:action", p95_ms_max=...)` entry in `lib/slo.py`.

Scenarios get the per-user `IdPool` via `self.user.id_pool` — call `pool.pick_workspace()` / `pool.pick_ws_ds()` for realistic targets.

## Scaling beyond one box

A single load-gen machine can sustain a few thousand Locust VUs. For more, run in distributed mode:

```bash
# Master
locust -f locustfile.py --master

# Workers (on each load-gen box, pointing at the master)
locust -f locustfile.py --worker --master-host=<master>
```

Aggregated stats are reported on the master. The plan calls for 2000 VUs — a single 4-core load-gen box handles that comfortably.

## What this harness deliberately does NOT do

- **No backend imports.** This is so the same harness runs against any deployed version, including ones that diverge from the current source tree.
- **No data seeding.** Use the backend's seed scripts (`backend/scripts/...`) or hit a staging clone of prod. Load tests should be repeatable, but the seed is the backend's responsibility.
- **No assertions during the run.** Locust runs to completion; SLO assertions happen post-run from the CSV. Keeps the request path tight and avoids per-request overhead.
- **No retries inside scenarios.** If the backend fails, that's the signal — we want it visible in the failure rate, not papered over.
