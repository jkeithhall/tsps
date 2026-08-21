from operate import *
from chat_with_LLM import chat
from log import *
from common import *
from manager import Manager
from custodian import Custodian
from transactional_state import TransactionalSharedState
import config

import argparse

logger = log(__name__).get_log_obj()

# Group / 1v1 Meeting
class Meeting:
    def __init__(self, recording_file_path, attendee_list, max_round_num=10, target=None) -> None:
        recording_file_path = os.path.abspath(recording_file_path)
        logger.info(f"Meeting started. The recording file is saved at: {recording_file_path}")
        os.makedirs(os.path.dirname(recording_file_path), exist_ok=True)

        prefix = "```markdown\n"
        self.recording_content = f"{prefix}## Meeting Object\n{target}\n\n## Attendee\n"
        for each_member in attendee_list:
            self.recording_content += f"- {each_member.get_intro()}\n"
        self.recording_content += "\n## Discussion\n"
        self.round = 0
        break_flag = False
        while self.round < max_round_num:
            for attendee in attendee_list:
                new_content = self.in_turn(attendee, target)
                if isinstance(attendee, Manager) and new_content is None:
                    break_flag = True
                    break
                elif new_content is not None:
                    self.recording_content += f"### {attendee.name}\n{new_content}\n\n"
                    with open(recording_file_path, "w") as f:
                        f.write(self.recording_content[len(prefix):])
            self.round += 1
            if break_flag:
                break
        new_content = self.in_turn(attendee_list[0], target, to_end=True) # suppose that attendee_list[0] is the manager
        self.recording_content += f"### {attendee.name}\n{new_content}\n\n"
        with open(recording_file_path, "w") as f:
            f.write(self.recording_content[len(prefix):])
        logger.info(f"Meeting finished. The recording file is saved at: {recording_file_path}")
        
    # Speak in turn
    def in_turn(self, attendee, target, to_end=False):
        if isinstance(attendee, Manager):
            if self.round == 0:
                speech_content = attendee.group_meeting(current_discussion=None, target=target)
            else:
                speech_content = attendee.group_meeting(current_discussion=f"{self.recording_content}```", target=target, to_end=to_end)
                if "<FINISH>" in speech_content:
                    speech_content =  None
        else:
            speech_content = attendee.group_meeting(current_discussion=f"{self.recording_content}```")
            if "<NONE>" in speech_content:
                speech_content =  None
        return speech_content
    
def main(args):
    try:
        enable_tsps = getattr(args, "enable_tsps", config.enable_tsps)
        # Shared Transactional State for this task, created once here (the orchestrator)
        # so the Custodian can populate it and the Manager/members can share it. Keyed by
        # the instance id and reset for fresh-run isolation.
        state = None
        if enable_tsps:
            try:
                state = TransactionalSharedState(repo_key=os.path.basename(args.repo_local_path))
                state.reset()
            except Exception as e:
                logger.error(f"Failed to create TransactionalSharedState: {e}")
                state = None
        else:
            logger.info("TSPS disabled for this run; using non-transactional ablation mode.")

        # (Step 0): prepare the candidate file list
        if args.candidate_file_path_list is None:
            args.candidate_file_path_list = []
            repo_full_name, issue_number = os.path.basename(args.repo_local_path).rsplit('-', 1)
            with open(args.requirement_file_path) as f:
                issue_content = f.read()
            logger.info("Ask the custodian to find the candidate file list ...")
            custodian = Custodian(repo_full_name, issue_number, issue_content, 
                                  model_name=args.model_name, use_cache=True,
                                  repository_directory = args.repo_local_path, bm_top_k = args.bm_top_k,
                                  state=state)
            args.candidate_file_path_list = custodian.get_candidate_file_list()

        if args.only_find_file_list:
            return args.candidate_file_path_list
        
        # Step 1: prepare team members
        manager = Manager(args.requirement_file_path, args.repo_local_path, \
                          model_name=args.model_name, candidate_file_path_list = args.candidate_file_path_list, \
                          granularity=args.granularity, prepare_member_folder=False, version=args.version, \
                          hints_text=args.hints_text, state=state, enable_tsps=enable_tsps)
        # ## Step 2: group meeting (double check each one's plan for the whole requirement on this project)
        kick_off_meeting_target = ( "1. Verify if the combination of instructions received by all team members is sufficient to resolve the Issue."
                                    "2. Evaluate whether the tasks assigned to each member can be executed simultaneously."
                                    " If concurrent execution is not feasible, determine the sequence in which they should be completed.")
        logger.debug(f"Type(str(args.version)):{type(str(args.version))}")
        kick_off_meeting_recording_file_path = os.path.join(config.virtual_env_path, str(args.version), \
                                                            os.path.basename(args.repo_local_path), "kick_off_meeting.md")
        kick_off_meeting = Meeting(kick_off_meeting_recording_file_path, [manager]+manager.team_member_list, max_round_num=10, 
                                   target = kick_off_meeting_target)
        # ## determine if member need to change the role / task / plan by manager (if so, the manager should update the plan) TODO
        # ## Step 3: implement plan
        # ### 3.1 Determine the order of work of each member
        with open(kick_off_meeting_recording_file_path, "r") as f:
            kick_off_meeting_content = f.read()
        main_code = manager.get_member_order(kick_off_meeting_content)
        logger.info(f"Main code: \n{main_code}")
        
        # # ### 3.2 Implement the plan
        manager.implement()

        return manager.merge_code_change()

    except Exception as e:
        logger.error("Main program error:")
        logger.error(e)
        logger.error(traceback.format_exc())

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo_local_path", type=str, default="/mnt/workspace/taow/virtual_env/manager/astropy__astropy-11693", help="Path to local repo", required=True)
    parser.add_argument("--requirement_file_path", type=str, default="/mnt/workspace/taow/virtual_env/manager/astropy__astropy-11693/new_require.txt", help="Path to requirement file", required=True)
    parser.add_argument("--candidate_file_path_list", type=list, default=None, help="Path to candidate file path list")
    parser.add_argument("--version", type=int, default=231219, help="Version of the project")
    parser.add_argument("--granularity", type=str, default="file", help="Granularity of the processing")
    parser.add_argument("--only_find_file_list", type=bool, default=False, help="Only find file list")
    parser.add_argument("--bm_top_k", type=int, default=20, help="Top k for BM25")
    parser.add_argument("--model_name", default=config.default_model_name, help="Chat model id")
    parser.add_argument("--hints_text", action="store_true", help="(Optional) Use hints_text with issue description") # TODO
    parser.add_argument("--enable_tsps", action=argparse.BooleanOptionalAction, default=config.enable_tsps,
                        help="Enable TSPS orchestration (default: on). Pass --no-enable-tsps for ablation runs.")
    args = parser.parse_args()
    main(args)