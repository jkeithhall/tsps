"""End-to-end scenario exercising the module without a full MAGIS run.

Run with:  python -m transactional_state
"""

import os
import shutil
import tempfile

from .constants import (
    REASON_ASSERTION_VIOLATION,
    REASON_LOCK_CONTENTION,
    REASON_SENSOR_FAIL,
    WIDEN_DYNAMIC_WRITE,
    WIDEN_STAR_IMPORT,
)
from .contracts import TransactionContract
from .sensors import SENSOR_PY_COMPILE, SENSOR_PYTEST, _module_available
from .state import TransactionalSharedState


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="tss_selftest_")
    db_path = os.path.join(tmp, "state.db")
    repo = os.path.join(tmp, "repo")
    os.makedirs(repo, exist_ok=True)

    models_v0 = (
        "from sqlalchemy import Column, String\n"
        "\n"
        "class User(Base):\n"
        "    email = Column(String)\n"
        "\n"
        "    def notify(self, message, urgent=False):\n"
        "        return send(self.email, message)\n"
    )
    service_v0 = (
        "from models import User\n"
        "\n"
        "def welcome(user):\n"
        "    if user.email is None:\n"
        "        return\n"
        "    send_welcome(user.email)\n"
    )
    with open(os.path.join(repo, "models.py"), "w") as f:
        f.write(models_v0)
    with open(os.path.join(repo, "service.py"), "w") as f:
        f.write(service_v0)

    state = TransactionalSharedState(repo_key="selftest", db_path=db_path, repo_dir=repo,
                                     seed_files=["models.py", "service.py"])

    # Tier 0/1 index sanity
    idx = state.index_of("models.py")
    assert idx.symbol_exists("User"), "class User should be indexed"
    assert idx.symbol_exists("User.email"), "class attribute User.email should be indexed (Tier 0)"
    assert idx.symbol_exists("User.notify"), "method User.notify should be indexed"
    assert state.get_symbol("models.py", "User.notify").signature["args"] == ["self", "message", "urgent"], \
        "Tier 1 signature should capture params"

    # Tier 2 reference sanity in the consuming file
    svc_idx = state.index_of("service.py")
    assert svc_idx.reference_lines("email"), "service.py should reference attribute `email`"
    print(f"[index] User.email defined; service.py references `email` at lines {svc_idx.reference_lines('email')}")

    # Agent A reads models.py and asserts User.email exists; will write service.py
    contract_a = TransactionContract(
        agent_id="agent_A",
        read_set=["models.py"],
        write_set=["service.py"],
        assumptions=[
            state.make_exists_assumption("models.py", "User.email"),
            state.make_signature_assumption("models.py", "User.notify"),
        ],
        new_contents={"service.py": service_v0 + "\n# edited by A\n"},
    )

    # Agent B commits FIRST: renames User.email -> User.email_address (semantic conflict)
    models_v1 = models_v0.replace("email = Column(String)", "email_address = Column(String)") \
                         .replace("send(self.email,", "send(self.email_address,")
    contract_b = TransactionContract(
        agent_id="agent_B",
        read_set=["models.py"],
        write_set=["models.py"],
        assumptions=[state.make_exists_assumption("models.py", "User")],
        new_contents={"models.py": models_v1},
    )
    res_b = state.submit_transaction(contract_b)
    assert res_b.accepted, f"Agent B commit should succeed, got {res_b}"
    print(f"[agent_B] committed models.py v{res_b.committed_versions['models.py']} (renamed email -> email_address)")

    # Now Agent A submits against the stale assumption -> must be REJECTED
    res_a = state.submit_transaction(contract_a)
    assert not res_a.accepted, "Agent A must be rejected: User.email was removed by B"
    assert res_a.rejection_reason == REASON_ASSERTION_VIOLATION, res_a.rejection_reason
    assert res_a.invalidated_by_symbol == "User.email", res_a.invalidated_by_symbol
    assert res_a.invalidated_by_agent == "agent_B", res_a.invalidated_by_agent
    print(
        f"[agent_A] REJECTED ({res_a.rejection_reason}): symbol `{res_a.invalidated_by_symbol}` "
        f"invalidated by `{res_a.invalidated_by_agent}` -> {res_a.detail}"
    )
    print(f"[agent_A] static check took {res_a.static_check_ms:.3f} ms")

    # Lock-contention path (uses a dedicated file so it does not collide with the
    # widening scenarios below)
    assert state.acquire_locks("agent_C", ["locked_example.py"]) is True
    contract_d = TransactionContract(
        agent_id="agent_D",
        write_set=["locked_example.py"],
        new_contents={"locked_example.py": "x = 1\n"},
    )
    res_d = state.submit_transaction(contract_d)
    assert not res_d.accepted and res_d.rejection_reason == REASON_LOCK_CONTENTION, res_d
    print(f"[agent_D] REJECTED ({res_d.rejection_reason}): {res_d.detail}")

    # -------------------------------------------------------------------
    # Widen-on-Ambiguity: wildcard imports
    # -------------------------------------------------------------------
    utils_v0 = (
        '__all__ = ["helper", "CONSTANT"]\n'
        "\n"
        "def helper(x):\n"
        "    return x\n"
        "\n"
        "CONSTANT = 42\n"
        "\n"
        "def _private():\n"
        "    return 0\n"
    )
    app_v0 = (
        "from utils import *\n"
        "\n"
        "def run():\n"
        "    return helper(CONSTANT)\n"
    )
    plugins_v0 = (
        "import utils\n"
        "\n"
        "def install():\n"
        "    setattr(utils, 'patched', True)\n"
    )
    for name, content in [("utils.py", utils_v0), ("app.py", app_v0), ("plugins.py", plugins_v0)]:
        with open(os.path.join(repo, name), "w") as f:
            f.write(content)
    state.seed(repo, ["utils.py", "app.py", "plugins.py"])

    # __all__ should drive the public namespace (excludes _private)
    assert state.public_namespace("utils.py") == {"helper", "CONSTANT"}, state.public_namespace("utils.py")

    # Agent E authors app.py which does `from utils import *`
    contract_e = TransactionContract(
        agent_id="agent_E",
        write_set=["app.py"],
        new_contents={"app.py": app_v0 + "\n# edited by E\n"},
    )
    widen_e = state.widen_contract(contract_e)
    assert "utils.py" in contract_e.read_set, "star import should widen read set to utils.py"
    assert any(ev.kind == WIDEN_STAR_IMPORT for ev in widen_e.events), widen_e.events
    assert any("assert_public_namespace_superset" in a for a in contract_e.assumptions), contract_e.assumptions
    print(f"[agent_E] widening: {widen_e.counts()} -> read set now {contract_e.read_set}")
    res_e = state.submit_transaction(contract_e)
    assert res_e.accepted, f"Agent E should commit; namespace intact. {res_e}"

    # Capture the namespace assumption, then Agent F shrinks utils' public namespace.
    ns_assumption = state.make_namespace_assumption("utils.py")  # {CONSTANT, helper}
    utils_v1 = utils_v0.replace('__all__ = ["helper", "CONSTANT"]', '__all__ = ["CONSTANT"]').replace(
        "def helper(x):\n    return x\n\n", ""
    )
    contract_f = TransactionContract(
        agent_id="agent_F",
        write_set=["utils.py"],
        new_contents={"utils.py": utils_v1},
    )
    assert state.submit_transaction(contract_f).accepted

    # A later agent that relied on the wildcard namespace must now be rejected.
    contract_g = TransactionContract(
        agent_id="agent_G",
        read_set=["utils.py"],
        write_set=["app.py"],
        assumptions=[ns_assumption],
        new_contents={"app.py": app_v0 + "\n# edited by G\n"},
    )
    res_g = state.submit_transaction(contract_g)
    assert not res_g.accepted and res_g.rejection_reason == REASON_ASSERTION_VIOLATION, res_g
    assert res_g.invalidated_by_symbol == "helper", res_g.invalidated_by_symbol
    assert res_g.invalidated_by_agent == "agent_F", res_g.invalidated_by_agent
    print(
        f"[agent_G] REJECTED ({res_g.rejection_reason}): wildcard-relied symbol "
        f"`{res_g.invalidated_by_symbol}` removed by `{res_g.invalidated_by_agent}`."
    )

    # -------------------------------------------------------------------
    # Widen-on-Ambiguity: dynamic write escalates an exclusive lock
    # -------------------------------------------------------------------
    contract_h = TransactionContract(
        agent_id="agent_H",
        write_set=["plugins.py"],
        new_contents={"plugins.py": plugins_v0},
    )
    widen_h = state.widen_contract(contract_h)
    assert "utils.py" in widen_h.added_write_locks, widen_h.added_write_locks
    assert "utils.py" in contract_h.write_set, "dynamic write should escalate lock to utils.py"
    assert any(ev.kind == WIDEN_DYNAMIC_WRITE for ev in widen_h.events), widen_h.events
    print(f"[agent_H] dynamic-write widening escalated locks to: {widen_h.added_write_locks}")

    # -------------------------------------------------------------------
    # write-interception: build_contract derives import-based assumptions
    # -------------------------------------------------------------------
    contract_x = state.build_contract("agent_X", "service.py", service_v0)
    assert "models.py" in contract_x.read_set, contract_x.read_set
    assert "service.py" in contract_x.write_set, contract_x.write_set
    assert any(a.get("assert_symbol_exists") == "User" for a in contract_x.assumptions), contract_x.assumptions
    print(
        f"[build_contract] service.py -> read {contract_x.read_set}, "
        f"{len(contract_x.assumptions)} assumption(s) derived from imports"
    )
    # app.py does `from utils import *` -> build_contract should widen to utils' namespace.
    contract_y = state.build_contract("agent_Y", "app.py", app_v0)
    assert "utils.py" in contract_y.read_set, contract_y.read_set
    assert any("assert_public_namespace_superset" in a for a in contract_y.assumptions), contract_y.assumptions
    # build_contract should expose the Widen-on-Ambiguity result on the contract so the
    # metrics layer can record widening_events_count / widening_kind per attempt.
    assert contract_y.widening is not None and contract_y.widening.total >= 1, contract_y.widening
    assert WIDEN_STAR_IMPORT in contract_y.widening.counts(), contract_y.widening.counts()
    print(f"[build_contract] app.py (star import) -> read {contract_y.read_set}, "
          f"widening {contract_y.widening.counts()}")

    # -------------------------------------------------------------------
    # pull-rebase: scoped working-tree projection + read-time signatures
    # -------------------------------------------------------------------
    lib_v0 = "def transform(data, mode='fast'):\n    return data\n"
    consumer_v0 = "from lib import transform\n\ndef go(d):\n    return transform(d, mode='fast')\n"
    for name, content in [("lib.py", lib_v0), ("consumer.py", consumer_v0)]:
        with open(os.path.join(repo, name), "w") as f:
            f.write(content)
    state.seed(repo, ["lib.py", "consumer.py"])

    # Simulate member I's clone working tree and pull (projects consumer.py + dep lib.py).
    clone = os.path.join(tmp, "clone_I")
    os.makedirs(clone, exist_ok=True)
    refreshed = state.pull(clone, ["consumer.py"])
    assert "consumer.py" in refreshed and "lib.py" in refreshed, refreshed
    assert os.path.exists(os.path.join(clone, "lib.py")), "pull should project dependency lib.py into the clone"
    print(f"[pull] projected {refreshed} into member clone (scoped to file + dep neighbors)")

    # I builds its contract against its read-time snapshot: lib transform(data, mode=...).
    contract_i = state.build_contract("agent_I", "consumer.py", consumer_v0 + "# edit\n", local_repo_dir=clone)
    assert any(a.get("assert_signature") and a.get("symbol") == "transform" for a in contract_i.assumptions), \
        contract_i.assumptions

    # Teammate J drops a required parameter from transform and commits first.
    lib_v1 = "def transform(mode='fast'):\n    return None\n"
    assert state.submit_transaction(state.build_contract("agent_J", "lib.py", lib_v1)).accepted

    # I's stale-signature contract must now be REJECTED (Tier-1 signature conflict).
    res_i = state.submit_transaction(contract_i)
    assert not res_i.accepted and res_i.rejection_reason == REASON_ASSERTION_VIOLATION, res_i
    assert res_i.invalidated_by_symbol == "transform", res_i.invalidated_by_symbol
    assert res_i.invalidated_by_agent == "agent_J", res_i.invalidated_by_agent
    print(f"[agent_I] REJECTED ({res_i.rejection_reason}): read-time signature of "
          f"`{res_i.invalidated_by_symbol}` changed incompatibly by `{res_i.invalidated_by_agent}`")

    # After re-pull, I sees lib v1 and its rebuilt contract matches the new signature -> commits.
    state.pull(clone, ["consumer.py"])
    consumer_v1 = "from lib import transform\n\ndef go(d):\n    return transform(mode='fast')\n"
    contract_i2 = state.build_contract("agent_I", "consumer.py", consumer_v1, local_repo_dir=clone)
    res_i2 = state.submit_transaction(contract_i2)
    assert res_i2.accepted, f"after rebase, I should commit against the new signature: {res_i2}"
    print(f"[agent_I] after re-pull + rebase: committed consumer.py v{res_i2.committed_versions['consumer.py']}")

    # -------------------------------------------------------------------
    # constructor-signature gap: `ClassName(...)` resolves to `__init__`, so a
    # class's constructor gaining a new mandatory param is caught even though
    # classes themselves carry no signature.
    # -------------------------------------------------------------------
    widget_v0 = "class Widget:\n    def __init__(self, name):\n        self.name = name\n"
    consumer_ctor_v0 = (
        "from widget import Widget\n\n"
        "def make(name):\n    return Widget(name)\n"
    )
    for name, content in [("widget.py", widget_v0), ("consumer_ctor.py", consumer_ctor_v0)]:
        with open(os.path.join(repo, name), "w") as f:
            f.write(content)
    state.seed(repo, ["widget.py", "consumer_ctor.py"])

    clone_k = os.path.join(tmp, "clone_K")
    os.makedirs(clone_k, exist_ok=True)
    state.pull(clone_k, ["consumer_ctor.py"])

    # K builds its contract against its read-time snapshot of the constructor.
    contract_k = state.build_contract(
        "agent_K", "consumer_ctor.py", consumer_ctor_v0 + "# edit\n", local_repo_dir=clone_k
    )
    assert any(a.get("assert_signature") and a.get("symbol") == "Widget" for a in contract_k.assumptions), \
        f"expected a Tier-1 signature assumption on the class 'Widget' (constructor), got {contract_k.assumptions}"

    # Teammate L adds a new MANDATORY keyword-only constructor param and commits first.
    widget_v1 = (
        "class Widget:\n"
        "    def __init__(self, name, *, size):\n"
        "        self.name = name\n"
        "        self.size = size\n"
    )
    assert state.submit_transaction(state.build_contract("agent_L", "widget.py", widget_v1)).accepted

    # K's stale constructor-signature contract must now be REJECTED. Before the
    # fix, this committed silently (classes were skipped in Tier-1 inference).
    res_k = state.submit_transaction(contract_k)
    assert not res_k.accepted and res_k.rejection_reason == REASON_ASSERTION_VIOLATION, res_k
    assert res_k.invalidated_by_symbol == "Widget", res_k.invalidated_by_symbol
    assert res_k.invalidated_by_agent == "agent_L", res_k.invalidated_by_agent
    print(f"[agent_K] REJECTED ({res_k.rejection_reason}): stale constructor call to "
          f"`{res_k.invalidated_by_symbol}` (changed by `{res_k.invalidated_by_agent}`) — "
          "constructor-signature gap closed")

    # The rendered rejection should show the real new signature, not a signature-less
    # class placeholder (`Widget(...)`), confirming the renderer redirect also works.
    rendered_k = state.render_rejection(contract_k, res_k)
    assert "size" in rendered_k, rendered_k
    print("\n[render assertion_violation/constructor-signature]\n" + rendered_k)

    # After re-pull + rebase, K's contract matches the new constructor -> commits.
    state.pull(clone_k, ["consumer_ctor.py"])
    consumer_ctor_v1 = (
        "from widget import Widget\n\n"
        "def make(name, size):\n    return Widget(name, size=size)\n"
    )
    contract_k2 = state.build_contract(
        "agent_K", "consumer_ctor.py", consumer_ctor_v1, local_repo_dir=clone_k
    )
    res_k2 = state.submit_transaction(contract_k2)
    assert res_k2.accepted, f"after rebase, K should commit against the new constructor: {res_k2}"
    print(f"[agent_K] after re-pull + rebase: committed consumer_ctor.py "
          f"v{res_k2.committed_versions['consumer_ctor.py']}")

    # -------------------------------------------------------------------
    # sensors: the default py_compile gate (in-process) rejects bad syntax
    # -------------------------------------------------------------------
    broken_src = "from lib import transform\n\ndef go(d):\n    return transform(  # missing close paren\n"
    contract_pc = state.build_contract("agent_PC", "consumer.py", broken_src, local_repo_dir=clone)
    assert any(ob.get("kind") == SENSOR_PY_COMPILE for ob in contract_pc.verifier_obligations), \
        contract_pc.verifier_obligations
    res_pc = state.submit_transaction(contract_pc)
    assert not res_pc.accepted and res_pc.rejection_reason == REASON_SENSOR_FAIL, res_pc
    assert res_pc.sensor_kind == SENSOR_PY_COMPILE, res_pc.sensor_kind
    assert state.get_committer("consumer.py") == "agent_I", "rejected write must NOT have committed"
    print(f"[agent_PC] REJECTED ({res_pc.rejection_reason}) by py_compile sensor "
          f"(sensor_check_ms={res_pc.sensor_check_ms:.3f}); committed content unchanged")

    # A syntactically valid follow-up still commits through the same gate.
    good_src = "from lib import transform\n\ndef go(d):\n    return transform(mode='fast')\n"
    res_pc_ok = state.submit_transaction(
        state.build_contract("agent_PC", "consumer.py", good_src, local_repo_dir=clone)
    )
    assert res_pc_ok.accepted, f"valid syntax should pass the py_compile gate: {res_pc_ok}"
    assert res_pc_ok.sensor_kind == SENSOR_PY_COMPILE, res_pc_ok.sensor_kind
    print(f"[agent_PC] committed consumer.py v{res_pc_ok.committed_versions['consumer.py']} "
          f"after passing py_compile (sensor_kind={res_pc_ok.sensor_kind})")

    # -------------------------------------------------------------------
    # sensors: heavy (subprocess) pytest sensor exercises the three-phase path
    # (fail-fast check -> sensors outside the lock -> re-verify + commit).
    # Skips cleanly when pytest is unavailable on the host.
    # -------------------------------------------------------------------
    if _module_available("pytest"):
        work = os.path.join(tmp, "pytest_work")
        os.makedirs(work, exist_ok=True)
        calc_v0 = "def add(a, b):\n    return a + b\n"
        test_v0 = "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
        with open(os.path.join(work, "calc.py"), "w") as f:
            f.write(calc_v0)
        with open(os.path.join(work, "test_calc.py"), "w") as f:
            f.write(test_v0)
        state.seed(work, ["calc.py", "test_calc.py"])

        pytest_ob = [{"kind": SENSOR_PY_COMPILE},
                     {"kind": SENSOR_PYTEST, "targets": ["test_calc.py"], "timeout": 120}]

        # A correct change passes the targeted test -> commit.
        good_calc = state.build_contract("agent_T", "calc.py", "def add(a, b):\n    return b + a\n",
                                         local_repo_dir=work)
        good_calc.verifier_obligations = pytest_ob
        res_t = state.submit_transaction(good_calc)
        assert res_t.accepted, f"passing test should commit: {res_t}"
        assert res_t.sensor_check_ms > 0.0, "pytest sensor should record sensor_check_ms"
        print(f"[agent_T] committed calc.py after pytest passed "
              f"(sensor_check_ms={res_t.sensor_check_ms:.1f})")

        # A change that breaks the targeted test -> reject (sensor_fail).
        bad_calc = state.build_contract("agent_T", "calc.py", "def add(a, b):\n    return a - b\n",
                                        local_repo_dir=work)
        bad_calc.verifier_obligations = pytest_ob
        res_t_bad = state.submit_transaction(bad_calc)
        assert not res_t_bad.accepted and res_t_bad.rejection_reason == REASON_SENSOR_FAIL, res_t_bad
        assert state.get_committed_content("calc.py") == "def add(a, b):\n    return b + a\n", \
            "test-failing write must not have committed"
        print(f"[agent_T] REJECTED ({res_t_bad.rejection_reason}) by pytest sensor "
              f"(sensor_check_ms={res_t_bad.sensor_check_ms:.1f})")
    else:
        print("[pytest sensor] skipped (pytest not importable in this environment)")

    # -------------------------------------------------------------------
    # rejection rendering: deterministic rejection -> developer-framed delta
    # for each category (assertion_violation [removal + signature],
    # lock_contention, sensor_fail). No LLM call is made here.
    # -------------------------------------------------------------------
    # (a) assertion_violation — symbol removed/renamed (User.email -> email_address)
    rendered_a = state.render_rejection(contract_a, res_a)
    assert "User.email" in rendered_a, rendered_a
    assert "removed" in rendered_a, rendered_a
    assert "email_address" in rendered_a and "best guess" in rendered_a.lower(), \
        f"labeled rename heuristic should appear: {rendered_a}"
    assert "L6:" in rendered_a or "L4:" in rendered_a, f"stale draft line numbers expected: {rendered_a}"
    assert "agent_B" in rendered_a, rendered_a
    print("\n[render assertion_violation/removal]\n" + rendered_a)

    # (b) assertion_violation — incompatible signature change (transform)
    rendered_i = state.render_rejection(contract_i, res_i)
    assert "transform(data, mode=...)" in rendered_i, rendered_i
    assert "transform(mode=...)" in rendered_i, rendered_i
    assert "agent_J" in rendered_i, rendered_i
    print("\n[render assertion_violation/signature]\n" + rendered_i)

    # (c) lock_contention — no symbol detail, points at the contended file/holder
    rendered_d = state.render_rejection(contract_d, res_d)
    assert "agent_C" in rendered_d and "locked_example.py" in rendered_d, rendered_d
    assert "User.email" not in rendered_d, "lock_contention must not leak symbol detail"
    print("\n[render lock_contention]\n" + rendered_d)

    # (d) sensor_fail — passes through the raw, developer-legible tool output
    rendered_pc = state.render_rejection(contract_pc, res_pc)
    assert "py_compile" in rendered_pc, rendered_pc
    print("\n[render sensor_fail]\n" + rendered_pc)

    state.close()
    shutil.rmtree(tmp, ignore_errors=True)
    print("\nAll transactional_state self-tests passed.")


if __name__ == "__main__":
    main()
