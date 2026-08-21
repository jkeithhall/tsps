"""The transactional state manager: the object agents interact with.

`TransactionalSharedState` owns the committed-file store, the AST symbol
index derived from it, file locks, the Widen-on-Ambiguity policy, and
`submit_transaction()` — the optimistic-concurrency commit pipeline that
replaces MAGIS's raw file writes and patch-concatenation merge.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Optional

from .ast_index import FileIndex, SymbolInfo, build_file_index
from .constants import (
    BASE_COMMITTER,
    REASON_ASSERTION_VIOLATION,
    REASON_LOCK_CONTENTION,
    REASON_SENSOR_FAIL,
    WIDEN_DYNAMIC_WRITE,
    WIDEN_STAR_IMPORT,
)
from .contracts import TransactionContract, TransactionResult, WideningEvent, WideningResult, _AssumptionFailure
from .paths import _DEFAULT_DB_PATH
from .sensors import SENSOR_PY_COMPILE, _normalize_obligations, _truncate, has_heavy_sensor, run_sensors
from .signatures import format_signature, signature_compatible, signature_fingerprint
from .store import _Store


class TransactionalSharedState:
    """Centralized transactional state for a single repository / task.

    Typical lifecycle:
        state = TransactionalSharedState(repo_key="django__django-123")
        state.seed(repo_dir, file_list)            # load base content + index
        result = state.submit_transaction(contract)
        if not result.accepted:
            ...  # pull latest + retry
    """

    def __init__(
        self,
        repo_key: str,
        db_path: Optional[str] = None,
        repo_dir: Optional[str] = None,
        seed_files: Optional[list] = None,
        default_sensors: Optional[list] = None,
    ) -> None:
        self.repo_key = repo_key
        self.db_path = db_path or _DEFAULT_DB_PATH
        self._store = _Store(self.db_path)
        # In-process critical-section guard (threads share this object); the
        # SQLite BEGIN IMMEDIATE guards cross-process access.
        self._lock = threading.RLock()
        self._index_cache: dict = {}
        # Sensors applied to every transaction whose contract does not specify
        # its own obligations. py_compile only by default: it is in-process and
        # deterministic. Heavier sensors (ruff/pyflakes/pytest) are opt-in per
        # contract to keep the common commit path fast and reproducible.
        self.default_sensors: list = (
            list(default_sensors) if default_sensors is not None else [SENSOR_PY_COMPILE]
        )
        if repo_dir is not None:
            self.seed(repo_dir, seed_files)

    def reset(self) -> None:
        """Clear all committed files + locks for this repo_key (fresh-run isolation).

        Lets a stable, shareable repo_key (e.g. the instance id) be reused across
        runs without a prior run's committed versions leaking into a new one.
        """
        with self._lock:
            self._store.begin_immediate()
            try:
                self._store.clear_repo(self.repo_key)
                self._store.commit()
            except Exception:
                self._store.rollback()
                raise
        self._index_cache.clear()

    def seed(self, repo_dir: str, file_list: Optional[list] = None) -> None:
        """Load base content for the given files into committed state (version 0,
        committer BASE) if not already present."""
        if file_list is None:
            file_list = []
            for root, _dirs, files in os.walk(repo_dir):
                if ".git" in root.split(os.sep):
                    continue
                for fn in files:
                    if fn.endswith(".py"):
                        rel = os.path.relpath(os.path.join(root, fn), repo_dir)
                        file_list.append(rel)
        with self._lock:
            self._store.begin_immediate()
            try:
                for rel in file_list:
                    if self._store.get_file(self.repo_key, rel) is not None:
                        continue
                    abs_path = os.path.join(repo_dir, rel)
                    try:
                        with open(abs_path, "r", encoding="utf-8") as f:
                            content = f.read()
                    except (OSError, UnicodeDecodeError):
                        continue
                    self._store.upsert_file(self.repo_key, rel, content, BASE_COMMITTER, 0)
                self._store.commit()
            except Exception:
                self._store.rollback()
                raise
        self._index_cache.clear()

    def get_committed_content(self, file_path: str) -> Optional[str]:
        row = self._store.get_file(self.repo_key, file_path)
        return row[0] if row else None

    def get_version(self, file_path: str) -> Optional[int]:
        row = self._store.get_file(self.repo_key, file_path)
        return row[2] if row else None

    def get_committer(self, file_path: str) -> Optional[str]:
        row = self._store.get_file(self.repo_key, file_path)
        return row[1] if row else None

    def index_of(self, file_path: str) -> Optional[FileIndex]:
        """Return the (cached) FileIndex for the committed version of file_path."""
        row = self._store.get_file(self.repo_key, file_path)
        if row is None:
            return None
        content, _committer, version = row
        cached = self._index_cache.get(file_path)
        if cached is not None and cached[0] == version:
            return cached[1]
        idx = build_file_index(file_path, content or "")
        self._index_cache[file_path] = (version, idx)
        return idx

    def get_symbol(self, file_path: str, qualified_name: str) -> Optional[SymbolInfo]:
        idx = self.index_of(file_path)
        if idx is None:
            return None
        return idx.symbols.get(qualified_name)

    def iter_committed(self, only_modified: bool = True):
        """Return ``[(file_path, content), ...]`` of committed files.

        With ``only_modified`` (default), skip files still at the seeded base
        (committer BASE / version 0) so callers see just the agent-authored
        changes — used by `Manager.merge_code_change()` to materialize a patch.
        """
        out = []
        for file_path, content, committer, version in self._store.list_files(self.repo_key):
            if only_modified and (committer == BASE_COMMITTER or not version):
                continue
            out.append((file_path, content))
        return out

    # Small helpers for building assumption dicts, used both by build_contract
    # below and directly by tests.
    def make_exists_assumption(self, file_path: str, qualified_name: str) -> dict:
        return {"file": file_path, "assert_symbol_exists": qualified_name}

    def make_signature_assumption(self, file_path: str, qualified_name: str) -> Optional[dict]:
        sym = self.get_symbol(file_path, qualified_name)
        if sym is None:
            return None
        if sym.kind == "class":
            # Constructor call: baseline on `__init__`'s signature, keyed under
            # the class's own name (matches `infer_import_dependencies`).
            init_sym = self.get_symbol(file_path, f"{qualified_name}.__init__")
            if init_sym is None:
                return None
            sym = init_sym
        return {
            "file": file_path,
            "symbol": qualified_name,
            "assert_signature": signature_fingerprint(sym),
        }

    def make_namespace_assumption(self, module_file: str, names=None) -> dict:
        if names is None:
            names = self.public_namespace(module_file)
        return {"file": module_file, "assert_public_namespace_superset": sorted(names)}

    def _read_local_index(self, local_repo_dir: str, mf: str, _cache: dict) -> Optional[FileIndex]:
        """Index a dependency file from the agent's own working tree, cached per call."""
        if mf in _cache:
            return _cache[mf]
        idx = None
        try:
            with open(os.path.join(local_repo_dir, mf), "r", encoding="utf-8") as fh:
                idx = build_file_index(mf, fh.read())
        except (OSError, UnicodeDecodeError):
            idx = None
        _cache[mf] = idx
        return idx

    def infer_import_dependencies(self, file_path: str, index: FileIndex, local_repo_dir: Optional[str] = None):
        """Derive ``(read_files, assumptions)`` from a file's import edges.

        For every symbol the file imports by name from a sibling repo module
        (`from M import a, b`), assert that symbol still exists (Tier 0). This is
        the in-scope cross-file semantic-conflict surface: a teammate removing or
        renaming a symbol this file imports is caught here.

        The existence assertions are derived from the *import statements of the
        agent's own code* and are NOT filtered against current committed state,
        because the import statement itself encodes the agent's belief that the
        symbol exists. Filtering against committed state would silently drop the
        exact symbol a teammate just removed - the conflict we want to catch.

        When ``local_repo_dir`` is given (the agent's working tree, after a
        `pull`), Tier-1 **signature** assertions are added for imported callables,
        baselined on the signature the agent actually SAW in its working tree (its
        read-time snapshot) — so a teammate later changing that signature
        incompatibly is caught. Reading the baseline from the agent's tree rather
        than committed state is what makes this a true read-time snapshot (the
        committed signature may advance during the agent's slow LLM calls).

        Submodule imports (`from pkg import sub` where `pkg/sub.py` exists) are
        treated as file dependencies, not symbol assertions. Attribute-level
        resolution (`user.email`) needs type inference and is out of scope.
        Star imports / dynamic access are widened separately by `widen_contract`.
        """
        read_files: list = []
        local_cache: dict = {}

        def add_read(mf):
            if mf is not None and mf != file_path and mf not in read_files:
                read_files.append(mf)

        assumptions: list = []
        for edge in index.imports:
            level = getattr(edge, "level", 0)
            # plain `import x` / `import x.y` rows: names hold module dotted paths
            if edge.module is None and level == 0:
                for name in edge.names:
                    add_read(self.resolve_module(name, file_path, 0))
                continue
            # from-import (absolute or relative `from . import x`), possibly star
            mf = self.resolve_module(edge.module, file_path, level)
            add_read(mf)
            if edge.is_star:
                continue  # handled by widen_contract (public-namespace assertion)
            for name in edge.names:
                if name == "*":
                    continue
                # Is the imported name actually a submodule file rather than a symbol?
                if edge.module:
                    submod = self.resolve_module(f"{edge.module}.{name}", file_path, level)
                else:
                    submod = self.resolve_module(name, file_path, level)
                if submod is not None:
                    add_read(submod)
                    continue
                if mf is None:
                    continue
                assumptions.append(self.make_exists_assumption(mf, name))
                # Tier-1: signature assumption from the agent's read-time snapshot.
                if local_repo_dir is not None:
                    local_idx = self._read_local_index(local_repo_dir, mf, local_cache)
                    if local_idx is not None:
                        sym = local_idx.symbols.get(name)
                        if sym is not None and sym.kind in ("function", "method") and sym.signature is not None:
                            assumptions.append(
                                {"file": mf, "symbol": name, "assert_signature": sym.signature}
                            )
                        elif sym is not None and sym.kind == "class":
                            # Constructor calls (`ClassName(...)`) resolve to `__init__`,
                            # which is indexed as an ordinary method under the dotted
                            # name. Baseline on it, but key the assumption under the
                            # class's own name so it matches how the call site actually
                            # references it (and so rejection rendering / stale-line
                            # search work unchanged).
                            init_sym = local_idx.symbols.get(f"{name}.__init__")
                            if init_sym is not None and init_sym.signature is not None:
                                assumptions.append(
                                    {"file": mf, "symbol": name, "assert_signature": init_sym.signature}
                                )
        return read_files, assumptions

    def build_contract(self, agent_id: str, file_path: str, new_content: str, *,
                       local_repo_dir: Optional[str] = None, widen: bool = True) -> TransactionContract:
        """Construct a `TransactionContract` for an agent's single-file write.

        Derives the read set + AST assumptions from the imports of the *proposed*
        content, then (by default) applies the Widen-on-Ambiguity policy. This is
        the entry point the write-interception in `Member.work()` uses. Pass
        ``local_repo_dir`` (the agent's working tree) to baseline Tier-1 signature
        assertions on the agent's read-time snapshot.
        """
        index = build_file_index(file_path, new_content)
        read_files, assumptions = self.infer_import_dependencies(file_path, index, local_repo_dir=local_repo_dir)
        contract = TransactionContract(
            agent_id=agent_id,
            read_set=read_files,
            write_set=[file_path],
            assumptions=assumptions,
            verifier_obligations=[{"kind": k} for k in self.default_sensors],
            new_contents={file_path: new_content},
            repo_dir=local_repo_dir,
        )
        contract.widening = self.widen_contract(contract) if widen else WideningResult()
        return contract

    # Pull / rebase: project committed state onto an agent's working tree.
    def compute_pull_scope(self, files: list) -> list:
        """Files to refresh in an agent's working tree: the given files plus their
        committed dependency-graph neighbors (Tier-2 import edges), so the
        agent's `git diff` stays scoped to its own file_path."""
        scope: list = []

        def add(f):
            if f and f not in scope:
                scope.append(f)

        for f in files:
            add(f)
            idx = self.index_of(f)
            if idx is None:
                continue
            deps, _ = self.infer_import_dependencies(f, idx)
            for d in deps:
                add(d)
        return scope

    def pull(self, target_repo_dir: str, files: list) -> list:
        """Project committed content for ``files`` (+ committed dep neighbors) onto
        the working tree at ``target_repo_dir``.

        This is the disk-projection half of abort-and-rebase: a rejected agent
        reconciles its working tree to the committed snapshot (its own discarded
        write resets to committed state; changed dependencies appear) before it
        regenerates. Files with no committed entry (e.g. a brand-new file the agent
        is creating) are left untouched. Returns the list of files actually
        refreshed.
        """
        refreshed: list = []
        for f in self.compute_pull_scope(files):
            content = self.get_committed_content(f)
            if content is None:
                continue
            abs_path = os.path.join(target_repo_dir, f)
            try:
                parent = os.path.dirname(abs_path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(abs_path, "w", encoding="utf-8") as fh:
                    fh.write(content)
                refreshed.append(f)
            except OSError:
                continue
        return refreshed

    # --- Widen-on-Ambiguity policy ---
    def public_namespace(self, file_path: str) -> set:
        idx = self.index_of(file_path)
        return idx.public_namespace() if idx is not None else set()

    def resolve_module(self, module: Optional[str], importing_file: Optional[str], level: int = 0) -> Optional[str]:
        """Best-effort map of an import target to a committed repo file path.

        Tries package layouts (`a/b.py`, `a/b/__init__.py`), both repo-root- and
        importing-file-relative, plus relative imports via `level`. Returns the
        first candidate that exists in committed state, else None.
        """
        candidates: list = []

        def _add(base_parts, mod):
            parts = mod.split(".") if mod else []
            if base_parts or parts:
                joined = os.path.join(*(base_parts + parts)) if (base_parts + parts) else ""
                candidates.append(joined + ".py")
                candidates.append(os.path.join(joined, "__init__.py") if joined else "__init__.py")
            else:
                candidates.append("__init__.py")

        if level and importing_file:
            base = os.path.dirname(importing_file)
            for _ in range(level - 1):
                base = os.path.dirname(base)
            base_parts = [base] if base else []
            _add(base_parts, module)
        if module:
            _add([], module)
            if importing_file:
                base = os.path.dirname(importing_file)
                _add([base] if base else [], module)

        for cand in candidates:
            norm = os.path.normpath(cand)
            if self._store.get_file(self.repo_key, norm) is not None:
                return norm
        return None

    def compute_widening(
        self,
        source_file: str,
        source_index: FileIndex,
        *,
        max_star_depth: int = 2,
        lock_dynamic_writes: bool = True,
        dynamic_read_scope: str = "imports",
    ) -> WideningResult:
        """Apply the Widen-on-Ambiguity policy to ONE file's index.

        Asymmetry: ambiguous reads (`import *`, getattr/dir, `__dict__` loops)
        add lock-free public-namespace assertions; ambiguous writes (setattr/
        delattr) escalate the exclusive lock to the resolved target module only.
        """
        result = WideningResult()
        widened_modules: set = set()  # dedupe assertions + guard star recursion

        def assert_namespace(module_file: str) -> int:
            names = self.public_namespace(module_file)
            if module_file not in widened_modules:
                widened_modules.add(module_file)
                if module_file not in result.added_read_files:
                    result.added_read_files.append(module_file)
                result.added_read_assumptions.append(self.make_namespace_assumption(module_file, names))
            return len(names)

        # 1. wildcard imports (with bounded transitive expansion)
        def expand_star(importing_file: str, idx: FileIndex, depth: int) -> None:
            for edge in idx.imports:
                if not edge.is_star:
                    continue
                mod_file = self.resolve_module(edge.module, importing_file, getattr(edge, "level", 0))
                label = edge.module or "."
                if mod_file is None:
                    result.events.append(
                        WideningEvent(
                            WIDEN_STAR_IMPORT, importing_file, edge.lineno, edge.module, None,
                            f"`from {label} import *` could not be resolved to a repo file; read set not widened.",
                        )
                    )
                    continue
                already = mod_file in widened_modules
                n = assert_namespace(mod_file)
                if not already:
                    result.events.append(
                        WideningEvent(
                            WIDEN_STAR_IMPORT, importing_file, edge.lineno, edge.module, mod_file,
                            f"`from {label} import *` -> widened read set to {n} public name(s) of `{mod_file}`.",
                        )
                    )
                    if depth < max_star_depth:
                        child = self.index_of(mod_file)
                        if child is not None:
                            expand_star(mod_file, child, depth + 1)

        expand_star(source_file, source_index, 1)

        # resolvable imported modules of the source file (for dynamic-read widening)
        imported_files: list = []
        for edge in source_index.imports:
            if edge.module:
                mf = self.resolve_module(edge.module, source_file, getattr(edge, "level", 0))
                if mf is not None and mf not in imported_files:
                    imported_files.append(mf)

        # 2. dynamic access
        for kind, lineno, target in source_index.dynamic_access:
            if kind == WIDEN_DYNAMIC_WRITE:
                target_file = self.resolve_module(target, source_file) if target else None
                if lock_dynamic_writes and target_file is not None and target_file != source_file:
                    if target_file not in result.added_write_locks:
                        result.added_write_locks.append(target_file)
                    result.events.append(
                        WideningEvent(
                            WIDEN_DYNAMIC_WRITE, source_file, lineno, target, target_file,
                            f"dynamic write to `{target}` -> escalated write lock to module `{target_file}`.",
                        )
                    )
                else:
                    result.events.append(
                        WideningEvent(
                            WIDEN_DYNAMIC_WRITE, source_file, lineno, target, None,
                            f"dynamic write at line {lineno} (target `{target}`) unresolved; recorded, no extra lock.",
                        )
                    )
            else:  # WIDEN_GETATTR / WIDEN_ATTR_LOOP — lock-free read widening
                tf = self.resolve_module(target, source_file) if target else None
                targets = [tf] if tf is not None else (list(imported_files) if dynamic_read_scope == "imports" else [])
                for mf in targets:
                    assert_namespace(mf)
                result.events.append(
                    WideningEvent(
                        kind, source_file, lineno, target, targets[0] if targets else None,
                        f"dynamic read ({kind}) at line {lineno} -> widened read assertions to "
                        f"{len(targets)} module(s).",
                    )
                )

        return result

    def widen_contract(
        self,
        contract: TransactionContract,
        *,
        max_star_depth: int = 2,
        lock_dynamic_writes: bool = True,
        dynamic_read_scope: str = "imports",
        apply: bool = True,
    ) -> WideningResult:
        """Compute (and by default apply) widening for an agent's contract.

        Analyzes each write-set file using its proposed `new_contents` (the code
        the agent authored), falling back to committed content. When `apply` is
        True, merges added read files/assumptions and write locks into the
        contract in place. Returns the aggregate WideningResult for metrics.
        """
        agg = WideningResult()
        for f in contract.write_set:
            content = contract.new_contents.get(f)
            idx = build_file_index(f, content) if content is not None else self.index_of(f)
            if idx is None:
                continue
            r = self.compute_widening(
                f, idx,
                max_star_depth=max_star_depth,
                lock_dynamic_writes=lock_dynamic_writes,
                dynamic_read_scope=dynamic_read_scope,
            )
            agg.added_read_assumptions.extend(r.added_read_assumptions)
            agg.added_read_files.extend(r.added_read_files)
            agg.added_write_locks.extend(r.added_write_locks)
            agg.events.extend(r.events)

        if apply:
            for af in agg.added_read_files:
                if af not in contract.read_set:
                    contract.read_set.append(af)
            contract.assumptions.extend(agg.added_read_assumptions)
            for wl in agg.added_write_locks:
                if wl not in contract.write_set:
                    contract.write_set.append(wl)
        return agg

    def acquire_locks(self, agent_id: str, files: list) -> bool:
        """Acquire write locks for `files`. All-or-nothing; returns False (and
        acquires nothing) if any file is held by another agent."""
        with self._lock:
            self._store.begin_immediate()
            try:
                for f in files:
                    lock = self._store.get_lock(self.repo_key, f)
                    if lock is not None and lock[0] != agent_id:
                        self._store.rollback()
                        return False
                for f in files:
                    self._store.set_lock(self.repo_key, f, agent_id)
                self._store.commit()
                return True
            except Exception:
                self._store.rollback()
                raise

    def release_locks(self, agent_id: str, files: list) -> None:
        with self._lock:
            self._store.begin_immediate()
            try:
                for f in files or []:
                    lock = self._store.get_lock(self.repo_key, f)
                    if lock is not None and lock[0] == agent_id:
                        self._store.del_lock(self.repo_key, f)
                self._store.commit()
            except Exception:
                self._store.rollback()
                raise

    # --- assumption verification ---
    def _verify_assumption(self, assumption: dict):
        """Return (ok: bool, failure: _AssumptionFailure | None)."""
        file_path = assumption.get("file")
        committer = self.get_committer(file_path) if file_path else None
        idx = self.index_of(file_path) if file_path else None

        if "assert_symbol_exists" in assumption:
            sym = assumption["assert_symbol_exists"]
            if idx is not None and idx.symbol_exists(sym):
                return True, None
            return False, _AssumptionFailure(
                symbol=sym,
                committer=committer,
                message=f"Symbol `{sym}` no longer exists in `{file_path}`.",
            )

        if "assert_symbol_absent" in assumption:
            sym = assumption["assert_symbol_absent"]
            if idx is None or not idx.symbol_exists(sym):
                return True, None
            return False, _AssumptionFailure(
                symbol=sym,
                committer=committer,
                message=f"Symbol `{sym}` unexpectedly exists in `{file_path}`.",
            )

        if "assert_public_namespace_superset" in assumption:
            required = set(assumption["assert_public_namespace_superset"])
            current = idx.public_namespace() if idx is not None else set()
            missing = required - current
            if not missing:
                return True, None
            return False, _AssumptionFailure(
                symbol=sorted(missing)[0],
                committer=committer,
                message=(
                    f"Public namespace of `{file_path}` no longer exports "
                    f"{sorted(missing)} (relied on via a wildcard/dynamic read)."
                ),
            )

        if "assert_signature" in assumption:
            sym = assumption.get("symbol")
            asserted_sig = assumption["assert_signature"]
            current = idx.symbols.get(sym) if idx is not None else None
            if current is not None and current.kind == "class":
                # The assumption is baselined on this class's constructor
                # (`ClassName(...)` -> `__init__`); re-resolve to the method
                # symbol that actually carries a signature to compare against.
                current = idx.symbols.get(f"{sym}.__init__")
            if current is None:
                return False, _AssumptionFailure(
                    symbol=sym,
                    committer=committer,
                    message=f"Symbol `{sym}` no longer exists in `{file_path}`.",
                )
            if signature_compatible(asserted_sig, current.signature):
                return True, None
            return False, _AssumptionFailure(
                symbol=sym,
                committer=committer,
                message=(
                    f"Signature of `{sym}` in `{file_path}` changed incompatibly "
                    f"(now: {current.signature})."
                ),
            )

        # Unknown assertion type -> treat as satisfied but note it.
        return True, None

    def _effective_obligations(self, contract: TransactionContract) -> list:
        """Normalized sensor obligations for a contract: its own if specified,
        else this state's `default_sensors`."""
        obs = _normalize_obligations(contract.verifier_obligations)
        if not obs:
            obs = _normalize_obligations(self.default_sensors)
        return obs

    def _static_check_locked(self, contract: TransactionContract) -> Optional[TransactionResult]:
        """Lock-contention + AST-assumption check. Caller MUST hold ``self._lock``
        and an open ``BEGIN IMMEDIATE``. Returns a reject result, or None if OK.
        Does not set timing fields (the caller owns timing)."""
        for f in contract.write_set:
            lock = self._store.get_lock(self.repo_key, f)
            if lock is not None and lock[0] != contract.agent_id:
                return TransactionResult(
                    status="reject",
                    rejection_reason=REASON_LOCK_CONTENTION,
                    invalidated_by_agent=lock[0],
                    detail=f"File `{f}` is locked by `{lock[0]}`.",
                )
        for assumption in contract.assumptions:
            ok, failure = self._verify_assumption(assumption)
            if not ok:
                return TransactionResult(
                    status="reject",
                    rejection_reason=REASON_ASSERTION_VIOLATION,
                    invalidated_by_symbol=failure.symbol,
                    invalidated_by_agent=failure.committer,
                    detail=failure.message,
                )
        return None

    def _commit_locked(self, contract: TransactionContract) -> dict:
        """Apply the contract's writes and release its write-set locks. Caller
        MUST hold ``self._lock`` and an open ``BEGIN IMMEDIATE`` (caller commits)."""
        committed_versions = {}
        for f, content in contract.new_contents.items():
            prev = self._store.get_file(self.repo_key, f)
            version = (prev[2] + 1) if prev is not None else 1
            self._store.upsert_file(self.repo_key, f, content, contract.agent_id, version)
            committed_versions[f] = version
        for f in contract.write_set:
            lock = self._store.get_lock(self.repo_key, f)
            if lock is not None and lock[0] == contract.agent_id:
                self._store.del_lock(self.repo_key, f)
        return committed_versions

    def submit_transaction(self, contract: TransactionContract) -> TransactionResult:
        """Optimistic-concurrency commit gated by AST assumptions + sensors.

        Pipeline:
          1. Lock-contention check (write-set files held by another agent).
          2. AST assumption verification against current committed state.
          3. Deterministic sensors (py_compile / ruff / pyflakes / pytest).
          4. Commit new contents, bump versions, record committer, release locks.

        Cheap (in-process) sensors run inside ONE critical section with the
        lock/assumption check and commit. When a heavy (subprocess) sensor is
        present the path splits into three phases: a fail-fast lock/assumption
        check, sensors run OUTSIDE the critical section (so a slow pytest never
        holds the cross-process write lock), then a re-verify + commit critical
        section that guards against another agent committing during the sensor run.
        """
        start = time.perf_counter()
        obligations = self._effective_obligations(contract)

        # ---- fast path: only cheap, in-process sensors (or none) ----
        if not has_heavy_sensor(obligations):
            with self._lock:
                self._store.begin_immediate()
                try:
                    reject = self._static_check_locked(contract)
                    if reject is not None:
                        self._store.rollback()
                        reject.static_check_ms = (time.perf_counter() - start) * 1000.0
                        return reject

                    sensor_ms = 0.0
                    sensor_kind = None
                    if obligations:
                        s = time.perf_counter()
                        outcome = run_sensors(contract, obligations, repo_dir=contract.repo_dir)
                        sensor_ms = (time.perf_counter() - s) * 1000.0
                        if not outcome.passed:
                            self._store.rollback()
                            return TransactionResult(
                                status="reject",
                                rejection_reason=REASON_SENSOR_FAIL,
                                detail=outcome.summary(),
                                sensor_kind=outcome.failed_kind,
                                static_check_ms=(time.perf_counter() - start) * 1000.0 - sensor_ms,
                                sensor_check_ms=sensor_ms,
                            )
                        sensor_kind = ",".join(outcome.ran) or None

                    committed_versions = self._commit_locked(contract)
                    self._store.commit()
                except Exception:
                    self._store.rollback()
                    raise
            for f in contract.new_contents:
                self._index_cache.pop(f, None)
            return TransactionResult(
                status="commit",
                committed_versions=committed_versions,
                sensor_kind=sensor_kind,
                static_check_ms=(time.perf_counter() - start) * 1000.0 - sensor_ms,
                sensor_check_ms=sensor_ms,
            )

        # ---- heavy path: fail-fast check -> sensors (no lock) -> re-check + commit ----
        # Phase 1: fail-fast lock/assumption check (avoid an expensive sensor run
        # for a transaction that is already doomed).
        with self._lock:
            self._store.begin_immediate()
            try:
                reject = self._static_check_locked(contract)
                self._store.commit()
            except Exception:
                self._store.rollback()
                raise
        if reject is not None:
            reject.static_check_ms = (time.perf_counter() - start) * 1000.0
            return reject
        static_ms = (time.perf_counter() - start) * 1000.0

        # Phase 2: sensors OUTSIDE the critical section.
        s = time.perf_counter()
        outcome = run_sensors(contract, obligations, repo_dir=contract.repo_dir)
        sensor_ms = (time.perf_counter() - s) * 1000.0
        if not outcome.passed:
            return TransactionResult(
                status="reject",
                rejection_reason=REASON_SENSOR_FAIL,
                detail=outcome.summary(),
                sensor_kind=outcome.failed_kind,
                static_check_ms=static_ms,
                sensor_check_ms=sensor_ms,
            )

        # Phase 3: re-verify (race guard) + commit.
        with self._lock:
            self._store.begin_immediate()
            try:
                reject = self._static_check_locked(contract)
                if reject is not None:
                    self._store.rollback()
                    reject.static_check_ms = static_ms
                    reject.sensor_check_ms = sensor_ms
                    return reject
                committed_versions = self._commit_locked(contract)
                self._store.commit()
            except Exception:
                self._store.rollback()
                raise
        for f in contract.new_contents:
            self._index_cache.pop(f, None)
        return TransactionResult(
            status="commit",
            committed_versions=committed_versions,
            sensor_kind=",".join(outcome.ran) or None,
            static_check_ms=static_ms,
            sensor_check_ms=sensor_ms,
        )

    # The methods below turn a structured TransactionResult into a
    # developer-framed delta without a model call: everything is read from
    # data the harness already has (the agent's rejected draft, the committed
    # ground truth, and the Tier-0/1/2 AST index). The prompt shown to the
    # agent never mentions "assertions" or raw contract JSON - just the
    # old-vs-new ground truth, who changed it, the stale line numbers in its
    # own draft, and the refreshed snippet to write against.
    def _symbol_source(self, file_path: str, qualified_name: str, limit: int = 1500) -> Optional[str]:
        """Committed source text of a symbol's definition (Tier-0 line span)."""
        sym = self.get_symbol(file_path, qualified_name)
        content = self.get_committed_content(file_path)
        if sym is None or content is None or not sym.lineno:
            return None
        lines = content.split("\n")
        start = max(sym.lineno - 1, 0)
        end = sym.end_lineno if sym.end_lineno else sym.lineno
        return _truncate("\n".join(lines[start:end]), limit)

    def _find_violated_assumption(self, contract: TransactionContract, result: TransactionResult):
        """Find the contract assumption the result invalidated; returns
        ``(file, asserted_sig)``, where asserted_sig is the agent's read-time
        signature for the symbol if it had one, else None."""
        sym = result.invalidated_by_symbol
        target_file = None
        asserted_sig = None
        for a in contract.assumptions:
            if a.get("assert_symbol_exists") == sym:
                target_file = target_file or a.get("file")
            elif a.get("assert_symbol_absent") == sym:
                target_file = target_file or a.get("file")
            elif "assert_signature" in a and a.get("symbol") == sym:
                target_file = target_file or a.get("file")
                asserted_sig = a.get("assert_signature")
            elif "assert_public_namespace_superset" in a and sym in (a.get("assert_public_namespace_superset") or []):
                target_file = target_file or a.get("file")
        return target_file, asserted_sig

    def _rename_candidates(self, file_path: str, removed_symbol: str,
                           draft_index: Optional[FileIndex], asserted_sig: Optional[dict]) -> list:
        """Best-guess replacements for a vanished symbol, labeled as a guess.

        Static AST analysis sees a symbol disappear, not a rename, so this only
        proposes a target that actually exists in the current committed state
        (never fabricated), in the same scope as the removed symbol, that the
        agent's draft doesn't already reference. If the agent had a read-time
        signature we require signature compatibility (a true callable rename);
        otherwise we match value-like symbols (no signature) so an attribute
        rename such as `User.email -> User.email_address` is caught without
        proposing unrelated methods. Returns [] unless 1-3 candidates fit.
        """
        idx = self.index_of(file_path)
        if idx is None:
            return []
        scope = removed_symbol.rsplit(".", 1)[0] if "." in removed_symbol else ""
        removed_short = removed_symbol.rsplit(".", 1)[-1]
        candidates: list = []
        for qn, sym in idx.symbols.items():
            if qn == removed_symbol:
                continue
            qscope = qn.rsplit(".", 1)[0] if "." in qn else ""
            if qscope != scope:
                continue
            short = qn.rsplit(".", 1)[-1]
            if short == removed_short:
                continue
            if draft_index is not None and draft_index.reference_lines(short):
                continue  # the agent already uses this symbol -> not a surprise rename
            if asserted_sig is not None:
                if sym.signature is None or not signature_compatible(asserted_sig, sym.signature):
                    continue
            else:
                if sym.signature is not None:
                    continue  # removed a value/attribute; don't propose a callable
            candidates.append(qn)
        candidates.sort()
        return candidates if 1 <= len(candidates) <= 3 else []

    def _stale_reference_lines(self, draft_content: str, symbol: str) -> list:
        """`(lineno, text)` pairs in the draft that still reference the invalidated symbol."""
        if not draft_content:
            return []
        short = symbol.rsplit(".", 1)[-1]
        draft_idx = build_file_index("<draft>", draft_content)
        lines = draft_content.split("\n")
        out = []
        for ln in draft_idx.reference_lines(short):
            if 1 <= ln <= len(lines):
                out.append((ln, lines[ln - 1].rstrip()))
        return out

    def render_rejection(self, contract: TransactionContract, result: TransactionResult) -> str:
        """Deterministically render a rejection into developer-facing feedback.

        Per-category templates (assertion_violation / lock_contention /
        sensor_fail). Returns a markdown string suitable for the existing
        `add_review` -> `after_review_info` channel; contains no LLM call.
        """
        if result is None or result.accepted:
            return ""
        reason = result.rejection_reason
        if reason == REASON_SENSOR_FAIL:
            return self._render_sensor_fail(result)
        if reason == REASON_LOCK_CONTENTION:
            return self._render_lock_contention(contract, result)
        if reason == REASON_ASSERTION_VIOLATION:
            return self._render_assertion_violation(contract, result)
        # Unknown reason: minimal, honest fallback.
        lines = ["## Shared Project State Update — your previous change was NOT applied"]
        if result.detail:
            lines.append(result.detail)
        lines.append("Reconcile your code with the current project state, then redo the change.")
        return "\n".join(lines)

    def _render_sensor_fail(self, result: TransactionResult) -> str:
        kind = result.sensor_kind or "a deterministic check"
        return (
            "## Your previous change did NOT pass automated verification\n"
            f"The `{kind}` check failed, so your change was not applied. "
            "Fix the problem reported below, then redo the change.\n\n"
            f"{result.detail or '(no output captured)'}"
        )

    def _render_lock_contention(self, contract: TransactionContract, result: TransactionResult) -> str:
        locked_file = contract.write_set[0] if contract.write_set else "your file"
        who = f"`{result.invalidated_by_agent}`" if result.invalidated_by_agent else "another developer"
        return (
            "## Shared Project State Update — your previous change was NOT applied\n"
            f"{who} is currently editing `{locked_file}`, which overlaps your change. "
            "Your working files have been refreshed to the latest committed version. "
            "Re-apply your change on top of the current contents of the file (shown above) "
            "and redo the change."
        )

    def _render_assertion_violation(self, contract: TransactionContract, result: TransactionResult) -> str:
        symbol = result.invalidated_by_symbol
        committer = result.invalidated_by_agent
        target_file, asserted_sig = self._find_violated_assumption(contract, result)

        lines = ["## Shared Project State Update — your previous change was NOT applied"]
        who = f"`{committer}`" if committer else "another developer"
        where = f"`{target_file}`" if target_file else "a file your code depends on"
        version = self.get_version(target_file) if target_file else None
        recency = f" (now at version {version})" if version else ""
        lines.append(f"While you were working, {who} changed {where}{recency}. Your draft "
                     f"relies on `{symbol}`, which is no longer consistent with the current code:")

        current_sym = self.get_symbol(target_file, symbol) if target_file else None
        if current_sym is not None and current_sym.kind == "class" and target_file:
            # Same class-vs-constructor redirect as _verify_assumption, so the
            # rendered "Now" signature reflects __init__, not the (signature-less)
            # class symbol itself.
            init_sym = self.get_symbol(target_file, f"{symbol}.__init__")
            if init_sym is not None:
                current_sym = init_sym
        draft_content = contract.new_contents.get(contract.write_set[0]) if contract.write_set else None
        draft_index = build_file_index("<draft>", draft_content) if draft_content else None

        if current_sym is not None and asserted_sig is not None:
            # Signature changed incompatibly (symbol still present).
            lines.append("")
            lines.append(f"- Before (what your code was written against): "
                         f"`{format_signature(symbol, asserted_sig)}`")
            lines.append(f"- Now (current ground truth): "
                         f"`{format_signature(symbol, current_sym.signature)}`")
        else:
            # Symbol removed / renamed away.
            lines.append("")
            lines.append(f"- Before: `{symbol}` existed and your code used it.")
            lines.append(f"- Now: `{symbol}` has been removed from {where}.")
            renames = self._rename_candidates(target_file, symbol, draft_index, asserted_sig) if target_file else []
            if renames:
                pretty = ", ".join(f"`{r}`" for r in renames)
                lines.append(f"- Likely renamed to {pretty} (best guess from the current code — "
                             f"verify before using).")

        stale = self._stale_reference_lines(draft_content, symbol) if draft_content else []
        if stale:
            lines.append("")
            lines.append("Your draft still uses the stale symbol at these line(s); update each:")
            for ln, text in stale:
                lines.append(f"  L{ln}: {text}")

        snippet = self._symbol_source(target_file, symbol) if target_file else None
        if snippet is None and current_sym is None:
            # Removed: if there is a single confident rename target, show its def.
            renames = self._rename_candidates(target_file, symbol, draft_index, asserted_sig) if target_file else []
            if len(renames) == 1:
                snippet = self._symbol_source(target_file, renames[0])
        if snippet:
            lines.append("")
            lines.append("Current authoritative definition to write against:")
            lines.append("```python")
            lines.append(snippet)
            lines.append("```")

        lines.append("")
        lines.append("Update your code to match the current ground truth above, then redo the change.")
        return "\n".join(lines)

    def close(self) -> None:
        self._store.close()
