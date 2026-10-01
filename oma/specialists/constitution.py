"""Loads the Odoo Development Agent Constitution -- the actual standard
every specialist (Build, Code-Review, Testing/QA) checks its own work
against. This is deliberately NOT a file this project authors or copies
text from: it's the project owner's own live document, and every specialist
reads this exact path directly, every time it assembles a system prompt, so
an edit to the document takes effect immediately with no second, drifting
copy anywhere.

The path is configurable via OMA_CONSTITUTION_PATH (see paths.py), defaulting
to registry/constitution/constitution.md inside this repo. load_constitution()
raises a clear, loud ConstitutionNotFoundError rather than silently proceeding
without it or substituting placeholder text if the file is ever missing.
"""

from __future__ import annotations

from paths import CONSTITUTION_PATH


class ConstitutionNotFoundError(RuntimeError):
    """Raised when CONSTITUTION_PATH doesn't exist. Must never be caught
    and worked around with placeholder content -- if this fires, the
    real file needs to be created (or this path corrected) before any
    specialist can honestly claim to be checking its work against it.
    """


def load_odoo_development_constitution() -> str:
    if not CONSTITUTION_PATH.exists():
        raise ConstitutionNotFoundError(
            f"The Odoo Development Agent Constitution is not present at "
            f"{CONSTITUTION_PATH}. This is the project owner's own document, "
            f"not something this project can fabricate a stand-in for -- it "
            f"needs to be created at this exact path (or OMA_CONSTITUTION_PATH "
            f"updated to wherever it really lives) before any specialist can "
            f"honestly load it in full as required."
        )
    return CONSTITUTION_PATH.read_text()
