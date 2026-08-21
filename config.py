import os
import subprocess

home_path = os.path.expanduser("~")

local_repo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "SWE-bench-repo")
if not os.path.exists(local_repo_path):
    print("Please download SWE-bench-repo ...")
    exit(1)
conda_path = subprocess.check_output(['conda', 'info', '--base']).decode('utf-8').strip()
virtual_env_path = os.path.join(home_path, "virtual_env")
if not os.path.exists(virtual_env_path):
    print(f"Creating the folder for Agents: {virtual_env_path}...")
    os.makedirs(virtual_env_path)
_project_root = os.path.dirname(os.path.abspath(__file__))
exp_result_save_path = os.path.join(_project_root, "exp/SWE-bench")
exp_json_result_path = os.path.join(exp_result_save_path, "assets/our_result")
if not os.path.exists(exp_json_result_path):
    print(f"Creating the folder for Agents: {exp_json_result_path}...")
    os.makedirs(exp_json_result_path)
db_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), "sqlite_db/magis.db")
if not os.path.exists(db_path):
    print(f"Creating the database: {db_path}...")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
print(f"Database path: {db_path}")

def _load_dotenv():
    """Load KEY=VALUE lines from .env into os.environ (does not override existing)."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)

_load_dotenv()
API_TOKEN = os.getenv("API_TOKEN", "")  # set in .env (see exp/SWE-bench/README.md)
cloudflare_account_id = "" # Edit this to your own account id
cloudflare_gateway = "" # Edit this to your own gateway

# Default LLM for MAGIS agents (override via --model_name or MODEL_NAME in .env).
default_model_name = os.getenv("MODEL_NAME", "claude-fable-5")

bm25_result_file_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), "exp/SWE-bench/file_name_and_contents.retrieval_with_LLM.jsonl")
review=False

# Toggle for the Transactional Shared Program State (TSPS) augmentation.
# True (default): use transactional orchestration and semantic commit gates.
# False: run ablation mode without TSPS state attachment.
enable_tsps = True

# Deterministic scheduler (replaces the old exec()-of-LLM-code path in Manager.implement()).
# When False (default), members within an LLM-designated "parallel" step run on a thread pool
# so transactions can be genuinely in-flight at once (needed to surface lock-contention /
# retry-storm metrics). Set True for fully serial, reproducible runs (e.g. headline eval).
serial_execution = False
# Upper bound on concurrent member threads within a single parallel step.
max_parallel_members = 8