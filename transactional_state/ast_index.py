"""AST-derived symbol index for a single Python source file.

Built with the stdlib `ast` module across three tiers:
  Tier 0: symbol presence (functions, classes, methods, module/class-level
          assignments)
  Tier 1: signature compatibility (params, varargs, kwargs, decorators,
          class bases)
  Tier 2: static reference / import edges (Import / ImportFrom incl. `*`,
          and Name / Attribute loads with line numbers)
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Optional

from .constants import WIDEN_ATTR_LOOP, WIDEN_DYNAMIC_WRITE, WIDEN_GETATTR


@dataclass
class SymbolInfo:
    """A single defined symbol (Tier 0 + Tier 1)."""

    qualified_name: str
    kind: str  # 'function' | 'method' | 'class' | 'assignment'
    lineno: int
    end_lineno: Optional[int] = None
    # Tier 1 signature data (functions / methods)
    signature: Optional[dict] = None
    # Tier 1 class data
    bases: Optional[list] = None
    decorators: list = field(default_factory=list)


@dataclass
class ImportEdge:
    """A static import edge (Tier 2)."""

    module: Optional[str]  # None for plain `import x` rows where x is in names
    names: list  # imported names ('*' for wildcard)
    is_star: bool
    lineno: int
    level: int = 0  # relative-import depth (0 = absolute, 1 = `from . import`)


@dataclass
class FileIndex:
    """The AST-derived view of a single file."""

    file_path: str
    symbols: dict  # qualified_name -> SymbolInfo
    imports: list  # list[ImportEdge]
    star_imports: list  # list[str] modules pulled in via `from m import *`
    references: dict  # short identifier -> sorted list[int] of line numbers
    dynamic_access: list  # list[(kind, lineno, target)]: getattr/setattr/... patterns
    dunder_all: Optional[list] = None  # value of `__all__` if a simple literal
    has_syntax_error: bool = False
    syntax_error: Optional[str] = None

    def symbol_exists(self, qualified_name: str) -> bool:
        return qualified_name in self.symbols

    def reference_lines(self, short_name: str) -> list:
        return self.references.get(short_name, [])

    def public_namespace(self) -> set:
        """The set of names this module exports to `from m import *`.

        Uses `__all__` when it is a simple literal; otherwise falls back to
        top-level symbols whose name does not start with an underscore.
        """
        if self.dunder_all is not None:
            return set(self.dunder_all)
        names = set()
        for qn in self.symbols:
            if "." in qn:  # top-level symbols only
                continue
            if qn.startswith("_"):
                continue
            names.add(qn)
        return names


def _expr_name(node: ast.AST) -> str:
    """Best-effort dotted name for a decorator / base expression."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _expr_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return _expr_name(node.func)
    try:
        return ast.unparse(node)  # py3.9+
    except Exception:
        return node.__class__.__name__


def _func_signature(node) -> dict:
    a = node.args
    return {
        "posonly": [x.arg for x in getattr(a, "posonlyargs", [])],
        "args": [x.arg for x in a.args],
        "vararg": a.vararg.arg if a.vararg else None,
        "kwonly": [x.arg for x in a.kwonlyargs],
        "kwarg": a.kwarg.arg if a.kwarg else None,
        "num_defaults": len(a.defaults),
        "num_kw_defaults": len([d for d in a.kw_defaults if d is not None]),
    }


# Names that signal dynamic, statically-unresolvable access. Split into read vs
# write because the Widen-on-Ambiguity policy treats them asymmetrically:
# ambiguous reads widen the (lock-free) assertion set; ambiguous writes widen the
# exclusive lock.
_DYNAMIC_READ_FUNCS = {"getattr", "hasattr", "vars", "dir", "globals", "locals"}
_DYNAMIC_WRITE_FUNCS = {"setattr", "delattr"}


def _literal_str_list(node: ast.AST) -> Optional[list]:
    """Return the list of string constants if `node` is a simple list/tuple/set
    literal of strings (used to read `__all__`), else None."""
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        out = []
        for elt in node.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                out.append(elt.value)
            else:
                return None
        return out
    return None


class _Indexer(ast.NodeVisitor):
    def __init__(self) -> None:
        self.symbols: dict = {}
        self.imports: list = []
        self.star_imports: list = []
        self.references: dict = {}
        self.dynamic_access: list = []
        self.dunder_all: Optional[list] = None
        self._scope: list = []

    # --- scope helpers ---
    def _qual(self, name: str) -> str:
        return ".".join(self._scope + [name])

    def _in_class(self) -> bool:
        return bool(self._scope)

    def _add_reference(self, short_name: str, lineno: int) -> None:
        self.references.setdefault(short_name, [])
        if lineno not in self.references[short_name]:
            self.references[short_name].append(lineno)

    # --- definitions (Tier 0 + Tier 1) ---
    def _visit_function(self, node) -> None:
        kind = "method" if self._in_class() else "function"
        qual = self._qual(node.name)
        self.symbols[qual] = SymbolInfo(
            qualified_name=qual,
            kind=kind,
            lineno=node.lineno,
            end_lineno=getattr(node, "end_lineno", None),
            signature=_func_signature(node),
            decorators=[_expr_name(d) for d in node.decorator_list],
        )
        # Do NOT descend into nested function scopes for symbol indexing; locals
        # are not part of the cross-file contract surface. We still walk the body
        # for references so consuming files can be analyzed.
        self._scope.append(node.name)
        for child in ast.iter_child_nodes(node):
            # skip the args node (parameter names are not references)
            if isinstance(child, ast.arguments):
                continue
            self.visit(child)
        self._scope.pop()

    def visit_FunctionDef(self, node):  # noqa: N802
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node):  # noqa: N802
        self._visit_function(node)

    def visit_ClassDef(self, node):  # noqa: N802
        qual = self._qual(node.name)
        self.symbols[qual] = SymbolInfo(
            qualified_name=qual,
            kind="class",
            lineno=node.lineno,
            end_lineno=getattr(node, "end_lineno", None),
            bases=[_expr_name(b) for b in node.bases],
            decorators=[_expr_name(d) for d in node.decorator_list],
        )
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def _record_assignment_target(self, target: ast.AST) -> None:
        # Only module-level and class-level simple targets become contract
        # symbols (e.g. `email = Column(...)` inside `class User` -> User.email).
        if isinstance(target, ast.Name):
            qual = self._qual(target.id)
            if qual not in self.symbols:
                self.symbols[qual] = SymbolInfo(
                    qualified_name=qual,
                    kind="assignment",
                    lineno=getattr(target, "lineno", 0),
                    end_lineno=getattr(target, "end_lineno", None),
                )
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                self._record_assignment_target(elt)

    def visit_Assign(self, node):  # noqa: N802
        for target in node.targets:
            self._record_assignment_target(target)
            # Capture module-level `__all__ = [...]` for public-namespace widening.
            if isinstance(target, ast.Name) and target.id == "__all__" and not self._scope:
                literal = _literal_str_list(node.value)
                if literal is not None:
                    self.dunder_all = literal
        self.generic_visit(node)

    def visit_AnnAssign(self, node):  # noqa: N802
        if node.target is not None:
            self._record_assignment_target(node.target)
        self.generic_visit(node)

    # --- imports (Tier 2) ---
    def visit_Import(self, node):  # noqa: N802
        self.imports.append(
            ImportEdge(module=None, names=[a.name for a in node.names], is_star=False, lineno=node.lineno, level=0)
        )
        self.generic_visit(node)

    def visit_ImportFrom(self, node):  # noqa: N802
        names = [a.name for a in node.names]
        is_star = any(n == "*" for n in names)
        self.imports.append(
            ImportEdge(module=node.module, names=names, is_star=is_star, lineno=node.lineno, level=node.level or 0)
        )
        if is_star and node.module:
            self.star_imports.append(node.module)
        self.generic_visit(node)

    # --- references + dynamic access (Tier 2) ---
    def visit_Name(self, node):  # noqa: N802
        if isinstance(node.ctx, ast.Load):
            self._add_reference(node.id, node.lineno)
        self.generic_visit(node)

    def visit_Attribute(self, node):  # noqa: N802
        if isinstance(node.ctx, ast.Load):
            self._add_reference(node.attr, node.lineno)
            # Iterating an object's `__dict__` is an attribute-loop dynamic read.
            if node.attr == "__dict__":
                base = node.value.id if isinstance(node.value, ast.Name) else _expr_name(node.value)
                self.dynamic_access.append((WIDEN_ATTR_LOOP, node.lineno, base))
        self.generic_visit(node)

    def visit_Call(self, node):  # noqa: N802
        callee = _expr_name(node.func)
        short = callee.split(".")[-1]
        if short in _DYNAMIC_READ_FUNCS or short in _DYNAMIC_WRITE_FUNCS:
            target = None
            if node.args:
                first = node.args[0]
                if isinstance(first, ast.Name):
                    target = first.id
                elif isinstance(first, ast.Attribute):
                    target = _expr_name(first)
            kind = WIDEN_DYNAMIC_WRITE if short in _DYNAMIC_WRITE_FUNCS else WIDEN_GETATTR
            self.dynamic_access.append((kind, node.lineno, target))
        self.generic_visit(node)


def build_file_index(file_path: str, source: str) -> FileIndex:
    """Parse `source` and return a FileIndex. Never raises on bad syntax."""
    try:
        tree = ast.parse(source, filename=file_path)
    except SyntaxError as exc:
        return FileIndex(
            file_path=file_path,
            symbols={},
            imports=[],
            star_imports=[],
            references={},
            dynamic_access=[],
            has_syntax_error=True,
            syntax_error=str(exc),
        )
    indexer = _Indexer()
    indexer.visit(tree)
    for lst in indexer.references.values():
        lst.sort()
    return FileIndex(
        file_path=file_path,
        symbols=indexer.symbols,
        imports=indexer.imports,
        star_imports=indexer.star_imports,
        references=indexer.references,
        dynamic_access=indexer.dynamic_access,
        dunder_all=indexer.dunder_all,
    )


def file_facts(file_path: str, source: str) -> dict:
    """Serialize a file's AST/symbol facts to a JSON-friendly dict.

    This is the machine-verifiable counterpart to the Custodian's natural-language
    repository-memory notes: same input file, but symbols / signatures / imports /
    public-export namespace the harness can check, rather than prose for the LLM.
    """
    idx = build_file_index(file_path, source)
    return {
        "file": file_path,
        "has_syntax_error": idx.has_syntax_error,
        "symbols": {
            qn: {
                "kind": s.kind,
                "lineno": s.lineno,
                "end_lineno": s.end_lineno,
                "signature": s.signature,
                "bases": s.bases,
                "decorators": s.decorators,
            }
            for qn, s in idx.symbols.items()
        },
        "imports": [
            {"module": e.module, "names": e.names, "is_star": e.is_star, "level": e.level, "lineno": e.lineno}
            for e in idx.imports
        ],
        "public_namespace": sorted(idx.public_namespace()),
    }
