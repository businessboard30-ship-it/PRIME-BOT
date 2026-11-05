"""Groq rejected the WHOLE tool call when the model sent null for an optional field ("/role: expected string, but got null"),
and the bot then fell back to plain chat (talks, never acts). Optional fields now accept null, nulls are dropped, and a validation
failure is retried once with tools before degrading."""
import asyncio
from types import SimpleNamespace as N

import pytest

from modules import ai_features as af
from modules import ai_skills
from modules.ai_tool_schema import drop_nulls, nullable

jsonschema = pytest.importorskip("jsonschema")


def test_nullable_helper():
    assert nullable({"type": "string"}) == {"type": ["string", "null"]}
    assert nullable({"type": "string", "enum": ["a", "b"]}) == {"type": ["string", "null"], "enum": ["a", "b", None]}
    assert nullable({"type": ["integer", "null"]}) == {"type": ["integer", "null"]}
    assert drop_nulls({"a": 1, "b": None, "c": ""}) == {"a": 1, "c": ""}


def _schema(props, required):
    return {"type": "object", "properties": props, "required": required}


def test_command_schema_accepts_null_only_for_optional_fields():
    from discord_bot.cogs.ai_tools import AIToolsCog
    cog = AIToolsCog.__new__(AIToolsCog)
    cog._param_defs = lambda command: [
        {"name": "channel", "type": "channel", "description": "c", "required": True},
        {"name": "role", "type": "role", "description": "r", "required": False},
        {"name": "mode", "type": "string", "description": "m", "required": False, "enum": ["a", "b"]}]
    fn = cog._command_tool_schema(N(name="channeledit_lock", description="d"), N())["function"]["parameters"]
    jsonschema.validate({"channel": "#general", "role": None, "mode": None}, fn)         # the exact failing shape
    jsonschema.validate({"channel": "#general", "role": "Mods", "mode": "a"}, fn)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"channel": None}, fn)                                         # required stays strict
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"channel": "#g", "mode": "zzz"}, fn)                          # enums stay enforced


def test_skill_schema_accepts_null_for_optional_fields():
    for sk in ai_skills.all_skills():
        fn = ai_skills.tool_schema(sk)["function"]["parameters"]
        nulls = {f.name: None for f in sk.fields if not f.required}
        base = {f.name: ("x" if f.kind != "int" else 1) for f in sk.fields if f.required}
        for f in sk.fields:
            if f.required and f.kind == "choice":
                base[f.name] = f.choices[0]
        jsonschema.validate({**base, **nulls}, fn)


def test_validation_error_detection():
    assert af._is_tool_validation_error('{"error":{"message":"Tool call validation failed: parameters for tool x did not match schema"}}')
    assert af._is_tool_validation_error("did not match schema")
    assert not af._is_tool_validation_error("rate limit")
    assert not af._is_tool_validation_error(None)


def test_clip_args_drops_nulls():
    from discord_bot.cogs.ai_tools import AIToolsCog
    assert AIToolsCog._clip_args({"channel": "#g", "role": None, "reason": "x" * 2000}) == {"channel": "#g", "reason": "x" * 1000}


def test_ai_features_retries_with_tools_before_dropping_them():
    src = open("modules/ai_features.py", encoding="utf-8").read()
    retry = src.index("_is_tool_validation_error(error_text)")
    drop = src.index("retrying without tools")
    assert retry < drop and "TOOL_FORMAT_REMINDER" in src[retry:drop]
    assert "if v is not None}      # null" in src
