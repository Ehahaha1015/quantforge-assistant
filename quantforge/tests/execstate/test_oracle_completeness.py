"""Literal falsifiable oracle-completeness gate for the mandatory A-BE range."""

from __future__ import annotations

import importlib
import re
import sqlite3
from pathlib import Path

from _helpers import make_store

from quantforge.execstate import store as store_module

_CONTRACT = Path(__file__).resolve().parents[2] / "tasks" / "EXECSTATE-001.md"

_EXPECTED_LABELS: list[str] = (
    [chr(code) for code in range(ord("A"), ord("Z") + 1)]
    + ["A" + chr(code) for code in range(ord("A"), ord("Z") + 1)]
    + ["B" + chr(code) for code in range(ord("A"), ord("E") + 1)]
)

_ORACLE_TESTS: dict[str, str] = {
    "A": "test_execstate_core::test_a_create_commit_prepared",
    "B": "test_execstate_core::test_b_duplicate_prepare_replays",
    "C": "test_execstate_core::test_c_rollback_and_uncommitted_transaction",
    "D": "test_execstate_core::test_d_first_claim",
    "E": "test_multiprocess::test_e_two_processes_compete_for_one_claim",
    "F": "test_execstate_core::test_f_wrong_target_is_denied",
    "G": "test_execstate_core::test_g_wrong_generation_is_denied",
    "H": "test_execstate_core::test_h_stale_epoch_is_denied",
    "I": "test_execstate_core::test_i_expired_lease_never_redispatches",
    "J": "test_execstate_core::test_j_prepared_survives_restart",
    "K": "test_execstate_core::test_k_dispatching_restart_becomes_unknown",
    "L": "test_execstate_core::test_l_unknown_survives_restart",
    "M": "test_recovery::test_m_acknowledged_survives_restart",
    "N": "test_recovery::test_n_rejected_survives_restart",
    "O": "test_recovery::test_o_recovery_scans_all_records_across_targets",
    "P": "test_recovery::test_p_recovery_evidence_is_target_bound_and_generation_covering",
    "Q": "test_recovery::test_q_unresolved_unknown_blocks_frontier",
    "R": "test_crash::test_r_through_w_real_process_crash_and_reopen[2-before-NO_SUBMISSION]",
    "S": "test_crash::test_r_through_w_real_process_crash_and_reopen[2-after-PREPARED]",
    "T": "test_crash::test_r_through_w_real_process_crash_and_reopen[3-after-PREPARED]",
    "U": "test_crash::test_r_through_w_real_process_crash_and_reopen[5-after-OUTCOME_UNKNOWN]",
    "V": "test_crash::test_r_through_w_real_process_crash_and_reopen[6-before-OUTCOME_UNKNOWN]",
    "W": "test_crash::test_r_through_w_real_process_crash_and_reopen[6-after-ACKNOWLEDGED]",
    "X": "test_corruption::test_x_database_contention_fails_closed",
    "Y": "test_corruption::test_y_schema_version_corruption_fails_closed",
    "Z": "test_corruption::test_z_record_artifact_corruption_fails_closed",
    "AA": "test_execstate_composition::test_aa_real_execstate_evidence_composes_with_frozen_frontier",
    "AB": "test_corruption::test_ab_claim_tamper_fails_closed",
    "AC": "test_execstate_composition::test_ac_determinism_across_processes_and_hash_seeds",
    "AD": "test_execstate_audit::test_ad_no_implicit_clock_or_randomness",
    "AE": "test_execstate_audit::test_ae_no_network_broker_or_credentials",
    "AF": "test_execstate_audit::test_af_no_retry_or_resubmit_api",
    "AG": "test_execstate_audit::test_ag_no_fill_or_portfolio_mutation",
    "AH": "test_execstate_audit::test_ah_audit_history_is_consistent",
    "AI": "test_recovery::test_ai_uncommitted_transaction_does_not_persist",
    "AJ": "test_execstate_audit::test_aj_pre_dispatch_no_effect_proof",
    "AK": "test_recovery::test_ak_receipt_epoch_after_restart",
    "AL": "test_multiprocess::test_al_two_threads_compete_for_one_claim",
    "AM": "test_recovery::test_am_empty_database_recovery",
    "AN": "test_recovery::test_an_recovery_target_a_cannot_authorize_target_b",
    "AO": "test_recovery::test_ao_foreign_key_relationships",
    "AP": "test_execstate_audit::test_ap_exact_replays_add_one_audit_row_and_no_authoritative_change",
    "AQ": "test_schema_v2_oracles::test_aq_h_valid_equal_instant_run_before_prepare",
    "AR": "test_schema_v2_oracles::test_ar_h_corrupt_equal_instant_coverage_omission",
    "AS": "test_schema_v2_oracles::test_as_prepared_falsely_reported_unresolved",
    "AT": "test_schema_v2_oracles::test_at_same_run_unknown_conversion_truth",
    "AU": "test_schema_v2_oracles::test_au_coverage_understatement_and_overstatement",
    "AV": "test_schema_v2_oracles::test_av_sibling_target_unresolved_truth",
    "AW": "test_schema_v2_oracles::test_aw_multiple_same_epoch_runs_are_distinguished",
    "AX": "test_schema_v2_oracles::test_ax_zero_mutation_and_empty_database_cuts",
    "AY": "test_schema_v2_oracles::test_ay_boundary_tamper_matrix",
    "AZ": "test_schema_v2_oracles::test_az_counter_divergence_is_irrelevant",
    "BA": "test_schema_v2_oracles::test_ba_pre_existing_corruption_zero_mutation",
    "BB": "test_schema_v2_oracles::test_bb_fresh_process_reopen_reproduces_validation",
    "BC": "test_schema_v2_oracles::test_bc_post_reopen_historical_tamper_fails_closed",
    "BD": "test_schema_v2_oracles::test_bd_valid_complex_positive_history",
    "BE": "test_schema_v2_oracles::test_be_real_frontier_composition",
}


def _contract_labels() -> list[str]:
    text = _CONTRACT.read_text(encoding="utf-8")
    section = text[
        text.index("## 20. Required implementation tests and falsifiable oracles") :
        text.index("## 21. Dependency and capability gate")
    ]
    return [
        match.group(1)
        for match in re.finditer(r"\* \*\*([A-Z]{1,2})\*\* ", section)
    ]


def test_contract_declares_contiguous_a_to_be_range() -> None:
    labels = _contract_labels()
    assert labels == _EXPECTED_LABELS
    assert labels[0] == "A"
    assert labels[-1] == "BE"
    assert len(labels) == 57


def test_every_mandatory_oracle_maps_to_a_real_unique_test() -> None:
    assert sorted(_ORACLE_TESTS) == sorted(_EXPECTED_LABELS)
    assert len(set(_ORACLE_TESTS.values())) == len(_ORACLE_TESTS)
    for label, node in _ORACLE_TESTS.items():
        module_name, node_id = node.split("::")
        function_name = node_id.split("[", 1)[0]
        module = importlib.import_module(module_name)
        assert hasattr(module, function_name), (label, node)
        assert function_name.startswith("test_"), (label, node)


def test_v2_amendment_added_exactly_aq_through_be() -> None:
    amendment_start = _EXPECTED_LABELS.index("AQ")
    new_labels = [
        label
        for label in _ORACLE_TESTS
        if _EXPECTED_LABELS.index(label) >= amendment_start
    ]
    assert new_labels == _EXPECTED_LABELS[amendment_start:]
    assert len(new_labels) == 15


def test_created_schema_matches_the_literal_contract_ddl(tmp_path) -> None:
    text = _CONTRACT.read_text(encoding="utf-8")
    section = text[
        text.index("### 6.1 Exact schema-v2 recovery-cut objects") :
        text.index("Schema initialization is one `BEGIN EXCLUSIVE` transaction")
    ]
    blocks = re.findall(r"```sql\n(.*?)```", section, flags=re.DOTALL)
    assert len(blocks) == 3
    audit_events_ddl, index_ddl, trigger_ddl = blocks
    store = make_store(tmp_path)
    store.close()
    connection = sqlite3.connect(str(tmp_path / "execstate.sqlite3"))
    connection.row_factory = sqlite3.Row
    try:
        objects = {
            str(row["name"]): str(row["sql"])
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE name IN ('audit_events', "
                "'audit_events_boundary_run_phase', "
                "'recovery_runs_require_cut')"
            ).fetchall()
        }
    finally:
        connection.close()
    assert store_module._schema_sql_tokens(
        objects["audit_events"]
    ) == store_module._schema_sql_tokens(audit_events_ddl)
    assert store_module._schema_sql_tokens(
        objects["audit_events_boundary_run_phase"]
    ) == store_module._schema_sql_tokens(index_ddl)
    assert store_module._schema_sql_tokens(
        objects["recovery_runs_require_cut"]
    ) == store_module._schema_sql_tokens(trigger_ddl)


def test_audit_vocabulary_matches_the_literal_contract_sets() -> None:
    text = _CONTRACT.read_text(encoding="utf-8")
    kind_block = text[
        text.index("The `event_kind` literals are exactly:") :
        text.index("The `reason` field is not free-form.")
    ]
    contract_kinds = [
        line.strip()
        for line in kind_block.splitlines()
        if re.fullmatch(r"[A-Z_]+", line.strip())
    ]
    contract_pairs = {
        (kind, reason)
        for kind, reason in re.findall(
            r"^\| `([A-Z_]+) \+ ([A-Z_]+)` \|", text, flags=re.MULTILINE
        )
    }
    assert contract_kinds == list(store_module._EVENT_KINDS)
    assert contract_pairs == set(store_module._REASON_PAIRS)
    assert len(contract_kinds) == 12
    assert len(contract_pairs) == 22
