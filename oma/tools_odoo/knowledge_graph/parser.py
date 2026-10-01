"""Stage A parser: reads one Odoo module's real source with `ast`/`csv`/`ElementTree`
and produces the structured record described in
docs/architecture/ODOO_KNOWLEDGE_PIPELINE_PILOT_RESULTS_2026-07-17.md §11.5.

No network access, no LLM call, no dependency on any other module in this package
except the standard library. Anything that can't be resolved with confidence from the
declarative syntax is emitted under `needs_llm_review` instead of guessed -- per the
explicit instruction this must never fabricate a fact it can't see in the source.
"""

from __future__ import annotations

import ast
import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

# Real Odoo field classes (fields.py in Odoo 16 core) -- used only to recognize a
# `fields.X(...)` call as a field declaration, not to validate exhaustively.
_FIELD_TYPES = {
    "Char", "Text", "Html", "Boolean", "Integer", "Float", "Monetary", "Date",
    "Datetime", "Binary", "Selection", "Many2one", "One2many", "Many2many",
    "Reference", "Json", "Image", "Properties", "PropertiesDefinition",
}

_RELATIONAL_TYPES = {"Many2one", "One2many", "Many2many", "Reference"}


@dataclass
class Review:
    reason: str
    snippet: str
    model: str | None = None

    def to_dict(self) -> dict:
        out = {"reason": self.reason, "snippet": self.snippet}
        if self.model:
            out["model"] = self.model
        return out


@dataclass
class ModuleRecord:
    module: str
    deps: list = field(default_factory=list)
    models: list = field(default_factory=list)
    extends: list = field(default_factory=list)
    views_extend: list = field(default_factory=list)
    # §0.9a: every <record model="ir.ui.view"> in views/*.xml with NO inherit_id
    # attribute -- i.e. a primary/base view declaration. views_extend only ever
    # captured *inherited* views, so a primary view (the self-inheriting-view
    # collision incident's actual shape) was structurally invisible until this
    # field existed. Entries: {"view": <external id>, "model": <view's own "model"
    # field>, "field_refs": [<field name>, ...]} -- field_refs per §0.9b below.
    views_primary: list = field(default_factory=list)
    security: list = field(default_factory=list)
    needs_llm_review: list = field(default_factory=list)
    # model_name -> sorted list of real view types (form/list/kanban/calendar/...)
    # this module defines a *primary* (non-inherited) <record model="ir.ui.view">
    # for. Additive field, §11.12 Step B -- answers "which view types actually exist
    # for this model" per Operator's own example (2026-07-20 meeting).
    view_types: dict = field(default_factory=dict)
    # Every real model this module re-opens via `_inherit`, regardless of whether
    # new fields were added -- fixes a real completeness gap found via §11.9
    # validation (2026-07-21): `extends` only records field-level additions, so a
    # class that reopens a model purely for method overrides (zero new fields) was
    # previously invisible everywhere. `reopens` is the superset; every entry in
    # `extends` also has a corresponding entry here, plus the field-less ones.
    reopens: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "module": self.module,
            "deps": self.deps,
            "models": self.models,
            "extends": self.extends,
            "views_extend": self.views_extend,
            "views_primary": self.views_primary,
            "security": self.security,
            "needs_llm_review": [
                r.to_dict() if isinstance(r, Review) else r for r in self.needs_llm_review
            ],
            "view_types": self.view_types,
            "reopens": self.reopens,
        }


# --------------------------------------------------------------------------- helpers


def _literal_str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _source_segment(source: str, node: ast.AST) -> str:
    seg = ast.get_source_segment(source, node)
    if seg is None:
        return ""
    # keep review snippets short and single-purpose
    return seg.strip()[:400]


def _is_fields_call(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "fields":
        if func.attr in _FIELD_TYPES:
            return func.attr
    return None


def _kwargs_by_name(call: ast.Call) -> dict:
    return {kw.arg: kw.value for kw in call.keywords if kw.arg is not None}


def _extract_depends_decorator(method: ast.FunctionDef) -> list | None:
    for dec in method.decorator_list:
        if not isinstance(dec, ast.Call):
            continue
        f = dec.func
        if isinstance(f, ast.Attribute) and f.attr == "depends" and isinstance(f.value, ast.Name) and f.value.id == "api":
            args = [_literal_str(a) for a in dec.args]
            if all(a is not None for a in args):
                return args
            return None  # non-literal depends() arg -- can't trust it
    return None


def _class_name_and_inherit(class_node: ast.ClassDef, source: str) -> tuple[str | None, object, Review | None]:
    """Returns (name, inherit, review). `inherit` is None, a str, or a list[str].
    A non-None `review` means _inherit was present but not staticly resolvable
    (dynamic value) -- name/inherit returned are best-effort only in that case.
    """
    name: str | None = None
    inherit: object = None
    review: Review | None = None
    for stmt in class_node.body:
        if not (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name)):
            continue
        target = stmt.targets[0].id
        if target == "_name":
            val = _literal_str(stmt.value)
            if val is None:
                review = Review(
                    f"class {class_node.name}: _name is not a string literal, cannot resolve statically",
                    _source_segment(source, stmt),
                )
            else:
                name = val
        elif target == "_inherit":
            if isinstance(stmt.value, ast.Constant):
                val = _literal_str(stmt.value)
                if val is None:
                    review = Review(
                        f"class {class_node.name}: _inherit constant is not a string",
                        _source_segment(source, stmt),
                    )
                else:
                    inherit = val
            elif isinstance(stmt.value, (ast.List, ast.Tuple)):
                items = [_literal_str(e) for e in stmt.value.elts]
                if all(i is not None for i in items):
                    inherit = items
                else:
                    review = Review(
                        f"class {class_node.name}: _inherit list contains a non-literal entry, cannot resolve statically",
                        _source_segment(source, stmt),
                    )
            else:
                review = Review(
                    f"class {class_node.name}: _inherit is dynamically constructed ({type(stmt.value).__name__}), cannot resolve statically",
                    _source_segment(source, stmt),
                )
    return name, inherit, review


def _method_map(class_node: ast.ClassDef) -> dict:
    out = {}
    for stmt in class_node.body:
        if isinstance(stmt, ast.FunctionDef):
            out[stmt.name] = stmt
    return out


def _extract_field(
    field_name: str, call: ast.Call, ftype: str, methods: dict, source: str
) -> tuple[dict | None, Review | None]:
    info: dict = {"name": field_name, "type": ftype}
    kw = _kwargs_by_name(call)
    review: Review | None = None

    comodel = None
    if ftype in _RELATIONAL_TYPES and call.args:
        comodel = _literal_str(call.args[0])
    if "comodel_name" in kw:
        val = _literal_str(kw["comodel_name"])
        comodel = val if val is not None else comodel
    if ftype in _RELATIONAL_TYPES and comodel is None and ("comodel_name" in kw or call.args):
        # relational field whose target couldn't be resolved to a literal string
        review = Review(
            f"field '{field_name}': relational comodel is not a string literal, cannot resolve statically",
            _source_segment(source, call),
        )
    if comodel:
        info["comodel"] = comodel

    if "required" in kw and isinstance(kw["required"], ast.Constant) and kw["required"].value is True:
        info["required"] = True

    if "compute" in kw:
        compute_name = _literal_str(kw["compute"])
        if compute_name is None:
            info["computed"] = True
            review = review or Review(
                f"field '{field_name}': compute= value is not a string literal",
                _source_segment(source, call),
            )
        else:
            info["computed"] = True
            method = methods.get(compute_name)
            if method is None:
                review = review or Review(
                    f"field '{field_name}': compute method '{compute_name}' not found in this class "
                    "(may be inherited/defined elsewhere -- dependencies unknown)",
                    _source_segment(source, call),
                )
            else:
                deps = _extract_depends_decorator(method)
                if deps is None:
                    review = review or Review(
                        f"field '{field_name}': compute method '{compute_name}' has no resolvable "
                        "@api.depends(...) decorator",
                        _source_segment(source, method),
                    )
                else:
                    info["depends"] = deps

    return info, review


def _scan_dynamic_attr_access(class_node: ast.ClassDef, source: str) -> list[Review]:
    """Flags getattr()/setattr() calls whose attribute-name argument is not a
    string literal -- a parser cannot know which field/attribute is actually
    being touched, so this must not be guessed.
    """
    reviews: list[Review] = []
    for node in ast.walk(class_node):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("getattr", "setattr"):
            if len(node.args) >= 2 and _literal_str(node.args[1]) is None:
                reviews.append(
                    Review(
                        f"class {class_node.name}: dynamic {node.func.id}() call with a non-literal "
                        "attribute name -- cannot statically resolve which field/attribute is touched",
                        _source_segment(source, node),
                    )
                )
    return reviews


# --------------------------------------------------------------------------- models/*.py


def parse_models_dir(models_dir: Path) -> tuple[list[dict], list[dict], list[Review], list[str]]:
    """Returns (models, extends, needs_llm_review, reopens) aggregated across every
    .py file directly under `models_dir` (Odoo's own convention: model classes live
    in `models/*.py`, one or more classes per file). `reopens` is the deduplicated
    list of every real model this module's classes re-open via `_inherit`, regardless
    of whether new fields were added -- see the real gap this fixes at the call site
    below, found via §11.9 validation.
    """
    models: dict[str, dict] = {}
    extends: list[dict] = []
    reviews: list[Review] = []
    reopens: list[str] = []

    if not models_dir.is_dir():
        return [], [], [], []

    for py_file in sorted(models_dir.glob("*.py")):
        if py_file.name == "__init__.py":
            continue
        source = py_file.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError as exc:
            reviews.append(Review(f"file {py_file.name}: failed to parse ({exc})", ""))
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            # only classes that look like Odoo models (inherit models.Model/TransientModel/AbstractModel)
            base_names = [
                b.attr if isinstance(b, ast.Attribute) else getattr(b, "id", None) for b in node.bases
            ]
            if not any(b in ("Model", "TransientModel", "AbstractModel") for b in base_names):
                continue

            name, inherit, class_review = _class_name_and_inherit(node, source)

            # Best-effort model label to attach to every Review raised while processing
            # this class -- without it, a downstream LLM reviewing the flag has no way
            # to know which model a flagged field/method actually belongs to (a real gap
            # found in practice: most Stage B review calls came back STILL_UNCERTAIN
            # citing "the model name is not specified"). Prefer the class's own _name,
            # fall back to its _inherit target, fall back to the raw class name.
            if name:
                model_label = name
            elif isinstance(inherit, str):
                model_label = inherit
            elif isinstance(inherit, list) and inherit:
                model_label = inherit[0]
            else:
                model_label = node.name

            if class_review:
                class_review.model = model_label
                reviews.append(class_review)

            methods = _method_map(node)
            dynamic_reviews = _scan_dynamic_attr_access(node, source)
            for r in dynamic_reviews:
                r.model = model_label
            reviews.extend(dynamic_reviews)

            field_infos: list[dict] = []
            for stmt in node.body:
                if not (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name)):
                    continue
                if not isinstance(stmt.value, ast.Call):
                    continue
                ftype = _is_fields_call(stmt.value)
                if ftype is None:
                    continue
                info, review = _extract_field(stmt.targets[0].id, stmt.value, ftype, methods, source)
                if info:
                    field_infos.append(info)
                if review:
                    review.model = model_label
                    reviews.append(review)

            if name is not None:
                inherits_list = inherit if isinstance(inherit, list) else ([inherit] if inherit else [])
                if name in models:
                    models[name]["fields"].extend(field_infos)
                    for m in inherits_list:
                        if m not in models[name].get("inherits", []):
                            models[name].setdefault("inherits", []).append(m)
                else:
                    entry = {"name": name, "fields": field_infos}
                    if inherits_list:
                        entry["inherits"] = inherits_list
                    models[name] = entry
            elif isinstance(inherit, str):
                for f_info in field_infos:
                    extends.append({"model": inherit, **f_info})
                # Real gap found via §11.9 validation (2026-07-21): a class that
                # reopens a model purely for method overrides (no new fields at all,
                # e.g. mass_mailing_crm's `_inherit = 'crm.lead'` / `_mailing_enabled
                # = True`) previously produced NO record of the relationship at all,
                # since this loop only ran when field_infos was non-empty. `reopens`
                # records every real _inherit relationship regardless of whether
                # fields were added, so the relationship itself is never silently lost.
                reopens.append(inherit)
            elif isinstance(inherit, list):
                for target in inherit:
                    for f_info in field_infos:
                        extends.append({"model": target, **f_info})
                    reopens.append(target)
            elif class_review is None:
                reviews.append(
                    Review(
                        f"class {node.name}: no resolvable _name or _inherit -- cannot classify as new "
                        "model or extension",
                        _source_segment(source, node),
                    )
                )

    deduped_reopens = sorted(set(reopens))
    return list(models.values()), extends, reviews, deduped_reopens


# --------------------------------------------------------------------------- __manifest__.py


def parse_manifest(manifest_path: Path) -> tuple[list[str], Review | None]:
    if not manifest_path.is_file():
        return [], Review("__manifest__.py not found", "")
    source = manifest_path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(source, filename=str(manifest_path))
    except SyntaxError as exc:
        return [], Review(f"__manifest__.py failed to parse ({exc})", "")

    # Manifest is a single dict literal expression statement.
    dict_node = None
    for stmt in tree.body:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Dict):
            dict_node = stmt.value
            break
    if dict_node is None:
        return [], Review("__manifest__.py does not contain a top-level dict literal", "")

    for key_node, val_node in zip(dict_node.keys, dict_node.values):
        if _literal_str(key_node) == "depends":
            try:
                val = ast.literal_eval(val_node)
            except (ValueError, SyntaxError):
                return [], Review(
                    "manifest 'depends' value is not a static literal, cannot resolve",
                    _source_segment(source, val_node),
                )
            if isinstance(val, (list, tuple)) and all(isinstance(v, str) for v in val):
                return list(val), None
            return [], Review("manifest 'depends' is not a list of string literals", _source_segment(source, val_node))

    return [], None  # no depends key at all -- valid (e.g. the root `base` module)


# --------------------------------------------------------------------------- views/*.xml


# Real Odoo <arch> root tag -> the compact type name used in the VIEWS: facet.
# "tree" is Odoo's internal XML tag name; renamed to "list" here to match the terms
# a human/agent actually uses (Operator's own words: "mostly only form views, not list
# views") -- everything else is the real, unmodified XML tag name.
_VIEW_TAG_TO_TYPE = {
    "form": "form", "tree": "list", "kanban": "kanban", "calendar": "calendar",
    "graph": "graph", "pivot": "pivot", "search": "search", "activity": "activity",
    "gantt": "gantt", "cohort": "cohort", "map": "map", "qweb": "qweb",
}


def _collect_field_refs(arch_field: ElementTree.Element) -> list[str]:
    """§0.9b: walks the parsed `<field name="arch">` element's subtree (already
    loaded to determine view_type) and collects every `<field name="...">`
    element's `name` attribute, at any nesting depth (group/notebook/tree/kanban
    templates all use the same tag) -- excludes the outer arch container field
    itself, which is not a view-body field reference.
    """
    refs: list[str] = []
    for el in arch_field.iter("field"):
        if el is arch_field:
            continue
        name = el.get("name")
        if name:
            refs.append(name)
    return refs


def parse_views_dir(views_dir: Path) -> tuple[list[dict], list[dict], list[Review], dict]:
    """Returns (views_extend, views_primary, reviews, view_types). `view_types` maps
    model name -> sorted list of real view types found in a PRIMARY (non-inherited)
    view record for that model -- this is what answers "which view types exist for
    this model," distinct from `views_extend`'s inherited-view-chain edges.

    `views_primary` (§0.9a) is every <record model="ir.ui.view"> with NO inherit_id
    attribute: [{"view": <external id>, "model": <view's own "model" field>,
    "field_refs": [...]}, ...]. Both `views_primary` and `views_extend` entries carry
    `field_refs` (§0.9b): every <field name="..."> found anywhere in that view's own
    parsed arch subtree, regardless of nesting depth.
    """
    if not views_dir.is_dir():
        return [], [], [], {}
    out: list[dict] = []
    primary: list[dict] = []
    reviews: list[Review] = []
    view_types: dict[str, set] = {}
    for xml_file in sorted(views_dir.glob("*.xml")):
        try:
            root = ElementTree.parse(xml_file).getroot()
        except ElementTree.ParseError as exc:
            reviews.append(Review(f"views/{xml_file.name}: malformed XML, not parsed ({exc})", ""))
            continue
        for record in root.iter("record"):
            if record.get("model") != "ir.ui.view":
                continue
            inherit_id = None
            target_model = None
            arch_field = None
            for f in record.findall("field"):
                if f.get("name") == "inherit_id":
                    inherit_id = f.get("ref")
                elif f.get("name") == "model":
                    target_model = (f.text or "").strip()
                elif f.get("name") == "arch":
                    arch_field = f

            field_refs = _collect_field_refs(arch_field) if arch_field is not None else []

            if inherit_id:
                out.append({
                    "view": record.get("id", "<unknown>"),
                    "inherit_id": inherit_id,
                    "field_refs": field_refs,
                })
                continue  # an inherited view extends an existing view's arch, it doesn't
                          # define a new primary view type for the model on its own.

            if not target_model:
                continue
            primary.append({
                "view": record.get("id", "<unknown>"),
                "model": target_model,
                "field_refs": field_refs,
            })

            if arch_field is None:
                continue
            children = list(arch_field)
            if not children:
                continue
            root_tag = children[0].tag
            view_type = _VIEW_TAG_TO_TYPE.get(root_tag)
            if view_type is None:
                reviews.append(Review(
                    f"view '{record.get('id', '<unknown>')}': unrecognized arch root tag "
                    f"'{root_tag}', cannot classify view type",
                    "", model=target_model,
                ))
                continue
            view_types.setdefault(target_model, set()).add(view_type)

    return out, primary, reviews, {m: sorted(v) for m, v in view_types.items()}


# --------------------------------------------------------------------------- security/*


def parse_security_csv(security_dir: Path) -> list[dict]:
    if not security_dir.is_dir():
        return []
    out: list[dict] = []
    for csv_file in sorted(security_dir.glob("*.csv")):
        with csv_file.open(newline="", encoding="utf-8", errors="replace") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                model_id = (row.get("model_id:id") or "").strip()
                if not model_id:
                    continue
                # Odoo convention: model_id:id is "model_<model_name_with_underscores>",
                # optionally module-prefixed ("module.model_res_partner").
                local = model_id.split(".")[-1]
                model_name = local[len("model_") :].replace("_", ".") if local.startswith("model_") else local
                perms = ""
                for letter, col in (("r", "perm_read"), ("w", "perm_write"), ("c", "perm_create"), ("u", "perm_unlink")):
                    if (row.get(col) or "").strip() == "1":
                        perms += letter
                out.append(
                    {
                        "model": model_name,
                        "group": (row.get("group_id:id") or "").strip() or None,
                        "perms": perms,
                    }
                )
    return out


def parse_security_rules(security_dir: Path) -> list[Review]:
    """ir.rule domains are Python expressions evaluated at runtime (reference
    `user`, `company_id`, etc.) -- never a static fact, always flagged.
    """
    if not security_dir.is_dir():
        return []
    reviews: list[Review] = []
    for xml_file in sorted(security_dir.glob("*.xml")):
        try:
            root = ElementTree.parse(xml_file).getroot()
        except ElementTree.ParseError as exc:
            reviews.append(Review(f"security/{xml_file.name}: malformed XML, not parsed ({exc})", ""))
            continue
        for record in root.iter("record"):
            if record.get("model") != "ir.rule":
                continue
            domain = None
            model_ref = None
            for f in record.findall("field"):
                if f.get("name") in ("domain_force", "domain"):
                    domain = f.get("eval") or (f.text or "").strip()
                elif f.get("name") == "model_id":
                    ref = f.get("ref") or ""
                    local = ref.split(".")[-1]
                    model_ref = local[len("model_"):].replace("_", ".") if local.startswith("model_") else (local or None)
            reviews.append(
                Review(
                    f"ir.rule '{record.get('id', '<unknown>')}': domain is a runtime-evaluated Python "
                    "expression, not a static fact",
                    domain or "",
                    model=model_ref,
                )
            )
    return reviews


# --------------------------------------------------------------------------- top level


def parse_module(module_dir: Path) -> ModuleRecord:
    module_dir = Path(module_dir)
    module_name = module_dir.name
    rec = ModuleRecord(module=module_name)

    deps, manifest_review = parse_manifest(module_dir / "__manifest__.py")
    rec.deps = deps
    if manifest_review:
        rec.needs_llm_review.append(manifest_review)

    # Real gap found via §11.9 validation (2026-07-21): a hardcoded `models/`
    # (plural) path silently produced zero results for modules using a non-standard
    # singular `model/` directory (e.g. mis_gpeople) -- the whole module was
    # invisible to Stage A, not just one field. Odoo's own convention is `models/`;
    # this fallback only kicks in when that doesn't exist, so it can't mask the
    # normal case, only recover the non-standard one.
    models_dir = module_dir / "models"
    if not models_dir.is_dir() and (module_dir / "model").is_dir():
        models_dir = module_dir / "model"

    models, extends, model_reviews, reopens = parse_models_dir(models_dir)
    rec.models = models
    rec.extends = extends
    rec.needs_llm_review.extend(model_reviews)
    rec.reopens = reopens

    views_extend, views_primary, view_reviews, view_types = parse_views_dir(module_dir / "views")
    rec.views_extend = views_extend
    rec.views_primary = views_primary
    rec.needs_llm_review.extend(view_reviews)
    rec.view_types = view_types

    rec.security = parse_security_csv(module_dir / "security")
    rec.needs_llm_review.extend(parse_security_rules(module_dir / "security"))

    return rec


def parse_module_to_json(module_dir: Path) -> str:
    return json.dumps(parse_module(module_dir).to_dict(), indent=2, sort_keys=False)
