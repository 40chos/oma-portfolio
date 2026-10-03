"""Read-only, deterministic source-level introspection against the real
addons tree in the Odoo container -- used to verify claims about whether
a Python method actually exists on a real base model, instead of trusting
an LLM's unverified assertion about it either way (Build inventing a
call to a method that was never really there, or Code-Review wrongly
REJECTING a real `super().<method>()` call by claiming that method "is
not defined in the base model" when a direct grep of the real source
shows it plainly is). Odoo's own XML-RPC surface has no method-
introspection call (only field/record introspection), so this is
necessarily a source grep, not an RPC call.

Deliberately its own tiny module, not part of
`tools_odoo.module_dev.toolchain`: that module is intentionally
write-capable (scaffolding, installs, uninstalls), and
`specialists/code_review/specialist.py`'s own docstring guarantees that
specialist never imports anything write-capable, structurally, not by
promise. Every command run here is a read-only `grep`/`test -d` --
nothing in this module can mutate container or database state.

Stage 5 port: reads used to go over SSH to a second real host; in this
single-host Compose port, `docker exec` into the sibling `odoo` container
replaces that transport directly.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess

_CONTAINER_ENV = "OMA_ODOO_CONTAINER"
_CUSTOM_SITE_ADDONS_ROOT = "/mnt/extra-addons"

# Module and method names are both plain Python identifiers here --
# anything else can't be a real module/method name, so rejecting it
# up front means every value below is safe to interpolate into the
# grep pattern without any risk of shell/regex injection.
_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _run_readonly(bash_command: str, timeout: int = 15) -> subprocess.CompletedProcess | None:
    container = os.environ.get(_CONTAINER_ENV, "oma-odoo-1")
    try:
        return subprocess.run(
            ["docker", "exec", container, "bash", "-c", bash_command],
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception:
        return None


def check_real_module_defines_method(module_name: str, method_name: str) -> bool | None:
    """True/False the real SITE module (under `/opt/site/site16`, e.g.
    `project_fieldjob`) defines a Python method of this name anywhere
    in its own `.py` source. None means the lookup itself could not be
    completed (SSH/network error, module directory doesn't exist,
    malformed name) -- callers MUST treat None as "unknown, don't
    trust either way", never silently coerce it to False.
    """
    if not (module_name and method_name and _IDENTIFIER_RE.match(module_name) and _IDENTIFIER_RE.match(method_name)):
        return None
    module_dir = f"{_CUSTOM_SITE_ADDONS_ROOT}/{module_name}"
    pattern = rf"def\s+{method_name}\s*\("
    # test -d and grep are run as two separately-checked commands, not
    # `test -d X && grep ...` -- both a missing module directory and a
    # genuine "method not found" grep produce the SAME shell exit code
    # (1) when chained with `&&`, which would silently misreport "the
    # module directory doesn't even exist" as "verified: method does
    # not exist" (a false negative that could wrongly validate a
    # reviewer's own false claim instead of catching it). Kept as two
    # distinct checks so a missing module is always None, never False.
    exists_check = _run_readonly(f"test -d {shlex.quote(module_dir)}")
    if exists_check is None:
        return None
    if exists_check.returncode != 0:
        return None  # module directory doesn't exist -- unknown, not "method missing"
    grep_check = _run_readonly(
        f"grep -rlqP {shlex.quote(pattern)} {shlex.quote(module_dir)} --include=*.py"
    )
    if grep_check is None:
        return None
    if grep_check.returncode == 0:
        return True
    if grep_check.returncode == 1:
        return False
    return None  # grep itself errored (e.g. bad pattern) -- unknown, not "method missing"
