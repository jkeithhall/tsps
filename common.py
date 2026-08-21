import re

from chat_with_LLM import chat
from difflib import unified_diff
from operate import get_file_content

import tiktoken


def _get_encoding(model_name: str):
    try:
        return tiktoken.encoding_for_model(model_name)
    except KeyError:
        # Claude and other non-OpenAI models: approximate with cl100k_base.
        return tiktoken.get_encoding("cl100k_base")


# common functions that may be used by many agents
def get_name(role, model_name, use_cache=True):
    system_prompt = f"You are an expert at naming people."
    user_prompt = (f"Please name the people based on the role description.\n"
                   f"Role description: {role}\n"
                   "Your answer should be no more than three words.")
    name = chat(system_prompt, user_prompt, model_name = model_name, use_cache = use_cache).strip()
    return name

def merge_multiple_items(requirement_list=None, file_path_list=None):
    if requirement_list is not None:
        requirement_text = "```\n"
        for idx, each_requirement in enumerate(requirement_list):
            requirement_text += f"{idx+1}: {each_requirement}\n"
        requirement_text += "```\n"
        return requirement_text
    if file_path_list is not None:
        file_content = ""
        for idx, file_path in enumerate(file_path_list):
            file_content += f"file_path:{file_path}\n{get_file_content(file_path)}\n"
        return file_content

def to_safe_folder_name(s):
    s = re.sub(r'[?*:|"<>\\\/\']', '_', s)
    s = s[:20] # 255
    if s.startswith('.'):
        s = 'folder' + s
    return s

def generate_diff(file1_path, file2_path):
    with open(file1_path, 'r') as file1:
        file1_lines = file1.readlines()
    with open(file2_path, 'r') as file2:
        file2_lines = file2.readlines()
    diff_lines = unified_diff(file1_lines, file2_lines, fromfile=file1_path, tofile=file2_path)
    diff_string = '\n'.join(diff_lines)
    return diff_string


def get_token_num(model_name, system_prompt, user_prompt=None):
    enc = _get_encoding(model_name)
    length_system_prompt = len(enc.encode(system_prompt))
    if user_prompt is None:
        return length_system_prompt
    return len(enc.encode(user_prompt)) + length_system_prompt


def truncate_user_prompt(model_name, system_prompt, user_prompt, max_token_num):
    enc = _get_encoding(model_name)
    length_system_prompt = len(enc.encode(system_prompt))
    return enc.decode(enc.encode(user_prompt)[:max_token_num - length_system_prompt])