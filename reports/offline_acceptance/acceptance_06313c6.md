# Offline acceptance @ 06313c6a5827

- result: **PASS** (pytest exit 0, 24.5s)
- dirty files at run: 0
- live E2E proven: **no** (agent, ChatGPT page, reviewer judgment simulated)

| scenario | result | s |
|---|---|---|
| test_A_spec_to_done_with_reject_fix_approve | passed | 4.9 |
| test_B_crash_restart_retry | passed | 3.7 |
| test_C_duplicate_wakes_and_decisions_change_nothing | passed | 2.9 |
| test_D_stalled_agent_alert_answer_idle_window_single_resend | passed | 4.9 |
| test_E_owner_gate_stops_the_loop_until_the_owner_acts | passed | 6.6 |
| test_F_api_credit_mode_is_off_by_default | passed | 0.0 |
