"""Data model for transaction contracts, their results, and widening effects."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TransactionContract:
    """Structured contract an agent submits instead of writing files directly."""

    agent_id: str
    read_set: list = field(default_factory=list)  # files examined
    write_set: list = field(default_factory=list)  # files to be modified
    assumptions: list = field(default_factory=list)  # list[dict] AST assertions
    verifier_obligations: list = field(default_factory=list)  # sensor specs (str | dict)
    new_contents: dict = field(default_factory=dict)  # file_path -> new content
    repo_dir: Optional[str] = None  # agent working tree, for disk-based sensors
    # Aggregate Widen-on-Ambiguity effect applied to this contract (set by
    # `build_contract`). Carried here so the metrics layer can record
    # widening_events_count / widening_kind per attempt without re-deriving it.
    widening: Optional["WideningResult"] = None


@dataclass
class TransactionResult:
    status: str  # 'commit' | 'reject'
    rejection_reason: Optional[str] = None
    invalidated_by_symbol: Optional[str] = None
    invalidated_by_agent: Optional[str] = None
    detail: Optional[str] = None
    committed_versions: dict = field(default_factory=dict)  # file -> new version
    static_check_ms: float = 0.0
    sensor_check_ms: float = 0.0
    # Sensor(s) involved: the comma-joined kinds that ran on a commit, or the
    # single failing kind on a `sensor_fail` reject. None when no sensor ran.
    sensor_kind: Optional[str] = None

    @property
    def accepted(self) -> bool:
        return self.status == "commit"


@dataclass
class _AssumptionFailure:
    symbol: Optional[str]
    committer: Optional[str]
    message: str


@dataclass
class WideningEvent:
    """One Widen-on-Ambiguity occurrence, recorded for instrumentation."""

    kind: str  # WIDEN_STAR_IMPORT | WIDEN_GETATTR | WIDEN_ATTR_LOOP | WIDEN_DYNAMIC_WRITE
    source_file: str  # file containing the ambiguous construct
    lineno: int
    target: Optional[str]  # module / name targeted (if any)
    resolved_file: Optional[str]  # repo file the widening applies to (if resolved)
    detail: str


@dataclass
class WideningResult:
    """The aggregate effect of applying the Widen-on-Ambiguity policy."""

    added_read_assumptions: list = field(default_factory=list)  # list[dict]
    added_read_files: list = field(default_factory=list)  # list[str]
    added_write_locks: list = field(default_factory=list)  # list[str] module files
    events: list = field(default_factory=list)  # list[WideningEvent]

    @property
    def total(self) -> int:
        return len(self.events)

    def counts(self) -> dict:
        out: dict = {}
        for ev in self.events:
            out[ev.kind] = out.get(ev.kind, 0) + 1
        return out
