"""P12 Tier B/C item 27 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md §4):
tests for extract_committed_symbols() -- a real, structured, purely deterministic record of
what a task has already committed to on disk across prior rounds. Pure, no I/O, zero live
calls -- direct dict-of-strings input, matching the real read_module_files()/GeneratedModuleFiles
shape.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools_odoo.committed_symbols import CommittedSymbols, extract_committed_symbols


def test_extracts_a_new_model_and_its_fields():
    files = {
        "models/models.py": (
            "from odoo import fields, models\n\n"
            "class StudentModel(models.Model):\n"
            "    _name = 'student.model'\n"
            "    _description = 'Student'\n\n"
            "    age = fields.Integer()\n"
            "    name = fields.Char(required=True)\n"
        ),
    }
    result = extract_committed_symbols(files)
    assert result.models == ["student.model"]
    assert result.fields == ["age", "name"]
    print("PASS: extracts a new model's own _name and every real field assignment")


def test_extracts_an_inherited_model():
    files = {
        "models/models.py": (
            "from odoo import fields, models\n\n"
            "class ResPartner(models.Model):\n"
            "    _inherit = 'res.partner'\n\n"
            "    x_custom_field = fields.Char()\n"
        ),
    }
    result = extract_committed_symbols(files)
    assert result.models == ["res.partner"]
    assert result.fields == ["x_custom_field"]
    print("PASS: extracts an inherited model's own _inherit value the same way as a new _name")


def test_extracts_xml_record_and_menuitem_ids():
    files = {
        "security/security.xml": (
            "<odoo><record id=\"group_x\" model=\"res.groups\"><field name=\"name\">X</field></record>\n"
            "<menuitem id=\"menu_x\" name=\"X\"/></odoo>"
        ),
    }
    result = extract_committed_symbols(files)
    assert result.xml_ids == ["group_x", "menu_x"]
    print("PASS: extracts both record and menuitem xml ids")


def test_symbols_are_deduplicated_across_files():
    files = {
        "models/models.py": "class X(models.Model):\n    _name = 'x.y'\n    a = fields.Char()\n",
        "models/other.py": "class X2(models.Model):\n    _name = 'x.y'\n    a = fields.Char()\n    b = fields.Integer()\n",
    }
    result = extract_committed_symbols(files)
    assert result.models == ["x.y"]
    assert result.fields == ["a", "b"]
    print("PASS: the same symbol defined identically across multiple files is only listed once")


def test_empty_files_produces_a_genuinely_empty_registry():
    result = extract_committed_symbols({})
    assert result == CommittedSymbols(models=[], fields=[], xml_ids=[])
    print("PASS: no files at all produces a genuinely empty registry, never a guess")


def test_never_invents_a_symbol_not_literally_present():
    files = {"models/models.py": "# just a comment, no real model or field definitions at all\n"}
    result = extract_committed_symbols(files)
    assert result.models == []
    assert result.fields == []
    assert result.xml_ids == []
    print("PASS: content with no real symbol definitions produces empty lists, never an inferred guess")


if __name__ == "__main__":
    test_extracts_a_new_model_and_its_fields()
    test_extracts_an_inherited_model()
    test_extracts_xml_record_and_menuitem_ids()
    test_symbols_are_deduplicated_across_files()
    test_empty_files_produces_a_genuinely_empty_registry()
    test_never_invents_a_symbol_not_literally_present()
    print("\nALL COMMITTED-SYMBOLS TESTS PASSED")
