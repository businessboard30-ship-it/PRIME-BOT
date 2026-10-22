import pytest
from api import dash
from utils import dash_schema as S
from tests.unit.test_dash_api import env, call, GUILD  # noqa: F401


class AppDB:
    def __init__(self, base):
        self.base, self.calls = base, []
    def __getattr__(self, n):
        return getattr(self.base, n)
    async def get_application_forms(self, gid, clone_id=None):
        return [{"id": 1, "title": "Staff", "status": "active", "pending": 2, "questions": ["Why?"], "review_channel_id": 100}]
    async def list_application_submissions(self, gid, clone_id=None, form_id=None, status=None, before=None, limit=31):
        self.calls.append((gid, form_id, status, before, limit))
        return [{"id": 9, "form_id": 1, "user_id": 5, "status": "pending", "answers": [{"q": "Why?", "a": "x" * 2000}]}]


@pytest.fixture
def adb(env, monkeypatch):
    fake, _ = env
    d = AppDB(fake)
    monkeypatch.setattr(dash, "db", d)
    return d


def test_forms_list_and_ids_are_strings(adb):
    st, p, _ = call("GET", {"action": "applications", "guild_id": str(GUILD)})
    assert st == 200 and p["forms"][0]["id"] == "1" and p["forms"][0]["pending"] == 2


def test_submissions_filters_passed_and_answers_truncated(adb):
    st, p, _ = call("GET", {"action": "application_submissions", "guild_id": str(GUILD), "form_id": "1", "status": "pending", "before": "20"})
    assert st == 200 and adb.calls[-1] == (GUILD, 1, "pending", 20, 31)
    assert len(p["submissions"][0]["answers"][0]["a"]) == S.APPLICATION_ANSWER_MAX


@pytest.mark.parametrize("q", [{"status": "bogus"}, {"form_id": "x"}, {"before": "x"}])
def test_bad_params(adb, q):
    assert call("GET", {"action": "application_submissions", "guild_id": str(GUILD), **q})[0] in (400,)


def test_auth_enforced(adb):
    assert call("GET", {"action": "application_submissions", "guild_id": "999"})[0] == 403
    assert call("GET", {"action": "application_submissions", "guild_id": str(GUILD)}, token=None)[0] == 401
    assert call("GET", {"action": "applications", "guild_id": "999"})[0] == 403


def test_read_only_no_write_routes():
    assert not any("application" in k for k in getattr(dash, "WRITES", {}) or {})
