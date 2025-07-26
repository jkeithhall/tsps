import os, pickle, shutil

from operate import get_file_content, get_git_tracked_file_path_list, is_code_or_text
from chat_with_LLM import chat
from log import log
from common import get_name, merge_multiple_items
from member import Member
from database_manager import DatabaseManager
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
                 version=None, hints_text=False):
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
        if file_content is not None:
            user_prompt = (f"Review the issue description and the content of the code file, then provide specific instructions for the developer on the actions they need to take to address the issue with {part_user_prompt}.\n"
                           f"# Issue Description:\n{requirement_text}\n# Code File:\n{file_content}\n"
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
                                                        db_manager=db_manager, task_index=self.task_index))
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
                                                    db_manager=db_manager, task_index=self.task_index))
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
                                                    db_manager=db_manager, task_index=self.task_index))

    def hire_one_employee(self, requirement_text, task_description, file_path=None):
        db_manager.add_action(self.task_index, "Hire a New Employee...")
        system_prompt = f"{self.system_prompt} You have a clear understanding of the qualities and skills needed in an employee to address specific issues and follow given instructions."
        user_prompt = (f"Based on the Issue Background and the Instructions on this file:\n"
                       f"# Issue Background:\n{requirement_text}\n"
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

        if len(self.team_member_list) <= 3:
            example_code = ("# Step 1: the 2-th member works\n"
                            "self.team_member_list[2].work()\n\n"
                            "# Step 2: the 0-th member works\n"
                            "self.team_member_list[0].work()\n\n\n"
                            "# Step 3: the 1-th member works\n"
                            "self.team_member_list[1].work()\n")
        else:
            example_code = ("import multiprocessing\n\n\n"
                            "# Step 1: the 3-th member works\n"
                            "self.team_member_list[3].work()\n\n"
                            "# Step 2: the 0-th member works\n"
                            "self.team_member_list[0].work()\n\n\n"
                            "# Step 3: the 1, 4, 5-th members work in parallel\n"
                            "parallel_p1 = multiprocessing.Process(self.team_member_list[1].work())\n"
                            "parallel_p2 = multiprocessing.Process(self.team_member_list[4].work())\n"
                            "parallel_p3 = multiprocessing.Process(self.team_member_list[5].work())\n\n"
                            "parallel_p1.start()\n"
                            "parallel_p2.start()\n"
                            "parallel_p3.start()\n\n"
                            "parallel_p1.join()\n"
                            "parallel_p2.join()\n"
                            "parallel_p3.join()\n\n\n"
                            "# Step 4: the 6-th member works\n"
                            "self.team_member_list[6].work()\n\n"
                            "# Step 5: the 2-th member works\n"
                            "self.team_member_list[2].work()\n")
        user_prompt = ( "# Action Order Comment:\n"
                        f"{comment_response}\n\n"
                        "Generate a segment of a Python script based on the above comment.\n"
                        "Use the existing variable `self.team_member_list` and its `.work()` method.\n"
                        f"Example Format:\n```python\n{example_code}\n```\n"
                        "Ensure the generated script segment closely adheres to the example format." )
        db_manager.add_action(self.task_index, "Generate the Python Script for Team Work ...")
        get_code_request = db_manager.add_request(self.role_index, user_prompt)
        code_response = chat(self.system_prompt, user_prompt, model_name=self.model_name, use_cache=self.use_cache, \
                             db_manager=db_manager, request_index=get_code_request)
        logger.debug(f"user_prompt:\n{user_prompt}")

        self.implement_code = code_response.split("```python")[1].split("```")[0]
        return code_response
    
    def implement(self):
        # ### wait for the admin to confirm
        # input_flag = input("Confirm the main code? (Y/n)")
        # if input_flag != "y" and input_flag != "Y" and input_flag != '':
        #     if input_flag == "n" or input_flag == "N":
        #         logger.info("Please modify the main code.")
        #         exit(0)
        #     raise Exception("Main code is not confirmed.")
        try:
            db_manager.add_action(self.task_index, "Team Work Started ...")
            exec(self.implement_code)
        except Exception as e:
            logger.error("1st Try, Implement error:")
            logger.error(e)
            try:
                db_manager.add_action(self.task_index, "Team Work Restarted ...")
                for each in self.team_member_list:
                    each.work()
            except Exception as e:
                logger.error("Finally, Implement error:")
                logger.error(e)
                db_manager.add_action(self.task_index, "Failed to Implement the Python Script for Team Work ...")

    def merge_code_change(self):
        patch = ""
        before_review_patch = ""
        for member in self.team_member_list:
            patch_dir = os.path.join(config.exp_result_save_path, self.project_name, member.username)
            if not os.path.exists(os.path.join(patch_dir, "gen.patch")):
                logger.error(f"Patch file does not exist: {os.path.join(patch_dir, 'gen.patch')}")
                continue
            with open(os.path.join(patch_dir, "gen.patch")) as f:
                patch += f.read() + '\n'
            if not os.path.exists(os.path.join(patch_dir, "gen_0.patch")):
                logger.error(f"Patch file does not exist: {os.path.join(patch_dir, 'gen.patch')}")
                continue
            with open(os.path.join(patch_dir, "gen_0.patch")) as f:
                before_review_patch += f.read() + '\n'
        return patch, before_review_patch