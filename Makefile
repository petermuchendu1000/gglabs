# Company pipeline — the two commands worth having.
#
#   make test   the hermetic suite: no network, no database, no registrar
#   make e2e    the real app in a real browser, screenshotted
#
# Neither ever places an order. `make e2e` doubles the HTTP boundary to
# Namecheap, Companies House and D&B; see e2e/app_under_test.py.

PYTHON ?= python3

# Every test that runs without a network, a database or a provider account.
# Deliberately NOT the whole test_*.py set: test_ibenty_live.py and
# test_ibenty_migration.py drive a real account, test_duns_api.py and
# test_intercept.py reach D&B, and test_fpp.py / test_stealth.py drive a
# browser against live sites. Those stay opt-in, by name.
HERMETIC_TESTS := \
	test_autofill.py \
	test_auto_recovery.py \
	test_build_source.py \
	test_check_stage.py \
	test_cloudflare_dns.py \
	test_deploy_scripts.py \
	test_email_stage.py \
	test_flavor_regen.py \
	test_generate_portability.py \
	test_gradle_wrapper.py \
	test_ibenty_rename.py \
	test_kenyan_filter.py \
	test_match_stage.py \
	test_no_committed_secrets.py \
	test_row_completeness.py \
	test_stage_feedback.py

.PHONY: help test e2e

help:
	@echo "make test   run the hermetic suite ($(words $(HERMETIC_TESTS)) files)"
	@echo "make e2e    run the real app in a real browser and screenshot it"
	@echo ""
	@echo "e2e uses \$$DATABASE_URL if set - it must be a THROWAWAY database,"
	@echo "because the run truncates the companies table. With none set it"
	@echo "starts a temporary PostgreSQL and removes it afterwards."

test:
	@fail=0; \
	for t in $(HERMETIC_TESTS); do \
		printf '%-34s' "$$t"; \
		if out=$$($(PYTHON) $$t 2>&1); then \
			echo "$$out" | grep -E 'passed /' | tail -1; \
		else \
			fail=1; echo "FAILED"; echo "$$out" | tail -25; \
		fi; \
	done; \
	exit $$fail

e2e:
	$(PYTHON) e2e/run_e2e.py
