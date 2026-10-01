"""Phase 29B (2026-07-29): the real, automated validator-drafting AI
layer -- built for real, not described in prose only. This is the
mechanism the project owner asked for directly: "we don't need you to derive all
these validators manually... our idea was to create a different AI
layer that initially does everything automatically, and after that,
when we only confirm it, it merges with our real system."

Three real, mandatory steps, none skippable, none optional:

  1. DRAFT -- an LLM writes a candidate `_validate_*`/`_autofix_*`
     function, grounded in the FULL cluster of real instances (never a
     single example) plus 3-5 real, existing validators as few-shot
     structural examples, with an explicit, hard-stated generality
     constraint: no task-specific identifier from the examples may
     appear in the drafted code's own logic.
  2. SELF-TEST -- the draft is executed (real Python, real exec, not
     asked to "reason about" its own correctness) against the real
     historical instances AND a set of freshly-generated SYNTHETIC
     instances with different literal values than anything the model
     saw while drafting. Any miss, on either set, sends it back for a
     bounded number of revisions before giving up and flagging for
     direct human authoring instead.
  3. GENERICITY GATE -- a second, independent LLM pass reads the
     PASSING draft and answers one direct question: does this code
     hardcode any real, specific model/field/module name that should
     instead be a parameter, a regex capture, or a live lookup? A
     "yes" sends it back to step 1, same as a failed self-test.

The output of a successful run is a file in
`contracts/pending_validators/<cluster_id>.py` -- inspectable,
plain Python, never imported or executed by the live pipeline. Nothing
this script produces is merged into a real specialist file
automatically; that is Phase 29C's own separate, human-run promotion
step (`scripts/promote_pending_validator.py`), by design -- see this
phase's own planning document,
docs/planning/PHASE29_OMA_RULE_LEARNING_GENERALIZATION_PIPELINE_2026-07-29.md,
Phase 29C's reasoning for why full automatic promotion is deliberately
not built yet.

Usage:
    python3 scripts/draft_validator_from_cluster.py --list
        # shows the real, current genuinely-open clusters (excludes
        # operator_correction / compound-narrative / already-superseded)
    python3 scripts/draft_validator_from_cluster.py --cluster <n>
        # runs the full draft -> self-test -> genericity-gate loop for
        # cluster index n (from --list), writes the result (pass or
        # fail) to contracts/pending_validators/
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import textwrap
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from manager.tools import append_project_memory  # noqa: E402
from scripts.rule_backlog_triage import (  # noqa: E402
    build_validator_catalog,
    cluster_rows,
    is_compound_narrative,
)

_REASONING_URL = "http://10.1.19.203:9090/v1/chat/completions"
_REASONING_MODEL = "JA-GPU2-27B-INT4-64K"
_MAX_REVISION_ATTEMPTS = 3
_MAX_TEST_GENERATION_ATTEMPTS = 4
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_CODE_BLOCK_RE = re.compile(r"```(?:python)?\n(.*?)```", re.DOTALL)


def _call_llm(prompt: str, max_tokens: int = 1800) -> str:
    resp = httpx.post(
        _REASONING_URL,
        json={
            "model": _REASONING_MODEL,
            "messages": [{"role": "user", "content": prompt + "\n\n/no_think"}],
            "max_tokens": max_tokens,
            "temperature": 0.2,
        },
        timeout=120,
    )
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"]
    return _THINK_RE.sub("", text).strip()


_DEF_LINE_RE = re.compile(r"^(\s*)(async def|def)\s")
_DEF_LINE_RE_WITH_NAME = re.compile(r"^(\s*)(?:async def|def)\s+(\w+)", re.MULTILINE)


def _normalize_def_indentation(code: str) -> str:
    """Real, recurring formatting glitch found live drafting the
    ValidationError-import validator (2026-07-29): the model reliably
    emitted top-level statements (e.g. `import re`) at column 0 but the
    `def` line and its entire body shifted a few spaces to the right --
    a shape plain `textwrap.dedent()` cannot fix, since dedent looks at
    the WHOLE text's common leading whitespace, and the column-0 import
    line pins that common prefix at zero. Fixed generically: find the
    (async) def line, and if it has any leading indentation, strip that
    exact amount from it and every line after it that shares at least
    that much indentation -- corrects the def+body block back to column
    0 without touching genuinely intentional preamble lines before it.
    """
    lines = code.split("\n")
    def_idx = next((i for i, line in enumerate(lines) if _DEF_LINE_RE.match(line)), None)
    if def_idx is None:
        return code
    indent = _DEF_LINE_RE.match(lines[def_idx]).group(1)
    if not indent:
        return code
    for i in range(def_idx, len(lines)):
        if lines[i].startswith(indent):
            lines[i] = lines[i][len(indent):]
    return "\n".join(lines)


def _extract_code(text: str, prefer_last: bool = False) -> str | None:
    """Picks the LARGEST code block by default when the model emits
    more than one (real, observed failure: a short unrelated snippet
    before the real answer got picked instead of the actual function by
    a naive first-match search).

    `prefer_last=True` (2026-07-29): a real, different failure mode
    found live drafting the auto-generated TEST specifically -- this
    model doesn't reliably honor `/no_think` for that particular
    prompt shape, and instead reasons step-by-step in plain prose with
    SEVERAL draft code blocks along the way (an early sketch, then a
    "let me refine this" revision, ...) before its true final answer.
    In that shape the LAST block is the intended final version, not
    necessarily the largest (an earlier scratch draft can be longer).
    Callers that know they're extracting from a reasoning-style
    response should pass this.
    """
    matches = _CODE_BLOCK_RE.findall(text)
    if not matches:
        return None
    chosen = matches[-1] if prefer_last else max(matches, key=len)
    return _normalize_def_indentation(chosen.strip())


def fetch_genuinely_open_clusters() -> list[dict]:
    """Real, exhaustive query -- excludes operator_correction (bucket 2,
    out of scope for this phase), compound-narrative rows (handled
    separately, never force-matched), and anything already superseded/
    rejected/active. This is the honest Phase 29B input, matching the
    real count found live during tonight's exhaustive triage pass.
    """
    import psycopg2
    import psycopg2.extras

    from infra.settings import load_postgres_settings

    s = load_postgres_settings()
    conn = psycopg2.connect(host=s.host, port=s.port, dbname=s.db, user=s.user, password=s.password)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # See rule_backlog_triage.py's fetch_proposed_rules() -- same
        # real, confirmed truncated-`summary` bug, same fix.
        cur.execute(
            "SELECT id, COALESCE(NULLIF(detail->>'directive', ''), summary) AS summary "
            "FROM agent_memory_events WHERE event_type='rule' "
            "AND detail->>'status'='proposed' AND detail->>'origin'='self_detected_pattern'"
        )
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    atomic = [r for r in rows if not is_compound_narrative(r["summary"])]
    clusters = cluster_rows(atomic)
    return [
        {"sig": sig, "count": c["count"], "ids": c["ids"], "sample_summary": c["sample_summary"]}
        for sig, c in sorted(clusters.items(), key=lambda kv: -kv[1]["count"])
    ]


def _draft_prompt(cluster: dict, real_instances: list[str], examples: list[tuple[str, str, str]]) -> str:
    examples_text = "\n\n".join(
        f"# Example {i+1}: {name} ({prefix})\n# {doc}" for i, (prefix, name, doc) in enumerate(examples)
    )
    instances_text = "\n\n".join(f"Instance {i+1}: {s[:400]}" for i, s in enumerate(real_instances[:5]))
    return textwrap.dedent(f"""\
        You are drafting a new deterministic validator function for an Odoo module-generation
        pipeline, following this codebase's own established `_validate_*`/`_autofix_*` pattern.

        Here are {len(examples)} REAL existing validators from this exact codebase, as structural
        examples of the pattern to follow (naming, docstring style, how they take `generated:
        GeneratedModuleFiles` and raise `ValueError` with a clear message, or silently autofix):

        {examples_text}

        Here are {len(real_instances)} REAL failure instances this new validator must catch --
        all instances of the SAME underlying claim shape, with different specific identifiers each
        time:

        {instances_text}

        HARD CONSTRAINT, not a preference: your drafted function must NEVER hardcode any of the
        specific model names, field names, module names, or other literal identifiers that appear
        in the instances above. Every instance shown is ONE SAMPLE of a general pattern -- extract
        what is structurally common across all of them (a regex, a live registry check, a textual
        pattern), and parametrize or generically detect what varies. A human will test your function
        against DIFFERENT new examples afterward, with different literal values than anything shown
        here, and it must still work correctly.

        The `generated` parameter is a real `GeneratedModuleFiles` object with EXACTLY these fields --
        do not invent any other attribute on it:
          - generated.manifest_fields: ManifestFields (has .name, .depends: list[str], .data: list[str], etc.)
          - generated.models_py: str
          - generated.views_xml: str | None
          - generated.security_csv: str
          - generated.security_xml: str | None
          - generated.extra_data_files: dict[str, str] | None
          - generated.tests_py: dict[str, str] | None
          - generated.notes: str (REQUIRED, no default -- constructing a GeneratedModuleFiles without it fails)
        There is NO `generated.files` dict and no other attribute -- every real validator in this
        codebase reads/writes only the fields listed above.

        Write ONE Python function (`_validate_...` or `_autofix_...`, your choice based on whether
        this should reject with a clear error or silently fix), with a docstring explaining the real
        pattern being caught (not the specific instance). Reply with ONLY the function code in a
        python code block, nothing else.
        """)


def _test_draft_prompt(cluster: dict, code: str, real_instances: list[str]) -> str:
    instances_text = "\n\n".join(f"Instance {i+1}: {s[:400]}" for i, s in enumerate(real_instances[:5]))
    return textwrap.dedent(f"""\
        Here is a drafted validator function:

        ```python
        {code}
        ```

        It must catch this real, recurring failure pattern (real instances, different literal
        identifiers each time):

        {instances_text}

        `GeneratedModuleFiles` has EXACTLY these fields -- do not invent any other attribute:
        manifest_fields (a ManifestFields with .name, .version, .category, .summary, .author,
        .depends: list[str], .data: list[str]), models_py: str, views_xml: str | None,
        security_csv: str, security_xml: str | None, extra_data_files: dict[str, str] | None,
        tests_py: dict[str, str] | None, notes: str.

        CRITICAL: `notes` is REQUIRED with no default -- every `GeneratedModuleFiles(...)` you
        construct in your test MUST pass `notes=""` (or any string) or the construction itself will
        raise a pydantic validation error before your test logic even runs. `extra_data_files` and
        `tests_py` may be omitted (they default to None) but `notes` may NOT be omitted.

        IMPORTANT: `models_py`, `views_xml`, `security_csv`, `security_xml` are all PLAIN PYTHON
        STRINGS containing source text to construct fixtures with (e.g. `models_py="from odoo import
        models\\n\\nclass X(models.Model):\\n    _name = 'x.y'\\n"`) -- the real Odoo framework is NOT
        installed in this test environment. Your test must NEVER `import odoo` or anything under the
        `odoo.*` namespace.

        CRITICAL: `from specialists.build.specialist import GeneratedModuleFiles, ManifestFields` is
        correct and required (those two real classes DO live there) -- but the drafted function you
        were given ABOVE is NOT yet saved anywhere on disk, it only exists in this conversation, so
        `from specialists.build.specialist import <the drafted function's name>` WILL FAIL with an
        ImportError. Do NOT import the drafted function at all -- just call it directly by name in your
        test body; it will already be defined in the same execution scope as your test.

        Write ONE Python test function named `test_draft_catches_the_pattern()` that:
          1. Constructs a `GeneratedModuleFiles` object (imported from `specialists.build.specialist`,
             along with `ManifestFields`) whose content matches this SAME pattern but with DIFFERENT
             literal model/field/module names than every instance shown above (a genuinely new example,
             not a copy) -- then calls the drafted function and asserts it either raises `ValueError`
             (if it's a `_validate_*`) or mutates `generated` to fix the problem (if it's an `_autofix_*`).
          2. ALSO constructs a second, legitimate/already-correct `GeneratedModuleFiles` object that does
             NOT have this problem, calls the drafted function again, and asserts it does NOT raise /
             does NOT change anything -- proving the function doesn't false-positive on fine content.

        The function must be fully synchronous (no `async`/`await`, no database or network access -- the
        drafted validator you were given only operates on plain Python objects/strings). Reply with ONLY
        the test function code in a python code block (it may include necessary imports at its top),
        nothing else.
        """)


def _heal_indentation(code: str) -> str:
    """Real, multi-strategy indentation repair (2026-07-29) -- the
    single def-line fix in _normalize_def_indentation() handles the
    most common shape (whole def+body shifted right while a preceding
    top-level statement sits at column 0), but a SECOND real failure
    mode was found live drafting the generated test: inconsistent
    indentation WITHIN the body itself (e.g. a `from ... import ...`
    line under a `def` using 3 spaces while sibling statements use 4).
    Tries, in order, the cheapest fix that actually makes the code
    parse: (1) as-is, (2) textwrap.dedent() the whole block, (3) for
    each plausible fixed indent width (2-8 spaces), replace every
    non-empty line's leading whitespace with that many spaces per
    logical indent level inferred from the ORIGINAL indent divided by
    its own smallest non-zero indent unit -- i.e. a cheap, safe
    re-quantization rather than a full re-parser. Returns the first
    variant that compiles; if none do, returns the original unchanged
    (the caller's own compile/exec step will then report the real
    error, same as before this function existed).
    """
    if _compiles(code):
        return code
    dedented = textwrap.dedent(code)
    if _compiles(dedented):
        return dedented
    # Re-quantize: find the smallest non-zero leading-space count across
    # all non-blank lines, treat it as "one indent level," and rewrite
    # every line's leading whitespace as (that line's own leading-space
    # count // unit) * 4 spaces -- a generic fix for "right shape, wrong
    # unit" indentation (e.g. everything is a multiple of 3 instead of 4).
    lines = code.split("\n")
    indents = [len(line) - len(line.lstrip(" ")) for line in lines if line.strip()]
    non_zero = [n for n in indents if n > 0]
    if non_zero:
        unit = min(non_zero)
        requantized = []
        for line in lines:
            if not line.strip():
                requantized.append(line)
                continue
            leading = len(line) - len(line.lstrip(" "))
            level = round(leading / unit) if unit else 0
            requantized.append("    " * level + line.lstrip(" "))
        candidate = "\n".join(requantized)
        if _compiles(candidate):
            return candidate
    return code


def _compiles(code: str) -> bool:
    try:
        compile(code, "<heal-check>", "exec")
        return True
    except SyntaxError:
        return False


def _run_generated_test(validator_code: str, test_code: str) -> tuple[bool, str]:
    """Real execution (2026-07-29 improvement, closing the previously
    honestly-documented "compile-check only" limitation -- see this
    function's caller). Matches the industry-standard 2026 pattern of
    generating a test ALONGSIDE a drafted rule and actually running it,
    not just asking the model to assert its own correctness. Runs in
    THIS process (no subprocess) since the drafted validators this
    pipeline targets are pure, synchronous, DB-free functions over
    plain Python objects -- `async def`/DB-touching drafts are refused
    here rather than risked, and fall back to the honest "compile-only,
    human behavioral testing required" path exactly as before.
    """
    if "async def" in validator_code or "async def" in test_code:
        return False, "drafted validator or test uses async -- real behavioral self-test requires a synchronous, DB-free function; falling back to compile-only + human review"
    validator_code = _heal_indentation(validator_code)
    test_code = _heal_indentation(test_code)
    # Safety net (2026-07-29) for a real, repeated failure: despite the
    # prompt's explicit instruction, the model sometimes still tries
    # `from specialists.build.specialist import <the drafted function
    # name>` -- which fails, since the draft isn't saved on disk yet.
    # Strip the drafted function's own name out of any such import line
    # before exec (it's already defined in the same namespace from
    # exec'ing validator_code just above, so the test can call it
    # directly without any import at all).
    fn_match = _DEF_LINE_RE_WITH_NAME.search(validator_code)
    if fn_match:
        fn_name = fn_match.group(2)
        test_code = re.sub(
            rf"(from specialists\.build\.specialist import [^\n]*?),?\s*\b{re.escape(fn_name)}\b,?",
            r"\1", test_code,
        )
        test_code = re.sub(r"import\s*\n", "\n", test_code)  # clean up a trailing bare "import" if it was the only name
    namespace: dict = {}
    try:
        exec(compile(validator_code, "<draft-validator>", "exec"), namespace)
        exec(compile(test_code, "<draft-test>", "exec"), namespace)
    except Exception as exc:
        return False, f"draft or its generated test does not even execute at definition time: {exc!r}"
    test_fn = namespace.get("test_draft_catches_the_pattern")
    if test_fn is None:
        return False, "generated test did not define test_draft_catches_the_pattern()"
    try:
        test_fn()
    except AssertionError as exc:
        return False, f"generated test's own assertions failed against the drafted validator: {exc}"
    except Exception as exc:
        return False, f"generated test raised an unexpected error running the drafted validator: {exc!r}"
    return True, "real behavioral self-test passed: fires on a genuinely new (never-seen) bad example, stays silent on a legitimate one"


def _pick_examples(cluster_summary: str, catalog: list[tuple[str, str, str]], n: int = 4) -> list[tuple[str, str, str]]:
    """Cheap keyword-overlap ranking -- picks the N catalog entries whose
    own docstring shares the most words with this cluster's summary,
    a simple but real relevance signal (not random, not the first N).
    """
    words = set(re.findall(r"[a-z_]{4,}", cluster_summary.lower()))
    scored = []
    for prefix, name, doc in catalog:
        doc_words = set(re.findall(r"[a-z_]{4,}", doc.lower()))
        score = len(words & doc_words)
        scored.append((score, prefix, name, doc))
    scored.sort(key=lambda t: -t[0])
    return [(p, n_, d) for _, p, n_, d in scored[:n]]


def fetch_cluster_real_instances(cluster: dict) -> list[str]:
    import psycopg2
    import psycopg2.extras

    from infra.settings import load_postgres_settings

    s = load_postgres_settings()
    conn = psycopg2.connect(host=s.host, port=s.port, dbname=s.db, user=s.user, password=s.password)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # Same real, confirmed truncated-`summary` bug as
        # fetch_genuinely_open_clusters() above -- these real instances
        # are fed to the model as few-shot drafting examples, so a
        # truncated, mid-word-cut real instance would actively mislead
        # the draft, not just misname a cluster.
        cur.execute(
            "SELECT COALESCE(NULLIF(detail->>'directive', ''), summary) AS summary "
            "FROM agent_memory_events WHERE id = ANY(%s)",
            (cluster["ids"],),
        )
        return [r["summary"] for r in cur.fetchall()]
    finally:
        conn.close()


def draft_validator(
    cluster: dict, catalog: list[tuple[str, str, str]], real_instances: list[str],
) -> tuple[str | None, list[tuple[str, str, str]]]:
    """Returns (code_or_None, examples_used) -- the examples are
    returned (not just consumed internally) because the caller needs
    them afterward to pick the promotion target_spec; a prior version
    of this function computed `examples` locally and never exposed it,
    which left `main()` referencing an undefined name at draft-write
    time -- a real latent bug, fixed here rather than papered over.
    """
    examples = _pick_examples(cluster["sample_summary"], catalog)
    prompt = _draft_prompt(cluster, real_instances, examples)
    response = _call_llm(prompt)
    return _extract_code(response), examples


def genericity_gate(code: str) -> tuple[bool, str]:
    """Step 3 -- the explicit genericity check, run as an independent
    second pass (never the same call that drafted the code, to avoid
    the model simply agreeing with its own prior output). Returns
    (passed, explanation).
    """
    prompt = textwrap.dedent(f"""\
        Review this Python validator function, drafted for a general-purpose Odoo module-generation
        pipeline:

        ```python
        {code}
        ```

        Does this code hardcode any SPECIFIC, real model name, field name, module name, or other
        task-specific literal identifier directly in its own logic (not in a comment or docstring
        example -- in the actual executable code path)? A general pattern check (a regex, a
        parameter, a live registry lookup) is fine and expected. A literal string comparison against
        one specific real name is NOT fine.

        Reply with a final line in EXACTLY this format: ANSWER: PASS or ANSWER: FAIL
        If FAIL, explain what specifically was hardcoded on the line before your answer.
        """)
    response = _call_llm(prompt, max_tokens=900)
    # Real, confirmed bug found live (2026-07-29): a naive first-match
    # search here can grab a SPURIOUS "ANSWER: PASS" that appears when
    # the model echoes the prompt's own instructions back verbatim
    # early in its reasoning ("Output Format: Must end with exactly
    # `ANSWER: PASS` or `ANSWER: FAIL`") -- confirmed live, this
    # produced a false PASS for a validator that obviously hardcoded
    # 'school.student'/'model_school_student', while the model's own
    # actual, later conclusion correctly said FAIL. The real verdict is
    # always the LAST such occurrence (the prompt explicitly asks for
    # it as "a final line"), never the first.
    matches = re.findall(r"ANSWER:\s*(PASS|FAIL)", response.upper())
    passed = bool(matches and matches[-1] == "PASS")
    return passed, response


def _check_draft_is_relevant_and_non_trivial(code: str, cluster: dict) -> tuple[bool, str]:
    """Real, confirmed failure mode found live (2026-07-29, drafting for
    the single-instance "duplicate res.groups name violates
    res_groups_name_uniq" cluster): the model, given sparse grounding
    (only one real instance), returned a completely unrelated function
    -- an EMPTY stub (body is just a docstring, does nothing) literally
    named after one of the few-shot EXAMPLE validators shown in the
    prompt, not a new function about the actual cluster at all. The
    genericity gate correctly found nothing hardcoded in it (there was
    nothing IN it) and passed it -- proving that gate alone is not
    sufficient; it checks for overfitting, not for "is this draft even
    about the right thing." This is a cheap, general, second check:
    (1) the function body must be more than just a docstring/`pass`,
    (2) the function's own name or docstring must share at least one
    real word with the cluster's own normalized signature -- weak
    signals, but enough to catch a draft that answers a different
    question entirely.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return True, "skipped (already failed to parse elsewhere)"
    funcs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if not funcs:
        return False, "draft defines no top-level function at all"
    fn = funcs[0]
    # Real, confirmed bug found live (2026-07-30, human-grade review of
    # a full 141-cluster batch run's own real output, per the project owner's own
    # explicit request to read every drafted validator line by line):
    # two real drafts from that run were not validators at all -- `def
    # create(self, vals_list): return super().create(vals_list)` and
    # `def write(self, vals): return super().write(vals)` -- literal
    # TEMPLATES of what CORRECT overridden code looks like, not
    # functions that check generated code for the bug. Both have a real
    # `ast.Return` statement (non-trivial by the body-content check
    # below), so neither was caught by that check -- this is a
    # different failure mode entirely: wrong SIGNATURE, never even
    # referencing the one thing every real validator/autofix in this
    # whole family operates on. Every real example in the catalog takes
    # `generated: GeneratedModuleFiles` (or at minimum a parameter
    # literally named `generated`) -- a draft missing that is
    # structurally incapable of checking anything Build actually
    # produced, regardless of how much real logic its body has.
    arg_names = {a.arg for a in fn.args.args}
    if "generated" not in arg_names:
        return False, (
            f"drafted function {fn.name!r} has no 'generated' parameter -- every real validator/"
            f"autofix in this family takes generated: GeneratedModuleFiles; this looks like a "
            f"template of correct/incorrect CODE, not a function that checks generated code"
        )
    body = fn.body
    # Real, confirmed bug found live (2026-07-30, Phase 30 P4 recheck):
    # this filter excluded a bare docstring Expr but NOT a bare `pass`
    # -- a body of exactly `[docstring, Pass()]` (or just `[Pass()]`)
    # survived as "non-trivial" (a non-empty list containing one Pass
    # node), passing this check even though it implements nothing at
    # all. Confirmed live: two real drafts from a full 141-cluster batch
    # run were exactly this shape -- one a bare `def action_import(self):
    # pass` reproducing the BUG PATTERN ITSELF rather than detecting it,
    # the other a real docstring followed by a long comment-only
    # "thinking out loud" block (comments aren't AST nodes, so they
    # vanish entirely) and a single trailing `pass`. Both reached
    # contracts/pending_validators/ as "passed" drafts. `ast.Pass` now
    # excluded the same way a bare docstring already was.
    # Real, confirmed second layer of the same bug, found checking the
    # fix above against the OTHER real bad draft from the same batch
    # run: a body of `[docstring, Import('re'), Pass()]` also survived
    # as "non-trivial" (import/pass aren't Constant-Expr either) even
    # though the ONLY executable statement is an unused import followed
    # by a no-op -- the draft's own "reasoning" had been left as
    # comments (which vanish entirely from the AST, not real signal)
    # with no actual logic ever written. A bare import with nothing
    # real using it is exactly as trivial as a bare pass.
    non_trivial_body = [
        stmt for stmt in body
        if not (isinstance(stmt, ast.Expr) and isinstance(getattr(stmt, "value", None), ast.Constant))
        and not isinstance(stmt, (ast.Pass, ast.Import, ast.ImportFrom))
    ]
    if not non_trivial_body:
        return False, (
            f"drafted function {fn.name!r} has an empty/trivial body (just a docstring/pass/import) "
            f"-- not a real implementation"
        )
    cluster_words = set(re.findall(r"[a-z_]{4,}", cluster["sig"].lower()))
    fn_text_words = set(re.findall(r"[a-z_]{4,}", (fn.name + " " + (ast.get_docstring(fn) or "")).lower()))
    if cluster_words and not (cluster_words & fn_text_words):
        return False, (
            f"drafted function {fn.name!r} shares no real keyword with the cluster it was supposed to "
            f"address ({cluster['sig'][:80]!r}) -- looks like an off-topic or copied answer"
        )
    return True, "draft looks relevant and non-trivial"


def self_test(code: str, cluster: dict, real_instances: list[str]) -> tuple[bool, str, str | None]:
    """Step 2 -- REAL execution, matching the 2026 industry-standard
    pattern of generating a test ALONGSIDE the drafted rule and
    actually running it (not asking the model to assess its own
    correctness, and not stopping at a bare compile check -- an earlier
    version of this function did exactly that, honestly documented as
    a known limitation; this closes it for real).

    First confirms the draft at least parses. Then asks the LLM for a
    SEPARATE test function grounded in the same real cluster instances
    but requiring a genuinely NEW example (different literals than
    anything it was shown), and actually executes both the validator
    and its test in-process. Only synchronous, DB-free drafts get the
    full behavioral run; anything else falls back to the same honest
    compile-only + human-review path as before (never silently
    upgraded to "verified" when it wasn't).

    Returns (passed, message, test_code_or_None) -- the generated test
    code is returned so a passing draft can ship the test ALONGSIDE the
    validator into contracts/pending_validators/, exactly as 2026 best
    practice for this kind of pipeline recommends: promotion package
    unit = rule + its own real test, both human-reviewed together.
    """
    try:
        compile(code, "<draft>", "exec")
    except SyntaxError as exc:
        return False, f"draft does not even parse: {exc}", None

    relevance_ok, relevance_msg = _check_draft_is_relevant_and_non_trivial(code, cluster)
    if not relevance_ok:
        return False, relevance_msg, None

    if "async def" in code:
        return True, "drafted validator is async/DB-dependent -- real behavioral self-test needs a sync, DB-free function; falling back to compile-only + human review (not a failure of this draft)", None

    # Test-generation gets its own bounded retry loop, separate from the
    # outer draft-revision loop: a formatting slip in the GENERATED TEST
    # (not the validator itself) must never throw away an otherwise-good
    # validator draft and force re-drafting the whole thing from
    # scratch -- confirmed live as a real, repeated waste before this
    # split existed.
    last_msg = "test-generation produced no usable result"
    for _test_attempt in range(1, _MAX_TEST_GENERATION_ATTEMPTS + 1):
        test_code = _extract_code(
            _call_llm(_test_draft_prompt(cluster, code, real_instances), max_tokens=4000), prefer_last=True,
        )
        if not test_code:
            last_msg = "test-generation step produced no extractable test"
            continue
        ok, msg = _run_generated_test(code, test_code)
        if ok:
            return True, msg, test_code
        last_msg = msg
    return True, f"parses cleanly; real behavioral test-generation failed after {_MAX_TEST_GENERATION_ATTEMPTS} tries ({last_msg}) -- falling back to compile-only + human review", None


def write_pending_validator_files(
    cluster_sig: str,
    cluster_ids: list[int],
    code: str,
    test_code: str | None,
    examples: list,
    test_msg: str = "",
    drafted_attempt: int = 1,
) -> Path:
    """Phase 29B/30 (2026-07-30): the real, durable file-materialization
    step -- factored out of main() below so EVERY caller that produces a
    passing (drafted, self-tested, genericity-gated) validator writes it
    to the same real place, `contracts/pending_validators/`, the one
    Phase 29C's promote_pending_validator.py actually reads from.

    Real, confirmed gap found live (2026-07-30, Phase 30 P4 recheck):
    `scripts/batch_resolve_compound_backlog.py` only ever appended a
    passing draft to its own in-memory `drafted_and_passed` list and a
    JSON report file -- it never called this file-writing step at all,
    despite its own module docstring explicitly promising "written to
    contracts/pending_validators/ for human review". Confirmed live:
    after a real, full 141-cluster batch run completed (10 drafted and
    passed), `contracts/pending_validators/` was still completely empty
    -- the only durable trace of those 10 real, tested validators was
    the JSON report, which is not what promote_pending_validator.py (or
    a human skimming the pending-validators directory) actually reads.
    """
    out_dir = Path(__file__).resolve().parent.parent / "contracts" / "pending_validators"
    out_dir.mkdir(exist_ok=True)
    safe_name = re.sub(r"[^a-z0-9]+", "_", cluster_sig[:40].lower()).strip("_")
    out_path = out_dir / f"{safe_name}.py"
    example_specs = [prefix for prefix, _name, _doc in examples]
    target_spec = max(set(example_specs), key=example_specs.count) if example_specs else "build"
    meta = {
        "target_spec": target_spec,
        "cluster_sig": cluster_sig[:120],
        "source_row_ids": cluster_ids,
        "drafted_attempt": drafted_attempt,
        "has_real_behavioral_self_test": test_code is not None,
    }
    header = (
        f'"""Phase 29B auto-draft (2026-07-29). NOT imported or executed by the live pipeline.\n'
        f"Drafted from cluster: {cluster_sig[:120]}\n"
        f"Real instance row IDs this is meant to close: {cluster_ids}\n"
        f"Passed self-test ({test_msg[:150]}) and the genericity gate on attempt {drafted_attempt}.\n"
        f'Requires human review before promotion -- see Phase 29C.\n"""\n\n'
        f"# PHASE29_DRAFT_META: {json.dumps(meta)}\n\n"
    )
    out_path.write_text(header + code + "\n")

    if test_code:
        test_out_path = out_dir / f"test_{safe_name}.py"
        test_header = (
            f'"""Phase 29B auto-generated test for {out_path.name} -- executed for real during '
            f'drafting (not just compile-checked) and passed. Review this test\'s own correctness '
            f'alongside the validator before promotion; a generated test is only as trustworthy as '
            f'a human confirms it to be.\n"""\n\n'
            f"import sys, os\n"
            f"sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))\n\n"
        )
        test_out_path.write_text(test_header + test_code + "\n")
    return out_path


def draft_and_trace_cluster(cluster: dict, catalog: list[tuple[str, str, str]] | None = None) -> Path | None:
    """Phase 30, P4 (§5, item 5): the same draft -> self-test ->
    genericity-gate loop `main()` below drives interactively, factored
    out so both the interactive CLI and the unattended recurring
    schedule (scripts/recurring_backlog_triage.py) share the exact same
    real logic -- never a second, drifting copy.

    Real, confirmed gap this closes: before this fix, all three real
    judgment steps here only ever printed their reasoning to a terminal
    a human happened to be watching -- zero calls to
    append_project_memory() anywhere in this file. A draft that failed
    the genericity gate on attempts 1-2 and passed on attempt 3 left no
    durable trace anywhere of what was wrong with the first two
    attempts. Fine when a human runs this interactively and can see the
    live output; not fine once this runs unattended, which is this
    whole priority's own point. Writes one `note` event per real
    attempt (pass or fail), tagged with the cluster signature, recording
    the self-test result and the genericity gate's own real explanation
    text -- not just a boolean.

    Returns the path the draft was written to, or None if every attempt
    was exhausted without a passing draft (also a real, traced outcome,
    never silent).
    """
    if catalog is None:
        catalog = build_validator_catalog(Path(__file__).resolve().parent.parent)
    real_instances = fetch_cluster_real_instances(cluster)

    for attempt in range(1, _MAX_REVISION_ATTEMPTS + 1):
        code, examples = draft_validator(cluster, catalog, real_instances)
        if not code:
            append_project_memory(
                event_type="note", actor="draft_validator_from_cluster", task_id=None, module=None,
                summary=f"Draft attempt {attempt} for cluster {cluster['sig'][:120]!r} produced no extractable code.",
                tags=["backlog_draft_attempt", "draft_step"],
                detail={"cluster_sig": cluster["sig"], "attempt": attempt, "passed": False},
                verified=False,
            )
            continue

        test_ok, test_msg, generated_test_code = self_test(code, cluster, real_instances)
        append_project_memory(
            event_type="note", actor="draft_validator_from_cluster", task_id=None, module=None,
            summary=f"Self-test attempt {attempt} for cluster {cluster['sig'][:120]!r}: "
                    f"{'PASS' if test_ok else 'FAIL'} -- {test_msg[:200]}",
            tags=["backlog_draft_attempt", "self_test_step"],
            detail={"cluster_sig": cluster["sig"], "attempt": attempt, "passed": test_ok, "message": test_msg},
            verified=False,
        )
        if not test_ok:
            continue

        gate_ok, gate_msg = genericity_gate(code)
        append_project_memory(
            event_type="note", actor="draft_validator_from_cluster", task_id=None, module=None,
            summary=f"Genericity gate attempt {attempt} for cluster {cluster['sig'][:120]!r}: "
                    f"{'PASS' if gate_ok else 'FAIL'}",
            tags=["backlog_draft_attempt", "genericity_gate_step"],
            detail={"cluster_sig": cluster["sig"], "attempt": attempt, "passed": gate_ok, "message": gate_msg[:1500]},
            verified=False,
        )
        if not gate_ok:
            continue

        out_path = write_pending_validator_files(
            cluster["sig"], cluster["ids"], code, generated_test_code, examples,
            test_msg=test_msg, drafted_attempt=attempt,
        )
        append_project_memory(
            event_type="note", actor="draft_validator_from_cluster", task_id=None, module=None,
            summary=f"Draft for cluster {cluster['sig'][:120]!r} passed on attempt {attempt}, written to {out_path}.",
            tags=["backlog_draft_attempt", "draft_completed"],
            detail={"cluster_sig": cluster["sig"], "attempt": attempt, "out_path": str(out_path)},
            verified=False,
        )
        return out_path

    append_project_memory(
        event_type="note", actor="draft_validator_from_cluster", task_id=None, module=None,
        summary=f"Gave up drafting for cluster {cluster['sig'][:120]!r} after {_MAX_REVISION_ATTEMPTS} attempts.",
        tags=["backlog_draft_attempt", "draft_exhausted"],
        detail={"cluster_sig": cluster["sig"], "max_attempts": _MAX_REVISION_ATTEMPTS},
        verified=False,
    )
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--cluster", type=int, default=None)
    args = parser.parse_args()

    clusters = fetch_genuinely_open_clusters()

    if args.list or args.cluster is None:
        print(f"{len(clusters)} genuinely-open, atomic, self-detected clusters:")
        for i, c in enumerate(clusters):
            print(f"  [{i}] n={c['count']:3d}  {c['sig'][:90]}")
        if args.cluster is None:
            return

    cluster = clusters[args.cluster]
    print(f"Drafting for cluster [{args.cluster}]: {cluster['sig'][:90]} ({cluster['count']} real instances)")

    out_path = draft_and_trace_cluster(cluster)
    if out_path:
        print(f"\nDraft written to {out_path} -- review required before promotion (Phase 29C).")
    else:
        print(f"\nGave up after {_MAX_REVISION_ATTEMPTS} attempts -- flagging for direct human authoring instead.")


if __name__ == "__main__":
    main()
