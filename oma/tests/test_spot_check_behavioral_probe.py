"""Phase 30, P1c (Phase I, §12): unit tests for
tools_odoo.spot_check.run_behavioral_probe() -- the real, deterministic
execution half of the new behavioral verification widening (closes the
gap where "verified" meant only "the field exists," never "the field
does what the goal claimed"). Against a monkeypatched
_run_odoo_shell_script, no live SSH/DB needed for these; a real,
live-verified smoke test (against the actual dev-duplicate database)
was run manually during implementation and is documented in
docs/reports/PHASE30_P1C_BEHAVIORAL_VERIFICATION_2026-07-30.md.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tools_odoo.spot_check as spot_check_module
from tools_odoo.spot_check import run_behavioral_probe


def test_single_record_create_and_readback(monkeypatch):
    captured = {}

    def fake_run(db, script, timeout=90):
        captured["script"] = script
        return "BEHAVIORAL_PROBE_RESULT:" + json.dumps({"active": True, "id": 41})

    monkeypatch.setattr(spot_check_module, "_run_odoo_shell_script", fake_run)
    result = run_behavioral_probe(
        "odoo16_dev", [("res.partner", {"name": "Test", "is_company": False})], ["active", "id"],
    )
    assert result == {"active": True, "id": 41}
    assert "env['res.partner'].create({'name': 'Test', 'is_company': False})" in captured["script"]
    print("PASS: a single-record create+readback produces the correct script and parses the real result")


def test_parent_child_placeholder_resolves_to_real_id(monkeypatch):
    captured = {}

    def fake_run(db, script, timeout=90):
        captured["script"] = script
        return "BEHAVIORAL_PROBE_RESULT:" + json.dumps({"amount_total": 300.0})

    monkeypatch.setattr(spot_check_module, "_run_odoo_shell_script", fake_run)
    result = run_behavioral_probe(
        "odoo16_dev",
        [
            ("sale.order", {"partner_id": 14}),
            ("sale.order.line", {"order_id": "$0", "product_uom_qty": 2, "price_unit": 150.0}),
        ],
        ["amount_total"],
        target_index=0,
    )
    assert result == {"amount_total": 300.0}
    assert "__record_1 = env['sale.order.line'].create({'order_id': __record_0.id" in captured["script"], (
        "the '$0' placeholder must resolve to the REAL created record's own .id, never a literal string"
    )
    print("PASS: a '$0' placeholder correctly resolves to the earlier record's real .id in the generated script")


def test_forward_reference_is_refused_not_guessed(monkeypatch):
    def fake_run(db, script, timeout=90):
        raise AssertionError("must never reach the shell for an invalid forward reference")

    monkeypatch.setattr(spot_check_module, "_run_odoo_shell_script", fake_run)
    result = run_behavioral_probe(
        "odoo16_dev",
        [("a.model", {"x": "$1"}), ("b.model", {"y": 1})],  # record 0 references record 1, which doesn't exist yet
        ["x"],
    )
    assert result is None, "a forward/self reference can never be real -- must refuse, never guess or crash"
    print("PASS: a forward reference is refused before ever reaching the real shell")


def test_returns_none_when_shell_script_fails(monkeypatch):
    monkeypatch.setattr(spot_check_module, "_run_odoo_shell_script", lambda db, script, timeout=90: None)
    result = run_behavioral_probe("odoo16_dev", [("res.partner", {"name": "x"})], ["id"])
    assert result is None, "a real shell failure must never be silently treated as a confirmed result"
    print("PASS: returns None (never a guessed result) when the underlying shell script fails")


def test_returns_none_on_unparseable_output(monkeypatch):
    monkeypatch.setattr(spot_check_module, "_run_odoo_shell_script", lambda db, script, timeout=90: "some unrelated output\n")
    result = run_behavioral_probe("odoo16_dev", [("res.partner", {"name": "x"})], ["id"])
    assert result is None
    print("PASS: returns None when the real marker line is missing from the shell output")


def test_no_creates_or_invalid_target_index_returns_none():
    assert run_behavioral_probe("odoo16_dev", [], ["id"]) is None
    assert run_behavioral_probe("odoo16_dev", [("res.partner", {})], ["id"], target_index=5) is None
    print("PASS: an empty creates list or an out-of-range target_index returns None, never crashes")


def test_values_are_repr_embedded_never_raw_code(monkeypatch):
    """Security-relevant: every value must be embedded via repr(), so a
    value that LOOKS like Python code is treated as inert data, never
    executed. This is what makes accepting LLM-synthesized `values`
    dicts safe in the first place.
    """
    captured = {}

    def fake_run(db, script, timeout=90):
        captured["script"] = script
        return "BEHAVIORAL_PROBE_RESULT:" + json.dumps({"id": 1})

    monkeypatch.setattr(spot_check_module, "_run_odoo_shell_script", fake_run)
    run_behavioral_probe(
        "odoo16_dev", [("res.partner", {"name": "__import__('os').system('echo pwned')"})], ["id"],
    )
    script = captured["script"]
    assert "repr" not in script  # the literal word never appears; repr() already ran in Python, not in the script
    assert "'__import__(\\'os\\').system(\\'echo pwned\\')'" in script or \
           '"__import__(\'os\').system(\'echo pwned\')"' in script, (
        "a value that looks like code must appear as an inert, quoted STRING literal in the "
        "generated script, never as executable code"
    )
    print("PASS: values reach the script as repr()'d string literals, never as executable code")


def test_forward_reference_is_refused_not_guessed_no_fixture():
    result = run_behavioral_probe(
        "odoo16_dev", [("a.model", {"x": "$1"}), ("b.model", {"y": 1})], ["x"],
    )
    assert result is None
    print("PASS: a forward reference is refused (no monkeypatch needed -- refused before any shell call)")


if __name__ == "__main__":
    test_no_creates_or_invalid_target_index_returns_none()
    test_forward_reference_is_refused_not_guessed_no_fixture()
    print("(this file requires pytest's monkeypatch fixture for the remaining tests; run via pytest)")
