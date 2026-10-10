from api.public_stats import build_body


def test_body_without_commands():
    assert build_body(12) == {"status": "ok", "servers": 12}
    assert build_body(12, "") == {"status": "ok", "servers": 12}
    assert build_body(12, "abc") == {"status": "ok", "servers": 12}


def test_body_with_commands():
    assert build_body(12, "87") == {"status": "ok", "servers": 12, "commands": 87}
