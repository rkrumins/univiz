# Loadtest smoke runner.
#
# Targets wrap the Locust CLI with sensible defaults so a one-line
# `make smoke` runs every scenario against a locally-running backend
# and gates on the relaxed smoke SLOs in lib/slo.py.
#
# All knobs are env-overridable:
#   SMOKE_HOST        backend base URL                 (default $SYNODIC_HOST or http://localhost:8000)
#   SMOKE_USERS       concurrent Locust users          (default 10)
#   SMOKE_SPAWN_RATE  user spawn rate (users/sec)      (default 10)
#   SMOKE_RUN_TIME    Locust --run-time value          (default 30s)
#   PYTHON            python interpreter               (default python3)
#   LOCUST            locust binary                    (default locust)
#
# Auth env vars are NOT defaulted here — set SYNODIC_BEARER_TOKEN or
# SYNODIC_USER + SYNODIC_PASSWORD in the calling shell (see README).

SMOKE_HOST       ?= $(if $(SYNODIC_HOST),$(SYNODIC_HOST),http://localhost:8000)
SMOKE_USERS      ?= 10
SMOKE_SPAWN_RATE ?= 10
SMOKE_RUN_TIME   ?= 30s
PYTHON           ?= python3
LOCUST           ?= locust

SMOKE_DIR := results/smoke

LOCUST_ARGS = --headless \
              --host $(SMOKE_HOST) \
              --users $(SMOKE_USERS) \
              --spawn-rate $(SMOKE_SPAWN_RATE) \
              --run-time $(SMOKE_RUN_TIME) \
              --only-summary

.PHONY: help smoke smoke-announcements smoke-views smoke-cached-stats smoke-mixed smoke-clean

help:
	@echo "Loadtest smoke targets:"
	@echo "  make smoke                 — run all scenarios + mixed traffic, gate on smoke SLOs"
	@echo "  make smoke-announcements   — only the announcements scenario"
	@echo "  make smoke-views           — only the views scenario"
	@echo "  make smoke-cached-stats    — only the cached-stats scenario (needs seeded workspaces)"
	@echo "  make smoke-mixed           — production traffic mix from locustfile.py"
	@echo "  make smoke-clean           — remove $(SMOKE_DIR)/"
	@echo ""
	@echo "Settings (override via env):"
	@echo "  SMOKE_HOST=$(SMOKE_HOST)"
	@echo "  SMOKE_USERS=$(SMOKE_USERS)  SMOKE_SPAWN_RATE=$(SMOKE_SPAWN_RATE)  SMOKE_RUN_TIME=$(SMOKE_RUN_TIME)"
	@echo "  PYTHON=$(PYTHON)  LOCUST=$(LOCUST)"
	@echo ""
	@echo "Required env: SYNODIC_BEARER_TOKEN, or SYNODIC_USER + SYNODIC_PASSWORD."

# Cheapest scenario first so misconfiguration surfaces quickly.
smoke: smoke-announcements smoke-views smoke-cached-stats smoke-mixed
	@echo ""
	@echo "All smoke scenarios passed."

smoke-announcements:
	@mkdir -p $(SMOKE_DIR)/announcements
	$(LOCUST) -f runners/announcements.py $(LOCUST_ARGS) --csv $(SMOKE_DIR)/announcements/run
	$(PYTHON) -m lib.slo --smoke $(SMOKE_DIR)/announcements/run_stats.csv

smoke-views:
	@mkdir -p $(SMOKE_DIR)/views
	$(LOCUST) -f runners/views.py $(LOCUST_ARGS) --csv $(SMOKE_DIR)/views/run
	$(PYTHON) -m lib.slo --smoke $(SMOKE_DIR)/views/run_stats.csv

smoke-cached-stats:
	@mkdir -p $(SMOKE_DIR)/cached_stats
	$(LOCUST) -f runners/cached_stats.py $(LOCUST_ARGS) --csv $(SMOKE_DIR)/cached_stats/run
	$(PYTHON) -m lib.slo --smoke $(SMOKE_DIR)/cached_stats/run_stats.csv

smoke-mixed:
	@mkdir -p $(SMOKE_DIR)/mixed
	$(LOCUST) -f locustfile.py $(LOCUST_ARGS) --csv $(SMOKE_DIR)/mixed/run
	$(PYTHON) -m lib.slo --smoke $(SMOKE_DIR)/mixed/run_stats.csv

smoke-clean:
	rm -rf $(SMOKE_DIR)
