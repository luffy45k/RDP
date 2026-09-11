"""my_ai_tool — self-running, self-updating, self-healing terminal AI agent.

Data is NEVER stored inside the code folder: it always lives in the OS
user-data folder so that code updates / deletes never destroy it.

    Linux/Mac : ~/.my_ai_tool/            (database.db, config.json, logs/)
    Windows   : %APPDATA%/my_ai_tool/
"""

__version__ = "1.0.0"
APP_NAME = "mytool"
DATA_DIR_NAME = "my_ai_tool"
