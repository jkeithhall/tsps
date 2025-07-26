import json
import re

from flask import Flask, render_template
import sqlite3

import db_config

app = Flask(__name__)

def regex_match(value, pattern):
    return re.match(pattern, value) is not None

app.jinja_env.filters['regex_match'] = regex_match

def get_task_data(task_id):
    conn = sqlite3.connect(db_config.db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT process_type, operate_index FROM process WHERE task_index = ? ORDER BY process_index ASC", (task_id,))
    process_group = cursor.fetchall()
    process_content = list()
    
    role_content = dict()
    cursor.execute("SELECT role_index, system_prompt, name, username, user_path FROM agent_role WHERE task_index = ?", (task_id,))
    role_group = cursor.fetchall()
    for role in role_group:
        role_content[role[0]] = role[1:]

    for process in process_group:
        process_type = process[0]
        operate_index = process[1]

        if process_type == 1:
            cursor.execute("SELECT role_index, system_prompt, name, username, user_path FROM agent_role WHERE task_index = ? AND role_index = ?", \
                           (task_id, operate_index))
            result = cursor.fetchone()
            
        elif process_type == 2:
            cursor.execute("SELECT request_index, role_index, user_prompt FROM chat_request WHERE request_index = ?", (operate_index,))
            result = cursor.fetchone()

        elif process_type == 3:
            cursor.execute("SELECT response_index, model, choices, request_index FROM chat_response WHERE response_index = ?", (operate_index,))
            result = cursor.fetchone()
            result = list(result)
            if "claude" in result[1]:
                result[2] = json.loads(result[2])[0]["text"]
            else:#if "gpt" in result[1]:
                result[2] = json.loads(result[2])[0]["message"]["content"]
            # list to turple
            result = tuple(result)

        elif process_type == 4:
            cursor.execute("SELECT action_index, action_content FROM action WHERE action_index = ?", (operate_index,))
            result = cursor.fetchone()

        process_content.append((process_type, result))
    print(role_content.keys())
    conn.close()
    return role_content, process_content

def get_pr_url(task_id):
    conn = sqlite3.connect(db_config.db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT path FROM task WHERE task_index = ?", (task_id,))
    result = cursor.fetchone()
    conn.close()
    repo_and_pr_number = result[0].split('/')[-1]
    repo_name = '-'.join(repo_and_pr_number.split('-')[0:-1]).replace('__', '/')
    pr_number = repo_and_pr_number.split('-')[-1]
    url = f"https://github.com/{repo_name}/pull/{pr_number}/files"
    return url

def get_version_id(task_id):
    conn = sqlite3.connect(db_config.db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT version FROM task WHERE task_index = ?", (task_id,))
    result = cursor.fetchone()
    conn.close()
    return result[0]

def get_task_name(task_id):
    conn = sqlite3.connect(db_config.db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT path FROM task WHERE task_index = ?", (task_id,))
    result = cursor.fetchone()
    conn.close()
    if result is None:
        return None
    return result[0].split('/')[-1]

def get_all_versions():
    conn = sqlite3.connect(db_config.db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT version FROM task ORDER BY version DESC")
    versions = cursor.fetchall()
    conn.close()
    return versions

def get_all_tasks(version):
    conn = sqlite3.connect(db_config.db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT task_index, path FROM task WHERE version = ?", (version,))
    tasks = cursor.fetchall()
    conn.close()
    return tasks

@app.route('/')
def index():
    versions = get_all_versions()
    return render_template('index.html', versions=versions)

@app.route('/version/<int:version_id>')
def version(version_id):
    tasks = get_all_tasks(version_id)
    return render_template('version.html', tasks=tasks)

@app.route('/task/<int:task_id>')
def task(task_id):
    task_name = get_task_name(task_id)
    if task_name is None:
        return render_template('error.html', error_message="Task not found")
    role_content, process_content = get_task_data(task_id)
    return render_template('task.html', role_content=role_content, process_content=process_content, 
                           pr_url=get_pr_url(task_id), task_name=task_name, task_id=task_id, 
                           version=get_version_id(task_id))

if __name__ == '__main__':
    app.run(debug=True)
