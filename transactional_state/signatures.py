"""Signature-compatibility checks used by Tier 1 assumptions."""

from __future__ import annotations

from typing import Optional

from .ast_index import SymbolInfo


def signature_fingerprint(sym: SymbolInfo) -> Optional[dict]:
    """A serializable signature snapshot usable inside an assumption."""
    return sym.signature


def signature_compatible(asserted: dict, current: dict) -> bool:
    """Conservative check: can code written against `asserted` still call
    `current`?

    True when:
      * current accepts at least as many positional args (or has *args),
      * every positional / keyword name the caller may rely on is still
        accepted (present in current, or current has **kwargs), and
      * current did not add a new *required* (no-default) positional or
        keyword-only parameter beyond what `asserted` already required — an
        old caller written against `asserted` has no value to supply for it.
    This is intentionally lenient about additions that carry a default (new
    optional params are fine) and strict about removals / renames / new
    mandatory params (the conflicts we care about).
    """
    if asserted is None or current is None:
        return asserted == current

    a_pos = list(asserted.get("posonly", [])) + list(asserted.get("args", []))
    c_pos = list(current.get("posonly", [])) + list(current.get("args", []))

    if current.get("vararg") is None and len(c_pos) < len(a_pos):
        return False

    if current.get("kwarg") is None:
        c_accepts = set(c_pos) | set(current.get("kwonly", []))
        for name in a_pos:
            if name not in c_accepts:
                return False
        for name in asserted.get("kwonly", []):
            if name not in c_accepts:
                return False

    # A new required parameter (positional or keyword-only) breaks old
    # callers even though every previously-accepted name is still accepted —
    # the "leniently allow additions" rule only holds when the addition has
    # a default. Positional defaults always trail (Python syntax constraint),
    # so `len(pos) - num_defaults` exactly gives the required-positional
    # count. Keyword-only defaults can appear in any order, so we can only
    # compare aggregate required-count, not identify the specific name — a
    # deliberate, documented approximation (still catches a new mandatory
    # kwonly param added alongside existing ones).
    if current.get("vararg") is None:
        a_required_pos = max(len(a_pos) - (asserted.get("num_defaults", 0) or 0), 0)
        c_required_pos = max(len(c_pos) - (current.get("num_defaults", 0) or 0), 0)
        if c_required_pos > a_required_pos:
            return False

    if current.get("kwarg") is None:
        a_required_kwonly = len(asserted.get("kwonly", [])) - (asserted.get("num_kw_defaults", 0) or 0)
        c_required_kwonly = len(current.get("kwonly", [])) - (current.get("num_kw_defaults", 0) or 0)
        if c_required_kwonly > a_required_kwonly:
            return False

    return True


def format_signature(name: str, sig: Optional[dict]) -> str:
    """Render a signature dict (from `_func_signature`) as a readable
    `name(param, ...)` string for developer-facing rejection feedback.

    Defaults are shown as `=...` (the AST index records arity, not literal
    default values). This is approximate-but-faithful about param names and
    arity — the things that actually break a caller — and never fabricates
    types or values.
    """
    if not sig:
        return f"{name}(...)"
    parts: list = []
    posonly = list(sig.get("posonly", []))
    pos = posonly + list(sig.get("args", []))
    num_defaults = sig.get("num_defaults", 0) or 0
    first_default = len(pos) - num_defaults
    for i, p in enumerate(pos):
        token = p + "=..." if i >= first_default else p
        parts.append(token)
        if posonly and i == len(posonly) - 1:
            parts.append("/")
    if sig.get("vararg"):
        parts.append("*" + sig["vararg"])
    elif sig.get("kwonly"):
        parts.append("*")
    for kw in sig.get("kwonly", []):
        parts.append(kw)
    if sig.get("kwarg"):
        parts.append("**" + sig["kwarg"])
    return f"{name}(" + ", ".join(parts) + ")"
