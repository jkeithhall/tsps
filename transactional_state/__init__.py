"""Transactional Shared Program State (TSPS) for MAGIS.

Replaces MAGIS's patch-concatenation "merge" and natural-language conflict
handling with a centralized, programmatic state object: the repository is
tracked as an AST symbol index plus static reference edges, guarded by file
locks and verified through a transaction contract before any write commits.

The AST indexing lives in `ast_index`/`signatures`, the contract/result data
model in `contracts`, commit-time checks in `sensors`, the SQLite-backed
store in `store`, and the orchestrating `TransactionalSharedState` class in
`state`. `selftest` exercises the whole thing end to end.

This package intentionally does not import `config` at import time -
`config.py` calls `exit(1)` when `~/SWE-bench-repo` is missing, which would
make the package impossible to import or unit-test on its own. The SQLite
path defaults to a location next to the package and can be overridden.
"""

from .ast_index import FileIndex, ImportEdge, SymbolInfo, build_file_index, file_facts
from .constants import (
    BASE_COMMITTER,
    REASON_ASSERTION_VIOLATION,
    REASON_LOCK_CONTENTION,
    REASON_SENSOR_FAIL,
    WIDEN_ATTR_LOOP,
    WIDEN_DYNAMIC_WRITE,
    WIDEN_GETATTR,
    WIDEN_STAR_IMPORT,
)
from .contracts import TransactionContract, TransactionResult, WideningEvent, WideningResult
from .sensors import (
    SENSOR_PY_COMPILE,
    SENSOR_PYFLAKES,
    SENSOR_PYTEST,
    SENSOR_RUFF,
    SensorOutcome,
    has_heavy_sensor,
    run_sensors,
)
from .signatures import format_signature, signature_compatible, signature_fingerprint
from .state import TransactionalSharedState

__all__ = [
    "TransactionalSharedState",
    "TransactionContract",
    "TransactionResult",
    "WideningEvent",
    "WideningResult",
    "FileIndex",
    "ImportEdge",
    "SymbolInfo",
    "build_file_index",
    "file_facts",
    "signature_fingerprint",
    "signature_compatible",
    "format_signature",
    "SensorOutcome",
    "has_heavy_sensor",
    "run_sensors",
    "SENSOR_PY_COMPILE",
    "SENSOR_PYFLAKES",
    "SENSOR_RUFF",
    "SENSOR_PYTEST",
    "BASE_COMMITTER",
    "REASON_LOCK_CONTENTION",
    "REASON_ASSERTION_VIOLATION",
    "REASON_SENSOR_FAIL",
    "WIDEN_STAR_IMPORT",
    "WIDEN_GETATTR",
    "WIDEN_ATTR_LOOP",
    "WIDEN_DYNAMIC_WRITE",
]
