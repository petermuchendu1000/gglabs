# AUTOFILL-001 — does selecting a company auto-fill the pipeline fields?

EV-001 | repo state at start
CMD:   git rev-parse HEAD; git status --porcelain
EXIT:  0
OUT:   cfc859c7c7b0b19698298e159bf465d59511f989
       (git status --porcelain produced no output: clean tree)
ESTABLISHES:      investigation runs against cfc859c with no uncommitted changes
DOES NOT ESTABLISH: what is deployed on the server

EV-002 | ENV=local | SHA=cfc859c | the add route calls an autofill
CMD:   sed -n '1670,1723p' api_v2.py
ESTABLISHES:      v2_add_company calls _autofill_company(cn) after _add_company_row
                  and returns autofilled[] / autofill_error
DOES NOT ESTABLISH: which columns actually get written, or that it runs

EV-003 | ENV=local | the mapping, read in full
CMD:   sed -n '448,506p' run_pipeline.py   (company_row_updates)
ESTABLISHES:      10 columns are derivable from Companies House: Company Name,
                  Status, Type, Date of Creation, SIC Codes, Address, Directors,
                  Director Nationalities, Director DOB, Director Gender
DOES NOT ESTABLISH: that all 10 arrive in the stored row

EV-004 | ENV=local+postgres16 | the real route, against a real database
CMD:   curl -X POST /api/v2/company/add -d '{"cn":"14123456","name":""}'
EXIT:  0
OUT:   {"autofilled":["Address","Company Name","Date of Creation","Director DOB",
        "Director Gender","Director Nationalities","Directors","SIC Codes",
        "Status","Type"],"name":"TAMU TRADERS 56 LTD","status":"added"}
ESTABLISHES:      the route reports 10 columns filled
DOES NOT ESTABLISH: that the database agrees - this is the route describing itself

EV-005 | ENV=postgres16 | the stored row, read independently of the route
CMD:   SELECT * FROM companies WHERE company_number='14123456'
ESTABLISHES:      13 of 37 columns populated: the 10 autofilled + company_number,
                  row_no, updated_at. 24 empty.
DOES NOT ESTABLISH: what the operator sees on screen

EV-006 | ENV=browser | the Edit-fields form after clicking a company in the dialog
CMD:   node /tmp/claude-0/repro/autofill3.cjs
OUT:   EDIT FORM: 29 inputs, 10 pre-filled
       ef-name, ef-status, ef-type, ef-date_of_creation, ef-sic, ef-address,
       ef-directors, ef-nationalities, ef-genders, ef-dobs
ESTABLISHES:      clicking a candidate pre-fills every field Companies House can supply
DOES NOT ESTABLISH: anything about the 17 stage-output fields, which are empty by design

EV-007 | ENV=browser | the summary pane omits two stored values
CMD:   (await p.locator('#detail').innerText()) - 1348 chars, searched
OUT:   "Kenyan" present: false ; "2021" present: false
ESTABLISHES:      nationality and date of registration are STORED (EV-005) and
                  pre-filled in Edit fields (EV-006) but are not rendered on the
                  detail pane
DOES NOT ESTABLISH: whether that omission was intended

EV-008 | ENV=local | no consumer for the Buvei columns
CMD:   grep -rn 'Buvei|buvei' --include=* . | grep -v .git/
OUT:   only static/app.html, api_v2.py, db.py (+ .pyc). No stage reads them.
ESTABLISHES:      the two unfilled Director-group fields drive nothing
DOES NOT ESTABLISH: whether they are meant to be retired

EV-009 | ENV=browser | one company, two cards
OUT:   candidates listed: 3
         - TAMU TRADERS 56 LTD 14123456 ... in progress 1/8
         - TAMU TRADERS 56 LTD 14123456 ... in progress 1/8
         - TAMU TRADERS 01 LTD 14777001 ... not started
ESTABLISHES:      the dialog lists one card per PSC RECORD, so a company with two
                  Kenyan PSCs appears twice
DOES NOT ESTABLISH: how many real companies this affects

EV-010 | ENV=local | _replace does not duplicate on a same-day re-run
CMD:   sed -n on psc_index._replace ; grep 'PRIMARY KEY|ON CONFLICT'
OUT:   PRIMARY KEY (company_number, etag); INSERT ... ON CONFLICT
       (company_number, etag) DO UPDATE SET ...
ESTABLISHES:      a re-fetch of the same snapshot upserts; the 3 rows in EV-009 came
                  from MY seed using two different etags for one company, not a defect
DOES NOT ESTABLISH: behaviour if Companies House reissues a record with a new etag
                    within one day
