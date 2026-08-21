# MAGIS + Transactional Shared Program State (TSPS)

This repo extends [MAGIS](https://arxiv.org/abs/2403.17927), an LLM-based multi-agent
framework that resolves GitHub issues by splitting a repository-level fix across a small
team of agents, with a **Transactional Shared Program State (TSPS)**: a centralized,
programmatic model of the codebase that mediates every agent's file write instead of
letting agents write to disk directly and reconciling the result with a text merge.

## Motivation

In MAGIS's baseline design, each agent edits its own file inside a fully isolated clone
of the repository, with no visibility into what its teammates are doing. When the run
finishes, the final patch is assembled by *string-concatenating* each agent's
diff. Two agents can therefore both "succeed" (no file overwrites, no textual merge
conflicts) while one has quietly deleted a function the other is calling in a different
file. The failure is semantic, not textual, so a normal merge cannot see it, and it
surfaces later as a hard-to-diagnose runtime bug.

TSPS addresses this by giving agents a shared, queryable model of the codebase's actual
structure (not just its text) and by verifying an agent's assumptions against that model
before its write is allowed to land.

## How it works

TSPS plugs into MAGIS at five points — the scheduler, each agent's write call, the merge
step, the retry loop, and the file-summarization pass — without changing MAGIS's agent
roles:

- **AST symbol index.** A single pass of Python's `ast` module builds a three-tier index
  of every file: symbol presence (functions/classes/assignments), signature
  compatibility (params, defaults, `*args`/`**kwargs`), and a static import/reference
  graph. This is the ground truth transactions are checked against.
- **Transaction contracts.** Instead of writing a file directly, an agent submits a
  contract: the files it read, the file it wants to write, machine-verifiable assertions
  about the code it depended on (e.g. "`User.email` still exists"), and any verifier
  obligations. The contract is derived automatically from the agent's own code without an
  extra LLM call.
- **Widen-on-Ambiguity policy.** Dynamic constructs (`import *`, `getattr`, `setattr`)
  can't be resolved statically. Ambiguous *reads* conservatively widen the assertion set; ambiguous *writes* escalate to a narrow
  exclusive lock on just the affected module, so parallelism isn't lost by default.
- **Deterministic commit gate.** Before a write commits, it passes through lock-contention
  and assertion checks, then a deterministic sensor gate (`py_compile` by default, with
  optional `ruff` / `pyflakes` / targeted `pytest`). All of this runs without invoking an
  LLM, so verification stays fast.
- **Rejection → retry.** A rejected write is discarded; the agent's working tree is
  refreshed to the current committed state (scoped to its file and its dependencies) and
  fed a deterministic, developer-framed explanation of what changed and why, then
  retries, reusing MAGIS's existing retry loop rather than adding a new control path.
- **Materialized merge.** The final patch is no longer a concatenation of independent
  diffs; it's generated directly from the shared committed state, so it's consistent by
  construction.
- **Deterministic scheduler.** MAGIS's original scheduler executed an LLM-generated
  `multiprocessing` script; TSPS replaces it with a small deterministic parser plus a
  thread pool (with a serial fallback), so agents can genuinely interleave without
  reintroducing model-generated control flow.
- **Instrumentation.** Every transaction attempt (commit or reject) is logged with its
  rejection reason, widening events, and timing split, to support analyzing retry
  behavior and fail-fast latency.

## Repository layout

- `magis.py`, `manager.py`, `member.py`, `custodian.py`, `process.py`,
  `quality_assurance.py`, `operate.py`, `chat_with_LLM.py`, `database_manager.py`,
  `common.py`, `config.py` comprise the MAGIS agent framework (Manager / Developer /
  Custodian / QA roles, LLM access, shell/file ops, SQLite bookkeeping).
- `transactional_state/` is the TSPS package: AST indexing (`ast_index.py`,
  `signatures.py`), the contract/result data model (`contracts.py`), the SQLite-backed
  store (`store.py`), deterministic sensors (`sensors.py`), and the orchestrating
  `TransactionalSharedState` class (`state.py`).
- `exp.py` is the batch runner for SWE-bench-style evaluation, including the
  `--enable_tsps` / `--no-enable_tsps` ablation flag.

## Environment

```bash
conda create -n magis python=3.11 -y
conda activate magis
conda install redis==5.0.3 -y  # optional: caches repeated LLM requests
pip install openai==0.27.8 pygments==2.17.2 pydeps==1.12.17 tiktoken==0.5.1 redis==5.0.1 flask==3.0.0 datasets==2.16.1
```

Set your LLM credentials in a `.env` file at the repo root (`API_TOKEN=...`, optionally
`MODEL_NAME=...`); see `config.py` for how it's loaded.

## Run

Resolve a single issue on a local repository:

```bash
python magis.py --repo_path /path/to/repo --requirement "<issue text>" \
  --candidate_file_path_list file1.py file2.py --result_save_dir /path/to/output
```

Run TSPS's own self-test (no repo or LLM required):

```bash
python -m transactional_state
```

Run a batch SWE-bench-style evaluation, with or without TSPS, to compare arms:

```bash
python exp.py --swe_bench_tasks path/to/tasks.json --oracle --oracle-dict path/to/oracle.json \
  --model_name <model> --version <run_tag> --enable_tsps

python exp.py --swe_bench_tasks path/to/tasks.json --oracle --oracle-dict path/to/oracle.json \
  --model_name <model> --version <run_tag> --no-enable_tsps
```

See [`exp/SWE-bench/README.md`](exp/SWE-bench/README.md) for downloading SWE-bench repos/
tasks and running the multi-agent visualization webpage.

## Acknowledgements

Built on top of [MAGIS: LLM-Based Multi-Agent Framework for GitHub Issue Resolution](https://arxiv.org/abs/2403.17927)
(Tao et al., NeurIPS 2024). If you use MAGIS, consider citing:

```bibtex
@inproceedings{magis,
  author       = {Wei Tao and Yucheng Zhou and Yanlin Wang and Wenqiang Zhang and
                  Hongyu Zhang and Yu Cheng},
  title        = {{MAGIS:} LLM-Based Multi-Agent Framework for GitHub Issue Resolution},
  booktitle    = {NeurIPS},
  year         = {2024}
}
```
