"""P12 Tier B/C item 27 (docs/planning/PHASE30_ARCH_SYNTHESIS_CRITIQUE_FINAL_2026-07-30.md
§4, reassessed from source C's §3.1 "Symbol Contract" proposal after checking it against OMA's
actual Build execution model): a lightweight, OMA-shaped "committed symbols" registry.

Real, confirmed gap this closes: OMA has no artifact enumerating the full set of model/field/
XML-ID names a task has already committed to across prior rounds, checked mechanically. Cross-
round symbol drift is currently handled entirely implicitly -- Build re-derives prior state from
real committed file content each round, which sidesteps *intra-Build* drift (Build reads its own
prior files) but nothing structured is exposed for Code-Review/Testing-QA to independently
consult the same "what symbols does this task already own" question.

Deliberately the NARROWER, cheaper analogue C's own full "Symbol Contract" proposal was reassessed
down to (§3.1 of the synthesis doc): OMA runs one sequential Build call per round, never N
parallel builders each owning a DAG node, so the specific failure mode the full Symbol Contract
exists to prevent (two parallel builders independently inventing two different names for the same
concept in the SAME round) cannot occur in OMA's actual shape. Scoped to cross-round drift only --
a small, structured, purely deterministic (no LLM call, no new model call) extraction over real
committed file content, using the exact same regexes Build's own validators already use elsewhere
in this codebase (never a second, drifting definition of "what counts as a model/field/xmlid").

Deliberately additive infrastructure, not yet wired into Code-Review's/Testing-QA's own
consumption paths -- per this item's own "Confidence: C" (the lowest-confidence, most speculative
item in the full 30-item list), building the real, tested extraction primitive is the right-sized
step for this pause window; the specific consumption points (e.g. cross-checking a Code-Review
finding's claimed symbol against what this task has actually committed to) are real, separate
follow-up work needing its own design pass, not something to bolt on speculatively here.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

# Mirrors specialists/build/specialist.py's own _XML_RECORD_ID_DEF_RE/_XML_MENUITEM_ID_DEF_RE/
# _MODEL_FIELD_DEF_RE and specialists/code_review/specialist.py's own _NEW_MODEL_NAME_LOCAL_RE
# exactly -- never a second, independently-drifting definition of what counts as a real symbol.
_MODEL_NAME_DEF_RE = re.compile(r"^\s*_name\s*=\s*['\"]([\w.]+)['\"]", re.MULTILINE)
_MODEL_INHERIT_DEF_RE = re.compile(r"^\s*_inherit\s*=\s*['\"]([\w.]+)['\"]", re.MULTILINE)
_FIELD_DEF_RE = re.compile(r"^\s*(\w+)\s*=\s*fields\.", re.MULTILINE)
_XML_RECORD_ID_DEF_RE = re.compile(r'<record\s+id="([\w.]+)"')
_XML_MENUITEM_ID_DEF_RE = re.compile(r'<menuitem\s+id="([\w.]+)"')


class CommittedSymbols(BaseModel):
    """The real, structured record of what a task has already committed to on disk, across
    however many prior rounds -- every field defaults to empty, never a guess. `models` is
    every real `_name`/`_inherit` value found; `fields` is every real ORM field assignment
    (`x = fields.Y(...)`) found anywhere in the given files (deliberately not scoped per-model
    -- this is a flat, whole-module registry, matching this item's own "small, lightweight"
    framing rather than building a full per-model symbol table). `xml_ids` is every real
    `<record id="...">`/`<menuitem id="...">` definition found.
    """

    models: list[str] = []
    fields: list[str] = []
    xml_ids: list[str] = []


def extract_committed_symbols(files: dict[str, str]) -> CommittedSymbols:
    """Pure, deterministic, zero-LLM-call extraction over `files` (the same `path -> content`
    shape `read_module_files()`/`GeneratedModuleFiles` already use throughout this codebase) --
    never guesses, never infers a symbol that isn't literally present in the real committed
    text. Order-preserving, de-duplicated (a symbol defined once per file, or once across
    multiple files carried forward unchanged round to round, is listed once).
    """
    models: list[str] = []
    fields: list[str] = []
    xml_ids: list[str] = []

    for content in files.values():
        for match in _MODEL_NAME_DEF_RE.finditer(content):
            if match.group(1) not in models:
                models.append(match.group(1))
        for match in _MODEL_INHERIT_DEF_RE.finditer(content):
            if match.group(1) not in models:
                models.append(match.group(1))
        for match in _FIELD_DEF_RE.finditer(content):
            if match.group(1) not in fields:
                fields.append(match.group(1))
        for match in _XML_RECORD_ID_DEF_RE.finditer(content):
            if match.group(1) not in xml_ids:
                xml_ids.append(match.group(1))
        for match in _XML_MENUITEM_ID_DEF_RE.finditer(content):
            if match.group(1) not in xml_ids:
                xml_ids.append(match.group(1))

    return CommittedSymbols(models=models, fields=fields, xml_ids=xml_ids)
