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
