import argparse
import shutil
import json
import os
import config
from log import *
from process import main as process_main

def main(args):
    instance_id = f"magis_{args.instance_id}"
    target_repo_path = os.path.join(config.virtual_env_path, "manager", instance_id)
    if not os.path.exists(target_repo_path):
        shutil.copytree(args.repo_path, target_repo_path, dirs_exist_ok=True)
    logger.info(f"Processing Task {instance_id}")

    # run the task
    process_args = argparse.Namespace()
    process_args.repo_local_path = target_repo_path
    with open(os.path.join(target_repo_path, "issue_description.md"), "w") as f:
        f.write(args.requirement)
    process_args.requirement_file_path = os.path.join(target_repo_path, "issue_description.md")
    process_args.candidate_file_path_list = args.candidate_file_path_list
    process_args.version = args.version
    process_args.granularity = "file"
    process_args.model_name = args.model_name
    process_args.cache = True
    process_args.only_find_file_list = args.only_find_file_list
    process_args.hints_text = args.hints_text
    process_args.bm_top_k = args.bm_top_k
    process_args.enable_tsps = args.enable_tsps
    logger.info(process_args)

    patch, before_review_patch = process_main(process_args)

    gen_result = {"instance_id": instance_id, "model_name_or_path": str(process_args.version),
                "model_patch": patch}
    os.makedirs(args.result_save_dir, exist_ok=True)
    with open(os.path.join(args.result_save_dir, "result.json"), "w") as f:
        json.dump(gen_result, f)

    before_review_gen_result = {"instance_id": instance_id, "model_name_or_path": f"{process_args.version}_before_review",
                                "model_patch": before_review_patch}
    with open(os.path.join(args.result_save_dir, "result_before_review.json"), "w") as f:
        json.dump(before_review_gen_result, f)

    logger.info(f"Finished Task {instance_id}")
    
    

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance_id", type=str, default="test", help="Instance ID")
    parser.add_argument("--repo_path", type=str, default="", help="Repository path") 
    parser.add_argument("--result_save_dir", type=str, default="", help="Result save directory")
    parser.add_argument("--candidate_file_path_list", type=str, nargs='+', help="Candidate file path list")
    parser.add_argument("--requirement", type=str, default="", help="Requirement")
    parser.add_argument("--model_name", default=config.default_model_name, help="Chat model id")
    parser.add_argument("--version", type=int, default=240101, help="Version to mark the experiment")
    parser.add_argument("--hints_text", action="store_true", help="(Optional) Use hints_text with issue description")
    parser.add_argument("--only_find_file_list", type=bool, default=False, help="Only find file list")
    parser.add_argument("--bm_top_k", type=int, default=20, help="Top k for BM25")
    parser.add_argument("--enable_tsps", action=argparse.BooleanOptionalAction, default=config.enable_tsps,
                        help="Enable TSPS orchestration (default: on). Pass --no-enable-tsps for ablation runs.")
    args = parser.parse_args()
    logger = log(__name__).get_log_obj()
    main(args)
