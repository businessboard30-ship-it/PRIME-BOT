import logging

from modules import log_feed as lf


def rec(msg, level=logging.ERROR, name="discord_bot.cogs.ai_tools", exc=None):
    return logging.LogRecord(name, level, "f.py", 1, msg, (), exc)


def test_only_wanted_records_are_queued():
    h = lf.QueueHandler()
    for r in (rec("boom"), rec("warn", logging.WARNING), rec("[aichat] second turn", logging.INFO), rec("noise", logging.INFO),
              rec("discord noise", logging.ERROR, "discord.gateway"), rec("loop", logging.ERROR, "modules.log_feed")):
        h.emit(r)
    lines = h.drain()
    assert len(lines) == 3 and "boom" in lines[0] and "noise" not in " ".join(lines) and "loop" not in " ".join(lines)
    assert h.drain() == []


def test_secrets_are_masked_and_fences_neutralised():
    h = lf.QueueHandler()
    fake = "ghp_" + "Ab1" * 12            # built at runtime: never a literal token in the repo
    h.emit(rec(f"token={fake} and ``` break"))
    out = h.drain()[0]
    assert fake not in out and "```" not in out


def test_exception_text_is_included_and_long_lines_cut():
    try:
        raise ValueError("bad thing")
    except ValueError:
        import sys
        line = lf.format_record(rec("failed", exc=sys.exc_info()))
    assert "ValueError: bad thing" in line
    assert len(lf.format_record(rec("x" * 5000))) <= lf.LINE_WIDTH


def test_queue_overflow_is_reported_not_crashing():
    h = lf.QueueHandler()
    for i in range(lf.MAX_QUEUE + 25):
        h.emit(rec(f"e{i}"))
    out = h.drain()
    assert out[0].startswith("... 25 older") and len(out) == lf.MAX_QUEUE + 1


def test_chunks_fit_discord_limit():
    msgs = lf.chunks(["y" * 250] * 40, "main bot")
    assert len(msgs) > 1 and all(0 < len(m) <= 2000 for m in msgs) and msgs[0].startswith("**main bot**")
    assert lf.chunks([], "x") == []


def test_channel_id_parsing(monkeypatch):
    monkeypatch.delenv("LOG_CHANNEL_ID", raising=False)
    assert lf.channel_id() is None
    monkeypatch.setenv("LOG_CHANNEL_ID", "123456789")
    assert lf.channel_id() == 123456789
    monkeypatch.setenv("LOG_CHANNEL_ID", "abc")
    assert lf.channel_id() is None


def test_cog_is_loaded_and_never_logs_inside_its_loop():
    assert "discord_bot.cogs.log_channel" in open("discord_bot/bot.py").read()
    src = open("discord_bot/cogs/log_channel.py").read()
    assert "logger." not in src.split("async def flush")[1].split("async def setup")[0]
