import json
import os
import sqlite3
import threading
import time

import config

_MAGIS_SCHEMA = """
CREATE TABLE IF NOT EXISTS repository (
    repo_index INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name TEXT UNIQUE,
    path TEXT
);

CREATE TABLE IF NOT EXISTS task (
    task_index INTEGER PRIMARY KEY AUTOINCREMENT,
    description TEXT,
    task_type INTEGER,
    repo_index INTEGER,
    path TEXT,
    version INTEGER
);

CREATE TABLE IF NOT EXISTS agent_role (
    role_index INTEGER PRIMARY KEY AUTOINCREMENT,
    system_prompt TEXT,
    task_index INTEGER,
    name TEXT,
    username TEXT,
    user_path TEXT
);

CREATE TABLE IF NOT EXISTS chat_request (
    request_index INTEGER PRIMARY KEY AUTOINCREMENT,
    role_index INTEGER,
    user_prompt TEXT
);

CREATE TABLE IF NOT EXISTS chat_response (
    response_index INTEGER PRIMARY KEY AUTOINCREMENT,
    response_id TEXT,
    object TEXT,
    created INTEGER,
    model TEXT,
    choices TEXT,
    usage TEXT,
    system_fingerprint TEXT,
    request_index INTEGER
);

CREATE TABLE IF NOT EXISTS action (
    action_index INTEGER PRIMARY KEY AUTOINCREMENT,
    action_content TEXT
);

CREATE TABLE IF NOT EXISTS process (
    process_index INTEGER PRIMARY KEY AUTOINCREMENT,
    process_type INTEGER,
    operate_index INTEGER,
    task_index INTEGER
);

CREATE TABLE IF NOT EXISTS transaction_metrics (
    metric_index INTEGER PRIMARY KEY AUTOINCREMENT,
    txn_id TEXT,
    instance_id TEXT,
    agent_id TEXT,
    attempt_number INTEGER,
    status TEXT,
    widening_events_count INTEGER,
    widening_kind TEXT,
    rejection_reason TEXT,
    invalidated_by_symbol TEXT,
    invalidated_by_agent TEXT,
    sensor_kind TEXT,
    static_check_ms REAL,
    sensor_check_ms REAL,
    created_at REAL,
    experiment_version INTEGER
);
"""


class DatabaseManager:
    def __init__(self, db_path=config.db_path):
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        # Members may run on a thread pool (Manager.implement); allow cross-thread use
        # and serialize access so one shared connection stays safe.
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.cursor = self.conn.cursor()
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self.cursor.executescript(_MAGIS_SCHEMA)
            cols = {
                row[1]
                for row in self.cursor.execute("PRAGMA table_info(transaction_metrics)")
            }
            if "experiment_version" not in cols:
                self.cursor.execute(
                    "ALTER TABLE transaction_metrics ADD COLUMN experiment_version INTEGER"
                )
            self.conn.commit()

    def insert_data(self, query, data):
        with self._lock:
            self.cursor.execute(query, data)
            self.conn.commit()

    def _fetchone(self, query, data=()):
        with self._lock:
            return self.cursor.execute(query, data).fetchone()

    def _fetchall(self, query, data=()):
        with self._lock:
            return self.cursor.execute(query, data).fetchall()
    
    def add_process(self, process_type, operate_index, task_index):
        '''
        process_type: 
        - 1: role
        - 2: request
        - 3: response
        - 4: action
        '''
        row = self._fetchone(
            "SELECT process_index FROM process WHERE process_type = ? AND operate_index = ? AND task_index = ?",
            (process_type, operate_index, task_index),
        )
        if row is None:
            self.insert_data("INSERT INTO process (process_type, operate_index, task_index) VALUES (?, ?, ?)", (process_type, operate_index, task_index))
            row = self._fetchone(
                "SELECT process_index FROM process WHERE process_type = ? AND operate_index = ? AND task_index = ?",
                (process_type, operate_index, task_index),
            )
        return row[0]
    
    def add_repo(self, repo_full_name, repo_path, fill=True):
        row = self._fetchone("SELECT repo_index FROM repository WHERE full_name = ?", (repo_full_name,))
        if row is None and fill:
            self.insert_data("INSERT INTO repository (full_name, path) VALUES (?, ?)", (repo_full_name, repo_path))
            row = self._fetchone("SELECT repo_index FROM repository WHERE full_name = ?", (repo_full_name,))
        return row[0]
    
    def add_task(self, repo_index, description, version=None, path=None, fill=True):
        row = self._fetchone(
            "SELECT task_index FROM task WHERE repo_index = ? AND description = ? AND version = ?",
            (repo_index, description, version),
        )
        if row is None and fill:
            self.insert_data("INSERT INTO task (description, task_type, repo_index, path, version) VALUES (?, ?, ?, ?, ?)", \
                                    (description, 1, repo_index, path, version)) # 1: issue
            row = self._fetchone(
                "SELECT task_index FROM task WHERE repo_index = ? AND description = ? AND version = ?",
                (repo_index, description, version),
            )
        return row[0]

    def add_role(self, task_index, system_prompt, name=None, username=None, user_path=None, fill=True):
        row = self._fetchone(
            "SELECT role_index FROM agent_role WHERE task_index = ? AND system_prompt = ?",
            (task_index, system_prompt),
        )
        if row is None and fill:
            self.insert_data("INSERT INTO agent_role (system_prompt, task_index, name, username, user_path) VALUES (?, ?, ?, ?, ?)", \
                             (system_prompt, task_index, name, username, user_path))
            row = self._fetchone(
                "SELECT role_index FROM agent_role WHERE task_index = ? AND system_prompt = ?",
                (task_index, system_prompt),
            )
        self.add_process(1, row[0], task_index)
        return row[0]
    
    def add_request(self, role_index, user_prompt, fill=True):
        if role_index is None:
            return None
        row = self._fetchone(
            "SELECT request_index FROM chat_request WHERE role_index = ? AND user_prompt = ?",
            (role_index, user_prompt),
        )
        if row is None and fill:
            self.insert_data("INSERT INTO chat_request (role_index, user_prompt) VALUES (?, ?)", (role_index, user_prompt))
            row = self._fetchone(
                "SELECT request_index FROM chat_request WHERE role_index = ? AND user_prompt = ?",
                (role_index, user_prompt),
            )
        task_index = self._fetchone("SELECT task_index FROM agent_role WHERE role_index = ?", (role_index,))[0]
        self.add_process(2, row[0], task_index)
        return row[0]
    
    def add_response(self, request_index, response, fill=True):
        row = self._fetchone("SELECT response_index FROM chat_response WHERE request_index = ?", (request_index,))
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
            row = self._fetchone("SELECT response_index FROM chat_response WHERE request_index = ?", (request_index,))
            
        role_index = self._fetchone("SELECT role_index FROM chat_request WHERE request_index = ?", (request_index,))[0]
        task_index = self._fetchone("SELECT task_index FROM agent_role WHERE role_index = ?", (role_index,))[0]
        self.add_process(3, row[0], task_index)
        return row[0]
    
    def add_action(self, task_index, action_content, fill=True):
        if action_content is None or task_index is None:
            return None
        row = self._fetchone("SELECT action_index FROM action WHERE action_content = ?", (action_content,))
        if row is None and fill:
            self.insert_data("INSERT INTO action (action_content) VALUES (?)", (action_content,))
            row = self._fetchone("SELECT action_index FROM action WHERE action_content = ?", (action_content,))
        self.add_process(4, row[0], task_index)
        return row[0]
    
    def add_transaction_metric(self, txn_id, instance_id, agent_id, attempt_number, status,
                               widening_events_count=0, widening_kind=None, rejection_reason=None,
                               invalidated_by_symbol=None, invalidated_by_agent=None, sensor_kind=None,
                               static_check_ms=None, sensor_check_ms=None, experiment_version=None):
        '''Record ONE transaction attempt (commit OR reject) from the Transactional
        Shared State, so rejection *rates* and the dynamic-typing-tax / failure-shift /
        fail-fast trade-off analyses are computable. Logged for every attempt, not
        just the final outcome.'''
        self.insert_data(
            "INSERT INTO transaction_metrics (txn_id, instance_id, agent_id, attempt_number, status, "
            "widening_events_count, widening_kind, rejection_reason, invalidated_by_symbol, "
            "invalidated_by_agent, sensor_kind, static_check_ms, sensor_check_ms, created_at, "
            "experiment_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (txn_id, instance_id, agent_id, attempt_number, status, widening_events_count, widening_kind,
             rejection_reason, invalidated_by_symbol, invalidated_by_agent, sensor_kind,
             static_check_ms, sensor_check_ms, time.time(), experiment_version))

    def clean_up_process(self, task_index):
        with self._lock:
            row = self.cursor.execute(
                "SELECT process_type, operate_index FROM process WHERE task_index = ?",
                (task_index,),
            ).fetchall()
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
