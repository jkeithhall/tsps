# communicate with / operate the computer / OS / file
import os
import subprocess
import shlex

from pygments.lexers import get_lexer_for_filename, ClassNotFound

from log import *
logger = log(__name__).get_log_obj()


def get_file_content(file_path, add_number=False):
    if not os.path.exists(file_path):
        return None
    try:
        programming_language = get_lexer_for_filename(file_path).name
    except ClassNotFound:
        programming_language = ''
    with open(file_path, "r") as f:
        content = f.read()
    if add_number:
        content = '\n'.join([f"{i+1} | {line}" for i, line in enumerate(content.splitlines())])
    markdown_format_content = f"```{programming_language}\n{content}\n```"
    return markdown_format_content

def browse_folder_structure(folder_path='.'):
    structure = {}
    for item in os.listdir(folder_path):
        full_path = os.path.join(folder_path, item)
        if os.path.isdir(full_path):
            structure[item] = browse_folder_structure(full_path)
        else:
            structure[item] = None
    return structure

def get_git_tracked_file_path_list(base_path="."):
    # Return a list of all files in a repository that are not ignored by .gitignore.
    try:
        # Use the git command to retrieve all tracked files (including those not staged/committed)
        # This will automatically exclude files that are in the .gitignore.
        # This command works from the repo's root directory.
        git_ls_files = subprocess.check_output(
            ['git', 'ls-files', base_path],
            stderr=subprocess.STDOUT,
            universal_newlines=True
        ).splitlines()

        return [os.path.join(base_path, f) for f in git_ls_files]
    except subprocess.CalledProcessError as e:
        logger.info(f"Error executing git command: {e}")
        return []

def get_git_tracked_files_structure(repo_root, tracked_files):

    all_parts = [file.split(os.sep) for file in tracked_files]
    all_parts = [part[1:] if part[0] == '.' else part for part in all_parts]
    result = []

    def append_parts(parts, indent=0):
        if not parts:
            return
        current_level = {}
        for part in parts:
            if part[0] not in current_level:
                current_level[part[0]] = []
            if len(part) > 1:
                current_level[part[0]].append(part[1:])
        
        for key, subparts in sorted(current_level.items()):
            result.append('    ' * indent + '|-- ' + key)
            append_parts(subparts, indent + 1)

    result.append(f"{os.path.basename(os.path.normpath(os.path.abspath(repo_root)))}/")
    append_parts(all_parts)

    return '\n'.join(result)

def is_code_or_text(filename, endswith=None):
    try:
        lexer = get_lexer_for_filename(filename)
        if endswith is None or filename.endswith(endswith):
            return True
    except ClassNotFound:
        return False

def check_syntax(file_path):
    try:
        with open(file_path, 'r') as file:
            code = file.read()
        compile(code, file_path, "exec")
        return True
    except SyntaxError as e:
        return e

class LinuxEnv:
    def __init__(self, user_directory, GitConfig):
        self.user_directory = user_directory # os.path.abspath() TODO all paths in this file
        if not os.path.exists(self.user_directory):
            os.mkdir(self.user_directory)
        self.git_config = GitConfig

    def bash(self, bash_command, run_directory=None):
        if run_directory is None:
            run_directory = self.user_directory
        os.chdir(run_directory)
        if isinstance(bash_command, str):
            bash_command = shlex.split(bash_command)
        elif isinstance(bash_command, list):
            pass
        result = subprocess.run(bash_command, capture_output=True, text=True)
        return result
    

class GitConfig:
    def __init__(self, name, email_domain=None):
        self.name = name
        self.email_domain = email_domain
        if self.email_domain is None:
            self.email = None
        else:
            self.email = f"{name}@{email_domain}"

    def gen_config_file(self, project_directory):
        git_config_file_path = os.path.join(project_directory, ".git/config")
        if os.path.exists(git_config_file_path):
            m = "a+"
        else:
            m = "w"
        with open(git_config_file_path, m) as f:
            f.write(f"[user]\n\tname = {self.name}\n\temail = {self.email}")