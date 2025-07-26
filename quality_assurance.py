from chat_with_LLM import chat
from log import *
from common import *
from member import get_name

logger = log(__name__).get_log_obj()

class CodeReviewer:
    # only read the code & requirement and give comments
    def __init__(self, task_description, pl, model_name, use_cache=False, 
                 db_manager=None, task_index=None, issue_bkgrd=None):
        self.task_description = task_description
        self.requirement_text = issue_bkgrd
        self.model_name = model_name
        self.use_cache = use_cache
        self.db_manager = db_manager
        self.task_index = task_index
        self.system_prompt = f"You are a senior {pl} developer focus on code review."
        self.role_index = None
        self.name = get_name(self.system_prompt, self.model_name, self.use_cache)
        self.username = '_'.join(self.name.split())
        self.username = to_safe_folder_name(self.username)
        if self.db_manager is not None and self.task_index is not None:
            self.db_manager.add_action(self.task_index, f"Reviewer {self.name} is Created and Employed ...")
            self.role_index = self.db_manager.add_role(self.task_index, self.system_prompt, self.name, self.username)

    def review_code_change(self, code_change, check_syntax_result=True):
        if check_syntax_result:
            check_syntax_info = "No syntax errors were introduced in this code change."
        else:
            check_syntax_info = f"Syntax errors were introduced in this code change. Error: {check_syntax_result}"

        user_prompt = (f"# Code Change\n```diff\n{code_change}\n```\n\n"
                       f"# Syntax Check\n{check_syntax_info}\n\n"
                       f"# Issue Background\n{self.requirement_text}\n\n"
                       f"# TODO\n"
                       "According to the Instructions, review this Code Change with only one focus on its functionality."
                       "Consider the following aspects in your assessment:\n"
                       "- Correctness: Does the Code Change correctly implement the intended feature or fix?\n"
                       "- Functionality: Are all new functions and modifications working as expected?\n"
                       "- Integration: How well does this Code Change integrate with the existing codebase and functionalities?\n"
                       "Respond concisely, within a 200-token limit.")
        tmp_request_index = self.db_manager.add_request(self.role_index, user_prompt)
        code_review_result = chat(self.system_prompt, user_prompt, model_name=self.model_name, use_cache=self.use_cache,
                                  db_manager=self.db_manager, request_index=tmp_request_index)
        logger.info(f"Code Review Result:{code_review_result}")

        user_prompt = ("# Review\n"
                       f"{code_review_result}\n\n"
                       
                       "# TODO\n"
                       "According to the Review above, please summarize a rating. Select from the following scale:\n"
                       "- 1: Requires major revisions.\n"
                       "- 2: Needs improvements as per the review.\n"
                       "- 3: No changes necessary.\n\n"
                       
                       "Please respond with the rating number only. "
                       "For example, reply with '3' if no changes are necessary.")

        tmp_request_index = self.db_manager.add_request(self.role_index, user_prompt)
        code_review_decision = chat(self.system_prompt, user_prompt, model_name=self.model_name, use_cache=self.use_cache,
                                    db_manager=self.db_manager, request_index=tmp_request_index)
        logger.info(f"Code Review Decision:{code_review_decision}")

        # only keep the number in code_review_decision
        code_review_decision = re.sub(r"[^1-3]", "", code_review_decision)

        if code_review_decision not in ["1", "2", "3"]:
            logger.error("Invalid code_review_decision.")
            return 2, code_review_result

        return int(code_review_decision), code_review_result