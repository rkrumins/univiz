# Loadtest runner.
#
# Two suites:
#   * smoke   — short per-scenario verification at low concurrency
#               (`make smoke`, gates on the relaxed SMOKE_SLOS)
#   * sweep   — concurrency tiers over the production mix
#               (`make sweep`, gates on TIER_SLOS in lib/slo.py)
#
# Smoke knobs:
#   SMOKE_HOST        backend base URL                 (default $SYNODIC_HOST or http://localhost:8000)
#   SMOKE_USERS       concurrent Locust users          (default 10)
#   SMOKE_SPAWN_RATE  user spawn rate (users/sec)      (default 10)
#   SMOKE_RUN_TIME    Locust --run-time value          (default 30s)
#
# Sweep knobs:
#   SWEEP_TIERS       space-separated user counts      (default "10 100 500 1000")
#   SWEEP_RUN_TIME    per-tier --run-time              (default 60s)
#   SWEEP_SPAWN_RATE  users/sec ramp                   (default 100 — ~10s ramp at 1000 users)
#
# Common knobs:
#   PYTHON            python interpreter               (default python3)
#   LOCUST            locust binary                    (default locust)
#
# Auth env vars are NOT defaulted here — set SYNODIC_BEARER_TOKEN or
# SYNODIC_USER + SYNODIC_PASSWORD in the calling shell (see README).

SMOKE_HOST       ?= $(if $(SYNODIC_HOST),$(SYNODIC_HOST),http://localhost:8000)
SMOKE_USERS      ?= 10
SMOKE_SPAWN_RATE ?= 10
SMOKE_RUN_TIME   ?= 30s

SWEEP_TIERS      ?= 10 100 500 1000
SWEEP_RUN_TIME   ?= 60s
SWEEP_SPAWN_RATE ?= 100

PYTHON           ?= python3
LOCUST           ?= locust

SMOKE_DIR := results/smoke
SWEEP_DIR := results/sweep

SMOKE_LOCUST_ARGS = --headless \
                    --host $(SMOKE_HOST) \
                    --users $(SMOKE_USERS) \
                    --spawn-rate $(SMOKE_SPAWN_RATE) \
                    --run-time $(SMOKE_RUN_TIME) \
                    --only-summary

.PHONY: help \
        smoke smoke-announcements smoke-views smoke-cached-stats \
        smoke-aggregation-jobs smoke-graph-schema smoke-mixed smoke-clean \
        sweep sweep-clean

help:
	@echo "Loadtest targets:"
	@echo ""
	@echo "  Smoke (short per-scenario verification at $(SMOKE_USERS) users):"
	@echo "    make smoke                     — all scenarios + mixed, gate on smoke SLOs"
	@echo "    make smoke-announcements"
	@echo "    make smoke-views"
	@echo "    make smoke-cached-stats        — needs seeded workspaces"
	@echo "    make smoke-aggregation-jobs    — Tier-2 heavy"
	@echo "    make smoke-graph-schema        — Tier-2 heavy, needs workspaces"
	@echo "    make smoke-mixed"
	@echo "    make smoke-clean               — remove $(SMOKE_DIR)/"
	@echo ""
	@echo "  Sweep (concurrency tiers over the production mix, $(SWEEP_RUN_TIME)/tier):"
	@echo "    make sweep                     — run all tiers, gate each on TIER_SLOS"
	@echo "    make sweep-clean               — remove $(SWEEP_DIR)/"
	@echo "    Override tiers: SWEEP_TIERS='10 50 200' make sweep"
	@echo ""
	@echo "  Settings:"
	@echo "    SMOKE_HOST=$(SMOKE_HOST)"
	@echo "    SMOKE_USERS=$(SMOKE_USERS)  SMOKE_SPAWN_RATE=$(SMOKE_SPAWN_RATE)  SMOKE_RUN_TIME=$(SMOKE_RUN_TIME)"
	@echo "    SWEEP_TIERS='$(SWEEP_TIERS)'  SWEEP_RUN_TIME=$(SWEEP_RUN_TIME)  SWEEP_SPAWN_RATE=$(SWEEP_SPAWN_RATE)"
	@echo "    PYTHON=$(PYTHON)  LOCUST=$(LOCUST)"
	@echo ""
	@echo "  Required env: SYNODIC_BEARER_TOKEN, or SYNODIC_USER + SYNODIC_PASSWORD."

# ── Smoke targets ─────────────────────────────────────────────────────

# Cheapest scenario first so misconfiguration surfaces quickly.
smoke: smoke-announcements smoke-views smoke-cached-stats smoke-aggregation-jobs smoke-graph-schema smoke-mixed
	@echo ""
	@echo "All smoke scenarios passed."

smoke-announcements:
	@mkdir -p $(SMOKE_DIR)/announcements
	$(LOCUST) -f runners/announcements.py $(SMOKE_LOCUST_ARGS) --csv $(SMOKE_DIR)/announcements/run
	$(PYTHON) -m lib.slo --smoke $(SMOKE_DIR)/announcements/run_stats.csv

smoke-views:
	@mkdir -p $(SMOKE_DIR)/views
	$(LOCUST) -f runners/views.py $(SMOKE_LOCUST_ARGS) --csv $(SMOKE_DIR)/views/run
	$(PYTHON) -m lib.slo --smoke $(SMOKE_DIR)/views/run_stats.csv

smoke-cached-stats:
	@mkdir -p $(SMOKE_DIR)/cached_stats
	$(LOCUST) -f runners/cached_stats.py $(SMOKE_LOCUST_ARGS) --csv $(SMOKE_DIR)/cached_stats/run
	$(PYTHON) -m lib.slo --smoke $(SMOKE_DIR)/cached_stats/run_stats.csv

smoke-aggregation-jobs:
	@mkdir -p $(SMOKE_DIR)/aggregation_jobs
	$(LOCUST) -f runners/aggregation_jobs.py $(SMOKE_LOCUST_ARGS) --csv $(SMOKE_DIR)/aggregation_jobs/run
	$(PYTHON) -m lib.slo --smoke $(SMOKE_DIR)/aggregation_jobs/run_stats.csv

smoke-graph-schema:
	@mkdir -p $(SMOKE_DIR)/graph_schema
	$(LOCUST) -f runners/graph_schema.py $(SMOKE_LOCUST_ARGS) --csv $(SMOKE_DIR)/graph_schema/run
	$(PYTHON) -m lib.slo --smoke $(SMOKE_DIR)/graph_schema/run_stats.csv

smoke-mixed:
	@mkdir -p $(SMOKE_DIR)/mixed
	$(LOCUST) -f locustfile.py $(SMOKE_LOCUST_ARGS) --csv $(SMOKE_DIR)/mixed/run
	$(PYTHON) -m lib.slo --smoke $(SMOKE_DIR)/mixed/run_stats.csv

smoke-clean:
	rm -rf $(SMOKE_DIR)

# ── Sweep target ──────────────────────────────────────────────────────
#
# Sequential per-tier sub-makes via $(MAKE) so a single tier failure
# stops the sweep (the slo check exits non-zero on violations). Each
# tier writes its own CSVs under $(SWEEP_DIR)/tier_<N>/.

sweep:
	@mkdir -p $(SWEEP_DIR)
	@set -e; \
	for tier in $(SWEEP_TIERS); do \
	    echo ""; echo "=== Sweep tier: $$tier users (run_time=$(SWEEP_RUN_TIME)) ==="; \
	    mkdir -p $(SWEEP_DIR)/tier_$$tier; \
	    $(LOCUST) -f locustfile.py \
	        --headless --host $(SMOKE_HOST) \
	        --users $$tier --spawn-rate $(SWEEP_SPAWN_RATE) \
	        --run-time $(SWEEP_RUN_TIME) --only-summary \
	        --csv $(SWEEP_DIR)/tier_$$tier/run; \
	    $(PYTHON) -m lib.slo --tier $$tier $(SWEEP_DIR)/tier_$$tier/run_stats.csv; \
	done
	@echo ""
	@echo "Sweep complete. CSVs under $(SWEEP_DIR)/."

sweep-clean:
	rm -rf $(SWEEP_DIR)
