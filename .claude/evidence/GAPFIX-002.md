# GAPFIX-002 — detail-pane fields, and one card per company

EV-001 | repo state at start
CMD:   git rev-parse HEAD; git status --porcelain
OUT:   b18c93b... ; (no output: clean tree)
ESTABLISHES:      work starts from a clean tree at b18c93b
DOES NOT ESTABLISH: what is deployed

EV-002 | ENV=local | files read completely before modifying (G2.1)
CMD:   wc -l; sequential reads
OUT:   psc_index.py 503 (1-503); api_v2.py 1887 (1-950, 951-1887);
       static/app.html 3322 (1-1120, 1121-2240, 2241-3322)
ESTABLISHES:      full coverage, contiguous, no skipped sections
DOES NOT ESTABLISH: anything about admin_routes.py, which was not modified

EV-003 | ENV=local | the data was already on the wire
CMD:   read api_v2.v2_company
OUT:   line 962 "nationalities", line 972 "date_of_creation"
ESTABLISHES:      gap 1 is a rendering omission, not a missing fetch
DOES NOT ESTABLISH: that renderDetail ever used them (it did not)

EV-004 | ENV=local | RED before GREEN (G8.2)
CMD:   python3 test_detail_fields.py; test_pagination.py; test_add_candidates.py
OUT:   5 passed/6 failed; 23 passed/5 failed/2 skipped; 26 passed/2 failed
ESTABLISHES:      13 checks failed against the unfixed code
DOES NOT ESTABLISH: that they fail for the right reason - read per-check output

EV-005 | ENV=postgres16 | grouping against a real table
CMD:   DATABASE_URL=... python3 test_pagination.py
OUT:   50 passed / 0 failed / 0 skipped
ESTABLISHES:      7 records across 4 companies -> 3 current companies, one row
                  each; count_companies()=3 while count()=5; psc_count=3 for the
                  three-PSC company; a ceased PSC excluded from its company's
                  count; paging by company covers each exactly once
DOES NOT ESTABLISH: behaviour on the real 6,152-company table

EV-006 | ENV=browser | both gaps, rendered
CMD:   node /tmp/claude-0/repro/gaps.cjs   (3 PSC records across 2 companies)
OUT:   cards listed: 2 (one per company); cards for 14777001: 1
       "TAMU TRADERS 01 LTD ... PSC Mr Simon Mwangi; Ms Grace Wanjiku
        · 2 Kenyan PSCs"
       header badges: ["14777001","active","ltd","Reg. 2021-03-01","1/7"]
       Kenyan highlighted: yes ; page errors: none
ESTABLISHES:      both fixes work in a browser against a real database
DOES NOT ESTABLISH: the deployed server, which this container cannot reach
                    on an authenticated route

EV-007 | ENV=browser | a text assertion passed over an unreadable chip
CMD:   PIL crop of gap1-detail.png at (20,150,700,290)
OUT:   chip rendered "WANJIKU, Grace | K..."
ESTABLISHES:      .dir-chip max-width:140px truncated the nationality to one
                  letter while innerText reported it PRESENT. Raised to 260px
                  and re-verified visually: "WANJIKU, Grace | Kenyan", green.
DOES NOT ESTABLISH: behaviour at narrower viewports than 1400px

EV-008 | ENV=local+postgres16 | mutation run (G9)
OUT:   10 mutants applied. 9 killed on the first pass; M5 ("psc_count": 1)
       SURVIVED - an uncovered defect path, not an equivalent mutant: every
       check was on source text, so a hardcoded 1 was invisible. Added a
       behavioural check through the route; M5 and a second variant
       ("or 9") then killed. Restoration proven byte-identical for
       psc_index.py, api_v2.py and static/app.html.
DOES NOT ESTABLISH: that no other uncovered path remains

EV-009 | ENV=local | full suite
CMD:   make test
OUT:   1978 passed / 0 failed / 3 skipped
ESTABLISHES:      no regression across 27 hermetic files
DOES NOT ESTABLISH: the 3 skips - 2 in test_pagination.py and 1 in
                    test_psc_index.py are DB-backed; the pagination pair was
                    run separately against real PostgreSQL (EV-005)
