"""Phase 4 tests: check_sensitive_paths() -- purely mechanical, no LLM
call anywhere in this file, matching the requirement that this stays a
deterministic script forever.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from manager.charter import (
    TIER_NOTIFY_AFTER,
    TIER_PAUSE_BEFORE,
    TIER_PRESIGN_OFF,
    TIER_READONLY,
    check_sensitive_paths,
    load_manager_constitution,
    load_sensitive_paths,
)


def test_files_load_for_real():
    constitution = load_manager_constitution()
    assert "Autonomy tiers" in constitution
    assert "Tier" in constitution and "4" in constitution
    print("PASS: MANAGER_CONSTITUTION.md loads and contains the autonomy tier table")

    rules = load_sensitive_paths()
    assert len(rules) >= 9
    assert any(r.get("model") == "account.move" for r in rules)
    assert any(r.get("model") == "ir.rule" for r in rules)
    print(f"PASS: sensitive_paths.yaml loads {len(rules)} real rules")


def test_no_scope_defaults_to_readonly_or_notify_after():
    assert check_sensitive_paths() == TIER_READONLY
    assert check_sensitive_paths(is_write=True) == TIER_NOTIFY_AFTER
    print("PASS: no sensitive-path hit -> tier 1 (readonly) or tier 2 (write) correctly")


def test_financial_model_hit_is_tier_3():
    tier = check_sensitive_paths(models=["account.move"], is_write=True)
    assert tier == TIER_PAUSE_BEFORE
    print("PASS: account.move hit -> tier 3, even though is_write was set")


def test_field_pattern_glob_match():
    tier = check_sensitive_paths(fields=["sale.order.line.price_unit"])
    assert tier == TIER_PAUSE_BEFORE
    tier_no_match = check_sensitive_paths(fields=["res.partner.name"])
    assert tier_no_match == TIER_READONLY
    print("PASS: field_pattern glob match fires correctly, non-matching field doesn't")


def test_concern_hit_is_tier_3():
    tier = check_sensitive_paths(concerns=["tax_computation"])
    assert tier == TIER_PAUSE_BEFORE
    print("PASS: concern-based hit (tax_computation) -> tier 3")


def test_security_file_and_model_hits_are_tier_4():
    tier_file = check_sensitive_paths(files=["ir.model.access.csv"])
    assert tier_file == TIER_PRESIGN_OFF
    tier_model = check_sensitive_paths(models=["ir.rule"])
    assert tier_model == TIER_PRESIGN_OFF
    print("PASS: ir.model.access.csv and ir.rule hits both -> tier 4")


def test_schema_migration_flag_forces_tier_4_regardless():
    # Even with no other sensitive-path hit at all, this flag alone must win.
    tier = check_sensitive_paths(touches_schema_or_permissions=True)
    assert tier == TIER_PRESIGN_OFF
    # And it must win even over a merely-tier-3 hit -- tier 4 always wins.
    tier2 = check_sensitive_paths(
        models=["account.move"], touches_schema_or_permissions=True
    )
    assert tier2 == TIER_PRESIGN_OFF
    print("PASS: touches_schema_or_permissions forces tier 4 regardless of anything else")


def test_multiple_hits_take_the_highest_tier():
    # account.move (tier 3) + ir.rule (tier 4) at once -> must return 4, not 3.
    tier = check_sensitive_paths(models=["account.move", "ir.rule"])
    assert tier == TIER_PRESIGN_OFF
    print("PASS: multiple simultaneous hits correctly resolve to the highest tier")


if __name__ == "__main__":
    test_files_load_for_real()
    test_no_scope_defaults_to_readonly_or_notify_after()
    test_financial_model_hit_is_tier_3()
    test_field_pattern_glob_match()
    test_concern_hit_is_tier_3()
    test_security_file_and_model_hits_are_tier_4()
    test_schema_migration_flag_forces_tier_4_regardless()
    test_multiple_hits_take_the_highest_tier()
    print("\nALL MANAGER CHARTER TESTS PASSED")
