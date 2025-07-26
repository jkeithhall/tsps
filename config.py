import os
import subprocess

home_path = os.path.expanduser("~")

local_repo_path = os.path.join(home_path, "SWE-bench-repo")
if not os.path.exists(local_repo_path):
    print("Please download SWE-bench-repo ...")
    exit(1)
conda_path = subprocess.check_output(['conda', 'info', '--base']).decode('utf-8').strip()
virtual_env_path = os.path.join(home_path, "virtual_env")
if not os.path.exists(virtual_env_path):
    print(f"Creating the folder for Agents: {virtual_env_path}...")
    os.makedirs(virtual_env_path)
exp_result_save_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "exp/SWE-bench")
exp_json_result_path = os.path.join(home_path, "exp/SWE-bench/assets/our_result")
if not os.path.exists(exp_json_result_path):
    print(f"Creating the folder for Agents: {exp_json_result_path}...")
    os.makedirs(exp_json_result_path)
db_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), "sqlite_db/magis.db")
print(f"Database path: {db_path}")

API_TOKEN = "" # Edit this to your own API token
cloudflare_account_id = "" # Edit this to your own account id
cloudflare_gateway = "" # Edit this to your own gateway

bm25_result_file_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), "exp/SWE-bench/file_name_and_contents.retrieval_with_LLM.jsonl")
review=False