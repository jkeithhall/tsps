"""Deterministic, no-LLM commit gates.

A sensor is a machine-verifiable check run before a transaction commits.
`py_compile` is in-process (instant, always available) and is the default
gate; `ruff`/`pyflakes`/`pytest` are subprocess-based, opt-in, and self-skip
when the tool is unavailable so the gate stays deterministic on machines
without them rather than failing spuriously.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Optional

from .contracts import TransactionContract

SENSOR_PY_COMPILE = "py_compile"
SENSOR_PYFLAKES = "pyflakes"
SENSOR_RUFF = "ruff"
SENSOR_PYTEST = "pytest"

# Sensors cheap enough to run inside the SQLite critical section (no subprocess,
# no risk of holding the cross-process write lock). Everything else is "heavy"
# and runs outside the lock.
_CHEAP_SENSORS = {SENSOR_PY_COMPILE}

# Cap captured sensor output so a noisy tool cannot bloat the rejection feedback.
_SENSOR_OUTPUT_LIMIT = 4000

# Default timeout for a subprocess sensor (seconds). A timeout is treated as a
# failure (the change could not be verified) to keep the gate conservative.
_SENSOR_TIMEOUT_S = 300


@dataclass
class SensorOutcome:
    """Aggregate result of running the deterministic sensor gate for one txn."""

    passed: bool = True
    failed_kind: Optional[str] = None
    output: str = ""
    ran: list = field(default_factory=list)      # sensor kinds actually executed
    skipped: list = field(default_factory=list)  # list[(kind, reason)] not run

    def summary(self) -> str:
        if self.passed:
            ran = ", ".join(self.ran) if self.ran else "none ran"
            return f"sensors passed ({ran})"
        return f"sensor `{self.failed_kind}` failed:\n{self.output}".strip()


def _truncate(text: Optional[str], limit: int = _SENSOR_OUTPUT_LIMIT) -> str:
    if not text:
        return ""
    text = text.strip()
    if len(text) > limit:
        return text[:limit] + f"\n... [truncated {len(text) - limit} chars]"
    return text


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _run_subprocess(cmd: list, cwd: Optional[str] = None, timeout: int = _SENSOR_TIMEOUT_S,
                    env: Optional[dict] = None):
    """Run a sensor subprocess. Returns (returncode, combined_output) or None if
    the executable itself is missing (tool unavailable -> sensor skipped).

    cwd is passed to subprocess.run (NOT os.chdir) so this is thread-safe: MAGIS
    members run on a thread pool, and a global chdir would race across members.
    """
    run_env = None
    if env is not None:
        run_env = dict(os.environ)
        run_env.update(env)
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout, env=run_env)
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") + (e.stderr or "")
        return (124, out + f"\n[sensor timed out after {timeout}s]")
    return (proc.returncode, (proc.stdout or "") + (proc.stderr or ""))


def _materialize_contract(contract: TransactionContract, repo_dir: str) -> None:
    """Write the contract's proposed file contents into ``repo_dir``.

    For the member flow this is idempotent (the file was already written there);
    it makes disk-based sensors (ruff/pyflakes/pytest) self-contained for any
    caller without depending on a prior on-disk write.
    """
    for path, content in contract.new_contents.items():
        abs_path = os.path.join(repo_dir, path)
        parent = os.path.dirname(abs_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)


def _py_files(contract: TransactionContract) -> list:
    return [p for p in contract.new_contents if p.endswith(".py")]


def _sensor_py_compile(contract: TransactionContract, repo_dir: Optional[str]):
    """In-process syntax gate (Python `compile`). Always available."""
    errors = []
    for path, content in contract.new_contents.items():
        if not path.endswith(".py"):
            continue
        try:
            compile(content, path, "exec")
        except SyntaxError as e:
            errors.append(f"{path}:{e.lineno}: {e.msg}")
    if errors:
        return False, "SyntaxError(s):\n" + "\n".join(errors), True
    return True, "", True


def _sensor_ruff(contract: TransactionContract, repo_dir: Optional[str]):
    if repo_dir is None or shutil.which("ruff") is None:
        return True, "", False
    files = _py_files(contract)
    if not files:
        return True, "", True
    _materialize_contract(contract, repo_dir)
    res = _run_subprocess(["ruff", "check", *files], cwd=repo_dir)
    if res is None:
        return True, "", False
    code, out = res
    return (code == 0, out, True)


def _sensor_pyflakes(contract: TransactionContract, repo_dir: Optional[str]):
    if repo_dir is None or not _module_available("pyflakes"):
        return True, "", False
    files = _py_files(contract)
    if not files:
        return True, "", True
    _materialize_contract(contract, repo_dir)
    res = _run_subprocess([sys.executable, "-m", "pyflakes", *files], cwd=repo_dir)
    if res is None:
        return True, "", False
    code, out = res
    return (code == 0, out, True)


def _sensor_pytest(contract: TransactionContract, repo_dir: Optional[str],
                   targets: list, extra_args: Optional[list], timeout: int):
    if repo_dir is None or not _module_available("pytest"):
        return True, "", False
    if not targets:
        return True, "", True
    _materialize_contract(contract, repo_dir)
    # -p no:cacheprovider + PYTHONDONTWRITEBYTECODE=1: a transaction re-runs the
    # same target file repeatedly across retries; without these, a stale `.pyc`
    # (or pytest's assertion-rewrite cache) from a prior attempt can be re-imported
    # when a rewrite lands within the same mtime-second at the same byte size,
    # making the sensor test the OLD code. Never writing bytecode guarantees the
    # proposed source is the source of truth each run.
    cmd = [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider",
           *(extra_args or []), *targets]
    res = _run_subprocess(cmd, cwd=repo_dir, timeout=timeout,
                          env={"PYTHONDONTWRITEBYTECODE": "1"})
    if res is None:
        return True, "", False
    code, out = res
    return (code == 0, out, True)


def _normalize_obligations(obligations: Optional[list]) -> list:
    """Accept sensor specs as bare strings or dicts; return normalized dicts."""
    norm: list = []
    for ob in obligations or []:
        if isinstance(ob, str):
            norm.append({"kind": ob})
        elif isinstance(ob, dict) and ob.get("kind"):
            norm.append(dict(ob))
    return norm


def has_heavy_sensor(obligations: list) -> bool:
    return any(ob.get("kind") not in _CHEAP_SENSORS for ob in obligations)


def run_sensors(contract: TransactionContract, obligations: list,
                *, repo_dir: Optional[str] = None) -> SensorOutcome:
    """Run the normalized sensor obligations in order; stop at the first failure.

    A sensor whose tool is unavailable is recorded in ``skipped`` and does NOT
    fail the gate, preserving determinism on machines lacking ruff/pyflakes/pytest.
    """
    if repo_dir is None:
        repo_dir = getattr(contract, "repo_dir", None)
    outcome = SensorOutcome(passed=True)
    for ob in obligations:
        kind = ob.get("kind")
        if kind == SENSOR_PY_COMPILE:
            passed, out, available = _sensor_py_compile(contract, repo_dir)
        elif kind == SENSOR_RUFF:
            passed, out, available = _sensor_ruff(contract, repo_dir)
        elif kind == SENSOR_PYFLAKES:
            passed, out, available = _sensor_pyflakes(contract, repo_dir)
        elif kind == SENSOR_PYTEST:
            passed, out, available = _sensor_pytest(
                contract, repo_dir, ob.get("targets", []), ob.get("args"),
                ob.get("timeout", _SENSOR_TIMEOUT_S),
            )
        else:
            outcome.skipped.append((str(kind), "unknown sensor kind"))
            continue
        if not available:
            outcome.skipped.append((kind, "tool unavailable"))
            continue
        outcome.ran.append(kind)
        if not passed:
            outcome.passed = False
            outcome.failed_kind = kind
            outcome.output = _truncate(out)
            return outcome
    return outcome
