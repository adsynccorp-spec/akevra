# AKEVRA Compliance Rules and Calculation Test Cases — For Approval

Prepared for AKEVRA review. Approving this document satisfies the Monthly Work & Hours milestone requirement that supervision-percentage and cumulative-hours calculations are validated against a test-case set reviewed and approved by AKEVRA.

All values below come from the BACB handbooks (BCBA, BCaBA and RBT Handbooks, updated 06/2026). Every value is configurable without a code change; once approved, it is recorded with `python manage.py approve_compliance_rules`.

## 1. Decisions to confirm

- **Rule values:** BACB 06/2026 handbook values per pathway (Section 2).
- **Months that miss a requirement:** BACB adjustment table: no client observation or under 20 h → no hours; over the maximum → independent hours removed to the maximum; supervision % short → independent hours removed until met; contacts short → hours prorated by contacts held; group above individual → group hours reduced. Concentrated months are never adjusted (no hours if any requirement is missed).
- **Mixed supervised / concentrated fieldwork:** Concentrated hours × 1.33 plus supervised hours must reach 2,000. With one type only, its own total applies (2,000 supervised or 1,500 concentrated).
- **Month with 0 service hours (RBT / BCaBA):** No supervision is required that month; it is treated as compliant (BCaBA Handbook: exempt when no services are delivered).
- **2027 BACB changes:** RBT and BCaBA ongoing supervision use the 2027 rules for months from January 2027. Supervised Fieldwork defaults to the same by-month switch, and each relationship can be pinned to the 2022 or 2027 standard, because BACB applies the fieldwork standard by application date.
- **BCaBA 1,000-hour step (5% → 2%):** Counts attested service hours in the system plus a per-relationship 'prior post-certification hours' figure the Supervisor enters.
- **60% unrestricted activities (fieldwork):** Each fieldwork entry is marked restricted or unrestricted; the cumulative record shows the unrestricted share and fieldwork is not complete until it reaches 60%.
- **Qualifying supervision:** Only completed, real-time sessions (in person or live video) count. Drafts, phone calls, asynchronous reviews and appointments never count.

## 2. Rule values

### Rbt Ongoing Supervision

| From | Parameters |
|---|---|
| Current | min_supervision_percent = 5; min_contacts = 2; min_individual_contacts = 1; min_observation_contacts = 1 |

### Supervised Fieldwork

| From | Parameters |
|---|---|
| Current | supervised.min_supervision_percent = 5; supervised.min_contacts = 4; supervised.total_required_hours = 2000; concentrated.min_supervision_percent = 10; concentrated.min_contacts = 6; concentrated.total_required_hours = 1500; concentrated_multiplier = 1.33; min_observation_contacts = 1; max_group_percent = 50; min_monthly_hours = 20; max_monthly_hours = 130; min_unrestricted_percent = 60 |
| 2027-01-01 | supervised.min_supervision_percent = 5; supervised.min_contacts = 0; supervised.total_required_hours = 2000; concentrated.min_supervision_percent = 7.5; concentrated.min_contacts = 0; concentrated.total_required_hours = 1500; concentrated_multiplier = 1.33; min_observation_contacts = 1; max_group_percent = 50; min_monthly_hours = 20; max_monthly_hours = 160; min_unrestricted_percent = 60 |

### Bcaba Ongoing Supervision

| From | Parameters |
|---|---|
| Current | min_supervision_percent = 5; reduced_supervision_percent = 2; reduced_after_service_hours = 1000; biweekly_min_minutes = 60; min_contacts = 1; max_group_percent = 50; quarterly_observations = 1 |
| 2027-01-01 | min_supervision_percent = 5; min_contacts = 1; quarterly_observations = 1 |

## 3. RBT ongoing supervision cases

| Case | Service h | Supervision min | Contacts | Individual | Observation | Expected |
|---|---|---|---|---|---|---|
| R1 exactly 5% | 100 | 300 | 2 | 1 | 1 | status = met; supervision_percent = 5.0; remaining_supervision_hours = 0.0 |
| R2 4% short by 1 h | 100 | 240 | 2 | 1 | 1 | status = shortfall; supervision_percent = 4.0; remaining_supervision_hours = 1.0 |
| R3 enough time, one contact | 160 | 480 | 1 | 1 | 1 | status = shortfall; supervision_percent = 5.0 |
| R4 no individual contact | 100 | 300 | 2 | 0 | 1 | status = shortfall |
| R5 no observation | 100 | 300 | 2 | 1 | 0 | status = shortfall |
| R6 boundary 130 h / 390 min | 130 | 390 | 2 | 1 | 1 | status = met; supervision_percent = 5.0; required_supervision_hours = 6.5 |
| R7 not yet attested | not attested | 120 | 2 | 1 | 1 | status = awaiting_service_hours; met = False |
| R8 no services this month | 0 | 0 | 0 | 0 | 0 | status = no_service_hours; met = True |

## 4. Supervised Fieldwork — monthly cases

| Case | Rules | Type | Total h | Supervision h | Group h | Contacts | Observation | Expected |
|---|---|---|---|---|---|---|---|---|
| F1 supervised 5% | 2026 | supervised | 100 | 5 | 2 | 4 | 1 | status = met; supervision_percent = 5.0; eligible_hours = 100.0 |
| F2 supervised 4.99%: independent trimmed | 2026 | supervised | 100 | 4.99 | 0 | 4 | 1 | status = shortfall; eligible_hours = 99.8 |
| F3 concentrated 10% | 2026 | concentrated | 100 | 10 | 0 | 6 | 1 | status = met; eligible_hours = 100.0 |
| F4 concentrated 8%: no adjusting | 2026 | concentrated | 100 | 8 | 0 | 6 | 1 | status = shortfall; eligible_hours = 0.0; remaining_supervision_hours = 2.0 |
| F5 concentrated 5 contacts | 2026 | concentrated | 100 | 10 | 0 | 5 | 1 | status = shortfall; eligible_hours = 0.0 |
| F6 under 20 h | 2026 | supervised | 15 | 1 | 0 | 4 | 1 | status = shortfall; eligible_hours = 0.0 |
| F7 over 130 h: trimmed to 130 | 2026 | supervised | 140 | 7 | 0 | 4 | 1 | status = shortfall; eligible_hours = 130.0 |
| F8 group over 50%: group cut, then % | 2026 | supervised | 100 | 5 | 3 | 4 | 1 | status = shortfall; eligible_hours = 80.0 |
| F9 group exactly 50% | 2026 | supervised | 100 | 5 | 2.5 | 4 | 1 | status = met; eligible_hours = 100.0 |
| F10 no observation | 2026 | supervised | 100 | 5 | 0 | 4 | 0 | status = shortfall; eligible_hours = 0.0 |
| F11 BACB example: 2 of 4 contacts | 2026 | supervised | 110 | 5.5 | 0 | 2 | 1 | status = shortfall; eligible_hours = 55.0 |
| F12 2027 concentrated 7.5% | 2027 | concentrated | 100 | 7.5 | 0 | 0 | 1 | status = met; eligible_hours = 100.0 |
| F13 2027 up to 160 h | 2027 | supervised | 150 | 7.5 | 0 | 0 | 1 | status = met; eligible_hours = 150.0 |

## 5. Supervised Fieldwork — cumulative (incl. mixed)

| Case | Eligible hours by type | Expected |
|---|---|---|
| C1 supervised only | 2000 supervised | percent_complete = 100.0; complete = True |
| C2 concentrated only | 1500 concentrated | percent_complete = 100.0; complete = True |
| C3 mixed: 1000 + 750 x 1.33 falls short | 1000 supervised, 750 concentrated | combined_hours = 1997.5; complete = False; remaining_if_supervised = 2.5 |
| C4 mixed partial | 1000 supervised, 300 concentrated | combined_hours = 1399.0; percent_complete = 69.95; remaining_if_supervised = 601.0; remaining_if_concentrated = 451.88 |
| C5 over-accrued caps at 100 | 2100 supervised | percent_complete = 100.0 |
| C6 nothing yet | none | percent_complete = 0.0; remaining_if_supervised = 2000.0 |
| C7 mixed complete | 1000 supervised, 752 concentrated | complete = True |

## 6. Unrestricted activities (60%)

| Case | Accrued h | Unrestricted h | Expected |
|---|---|---|---|
| U1 exactly 60% unrestricted | 2000 | 1200 | unrestricted_met = True; complete = True |
| U2 50% unrestricted blocks completion | 2000 | 1000 | unrestricted_met = False; complete = False; unrestricted_percent = 50.0 |
| U3 nothing logged yet | 0 | 0 | unrestricted_met = True; unrestricted_percent = None |

## 7. BCaBA ongoing supervision cases

| Case | Rules | Inputs | Expected |
|---|---|---|---|
| B1 first 1,000 h at 5% | 2026 | service_hours = 100; supervision_minutes = 300; contacts = 1; first_half_minutes = 150; second_half_minutes = 150; quarter_observations = 1 | status = met; required_supervision_percent = 5.0 |
| B2 after 1,000 h drops to 2% | 2026 | service_hours = 100; supervision_minutes = 120; contacts = 1; prior_service_hours = 1200; quarter_observations = 1 | status = met; required_supervision_percent = 2.0 |
| B3 5% tier, nothing in second half | 2026 | service_hours = 100; supervision_minutes = 300; contacts = 1; first_half_minutes = 300; second_half_minutes = 0; quarter_observations = 1 | status = shortfall |
| B4 group more than individual | 2026 | service_hours = 100; supervision_minutes = 300; group_minutes = 200; contacts = 2; first_half_minutes = 150; second_half_minutes = 150; quarter_observations = 1 | status = shortfall |
| B5 quarter ends without observation | 2026 | service_hours = 100; supervision_minutes = 300; contacts = 1; first_half_minutes = 150; second_half_minutes = 150; quarter_observations = 0; quarter_end = True | status = shortfall |
| B6 observation not yet due | 2026 | service_hours = 100; supervision_minutes = 300; contacts = 1; first_half_minutes = 150; second_half_minutes = 150; quarter_observations = 0 | status = met |
| B7 no services this month | 2026 | service_hours = 0; supervision_minutes = 0; contacts = 0 | status = no_service_hours; met = True |
| B8 2027 flat 5% even after 1,000 h | 2027 | service_hours = 100; supervision_minutes = 120; contacts = 1; prior_service_hours = 1200; quarter_observations = 1 | status = shortfall; required_supervision_percent = 5.0 |

## 8. Approval

39 test cases above run automatically on every build (`apps/supervision/test_monthly_work.py`).

Approved by (AKEVRA): ______________________   Date: ____________

Changes requested (if any): ____________________________________________
