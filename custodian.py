import json
import os
import shutil

from operate import *
from chat_with_LLM import chat
from log import *
from common import *
from transactional_state import file_facts
import config

logger = log(__name__).get_log_obj()


class Custodian:
    def __init__(self, repo_full_name, issue_number, issue_content, 
                 repository_directory, model_name, use_cache, bm_top_k = 20,
                 virtual_env_folder=config.virtual_env_path,
                 db_manager=None, task_index=None, state=None):
        self.repo_full_name = repo_full_name # supposing its format: [repo_full_name]-[issue_number]
        self.issue_number = issue_number
        if isinstance(issue_content, str):
            self.issue_number = int(self.issue_number)
        self.repository_directory = repository_directory
        self.bm_top_k = bm_top_k
        self.model_name = model_name
        self.use_cache = use_cache
        self.db_manager = db_manager
        self.task_index = task_index
        # Optional Transactional Shared State: when attached, the Custodian's
        # repository-memory pass also populates the TSS with machine-verifiable
        # AST/symbol facts (alongside its natural-language `_note.md` summaries).
        self.state = state

        self.note_folder = os.path.abspath(os.path.join(virtual_env_folder, self.repo_full_name, str(self.issue_number)))
        os.makedirs(self.note_folder, exist_ok=True)
        self.issue_content = issue_content
        if isinstance(issue_content, str):
            self.issue_content = [self.issue_content]
        self.instance_id = f"{repo_full_name.replace('/', '__')}-{issue_number}"
        self.candidate_file_list = self.find_by_BM25(top_k=bm_top_k)
        logger.info(f"BM25 result: {self.candidate_file_list}")

        logger.info(f"Prepare the candidate file list for the issue: {self.instance_id}")
        correlation_analysis = self.correlation_analysis(self.candidate_file_list)
        self.candidate_file_list = []
        for requirement_text in correlation_analysis.keys():
            for file_path, correlation_score in correlation_analysis[requirement_text].items():
                if correlation_score == 1:
                    self.candidate_file_list.append(file_path)
        logger.info(f"Correlation analysis result: {correlation_analysis}")

    def find_by_BM25(self, bm25_result_file_path=config.bm25_result_file_path, top_k=20): # top_k <= 20
        result_list = []
        if os.path.exists(bm25_result_file_path):
            with open(bm25_result_file_path) as f:
                result_list = f.readlines()
            result_list = [json.loads(each) for each in result_list]
        else:
            pass
        for each_result in result_list:
            if each_result['instance_id'] == self.instance_id:
                return [each['docid'] for each in each_result['hits'][:top_k]]
        return []
    
    def correlation_analysis(self, candidate_file_list):
        if candidate_file_list is None or len(candidate_file_list) == 0:
            os.chdir(self.repository_directory)
            candidate_file_list = get_git_tracked_file_path_list()
            # filter and only keep ".py" files in self.candidate_file_list
            candidate_file_list = [file_path for file_path in candidate_file_list if is_code_or_text(file_path, endswith=".py")]
            other_file_path_list = [file_path for file_path in candidate_file_list if not is_code_or_text(file_path, endswith=".py")]
            logger.info(f"These files might be changed in this agile development cycle:{candidate_file_list}\n"
                        f"These files are not changed in this cycle:{other_file_path_list}")

        ### Step 1.2: get the correlation score
        correlation_score_file_path_and_require = {
            atomic_requirement: {file_path: -1 for file_path in candidate_file_list}
            for atomic_requirement in self.issue_content
        }
        
        for requirement_text in self.issue_content:
            for file_path in candidate_file_list:
                correlation_score_file_path_and_require[requirement_text][file_path] = self.get_correlation_score(file_path, requirement_text)
        logger.info(f"Correlation between each file and each atomic requirement:\n{correlation_score_file_path_and_require}")

        return correlation_score_file_path_and_require

    def get_correlation_score(self, file_path, requirement_text, score_range=[0,1]):
        if not os.path.exists(os.path.join(self.repository_directory, file_path)):
            return 0
        history_issue_list = []
        for entry in os.scandir(os.path.dirname(self.note_folder)):
            if entry.is_dir():
                try:
                    directory_value = int(entry.name)
                    if directory_value < self.issue_number:
                        history_issue_list.append(entry.name)
                except ValueError:
                    continue
        history_issue_list = sorted(history_issue_list, reverse=True)
        find_latest_history_file = None
        find_latest_history_note_file = None
        find_last_issue = None
        for issue_num in history_issue_list:
            history_file_path = os.path.join(os.path.dirname(self.note_folder), issue_num, file_path)
            if os.path.exists(history_file_path):
                find_latest_history_file = history_file_path
                find_latest_history_note_file = os.path.join(os.path.dirname(find_latest_history_file), 
                                                             f"{os.path.basename(find_latest_history_file).rsplit('.', 1)[0]}_note.md")
                find_last_issue = issue_num
                break

        system_prompt = ("You are a software engineer. "
                         "You can provide a comprehensive yet concise overview of the following code file. "
                         "Highlight its primary purpose, key functions, and any notable algorithms or techniques it employs. "
                         "Summarize any documentation or comments that provide insight into the code's usage or design philosophy. "
                         "If the code interacts with external systems or libraries, briefly describe how and why. "
                         "Aim to give the user a clear understanding of what the code does, how it does it, and in what context it might be used.")
        if find_latest_history_file is not None:
            with open(find_latest_history_note_file, "r") as f:
                old_file_summary = f.read()
            code_diff = generate_diff(find_latest_history_file, os.path.join(self.repository_directory, file_path))
            if get_token_num(self.model_name, old_file_summary) > get_token_num(self.model_name, get_file_content(os.path.join(self.repository_directory, file_path))):
                find_latest_history_file = None
            else:
                user_prompt = ("Given the following unified diff output, summarize the changes made to the code in a clear and concise manner. "
                            "Your summary should be detailed enough for developers to understand the modifications without needing to refer back to the full codebase. "
                            "Highlight key changes, including any added or removed functionalities, modifications to existing logic, and updates to comments or documentation. "
                            "The goal is to provide a comprehensive overview of the changes, enabling developers to update the software documentation accordingly. "
                            "Please structure your summary by addressing the following points:\n"
                            "Overview of the purpose of the changes.\n"
                            "Detailed description of any new functions or methods added, including their expected behavior.\n"
                            "Summary of modifications to existing code, with emphasis on how the logic or functionality has been altered.\n"
                            "Explanation of any removed features or functions and the reason for their removal, if known.\n"
                            "Updates to comments, documentation within the code, or external documentation impacted by these changes.\n"
                            "Use clear and accessible language to ensure the summary is understandable to developers with varying levels of expertise.\n\n"
                            f"```diff\n{code_diff}\n```")
                code_diff_summary = chat(system_prompt, user_prompt, model_name=self.model_name, use_cache=self.use_cache)
                save_dir = os.path.join(self.note_folder, os.path.dirname(file_path))
                os.makedirs(save_dir, exist_ok=True)
                with open(os.path.join(save_dir, f"{os.path.basename(file_path).rsplit('.', 1)[0]}_diff_{find_last_issue}_note.md"), "w") as f:
                    f.write(code_diff_summary)
                file_summary = f"## Last Commit Message:\n{code_diff_summary}\n\n{old_file_summary}"
                with open(os.path.join(save_dir, f"{os.path.basename(file_path).rsplit('.', 1)[0]}_note.md"), "w") as f:
                    f.write(file_summary)
                shutil.copyfile(os.path.join(self.repository_directory, file_path), os.path.join(save_dir, os.path.basename(file_path)))
        
        if find_latest_history_file is None:
            file_content = get_file_content(os.path.join(self.repository_directory, file_path))
            user_prompt = (f"# Code File:\n{file_content}\n"
                           f"Can you explain what this code file does? "
                           f"I'm looking for an overview of its main purpose, the important functions it contains, and any special algorithms or methods it uses. "
                           f"It would also be helpful to know about any external systems or libraries it interacts with.")
            file_summary = chat(system_prompt, user_prompt, model_name=self.model_name, use_cache=self.use_cache)
            file_summary = f"## Original File Summary:\n{file_summary}"
            save_dir = os.path.join(self.note_folder, os.path.dirname(file_path))
            os.makedirs(save_dir, exist_ok=True)
            with open(os.path.join(save_dir, f"{os.path.basename(file_path).rsplit('.', 1)[0]}_note.md"), "w") as f:
                f.write(file_summary)
            shutil.copyfile(os.path.join(self.repository_directory, file_path), os.path.join(save_dir, os.path.basename(file_path)))

        # Alongside the NL summary, record machine-verifiable AST/symbol facts and,
        # when a shared state is attached, populate it.
        self._record_symbol_facts(file_path)

        system_prompt = ("Your task is to assess the relevance of a file in relation to a specific issue. "
                         "A file is considered relevant if modifying it is necessary to resolve the issue.")
        user_prompt = (f"# Issue Description:\n{requirement_text}\n"
                       f"# Code File Summray:\n{file_summary}\n\n"
                       "Based on the information provided above, determine if the code file is relevant to the issue described. "
                       "If relevant, respond with '1'; if not, respond with '0'. "
                       "You may include a brief analysis, but ensure the final line of your response contains only the number '1' or '0'.")
        correlation_score = chat(system_prompt, user_prompt, model_name=self.model_name, use_cache=self.use_cache).split("\n")[-1].strip()
        if "0" in correlation_score:
            correlation_score = '0'
        if "1" in correlation_score:
            correlation_score = '1'
        if correlation_score != '1' and correlation_score != '0':
            logger.error(f"Correlation Score Error: {correlation_score}")
            correlation_score = '1'
        logger.info(f"Correlation score between requirement ({requirement_text}) and file_path ({file_path}): {correlation_score}")
        return int(correlation_score)
        
    def _record_symbol_facts(self, file_path):
        """Write machine-verifiable AST/symbol facts for `file_path` next to its NL
        note, and (if a shared state is attached) populate the TSS symbol index.

        Facts are the TSS counterpart of the Custodian's `_note.md` prose: symbols,
        signatures, imports and the public-export namespace the harness can verify.
        Best-effort: never raises into the correlation flow."""
        abs_path = os.path.join(self.repository_directory, file_path)
        if not os.path.exists(abs_path):
            return
        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                source = f.read()
        except (OSError, UnicodeDecodeError):
            return
        try:
            facts = file_facts(file_path, source)
            save_dir = os.path.join(self.note_folder, os.path.dirname(file_path))
            os.makedirs(save_dir, exist_ok=True)
            facts_path = os.path.join(save_dir, f"{os.path.basename(file_path).rsplit('.', 1)[0]}_symbols.json")
            with open(facts_path, "w", encoding="utf-8") as f:
                json.dump(facts, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to write symbol facts for {file_path}: {e}")
        if getattr(self, "state", None) is not None:
            try:
                self.state.seed(self.repository_directory, [file_path])
            except Exception as e:
                logger.error(f"Failed to populate TransactionalSharedState for {file_path}: {e}")

    def get_candidate_file_list(self):
        return self.candidate_file_list