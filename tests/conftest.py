import os
import sys

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/testdb")
os.environ.setdefault("SINOBANED2_BOT_TOKEN", "111111:main-bot-fake-token-for-tests")
os.environ.setdefault("ADMIN_ID", "999999")
os.environ.setdefault("PUBLIC_BASE_URL", "https://example-deploy.vercel.app")
os.environ.setdefault("CLONE_BOT_REAL_ENABLED", "true")
os.environ.setdefault("ENCRYPTION_KEY", "eJgAECfAN2UynnjQcxqPCNjIxqLrS8kLiC_EQ92zNs8=")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
API_DIR = os.path.join(ROOT, "api")
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _catch_open_to_all_servers_in_tests(monkeypatch):
    """The catch game is limited to the support server in production (config
    CATCH_SUPPORT_SERVER_ONLY). Existing tests use arbitrary guild ids, so they run with the
    restriction off; tests/unit/test_catch_support_only.py turns it back on explicitly."""
    import config

    monkeypatch.setenv("CATCH_SUPPORT_SERVER_ONLY", "0")
    monkeypatch.setattr(config, "CATCH_SUPPORT_SERVER_ONLY", False, raising=False)
