"""pytest 共享夹具。"""

from __future__ import annotations

import os

import pytest

# 测试环境：使用 SQLite 内存库 + mock LLM + 本地沙箱
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("DATABASE_URL", "sqlite:///./data/test.db")
os.environ.setdefault("LLM_MODE", "mock")
os.environ.setdefault("GITHUB_TOKEN", "test-token")
os.environ.setdefault("SANDBOX_ENABLED", "false")
os.environ.setdefault("REDIS_URL", "")


@pytest.fixture(autouse=True)
def _reset_singletons():
    """每个测试前后重置单例与数据库，保证隔离。"""
    from app.config import reload_config
    from app.db.session import get_engine, reset_engine

    reload_config()
    reset_engine()

    # 清空所有表后重建，保证测试间数据隔离
    from app.db.base import Base
    from app.db.models import (  # noqa: F401  确保所有 ORM 模型已注册
        BlackboardORM,
        IssueORM,
        MemoryItemORM,
        PatchORM,
        ReviewTaskORM,
        ToolCallLogORM,
    )
    Base.metadata.drop_all(get_engine())
    Base.metadata.create_all(get_engine())

    from app.blackboard import reset_blackboard
    from app.llm import reset_llm_client
    from app.orchestrator import reset_orchestrator
    from app.tools import reset_all_tools

    reset_all_tools()
    reset_blackboard()
    reset_llm_client()
    reset_orchestrator()

    yield

    reset_all_tools()
    reset_blackboard()
    reset_llm_client()
    reset_orchestrator()
    reset_engine()


@pytest.fixture
def sample_changed_files():
    return [
        {"path": "src/auth.py", "status": "modified", "additions": 10, "deletions": 2, "changes": 12, "patch": ""},
        {"path": "tests/test_auth.py", "status": "modified", "additions": 5, "deletions": 1, "changes": 6, "patch": ""},
        {"path": "README.md", "status": "modified", "additions": 3, "deletions": 0, "changes": 3, "patch": ""},
        {"path": "requirements.txt", "status": "modified", "additions": 1, "deletions": 0, "changes": 1, "patch": ""},
    ]


@pytest.fixture
def sample_file_contents():
    return {
        "src/auth.py": '''\
import sqlite3

def login(username, password):
    conn = sqlite3.connect("users.db")
    query = "SELECT * FROM users WHERE username='" + username + "' AND password='" + password + "'"
    cursor = conn.execute(query)
    return cursor.fetchone()

def get_token():
    return "sk-1234567890abcdef"
''',
        "tests/test_auth.py": '''\
def test_login():
    assert login("admin", "pass") is not None
''',
    }


@pytest.fixture
def sample_diff():
    return '''\
diff --git a/src/auth.py b/src/auth.py
index 1234567..abcdefg 100644
--- a/src/auth.py
+++ b/src/auth.py
@@ -10,7 +10,9 @@ import sqlite3
 
 def login(username, password):
     conn = sqlite3.connect("users.db")
-    query = "SELECT * FROM users WHERE username='%s'" % username
+    query = "SELECT * FROM users WHERE username='" + username + "' AND password='" + password + "'"
     cursor = conn.execute(query)
     return cursor.fetchone()
+
+def get_token():
+    return "sk-1234567890abcdef"
'''
