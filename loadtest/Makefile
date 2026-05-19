# Loadtest runner.
#
# Three suites:
#   * smoke   — short per-scenario verification at low concurrency
#               (`make smoke`, gates on the relaxed SMOKE_SLOS)
#   * sweep   — concurrency tiers over the production mix, including
#               graph queries (`make sweep`, gates on TIER_SLOS)
#   * stress  — single graph-query scenarios at sweep tiers, isolating
#               trace / walks / children individually (`make stress-*`)
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
# Stress knobs (re-use SWEEP_* by default — single-endpoint sweep):
#   STRESS_TIERS      space-separated user counts      (default $(SWEEP_TIERS))
#   STRESS_RUN_TIME   per-tier --run-time              (default $(SWEEP_RUN_TIME))
#   STRESS_SPAWN_RATE users/sec ramp                   (default $(SWEEP_SPAWN_RATE))
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

STRESS_TIERS      ?= $(SWEEP_TIERS)
STRESS_RUN_TIME   ?= $(SWEEP_RUN_TIME)
STRESS_SPAWN_RATE ?= $(SWEEP_SPAWN_RATE)

PYTHON           ?= python3
LOCUST           ?= locust

SMOKE_DIR  := results/smoke
SWEEP_DIR  := results/sweep
STRESS_DIR := results/stress

SMOKE_LOCUST_ARGS = --headless \
                    --host $(SMOKE_HOST) \
                    --users $(SMOKE_USERS) \
                    --spawn-rate $(SMOKE_SPAWN_RATE) \
                    --run-time $(SMOKE_RUN_TIME) \
                    --only-summary

.PHONY: help \
        smoke smoke-announcements smoke-views smoke-cached-stats \
        smoke-aggregation-jobs smoke-graph-schema \
        smoke-graph-lineage smoke-graph-walks smoke-graph-children \
        smoke-mixed smoke-clean \
        sweep sweep-clean \
        stress stress-trace stress-walks stress-children stress-clean

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
	@echo "    make smoke-graph-lineage       — Tier-1 heavy: trace/v2,    needs seeded graph"
	@echo "    make smoke-graph-walks         — Tier-1 heavy: ancestors/descendants"
	@echo "    make smoke-graph-children      — Tier-1 heavy: children/+edges"
	@echo "    make smoke-mixed"
	@echo "    make smoke-clean               — remove $(SMOKE_DIR)/"
	@echo ""
	@echo "  Sweep (concurrency tiers over the production mix, $(SWEEP_RUN_TIME)/tier):"
	@echo "    make sweep                     — run all tiers, gate each on TIER_SLOS"
	@echo "    make sweep-clean               — remove $(SWEEP_DIR)/"
	@echo "    Override tiers: SWEEP_TIERS='10 50 200' make sweep"
	@echo ""
	@echo "  Stress (per-graph-endpoint concurrency sweep — isolates one query type):"
	@echo "    make stress                    — trace + walks + children at all tiers"
	@echo "    make stress-trace              — POST /graph/trace/v2 at all tiers"
	@echo "    make stress-walks              — ancestors + descendants at all tiers"
	@echo "    make stress-children           — children + children-with-edges at all tiers"
	@echo "    make stress-clean              — remove $(STRESS_DIR)/"
	@echo ""
	@echo "  Settings:"
	@echo "    SMOKE_HOST=$(SMOKE_HOST)"
	@echo "    SMOKE_USERS=$(SMOKE_USERS)  SMOKE_SPAWN_RATE=$(SMOKE_SPAWN_RATE)  SMOKE_RUN_TIME=$(SMOKE_RUN_TIME)"
	@echo "    SWEEP_TIERS='$(SWEEP_TIERS)'  SWEEP_RUN_TIME=$(SWEEP_RUN_TIME)  SWEEP_SPAWN_RATE=$(SWEEP_SPAWN_RATE)"
	@echo "    STRESS_TIERS='$(STRESS_TIERS)'  STRESS_RUN_TIME=$(STRESS_RUN_TIME)  STRESS_SPAWN_RATE=$(STRESS_SPAWN_RATE)"
	@echo "    PYTHON=$(PYTHON)  LOCUST=$(LOCUST)"
	@echo ""
	@echo "  Required env: SYNODIC_BEARER_TOKEN, or SYNODIC_USER + SYNODIC_PASSWORD."

# ── Smoke targets ─────────────────────────────────────────────────────

# Cheapest scenario first so misconfiguration surfaces quickly.
smoke: smoke-announcements smoke-views smoke-cached-stats \
       smoke-aggregation-jobs smoke-graph-schema \
       smoke-graph-lineage smoke-graph-walks smoke-graph-children \
       smoke-mixed
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

smoke-graph-lineage:
	@mkdir -p $(SMOKE_DIR)/graph_lineage
	$(LOCUST) -f runners/graph_lineage.py $(SMOKE_LOCUST_ARGS) --csv $(SMOKE_DIR)/graph_lineage/run
	$(PYTHON) -m lib.slo --smoke $(SMOKE_DIR)/graph_lineage/run_stats.csv

smoke-graph-walks:
	@mkdir -p $(SMOKE_DIR)/graph_walks
	$(LOCUST) -f runners/graph_walks.py $(SMOKE_LOCUST_ARGS) --csv $(SMOKE_DIR)/graph_walks/run
	$(PYTHON) -m lib.slo --smoke $(SMOKE_DIR)/graph_walks/run_stats.csv

smoke-graph-children:
	@mkdir -p $(SMOKE_DIR)/graph_children
	$(LOCUST) -f runners/graph_children.py $(SMOKE_LOCUST_ARGS) --csv $(SMOKE_DIR)/graph_children/run
	$(PYTHON) -m lib.slo --smoke $(SMOKE_DIR)/graph_children/run_stats.csv

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

# ── Stress targets ────────────────────────────────────────────────────
#
# Per-graph-endpoint concurrency sweep. Each target walks the same
# tier list as `make sweep` but points Locust at a single runner so the
# resulting CSVs isolate one query type's behavior at scale. Use these
# when the mixed sweep shows trouble and you want to know whether the
# culprit is trace orchestration, raw traversal, or child enumeration.

stress: stress-trace stress-walks stress-children
	@echo ""
	@echo "All graph stress scenarios complete."

stress-trace:
	@mkdir -p $(STRESS_DIR)/trace
	@set -e; \
	for tier in $(STRESS_TIERS); do \
	    echo ""; echo "=== stress-trace tier: $$tier users ==="; \
	    mkdir -p $(STRESS_DIR)/trace/tier_$$tier; \
	    $(LOCUST) -f runners/graph_lineage.py \
	        --headless --host $(SMOKE_HOST) \
	        --users $$tier --spawn-rate $(STRESS_SPAWN_RATE) \
	        --run-time $(STRESS_RUN_TIME) --only-summary \
	        --csv $(STRESS_DIR)/trace/tier_$$tier/run; \
	    $(PYTHON) -m lib.slo --tier $$tier $(STRESS_DIR)/trace/tier_$$tier/run_stats.csv; \
	done

stress-walks:
	@mkdir -p $(STRESS_DIR)/walks
	@set -e; \
	for tier in $(STRESS_TIERS); do \
	    echo ""; echo "=== stress-walks tier: $$tier users ==="; \
	    mkdir -p $(STRESS_DIR)/walks/tier_$$tier; \
	    $(LOCUST) -f runners/graph_walks.py \
	        --headless --host $(SMOKE_HOST) \
	        --users $$tier --spawn-rate $(STRESS_SPAWN_RATE) \
	        --run-time $(STRESS_RUN_TIME) --only-summary \
	        --csv $(STRESS_DIR)/walks/tier_$$tier/run; \
	    $(PYTHON) -m lib.slo --tier $$tier $(STRESS_DIR)/walks/tier_$$tier/run_stats.csv; \
	done

stress-children:
	@mkdir -p $(STRESS_DIR)/children
	@set -e; \
	for tier in $(STRESS_TIERS); do \
	    echo ""; echo "=== stress-children tier: $$tier users ==="; \
	    mkdir -p $(STRESS_DIR)/children/tier_$$tier; \
	    $(LOCUST) -f runners/graph_children.py \
	        --headless --host $(SMOKE_HOST) \
	        --users $$tier --spawn-rate $(STRESS_SPAWN_RATE) \
	        --run-time $(STRESS_RUN_TIME) --only-summary \
	        --csv $(STRESS_DIR)/children/tier_$$tier/run; \
	    $(PYTHON) -m lib.slo --tier $$tier $(STRESS_DIR)/children/tier_$$tier/run_stats.csv; \
	done

stress-clean:
	rm -rf $(STRESS_DIR)
