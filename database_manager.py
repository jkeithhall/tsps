import json
import sqlite3

import config

class DatabaseManager:
    def __init__(self, db_path=config.db_path):
        self.conn = sqlite3.connect(db_path)
        self.cursor = self.conn.cursor()

    def insert_data(self, query, data):
        self.cursor.execute(query, data)
        self.conn.commit()

    def query_data(self, query, data):
        return self.cursor.execute(query, data)
    
    def add_process(self, process_type, operate_index, task_index):
        '''
        process_type: 
        - 1: role
        - 2: request
        - 3: response
        - 4: action
        '''
        row = self.query_data(f"SELECT process_index FROM process WHERE process_type = ? AND operate_index = ? AND task_index = ?", \
                              (process_type, operate_index, task_index)).fetchone()
        if row is None:
            self.insert_data("INSERT INTO process (process_type, operate_index, task_index) VALUES (?, ?, ?)", (process_type, operate_index, task_index))
            row = self.query_data(f"SELECT process_index FROM process WHERE process_type = ? AND operate_index = ? AND task_index = ?", \
                                    (process_type, operate_index, task_index)).fetchone()
        return row[0]
    
    def add_repo(self, repo_full_name, repo_path, fill=True):
        row = self.query_data(f"SELECT repo_index FROM repository WHERE full_name = ?", (repo_full_name, )).fetchone()
        if row is None and fill:
            self.insert_data("INSERT INTO repository (full_name, path) VALUES (?, ?)", (repo_full_name, repo_path))
            row = self.query_data(f"SELECT repo_index FROM repository WHERE full_name = ?", (repo_full_name, )).fetchone()
        return row[0]
    
    def add_task(self, repo_index, description, version=None, path=None, fill=True):
        row = self.query_data(f"SELECT task_index FROM task WHERE repo_index = ? AND description = ? AND version = ?", (repo_index, description, version)).fetchone()
        if row is None and fill:
            self.insert_data("INSERT INTO task (description, task_type, repo_index, path, version) VALUES (?, ?, ?, ?, ?)", \
                                    (description, 1, repo_index, path, version)) # 1: issue
            row = self.query_data(f"SELECT task_index FROM task WHERE repo_index = ? AND description = ? AND version = ?", (repo_index, description, version)).fetchone()
        return row[0]

    def add_role(self, task_index, system_prompt, name=None, username=None, user_path=None, fill=True):
        row = self.query_data(f"SELECT role_index FROM agent_role WHERE task_index = ? AND system_prompt = ?", (task_index, system_prompt)).fetchone()
        if row is None and fill:
            self.insert_data("INSERT INTO agent_role (system_prompt, task_index, name, username, user_path) VALUES (?, ?, ?, ?, ?)", \
                             (system_prompt, task_index, name, username, user_path))
            row = self.query_data(f"SELECT role_index FROM agent_role WHERE task_index = ? AND system_prompt = ?", (task_index, system_prompt)).fetchone()
        self.add_process(1, row[0], task_index)
        return row[0]
    
    def add_request(self, role_index, user_prompt, fill=True):
        if role_index is None:
            return None
        row = self.query_data(f"SELECT request_index FROM chat_request WHERE role_index = ? AND user_prompt = ?", (role_index, user_prompt)).fetchone()
        if row is None and fill:
            self.insert_data("INSERT INTO chat_request (role_index, user_prompt) VALUES (?, ?)", (role_index, user_prompt))
            row = self.query_data(f"SELECT request_index FROM chat_request WHERE role_index = ? AND user_prompt = ?", (role_index, user_prompt)).fetchone()
        task_index = self.query_data(f"SELECT task_index FROM agent_role WHERE role_index = ?", (role_index,)).fetchone()[0]
        self.add_process(2, row[0], task_index)
        return row[0]
    
    def add_response(self, request_index, response, fill=True):
        row = self.query_data(f"SELECT response_index FROM chat_response WHERE request_index = ?", (request_index,)).fetchone()
        if row is None and fill:
            if "claude" in response['model']:
                self.insert_data("INSERT INTO chat_response (response_id, object, created, model, choices, usage, system_fingerprint, request_index) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", \
                                    (response['id'], None, None, response['model'], json.dumps(response['content']), json.dumps(response['usage']), \
                                        None, request_index))
            elif "abab" in response['model'] or "fireworks" in response['model']:
                self.insert_data("INSERT INTO chat_response (response_id, object, created, model, choices, usage, system_fingerprint, request_index) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", \
                                    (response['id'], response['object'], response['created'], response['model'], json.dumps(response['choices']), json.dumps(response['usage']), \
                                        response['id'], request_index))
            else: #if "gpt" in response['model']:
                self.insert_data("INSERT INTO chat_response (response_id, object, created, model, choices, usage, system_fingerprint, request_index) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", \
                                    (response['id'], response['object'], response['created'], response['model'], json.dumps(response['choices']), json.dumps(response['usage']), \
                                        response['system_fingerprint'], request_index))
            row = self.query_data(f"SELECT response_index FROM chat_response WHERE request_index = ?", (request_index,)).fetchone()
            
        role_index = self.query_data(f"SELECT role_index FROM chat_request WHERE request_index = ?", (request_index,)).fetchone()[0]
        task_index = self.query_data(f"SELECT task_index FROM agent_role WHERE role_index = ?", (role_index,)).fetchone()[0]
        self.add_process(3, row[0], task_index)
        return row[0]
    
    def add_action(self, task_index, action_content, fill=True):
        if action_content is None or task_index is None:
            return None
        row = self.query_data(f"SELECT action_index FROM action WHERE action_content = ?", (action_content,)).fetchone()
        if row is None and fill:
            self.insert_data("INSERT INTO action (action_content) VALUES (?)", (action_content,))
            row = self.query_data(f"SELECT action_index FROM action WHERE action_content = ?", (action_content,)).fetchone()
        self.add_process(4, row[0], task_index)
        return row[0]
    
    def clean_up_process(self, task_index):
        row = self.query_data(f"SELECT process_type, operate_index FROM process WHERE task_index = ?", (task_index,)).fetchall()
        if row is not None:
            for process in row:
                process_type = process[0]
                operate_index = process[1]
                if process_type == 1:
                    self.cursor.execute("DELETE FROM agent_role WHERE role_index = ?", (operate_index,))
                elif process_type == 2:
                    self.cursor.execute("DELETE FROM chat_request WHERE request_index = ?", (operate_index,))
                elif process_type == 3:
                    self.cursor.execute("DELETE FROM chat_response WHERE response_index = ?", (operate_index,))
                elif process_type == 4:
                    self.cursor.execute("DELETE FROM action WHERE action_index = ?", (operate_index,))
            self.cursor.execute("DELETE FROM process WHERE task_index = ?", (task_index,))
            self.conn.commit()

    def close(self):
        self.cursor.close()
        self.conn.close()
