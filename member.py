import json
import os
import uuid

from operate import *
from chat_with_LLM import chat
from log import *
from common import *
import config
from quality_assurance import CodeReviewer


logger = log(__name__).get_log_obj()


def _extract_fenced_code(response: str, lexer_name: str) -> str:
    """Extract code from the last ```{lexer} fenced block in an LLM response.

    Using the *last* block handles self-corrections ("wait, let me fix...") where
    the model emits a draft block then a corrected one. Taking the first block
    instead can silently commit a no-op diff when the draft still matches the
    original file."""
    marker = f"```{lexer_name}\n"
    if marker not in response:
        return response
    return response.split(marker)[-1].split("\n```")[0]


# Team Member
class Member:
    def __init__(self, role_system_prompt, file_path, requirement, task_description, project_name, \
                 virtual_env_folder, model_name, use_cache=True, email_domain="itaowe.com", \
                 prepare_member_folder=True, db_manager=None, task_index=None, experiment_version=None,
                 reserved_usernames=None):
        self.system_prompt = role_system_prompt
        self.requirement = requirement
        if isinstance(self.requirement, list):
            self.requirement_text = merge_multiple_items(requirement_list = self.requirement)
        else:
            self.requirement_text = requirement

        self.task_description = task_description
        self.project_name = project_name
        self.virtual_env_folder = os.path.abspath(virtual_env_folder)
        self.model_name = model_name
        self.use_cache = use_cache
        self.name = get_name(role_system_prompt, self.model_name, self.use_cache)
        logger.info(f"Member role: {role_system_prompt}\nMember name: {self.name}")

        # Preparing the folder for this member. Usernames must be unique within a
        # team so each developer's gen.patch lands in a distinct path (multifile
        # runs often hire the same persona for multiple oracle files).
        reserved = reserved_usernames if reserved_usernames is not None else set()
        base_username = to_safe_folder_name('_'.join(self.name.split()))
        self.username = base_username
        count = 0

        def _username_taken(username: str) -> bool:
            if username in reserved:
                return True
            if os.path.exists(os.path.join(self.virtual_env_folder, username)):
                return True
            exp_member_dir = os.path.join(
                config.exp_result_save_path, self.project_name, username)
            return os.path.exists(exp_member_dir)

        while _username_taken(self.username):
            count += 1
            self.username = f"{base_username}_{count}"
        reserved.add(self.username)

        self.user_path = os.path.join(self.virtual_env_folder, self.username)
        if prepare_member_folder or not os.path.exists(self.user_path):
            os.makedirs(self.user_path, exist_ok=True)
        
        self.project_path = os.path.abspath(os.path.join(self.user_path, self.project_name))
        self.operator_gitconfig = GitConfig(self.username, email_domain)
        self.operator_linux_env = LinuxEnv(self.user_path, self.operator_gitconfig)

        logger.info(f"Preparing Member folder: {self.user_path}")
        self.update_folder()

        self.file_path = file_path
        if isinstance(self.file_path, list):
            self.file_content = merge_multiple_items(file_path_list = os.path.join(self.project_path ,file_path))
            role_file_path = json.dumps(file_path)
        else:
            self.file_content = get_file_content(os.path.join(self.project_path ,file_path))
            role_file_path = file_path

        self.db_manager = db_manager
        self.task_index = task_index
        self.experiment_version = experiment_version
        if self.experiment_version is None and db_manager is not None and task_index is not None:
            row = db_manager._fetchone(
                "SELECT version FROM task WHERE task_index = ?", (task_index,)
            )
            if row and row[0] is not None:
                self.experiment_version = int(row[0])
        self.role_index = None
        if self.db_manager is not None and self.task_index is not None:
            self.role_index = db_manager.add_role(task_index, self.system_prompt, self.name, self.username, role_file_path)

        # Transactional Shared Program State; set by the Manager after team formation.
        # When None (e.g. TSS init failed), work() falls back to the original behavior.
        self.state = None

        self.make_plan()

    def get_intro(self):
        user_prompt = (f"Instruction received: {self.task_description}\n"
            f"File managed: {self.file_path}\n"
            f"Create a brief introduction (max 50 tokens).")
        tmp_request_index = self.db_manager.add_request(self.role_index, user_prompt)
        introduction = chat(self.system_prompt, user_prompt, model_name=self.model_name, use_cache=self.use_cache, remove_quote=True, \
                            db_manager=self.db_manager, request_index=tmp_request_index)
        return f"{self.name}: {introduction}"
    
    def update_folder(self):
        logger.info(f"Update Member folder: {self.user_path}")
        self.manager_repo_path = os.path.join(config.virtual_env_path, "manager", self.project_name)
        if os.path.exists(self.manager_repo_path):
            git_clone_bash = f"cp -r {self.manager_repo_path} {self.user_path}/"
        else:
            git_clone_bash = f"git clone git@github.com:itaowei/{self.project_name}.git"
        logger.info(git_clone_bash)
        if not os.path.exists(self.project_path):
            self.operator_linux_env.bash(git_clone_bash)
            self.operator_gitconfig.gen_config_file(self.project_path)
        else:
            logger.info(f"Project folder {self.project_path} already exists.")
        
        result = self.operator_linux_env.bash("git status -uno", self.project_path)
        logger.info("git status result:\n" + result.stdout)

    def make_plan(self, plan_note_path=None):
        if plan_note_path is None:
            plan_note_path = os.path.join(self.user_path, "note", self.project_name, "plan.md")
        os.makedirs(os.path.dirname(plan_note_path), exist_ok=True)

        self.db_manager.add_action(self.task_index, f"{self.name} is Making the Plan for His/Her Task ...")
        user_prompt = (f"# Issue Background:\n{self.requirement_text}\n"
            f"# Instruction Received:\n{self.task_description}\n")
        if self.file_content is not None:
            user_prompt += (f"# File Content:\n{self.file_content}\n"
            f"Outline concise steps to execute the instruction on the given file content, within a 200-token limit.")
        else:
            user_prompt += (f"Outline concise steps to execute the instruction on the new file `{self.file_path}`, within a 200-token limit.")
        
        tmp_request_index = self.db_manager.add_request(self.role_index, user_prompt)
        self.plan = chat(self.system_prompt, user_prompt, model_name=self.model_name, use_cache=self.use_cache, \
                         db_manager=self.db_manager, request_index=tmp_request_index)
        
        with open(plan_note_path, "w") as f:
            f.write(self.plan)

    def group_meeting(self, current_discussion):
        user_prompt = (f"# Group Meeting Context:\n"
               f"## Your Name:\n{self.name}\n"
               f"## Instruction You Received:\n{self.task_description}\n"
               f"## Your Plan:\n{self.plan}\n"
               f"## Meeting History:\n{current_discussion}\n"
               "What do you want to say next? Please respond only with the content that you want to say. If silence is preferable, reply with `<NONE>`.")
        
        tmp_request_index = self.db_manager.add_request(self.role_index, user_prompt)
        response = chat(self.system_prompt, user_prompt, model_name=self.model_name, use_cache=self.use_cache, remove_quote=True, \
                        db_manager=self.db_manager, request_index=tmp_request_index)
        
        return response
    
    def work(self, review=config.review, test=False, max_try_times=2):
        self.update_folder()
        add_review = None
        code_review_decision = None
        after_review_info = None
        if review:
            reviewer = CodeReviewer(self.task_description, pl=get_lexer_for_filename(self.file_path).name,
                                    model_name=self.model_name, use_cache=self.use_cache, 
                                    db_manager=self.db_manager, task_index=self.task_index, issue_bkgrd=self.requirement_text)
        for try_i in range(max_try_times):
            # Pull / rebase: before regenerating, reconcile this member's working tree
            # to the committed snapshot (abort-and-rebase). A discarded write resets to
            # committed state and any teammate-changed dependencies appear, so the agent
            # regenerates against ground truth instead of stale context. Scoped to the
            # member's file + its dependency neighbors so its git diff stays focused.
            if getattr(self, "state", None) is not None and isinstance(self.file_path, str):
                try:
                    refreshed = self.state.pull(self.project_path, [self.file_path])
                    if refreshed:
                        logger.info(f"[TSS] {self.name} pulled latest state for {refreshed}")
                except Exception as e:
                    logger.error(f"[TSS] pull failed for {self.name}: {e}")
            try:
                if add_review is not None:
                    after_review_info = (f"## Last Round of Code Modification and Its Review\n"
                                            f"### Code Modification\n"
                                            f"```diff\n{code_diff}\n```\n"
                                            f"### Code Review\n"
                                            f"{add_review}\n\n")
                else:
                    after_review_info = f"## Plan\n{self.plan}\n\n"

                if self.file_content is not None:
                    number_with_code = get_file_content(os.path.join(self.project_path, self.file_path), add_number=True)
                    
                    user_prompt = (
                        f"# Modification Request\n"
                        # f"## Issue Background\n{self.requirement_text}\n"
                        f"## Instruction received\n{self.task_description}\n"
                        f"{after_review_info}"
                        f"# Code to Modify\n"
                        f"Note that every line below is preceded by a line number, with the first ` | ` symbol serving as a delimiter to distinguish the line number from the code itself.\n"
                        f"{number_with_code}\n"
                        f"# TODO\n" 
                        f"- Please review the Code to Modify in light of the Modification Request.\n"
                        f"- Identify the range of lines that require changes.\n"
                        f"- Respond in the format:\n"
                        f"BEGIN LINE NUM: [start line number]\n"
                        f"END LINE NUM: [end line number]"
                        )
                    tmp_request_index = self.db_manager.add_request(self.role_index, user_prompt)
                    range_response = chat(self.system_prompt, user_prompt, model_name=self.model_name, use_cache=self.use_cache, \
                                        db_manager=self.db_manager, request_index=tmp_request_index)


                    range_list = []
                    for line in range_response.split("\n"):
                        if "BEGIN LINE NUM:" in line:
                            begin_line_number = int(line.split("BEGIN LINE NUM:", 2)[1].strip())
                        elif "END LINE NUM:" in line:
                            end_line_number = int(line.split("END LINE NUM:")[-1].strip())
                            range_list.append([begin_line_number, end_line_number])
                    begin_line_number = min([x[0] for x in range_list])
                    end_line_number = max([x[1] for x in range_list])
                    if begin_line_number < 1 or end_line_number < begin_line_number:
                        raise ValueError(
                            f"invalid line range {begin_line_number}-{end_line_number}"
                        )

                    logger.info(f"begin_line_number: {begin_line_number}\nend_line_number: {end_line_number}\n")
                    self.db_manager.add_action(self.task_index, 
                                            f"{self.name} is Changing the Code of {self.file_path} between Line {begin_line_number} and Line {end_line_number} ...")

                    # replace the code in the range (begin_line_number, end_line_number) with TODO
                    current_file_content = get_file_content(os.path.join(self.project_path, self.file_path))
                    old_version_clean_code = current_file_content.split(f"```{get_lexer_for_filename(self.file_path).name}\n", 1)[1].split('\n```')[0] # suppose there is no ``` in the code
                    before_modify_code = "\n".join(old_version_clean_code.split("\n")[:begin_line_number-1])
                    after_modify_code = "\n".join(old_version_clean_code.split("\n")[end_line_number:])
                    old_part_code = "\n".join(old_version_clean_code.split("\n")[begin_line_number-1:end_line_number])
                    old_part_code_first_line = old_part_code.split("\n")[0]
                    space_count = len(old_part_code_first_line) - len(old_part_code_first_line.lstrip(' '))
                    code_with_todo = f"{before_modify_code}\n{' '*space_count}# [NEW CODE]\n{after_modify_code}"

                    user_prompt = (
                        f"# Modification Request\n"
                        f"## Instruction received\n{self.task_description}\n"
                        f"{after_review_info}"
                        f"# Code to Modify\n"
                        f"```{get_lexer_for_filename(self.file_path).name}\n{code_with_todo}\n```"
                        f"## The Old Code below has been replaced by '# [NEW CODE]'\n"
                        f"```{get_lexer_for_filename(self.file_path).name}\n{old_part_code}\n```"
                        f"# TODO\n" 
                        f"- Based on the Modification Request, please generate the new code to replace the place of `# [NEW CODE]` in the Code to Modify.\n"
                        f"Respond concisely in the format:\n"
                        f"```{get_lexer_for_filename(self.file_path).name}\n"
                        f"{' '*space_count}# [NEW CODE]\n"
                        f"```"
                        )

                    tmp_request_index = self.db_manager.add_request(self.role_index, user_prompt)
                    new_file_content_response = chat(self.system_prompt, user_prompt, model_name=self.model_name, use_cache=self.use_cache, \
                                                    db_manager=self.db_manager, request_index=tmp_request_index)
                    logger.debug(f"\nfile_path: {self.file_path}\nnew content:\n{new_file_content_response}\n")
                    self.db_manager.add_action(self.task_index, f"{self.name} changed the code of {self.file_path} shown below")

                    new_version_file_content = \
                        _extract_fenced_code(new_file_content_response, get_lexer_for_filename(self.file_path).name)
                    logger.info(f"new_version_file_content:\n{new_version_file_content}\n")
                    first_line_space_count = len(new_version_file_content.split("\n")[0]) - len(new_version_file_content.split("\n")[0].lstrip(' '))
                    if first_line_space_count == 0:
                        add_space_num = space_count
                        new_version_file_content = "\n".join([f"{' '*add_space_num}{line}" for line in new_version_file_content.split("\n")])


                    logger.info(f"self.user_path:{self.user_path}")
                    logger.info(f"os.path.join(self.user_path, self.project_name):{os.path.join(self.user_path, self.project_name)}")
                    revised_file_path = os.path.join(self.user_path, self.project_name, self.file_path)
                    logger.info(f"Revising the file saved in {revised_file_path}...")
                    new_version_file_content = f"{before_modify_code}\n{new_version_file_content}\n{after_modify_code}"
                    with open(revised_file_path, "w") as f:
                        f.write(new_version_file_content)
                else:
                    user_prompt = (
                        f"# Code Generation Request\n"
                        f"## Instruction received\n{self.task_description}\n"
                        f"{after_review_info}"
                        f"# TODO\n" 
                        f"- Based on the Code Generation Request, please generate the new code for file `{self.file_path}`.\n"
                        f"Respond in the format:\n"
                        f"```{get_lexer_for_filename(self.file_path).name}\n"
                        f"# [NEW CODE]\n"
                        f"```"
                        )
                    tmp_request_index = self.db_manager.add_request(self.role_index, user_prompt)
                    new_file_content_response = chat(self.system_prompt, user_prompt, model_name=self.model_name, use_cache=self.use_cache, \
                                                    db_manager=self.db_manager, request_index=tmp_request_index)
                    logger.debug(f"\nfile_path: {self.file_path}\nnew content:\n{new_file_content_response}\n")
                    self.db_manager.add_action(self.task_index, f"{self.name} changed the code of {self.file_path} shown below")
                    new_version_file_content = \
                        _extract_fenced_code(new_file_content_response, get_lexer_for_filename(self.file_path).name)
                    logger.info(f"new_version_file_content:\n{new_version_file_content}\n")

                    logger.info(f"self.user_path:{self.user_path}")
                    logger.info(f"os.path.join(self.user_path, self.project_name):{os.path.join(self.user_path, self.project_name)}")
                    revised_file_path = os.path.join(self.user_path, self.project_name, self.file_path)
                    logger.info(f"Adding the file saved in {revised_file_path}...")
                    os.makedirs(os.path.dirname(revised_file_path), exist_ok=True)
                    with open(revised_file_path, "w") as f:
                        f.write(new_version_file_content)
                    self.operator_linux_env.bash(f"git add {self.file_path}", self.project_path)

            except:
                self.db_manager.add_action(self.task_index, f"{self.name} failed to change the code of {self.file_path}")
                logger.error("Nothing changed.")

            code_diff = self.operator_linux_env.bash(f"git diff {self.file_path}", self.project_path).stdout
            logger.info(f"git diff result ({try_i+1}-th time):\n" + code_diff)
            self.db_manager.add_action(self.task_index, (f"<div class='code-diff'>"
                                                        f"    <div class='header'>{try_i+1}-th Time Code Diff by {self.name}</div>"
                                                        f"    <pre><code>{code_diff}</code></pre>"
                                                        f"</div>"))

            save_dir = os.path.join(config.exp_result_save_path, self.project_name, self.username)
            os.makedirs(save_dir, exist_ok=True)
            with open(os.path.join(save_dir, f"gen_{try_i}.patch"), "w") as f:
                f.write(code_diff)
            logger.info(f"Code diff saved at {os.path.join(save_dir, f'gen_{try_i}.patch')}")
            self.db_manager.add_action(self.task_index, f"{self.name} saved the code diff at {os.path.join(save_dir, f'gen_{try_i}.patch')}")

            # No-op write detection: an empty diff can happen either via the bare `except:`
            # above (a crash, already logged as "failed to change the code of ...") or
            # silently, when the model complies with the expected format but echoes back
            # unchanged content (no exception at all). Log this signature explicitly so
            # both failure modes are equally visible, and — when attempts remain and
            # review/test are both disabled — retry instead of letting a no-op fall
            # through to TSPS as a spuriously "clean" commit.
            if not code_diff.strip():
                self.db_manager.add_action(self.task_index,
                    f"{self.name} produced no change to {self.file_path} (empty diff) on attempt {try_i+1}.")
                if try_i + 1 < max_try_times and not review and not test:
                    add_review = (
                        f"Your previous attempt made no change to `{self.file_path}`. This file has been "
                        f"confirmed to require a modification for this issue — even if you believe the fix "
                        f"belongs elsewhere, you must still make a concrete edit to this exact file. Try again."
                    )
                    logger.info(f"[TSS] {self.name} produced an empty diff for {self.file_path}; retrying "
                                f"(attempt {try_i+1}/{max_try_times}) instead of committing a no-op.")
                    continue

            # Transactional Shared State gate: submit the proposed change as a contract
            # instead of trusting the raw file write. A rejection (semantic conflict or
            # lock contention) is fed back into THIS retry loop via add_review, so the
            # existing max_try_times loop doubles as the retry-after-conflict loop.
            if getattr(self, "state", None) is not None and isinstance(self.file_path, str):
                txn_result = self._submit_to_state(attempt_number=try_i)
                if txn_result is not None and not txn_result.accepted:
                    add_review = self._render_state_rejection(txn_result)
                    logger.info(f"[TSS] {self.name}'s change to {self.file_path} REJECTED "
                                f"({txn_result.rejection_reason}): {txn_result.detail}")
                    self.db_manager.add_action(self.task_index,
                        f"{self.name}'s change to {self.file_path} was rejected by shared state "
                        f"({txn_result.rejection_reason}). Retrying with updated context.")
                    continue
                elif txn_result is not None:
                    logger.info(f"[TSS] {self.name}'s change to {self.file_path} committed to shared "
                                f"state (versions {txn_result.committed_versions}).")
                    self.db_manager.add_action(self.task_index,
                        f"{self.name}'s change to {self.file_path} was committed to shared state.")

            if not review and not test:
                break

            # Quality Assurance

            ## Review
            if review:
                check_syntax_result = check_syntax(self.file_path)
                code_review_decision, code_review_result = reviewer.review_code_change(code_diff, check_syntax_result)
                if code_review_decision == 3:
                    logger.info(f"After {try_i+1} time(s) code review, the code change for {self.file_path} by {self.name} is accpeted.")
                    break
                else:
                    add_review = code_review_result
                    # checkout_stdout = self.operator_linux_env.bash(f"git checkout {self.file_path}", self.project_path).stdout
                    # logger.info(f"git checkout result ({try_i+1}-th time):\n" + checkout_stdout)
                    if code_review_decision == 1:
                        logger.info(f"After {try_i+1} time(s) code review, the code change for {self.file_path} by {self.name} is rejected.")
                    elif code_review_decision == 2:
                        logger.info(f"After {try_i+1} time(s) code review, the code change for {self.file_path} by {self.name} is partially accepted.")

        gen_patch_path = os.path.join(save_dir, "gen.patch")
        if (not code_diff.strip() and os.path.exists(gen_patch_path)
                and open(gen_patch_path, encoding="utf-8").read().strip()):
            logger.warning(
                "Keeping existing non-empty gen.patch at %s "
                "(this attempt produced no diff for %s).",
                gen_patch_path, self.file_path,
            )
        else:
            with open(gen_patch_path, "w", encoding="utf-8") as f:
                f.write(code_diff)
        logger.info(f"Finally, Code diff saved at {gen_patch_path}")
        self.db_manager.add_action(self.task_index, f"Finally, {self.name} saved the code diff at {os.path.join(save_dir, f'gen.patch')}")

    def _submit_to_state(self, attempt_number=0):
        """Submit this member's freshly-written file as a transaction contract.

        Reads the raw on-disk content (the same bytes that produce gen.patch),
        builds a contract (import-derived assumptions + Widen-on-Ambiguity), and
        submits it. Every attempt (commit AND reject) is recorded in the
        transaction_metrics table. Returns the TransactionResult, or None when
        there is nothing to submit (file missing / unreadable)."""
        file_abs = os.path.join(self.project_path, self.file_path)
        if not os.path.exists(file_abs):
            return None
        try:
            with open(file_abs, "r", encoding="utf-8") as f:
                new_content = f.read()
        except (OSError, UnicodeDecodeError):
            return None
        contract = self.state.build_contract(self.username, self.file_path, new_content,
                                             local_repo_dir=self.project_path)
        result = self.state.submit_transaction(contract)
        # Retain the contract so a subsequent rejection can be rendered into a
        # line-precise, developer-framed delta.
        self._last_txn_contract = contract
        self._log_transaction_metric(contract, result, attempt_number)
        return result

    def _log_transaction_metric(self, contract, result, attempt_number):
        """Record one transaction attempt to the transaction_metrics table.

        Logged for commits AND rejections so rejection *rates* (not just outcomes)
        are computable, with widening_kind / rejection_reason / invalidated_by_* and
        the static-vs-sensor timing split feeding the trade-off analyses. Best-effort:
        never lets bookkeeping abort the work."""
        if self.db_manager is None or result is None:
            return
        try:
            widening = getattr(contract, "widening", None)
            widening_count = widening.total if widening is not None else 0
            widening_kind = None
            if widening is not None and widening.events:
                widening_kind = ",".join(sorted(widening.counts().keys()))
            self.db_manager.add_transaction_metric(
                txn_id=str(uuid.uuid4()),
                instance_id=self.project_name,
                agent_id=self.username,
                attempt_number=attempt_number,
                status=result.status,
                widening_events_count=widening_count,
                widening_kind=widening_kind,
                rejection_reason=result.rejection_reason,
                invalidated_by_symbol=result.invalidated_by_symbol,
                invalidated_by_agent=result.invalidated_by_agent,
                sensor_kind=getattr(result, "sensor_kind", None),
                static_check_ms=result.static_check_ms,
                sensor_check_ms=result.sensor_check_ms,
                experiment_version=self.experiment_version,
            )
        except Exception as e:
            logger.error(f"[TSS] failed to log transaction metric for {self.name}: {e}")

    def _render_state_rejection(self, result):
        """Render a transaction rejection into developer-facing feedback.

        Deterministic (no LLM call): delegates to the shared state's per-category
        renderer, which builds a developer-framed delta (old->new ground truth,
        committer + recency, exact stale line numbers from Tier-2 reference edges,
        refreshed authoritative snippet). The result flows into the next prompt
        through the existing add_review -> after_review_info channel. Falls back to
        a minimal summary if rendering is unavailable."""
        contract = getattr(self, "_last_txn_contract", None)
        state = getattr(self, "state", None)
        if state is not None and contract is not None:
            try:
                rendered = state.render_rejection(contract, result)
                if rendered:
                    return rendered
            except Exception as e:
                logger.error(f"[TSS] failed to render rejection for {self.name}: {e}")
        lines = ["## Shared Project State Conflict (your change was NOT applied)",
                 f"Reason: {result.rejection_reason}"]
        if result.detail:
            lines.append(result.detail)
        if result.invalidated_by_symbol:
            who = f" by `{result.invalidated_by_agent}`" if result.invalidated_by_agent else ""
            lines.append(f"The symbol `{result.invalidated_by_symbol}` your code depends on "
                         f"was changed or removed{who}.")
        lines.append("Update your code to be consistent with the current project state, then redo the change.")
        return "\n".join(lines)
