import os, re, pickle, shutil, subprocess
from concurrent.futures import ThreadPoolExecutor

from operate import get_file_content, get_git_tracked_file_path_list, is_code_or_text
from chat_with_LLM import chat
from log import log
from common import get_name, merge_multiple_items
from member import Member
from database_manager import DatabaseManager
from transactional_state import TransactionalSharedState
import config


db_manager = DatabaseManager()

logger = log(__name__).get_log_obj()


# Software Development Manager / Scrum Master
class Manager:
    # assuming the 1st file of candidate_file_path_list is like README.md/rst etc.
    def __init__(self, requirement_file_path, repository_directory, 
                 model_name, use_cache=True, candidate_file_path_list=None,
                 project_name=None, role_system_prompt="You are a software development manager.",
                 granularity = "file", prepare_member_folder=True, split_requirement=False, clean_up=True,
                 version=None, hints_text=False, state=None, enable_tsps=True):
        with open(requirement_file_path, "r") as f:
            self.requirement_file_content = f.read()
        hints_text_fp = os.path.join(os.path.dirname(requirement_file_path), "hints_text.md")
        self.hints_text = None
        if hints_text and os.path.exists(hints_text_fp):
            with open(hints_text_fp, "r") as f:
                self.hints_text = f.read()
            self.requirement_file_content += "\n" + self.hints_text
            logger.info(f"Requirement (with hints): {self.requirement_file_content}")
        logger.info(f"Requirements (given by human): {self.requirement_file_content}")
        self.repository_directory = os.path.abspath(repository_directory)
        self.project_name = project_name
        if project_name is None:
            self.project_name = os.path.basename(self.repository_directory)
        self.model_name = model_name
        self.use_cache = use_cache
        self.system_prompt = role_system_prompt
        self.name = get_name(self.system_prompt, self.model_name, self.use_cache)
        self.granularity = granularity
        self.prepare_member_folder = prepare_member_folder
        self.enable_tsps = enable_tsps
        self.experiment_version = version

        self.repo_index = db_manager.add_repo(self.project_name, self.repository_directory)
        logger.info("Processing the repository index: %s", self.repo_index)

        self.task_index = db_manager.add_task(self.repo_index, self.requirement_file_content, path=self.repository_directory, version=version)
        logger.info("Processing the task index: %s", self.task_index)

        self.virtual_env_folder = os.path.join(config.virtual_env_path, self.project_name)
        if clean_up:
            # clean up the files saved in self.virtual_env_folder
            shutil.rmtree(self.virtual_env_folder, ignore_errors=True)
            logger.info(f"Cleaning up the files in {self.virtual_env_folder}")
            db_manager.clean_up_process(self.task_index)
            db_manager.add_action(self.task_index, f"Cleaning up the files in `{self.virtual_env_folder}`")

        ## Step 0: split requirements (optional)
        if split_requirement:
            self.atomic_requirement_list = self.split_requirement()
        else:
            self.atomic_requirement_list = [self.requirement_file_content]
        
        self.role_index = db_manager.add_role(self.task_index, self.system_prompt, name=self.name, \
                                              username='_'.join(self.name.split()), user_path=self.repository_directory)
        logger.info("Processing the role index: %s", self.role_index)

        ## Step 1: split repo files (each unit is guared by a custodian)
        ### Step 1.1: get the file list
        if candidate_file_path_list is None:
            os.chdir(self.repository_directory)
            self.file_path_list = get_git_tracked_file_path_list()
            # filter and only keep ".py" files in self.file_path_list
            self.candidate_file_path_list = [file_path for file_path in self.file_path_list if is_code_or_text(file_path, endswith=".py")]
            other_file_path_list = [file_path for file_path in self.file_path_list if not is_code_or_text(file_path, endswith=".py")]
            logger.info(f"These files might be changed in this agile development cycle:{self.candidate_file_path_list}\n"
                        f"These files are not changed in this cycle:{other_file_path_list}")
        else:
            self.candidate_file_path_list = candidate_file_path_list
            self.file_path_list = self.candidate_file_path_list
        
        ### Step 1.2: get the correlation score
        self.correlation_score_file_path_and_require = {
            key: {atomic_requirement: -1 for atomic_requirement in self.atomic_requirement_list} 
            for key in self.candidate_file_path_list
        }
        
        for requirement_text in self.atomic_requirement_list:
            for file_path in self.candidate_file_path_list:
                if candidate_file_path_list is None:
                    self.correlation_score_file_path_and_require[file_path][requirement_text] \
                        = self.get_correlation_score(file_path, requirement_text)
                else:
                    self.correlation_score_file_path_and_require[file_path][requirement_text] = 1
        logger.info(f"Correlation between each file and each atomic requirement:\n{self.correlation_score_file_path_and_require}")

        ## Step 2: define task according to the correlation score
        self.atomic_task_dict = self.define_task(granularity = self.granularity)

        ## Step 3: build the team according to the task (each task determines a role member)
        self.build_team(granularity=self.granularity)

        logger.info("TEAM FORMATION COMPLETED!")
        # introduce each member and its file path and task
        logger.info("{:<20} {:<30} {:<50}".format("Member Name", "File Path", "Task Description"))
        logger.info("="*100)
        for each_member in self.team_member_list:
            logger.info("{:<20} {:<30} {:<50}".format(each_member.name, each_member.file_path, each_member.task_description))
        logger.info("="*100)

        db_manager.add_action(self.task_index, "TEAM FORMATION COMPLETED!")
        table_string_html = "<table class='member_table'><tr><th>Member Name</th><th>File Path</th><th>Task Description</th></tr>"
        for each_member in self.team_member_list:
            table_string_html += f"<tr><td>{each_member.name}</td><td>{each_member.file_path}</td><td>{each_member.task_description}</td></tr>"
        table_string_html += "</table>"
        db_manager.add_action(self.task_index, table_string_html)

        ## Step 4: build / attach the Transactional Shared Program State (TSS)
        # One shared, SQLite-backed state per task, seeded from the base repo so every
        # member's transaction contract is verified against a common ground truth. A
        # `state` may already be passed in (created by process.main and populated by the
        # Custodian); otherwise create one keyed by project_name#task_index so standalone
        # runs stay isolated. Attached to each member below for write-interception.
        self.state = None
        if self.enable_tsps:
            self.state = state
            try:
                if self.state is None:
                    self.state = TransactionalSharedState(repo_key=f"{self.project_name}#{self.task_index}")
                seed_files = [f for f in self.candidate_file_path_list if isinstance(f, str)]
                self.state.seed(self.repository_directory, seed_files)
                for each_member in self.team_member_list:
                    each_member.state = self.state
                logger.info(f"Attached TransactionalSharedState (repo_key={self.state.repo_key}), "
                            f"seeded {len(seed_files)} file(s).")
                db_manager.add_action(self.task_index, f"Attached Transactional Shared State seeded with {len(seed_files)} file(s).")
            except Exception as e:
                logger.error(f"Failed to initialize TransactionalSharedState: {e}")
                self.state = None
        else:
            logger.info("TSPS disabled in manager; running non-transactional ablation mode.")
            db_manager.add_action(self.task_index, "TSPS disabled; running non-transactional ablation mode.")

    def split_requirement(self):
        system_prompt = f"{self.system_prompt} You can split the requirement into atomic requirements."
        user_prompt = (f"Please split the requirements below into the atomic requirements. \n"
            f"Requirements: {self.requirement_file_content}\n"
            f"The output format is one atomic requirement per row.\n"
            f"Note that there should be no dependency between atomic requirements,"
            "and each atomic requirement description must be independently understandable and implementable.")
        logger.info("Chat with LLM to split requirements")
        atomic_requirement_response = chat(system_prompt, user_prompt, model_name=self.model_name, use_cache=self.use_cache)
        logger.debug(f"atomic_requirement_response:\n{atomic_requirement_response}")
        atomic_requirement_list = atomic_requirement_response.split("\n")
        logger.info(f"Atomic Requirements (split by manager):\n{atomic_requirement_list}")
        return atomic_requirement_list

    def trans_role(self):
        system_prompt = f"{self.system_prompt} You know what kind of employer you want to be based on the requirement."
        user_prompt = (f"Based on the overall requirement: {self.requirement_file_content}\n"
                       f"Please generate one concise description of the software development manager that you want to be."
                        "Remember your answer must start with \"You are\"")
        self.system_prompt = chat(system_prompt, user_prompt, model_name=self.model_name, use_cache=self.use_cache)
        self.name = get_name(self.system_prompt, self.model_name, self.use_cache)

    def get_intro(self):
        user_prompt = f"You are the leader of this project. Introduce yourself in one short sentence. The length should not exceed 50 tokens."
        introduction = chat(self.system_prompt, user_prompt, model_name=self.model_name, use_cache=self.use_cache)
        return f"{self.name}: {introduction}"
        
    def get_correlation_score(self, file_path, requirement_text, score_range=[0,1]):
        file_text = get_file_content(os.path.join(self.repository_directory, file_path))
        system_prompt = (f"{self.system_prompt} You can determine whether a file is relevant to a provided requirement."
                         "The relevance means that if we want to implement the requirement, we need to modify that file.")
        user_prompt = (f"Please determine if the file below is relevant to the requirement.\n"
                       f"Requirement: {requirement_text}\n"
                       f"File content: {file_text}\n"
                       "If they are relevant, please output 1 else 0."
                       "You can give some analysis first but the last line in your answer should just be one number (1 or 0).")
        correlation_score = chat(system_prompt, user_prompt, model_name=self.model_name, use_cache=self.use_cache).split("\n")[-1].strip()
        if correlation_score != '1' and correlation_score != '0':
            logger.error(f"Correlation score error: {correlation_score}")
            return -1
        logger.info(f"Correlation score between requirement ({requirement_text}) and file_path ({file_path}): {correlation_score}")
        return int(correlation_score)
    
    def define_task(self, granularity="file"):
        atomic_task_dict = dict()
        if granularity == "both":
            for file_path in self.correlation_score_file_path_and_require.keys():
                if file_path not in atomic_task_dict.keys():
                    atomic_task_dict[file_path] = dict()
                for requirement_text in self.correlation_score_file_path_and_require[file_path].keys():
                    if self.correlation_score_file_path_and_require[file_path][requirement_text] == 1:
                        atomic_task_dict[file_path][requirement_text] = \
                            self.define_one_task(requirement_text, file_path, granularity)
        elif granularity == "file":
            for file_path in self.correlation_score_file_path_and_require.keys():
                if file_path not in atomic_task_dict.keys():
                    atomic_task_dict[file_path] = dict()
                file_path_requirement_list = self.get_requirement_list(file_path)
                requirement_text = merge_multiple_items(requirement_list = file_path_requirement_list)
                atomic_task_dict[file_path] = \
                    self.define_one_task(requirement_text, file_path, granularity)
        elif granularity == "atomic_requirement":
            for requirement_text in self.atomic_requirement_list:
                if requirement_text not in atomic_task_dict.keys():
                    atomic_task_dict[requirement_text] = dict()
                file_path_list = self.get_file_path_list(requirement_text)
                atomic_task_dict[requirement_text] = \
                    self.define_one_task(requirement_text, file_path_list, granularity)
        return atomic_task_dict

    def define_one_task(self, requirement_text, file_path=None, granularity="file"):
        if granularity == "atomic_requirement":
            part_user_prompt = "these files"
            file_content = merge_multiple_items(file_path_list=file_path)
        else:
            part_user_prompt = "this file"
            file_content = get_file_content(os.path.join(self.repository_directory, file_path))
        
        db_manager.add_action(self.task_index, "Define the Task ...")
        system_prompt = (f"{self.system_prompt}"
                         "Your responsibility is to provide clear guidance and instructions to a developer regarding modifications or improvements needed in a specific code file. "
                         "This guidance should be based on the details provided in the issue description and the existing content of the code file.")
        # Ground the instruction generator with the exact target file. Without this, a
        # model can produce advice like "leave this file untouched, the real fix belongs
        # in file X" when the issue text is skewed toward a sibling file — even though
        # this file was already confirmed (by the oracle/retrieval file list) to need a
        # change. That advice then poisons the actual developer's code-generation call.
        file_note = f"# Target File Path:\n{file_path}\n" if isinstance(file_path, str) else ""
        if file_content is not None:
            user_prompt = (f"Review the issue description and the content of the code file, then provide specific instructions for the developer on the actions they need to take to address the issue with {part_user_prompt}.\n"
                           f"{file_note}"
                           f"# Issue Description:\n{requirement_text}\n# Code File:\n{file_content}\n"
                           "This file has been confirmed to require a modification for this issue. Your "
                           "instructions MUST describe a concrete code change to make in THIS file. Do not "
                           "say the file should remain unmodified or that the fix belongs solely in a "
                           "different file, even if a different file would also need a related change.\n"
                           "Respond concisely and clearly, focusing on key actions to resolve the issue. Limit your answer to no more than 100 tokens.")
        else:
            user_prompt = (f"Review the issue description, then provide specific instructions for the developer on the actions they need to take to address the issue with {part_user_prompt}.\n"
                           f"# Issue Description:\n{requirement_text}\n# New Code File Path:\n{file_path}\n"
                           "Respond concisely and clearly, focusing on key actions to resolve the issue. Limit your answer to no more than 100 tokens.")

        tmp_role_index = db_manager.add_role(self.task_index, system_prompt, name=self.name, username='_'.join(self.name.split()), user_path=self.repository_directory)
        request_index = db_manager.add_request(tmp_role_index, user_prompt)
        
        task_description = chat(system_prompt, user_prompt, model_name=self.model_name, use_cache=self.use_cache, \
                                db_manager=db_manager, request_index=request_index)
        debug_info = (f"***requirement_text***: {requirement_text}\n"
                      f"***task_description***: {task_description}\n"
                      f"***file_path(_list)***: {file_path}")
        logger.debug(debug_info)
        return task_description

    def get_requirement_list(self, file_path):
        requirement_list = list()
        for each_requirement in self.correlation_score_file_path_and_require[file_path].keys():
            if self.correlation_score_file_path_and_require[file_path][each_requirement] == 1:
                requirement_list.append(each_requirement)
        return requirement_list
    
    def get_file_path_list(self, requirement_text):
        file_path_list = list()
        for file_path in self.correlation_score_file_path_and_require.keys():
            if self.correlation_score_file_path_and_require[file_path][requirement_text] == 1:
                file_path_list.append(file_path)
        return file_path_list

    # build the team
    def build_team(self, granularity="file"):
        self.team = dict()
        self.team_member_list = list()
        reserved_usernames = set()
        ## Development Group
        if granularity == "both": 
            for file_path in self.atomic_task_dict.keys():
                if file_path not in self.team.keys():
                    self.team[file_path] = dict()
                for requirement_text in self.atomic_task_dict[file_path].keys():
                    task_description = self.atomic_task_dict[file_path][requirement_text]
                    self.team[file_path][requirement_text] = \
                        self.hire_one_employee(requirement_text, task_description, file_path)
                    self.team_member_list.append(Member(self.team[file_path][requirement_text],\
                                                        file_path, requirement_text, task_description, \
                                                        self.project_name, prepare_member_folder=self.prepare_member_folder, \
                                                        virtual_env_folder = self.virtual_env_folder, \
                                                        model_name=self.model_name, use_cache=self.use_cache, \
                                                        db_manager=db_manager, task_index=self.task_index,
                                                        experiment_version=self.experiment_version,
                                                        reserved_usernames=reserved_usernames))
        elif granularity == "file":
            for file_path in self.atomic_task_dict.keys():
                file_path_requirement_list = self.get_requirement_list(file_path)
                requirement_text = merge_multiple_items(file_path_requirement_list)
                task_description = self.atomic_task_dict[file_path]
                self.team[file_path] = self.hire_one_employee(requirement_text, task_description, file_path)
                self.team_member_list.append(Member(self.team[file_path], \
                                                    file_path, file_path_requirement_list, task_description, \
                                                    self.project_name, prepare_member_folder=self.prepare_member_folder, \
                                                    virtual_env_folder = self.virtual_env_folder, \
                                                    model_name=self.model_name, use_cache=self.use_cache, \
                                                    db_manager=db_manager, task_index=self.task_index,
                                                    experiment_version=self.experiment_version,
                                                    reserved_usernames=reserved_usernames))
        elif granularity == "atomic_requirement":
            for requirement_text in self.atomic_task_dict.keys():
                task_description = self.atomic_task_dict[requirement_text]
                self.team[requirement_text] = self.hire_one_employee(requirement_text, task_description)
                file_path_list = self.get_file_path_list(requirement_text)
                self.team_member_list.append(Member(self.team[requirement_text], \
                                                    file_path_list, requirement_text, task_description, \
                                                    self.project_name, prepare_member_folder=self.prepare_member_folder, \
                                                    virtual_env_folder = self.virtual_env_folder, \
                                                    model_name=self.model_name, use_cache=self.use_cache, \
                                                    db_manager=db_manager, task_index=self.task_index,
                                                    experiment_version=self.experiment_version,
                                                    reserved_usernames=reserved_usernames))

    def hire_one_employee(self, requirement_text, task_description, file_path=None):
        db_manager.add_action(self.task_index, "Hire a New Employee...")
        system_prompt = f"{self.system_prompt} You have a clear understanding of the qualities and skills needed in an employee to address specific issues and follow given instructions."
        # Ground the persona in the assigned file. requirement_text is the FULL shared
        # issue text (which may explicitly name a sibling file, e.g. in a traceback);
        # without stating which file this candidate is for, the model can free-associate
        # that sibling filename into the persona description, which then becomes
        # self.system_prompt for every future call this member makes — including the
        # actual code-generation call.
        file_note = ""
        if isinstance(file_path, str):
            file_note = (
                f"# Assigned File:\n{file_path}\n"
                f"This candidate is hired specifically to make the required change in `{file_path}`. "
                f"Other files mentioned in the Issue Background belong to other team members; do not "
                f"describe this candidate as needing to work on, or defer to, any other file.\n"
            )
        user_prompt = (f"Based on the Issue Background and the Instructions on this file:\n"
                       f"# Issue Background:\n{requirement_text}\n"
                       f"{file_note}"
                       f"# Instructions:\n{task_description}\n"
                       f"Craft a brief and precise description of your ideal candidate."
                       f"Ensure that your response begins with the phrase 'You are'.")
        
        tmp_role_index = db_manager.add_role(self.task_index, system_prompt, name=self.name, username='_'.join(self.name.split()), user_path=self.repository_directory)
        tmp_request_index = db_manager.add_request(tmp_role_index, user_prompt)

        new_role_system_prompt_response = chat(system_prompt, user_prompt, model_name=self.model_name, use_cache=self.use_cache, \
                                               db_manager=db_manager, request_index=tmp_request_index)

        logger.debug(f"***requirement_text***: {requirement_text}\n***task_description***: {task_description}\n"
                     f"***file_path***: {file_path}\n***new_role_system_prompt***: {new_role_system_prompt_response}")
        return new_role_system_prompt_response
    
    def group_meeting(self, current_discussion=None, target=None, to_end=False):
        '''
        current_discussion: the discussion that has been discussed
        target: aim of the discussion: kick-off, problem-solving, review, merge, etc.
        to_end -> status: start(current_discussion:None), continue(to_end:False), end(to_end:True)
        '''
        system_prompt = f"{self.system_prompt} You are skilled in facilitating productive and efficient group discussions among your team members."
        tmp_role_index = db_manager.add_role(self.task_index, system_prompt, name=self.name, \
                                             username='_'.join(self.name.split()), user_path=self.repository_directory)

        if current_discussion is None:
            user_prompt = ( f"# Meeting Objective:\n{target}\n"
                            f"# Potential Files for Modification:\n{self.file_path_list}\n"
                            f"# Attendees:\n{[each.get_intro() for each in self.team_member_list]}\n"
                            f"# Issue Background:\n{self.requirement_file_content}\n"
                            "What should be your opening statement for the meeting? Please respond only with the content that you want to say concisely.")
        elif to_end is False:
            user_prompt = ( f"# Group Meeting Context:\n"
                            f"## Your Name:\n{self.name}\n"
                            f"## Meeting History:\n{current_discussion}\n"
                            "What statement should you make to guide the meeting towards its objective, based on the history? "
                            "Please respond only with the content that you want to say concisely. If the meeting's objective has already been achieved, respond with `<FINISH>`.")
        else:
            user_prompt = ( f"# Group Meeting Context:\n"
                            f"## Your Name:\n{self.name}\n"
                            f"## Meeting History:\n{current_discussion}\n"
                            "Considering the meeting history, what concise statement should you make to conclude the meeting? "
                            "Please respond only with the content that you want to say concisely.")
        
        request_index = db_manager.add_request(tmp_role_index, user_prompt)
        response = chat(system_prompt, user_prompt, model_name=self.model_name, use_cache=self.use_cache, remove_quote=True, \
                        db_manager=db_manager, request_index=request_index)
        return response
    
    def get_member_order(self, meeting_content=None):
        # determine the sequence of the tasks
        ## Step 1: output the comments
        if len(self.team_member_list) <= 3:
            example_comment = ( "# Step 1: the 2-th member works\n"
                                "# Step 2: the 0-th member works\n"
                                "# Step 3: the 1-th member works\n")
        else:
            example_comment = ( "# Step 1: the 3-th member works\n"
                                "# Step 2: the 0-th member works\n"
                                "# Step 3: the 1, 4, 5-th members work in parallel\n"
                                "# Step 4: the 6-th member works\n"
                                "# Step 5: the 2-th member works\n")
        
        user_prompt = ( f"Please create a clear sequence of actions for each team member, based on the Meeting Content. "
                        f"The sequence should follow the format of the provided example.\n"
                        f"# Meeting Content:\n```markdown\n{meeting_content}```\n"
                        f"Example Format:\n```markdown\n{example_comment}```\n"
                        f"Below is the list of each member's name along with their corresponding index number:\n"
                        f"`{[(each.name, idx) for idx, each in enumerate(self.team_member_list)]}`."
                        f"Note that one member just works once. Respond following the example format.")

        logger.debug(f"user_prompt:\n{user_prompt}")
        db_manager.add_action(self.task_index, "Determine the Order of Team Work ...")

        get_action_order_request = db_manager.add_request(self.role_index, user_prompt)
        comment_response = chat(self.system_prompt, user_prompt, model_name=self.model_name, use_cache=self.use_cache, \
                                db_manager=db_manager, request_index=get_action_order_request)
        logger.debug(f"comment_response:\n{comment_response}")

        # Parse the LLM's natural-language order into a deterministic execution plan
        # instead of exec()-ing LLM-generated code. member_order_plan is a list of
        # "steps", each a list of member indices; steps run sequentially, and members
        # within a step may run on a thread pool (see implement()) so the shared
        # transactional state sees genuine concurrency.
        self.member_order_plan = self._parse_member_order(comment_response)
        logger.info(f"Parsed member order plan (steps of member indices): {self.member_order_plan}")
        db_manager.add_action(self.task_index, f"Parsed team work order: {self.member_order_plan}")
        return comment_response

    def _parse_member_order(self, comment_response):
        """Deterministically parse an action-order comment into a list of steps.

        Each step is a list of member indices to run; steps execute in order, members within a
        step may run concurrently. We only read integers that appear AFTER the first ``:`` on a
        line (so the "Step N" counter is never mistaken for a member index). Every member is run
        exactly once: duplicates are dropped (first mention wins) and any member never mentioned
        is appended as a trailing serial step so nobody is silently skipped.
        """
        num_members = len(self.team_member_list)
        plan = []
        seen = set()
        for line in (comment_response or "").split("\n"):
            if ":" not in line:
                continue
            remainder = line.split(":", 1)[1]
            indices = []
            for token in re.findall(r"\d+", remainder):
                idx = int(token)
                if 0 <= idx < num_members and idx not in seen:
                    seen.add(idx)
                    indices.append(idx)
            if indices:
                plan.append(indices)
        # Append any members the LLM never scheduled, each as its own serial step.
        leftover = [i for i in range(num_members) if i not in seen]
        plan.extend([i] for i in leftover)
        # Total fallback: nothing parsed -> run every member serially in index order.
        if not plan:
            plan = [[i] for i in range(num_members)]
        return plan

    def implement(self):
        plan = getattr(self, "member_order_plan", None)
        if not plan:
            plan = [[i] for i in range(len(self.team_member_list))]
        # In TSPS-off ablation mode we force serial execution to avoid adding a
        # thread-level concurrency confound to non-transactional runs.
        serial = (not self.enable_tsps) or getattr(config, "serial_execution", False)
        max_parallel = max(1, getattr(config, "max_parallel_members", 8))

        db_manager.add_action(self.task_index, "Team Work Started ...")
        for step_i, step in enumerate(plan):
            members = [self.team_member_list[i] for i in step if 0 <= i < len(self.team_member_list)]
            if not members:
                continue
            if serial or len(members) == 1:
                for member in members:
                    self._run_member(member)
            else:
                workers = min(len(members), max_parallel)
                db_manager.add_action(self.task_index,
                                      f"Step {step_i + 1}: {len(members)} members working in parallel ...")
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    # Materialize the list so every task is submitted before we block on results.
                    list(executor.map(self._run_member, members))

    def _run_member(self, member):
        """Run one member's work(), isolating failures so one member cannot abort the whole team."""
        try:
            member.work()
        except Exception as e:
            logger.error(f"Member {getattr(member, 'name', '?')} failed to implement its task:")
            logger.error(e)
            db_manager.add_action(self.task_index,
                                  f"{getattr(member, 'name', 'A member')} failed to complete its task.")

    def merge_code_change(self):
        """Assemble the final patch.

        With a Transactional Shared State attached, the final patch is
        *materialized* from committed state (one globally-consistent diff) rather
        than string-concatenating each developer's isolated `gen.patch`. The
        `before_review` artifact keeps the original first-attempt concatenation so
        the pre/post-review comparison stays available. Falls back entirely to the
        original concatenation when no state is attached (TSS init failed).
        """
        before_review_patch = self._concat_member_patches("gen_0.patch")
        if (not self.enable_tsps) or getattr(self, "state", None) is None:
            return self._concat_member_patches("gen.patch"), before_review_patch

        materialized = self._materialize_state_patch()
        if materialized is None:
            # materialization failed (e.g. git error) -> safe fallback
            logger.error("State materialization failed; falling back to patch concatenation.")
            return self._concat_member_patches("gen.patch"), before_review_patch
        if not materialized.strip():
            logger.warning(
                "State materialization produced an empty patch (likely no-op commits); "
                "falling back to patch concatenation."
            )
            return self._concat_member_patches("gen.patch"), before_review_patch
        return materialized, before_review_patch

    def _concat_member_patches(self, patch_name):
        """Original behavior: concatenate each member's saved patch file."""
        patch = ""
        seen_paths = set()
        for member in self.team_member_list:
            patch_path = os.path.join(config.exp_result_save_path, self.project_name, member.username, patch_name)
            if patch_path in seen_paths:
                continue
            seen_paths.add(patch_path)
            if not os.path.exists(patch_path):
                logger.error(f"Patch file does not exist: {patch_path}")
                continue
            with open(patch_path, encoding="utf-8") as f:
                patch += f.read() + '\n'
        return patch

    def _materialize_state_patch(self):
        """Materialize committed transactional state into one coherent git diff.

        Writes each committed (agent-authored) file's content into the base-commit
        manager repo working tree, runs `git diff` (the same tool MAGIS uses, so the
        output format matches what the SWE-bench harness expects), then restores the
        working tree. Returns the patch string, or None on failure.
        """
        modified = self.state.iter_committed(only_modified=True)
        if not modified:
            return ""
        repo = self.repository_directory
        backups = {}
        created = []
        changed_paths = []
        try:
            for file_path, content in modified:
                abs_path = os.path.join(repo, file_path)
                if os.path.exists(abs_path):
                    with open(abs_path, "r", encoding="utf-8") as fh:
                        backups[file_path] = fh.read()
                    if backups[file_path] == content:
                        logger.info(
                            f"Skipping no-op commit for {file_path} during materialization."
                        )
                        continue
                else:
                    created.append(file_path)
                    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
                changed_paths.append(file_path)
                with open(abs_path, "w", encoding="utf-8") as fh:
                    fh.write(content)
            if not changed_paths:
                logger.warning("No net file changes to materialize from committed state.")
                return ""
            # Stage the changes and diff the index against HEAD (the base commit).
            # Staging (vs intent-to-add) cleanly includes new files; --no-renames
            # prevents git from pairing an added file with a modified one as a rename.
            subprocess.run(["git", "add", "--"] + changed_paths, cwd=repo,
                           capture_output=True, text=True)
            result = subprocess.run(["git", "diff", "--cached", "--no-renames", "--"] + changed_paths,
                                    cwd=repo, capture_output=True, text=True)
            if result.returncode != 0:
                logger.error(f"git diff failed during materialization: {result.stderr}")
                return None
            logger.info(f"Materialized patch from committed state for {len(changed_paths)} file(s).")
            return result.stdout
        except Exception as e:
            logger.error(f"Error materializing state patch: {e}")
            return None
        finally:
            # Unstage, then restore the working tree so the manager repo is left untouched.
            subprocess.run(["git", "reset", "-q", "--"] + changed_paths, cwd=repo,
                           capture_output=True, text=True)
            for file_path, original in backups.items():
                try:
                    with open(os.path.join(repo, file_path), "w", encoding="utf-8") as fh:
                        fh.write(original)
                except OSError as e:
                    logger.error(f"Failed to restore {file_path}: {e}")
            for file_path in created:
                try:
                    os.remove(os.path.join(repo, file_path))
                except OSError:
                    pass