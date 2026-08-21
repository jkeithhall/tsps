"""Default on-disk location of the TSPS SQLite store.

Resolved relative to this package's parent directory (the project root)
rather than this file's own directory, so the path is the same regardless of
how deeply nested the module that asks for it is.
"""

import os

_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_PACKAGE_DIR)

_DEFAULT_DB_PATH = os.path.join(_PROJECT_ROOT, "sqlite_db", "transactional_state.db")
