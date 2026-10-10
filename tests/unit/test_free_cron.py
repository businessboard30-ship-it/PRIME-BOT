"""free-cron/ is a Cloudflare Worker with its own Node tests (Node 22+, built-in SQLite as a D1 stand-in). Run them from pytest too."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "free-cron"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_free_cron_node_tests_pass():
    r = subprocess.run(["node", "--test", "test/logic.test.js", "test/worker.test.js"], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-1000:]


def test_free_cron_is_separate_from_the_bot_worker():
    toml = (ROOT / "wrangler.toml").read_text()
    assert 'name = "free-cron"' in toml and "BACKEND_URL" not in toml and "CRON_SECRET" not in toml
    assert '"*/5 * * * *"' in toml and "da31e9fd" in toml
    assert 'name = "prime-bot-cron"' in (ROOT.parent / "cron-worker" / "wrangler.toml").read_text()


def test_free_cron_never_stores_responses_or_follows_redirects():
    src = (ROOT / "src" / "index.js").read_text()
    assert 'redirect: "manual"' in src and "r.text()" not in src and "r.json()" not in src.split("export async function runDue")[1]
    page = "".join(f.read_text() for f in [ROOT / "src" / "page.js", *sorted((ROOT / "src" / "ui").glob("*.js"))])
    assert ".innerHTML" not in page and "outerHTML" not in page and "insertAdjacentHTML" not in page and "document.write" not in page


def test_deploy_workflow_runs_tests_migrations_and_never_prints_secrets():
    wf = (ROOT.parent / ".github" / "workflows" / "deploy-free-cron.yml").read_text()
    assert "npm test" in wf and "d1 migrations apply free-cron --remote" in wf and "free-cron/**" in wf
    assert "echo $" not in wf and "echo \"$val\"" not in wf
