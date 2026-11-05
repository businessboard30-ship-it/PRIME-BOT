"""Small helpers for the tool schemas sent to the model."""


def nullable(prop: dict) -> dict:
    """Let an OPTIONAL argument be null as well as its normal type (in place). Models often send null for a field they skip, and the
    provider rejects the entire tool call ("expected string, but got null"), after which the bot used to fall back to plain chat."""
    t = prop.get("type")
    if isinstance(t, str):
        prop["type"] = [t, "null"]
    if prop.get("enum") is not None and None not in prop["enum"]:
        prop["enum"] = list(prop["enum"]) + [None]
    return prop


def drop_nulls(args: dict) -> dict:
    return {k: v for k, v in (args or {}).items() if v is not None}
