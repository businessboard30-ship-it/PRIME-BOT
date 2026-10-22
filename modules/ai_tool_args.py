"""Checks what the model sent for ONE command against that command's own tool schema, before anything runs.

Why: a model that is unsure tends to invent argument names, send "12" where a number is wanted, spell an enum value
in another case, or leave a required argument out. Before this, unknown/missing things were silently dropped and the
command then failed (or did something different from what was meant). Now the arguments are cleaned up where that is
safe, and the person is told plainly what is still missing -- nothing half-formed ever reaches a command.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List

_EMPTY = (None, "", "null", "none", "undefined")
_TRUE = {"true", "yes", "y", "1", "on"}
_FALSE = {"false", "no", "n", "0", "off"}


@dataclass
class CleanResult:
    args: Dict[str, Any] = field(default_factory=dict)
    missing: List[str] = field(default_factory=list)
    invalid: List[str] = field(default_factory=list)      # human-readable "name: reason"
    dropped: List[str] = field(default_factory=list)      # argument names the command doesn't have

    @property
    def problems(self) -> bool:
        return bool(self.missing or self.invalid)

    def message(self, command_label: str) -> str:
        parts = []
        if self.missing:
            parts.append("I still need: " + ", ".join(f"**{m}**" for m in self.missing))
        if self.invalid:
            parts.append("these don't look right: " + "; ".join(self.invalid))
        return f"I can't run **/{command_label}** yet -- " + " | ".join(parts) + ". Tell me and I'll do it."


def _coerce(name: str, value: Any, prop: dict):
    """(ok, value_or_reason)."""
    typ = prop.get("type", "string")
    enum = prop.get("enum")
    if typ == "boolean":
        if isinstance(value, bool):
            return True, value
        s = str(value).strip().lower()
        if s in _TRUE:
            return True, True
        if s in _FALSE:
            return True, False
        return False, "must be yes or no"
    if typ in ("integer", "number"):
        if isinstance(value, bool):
            return False, "must be a number"
        try:
            num = float(str(value).strip().replace(",", ""))
        except ValueError:
            return False, "must be a number"
        if typ == "integer":
            if num != int(num):
                return False, "must be a whole number"
            num = int(num)
        return True, num
    s = value if isinstance(value, str) else str(value)
    if enum:
        for e in enum:
            if str(e).lower() == s.strip().lower():
                return True, e
        return False, f"must be one of {', '.join(map(str, enum))}"
    return True, s


def clean_args(args: Any, parameters: dict) -> CleanResult:
    """`parameters` is the tool schema's {"properties": {...}, "required": [...]}."""
    props = (parameters or {}).get("properties") or {}
    required = list((parameters or {}).get("required") or [])
    res = CleanResult()
    given = args if isinstance(args, dict) else {}
    for key, value in given.items():
        if key not in props:
            res.dropped.append(str(key))
            continue
        if value in _EMPTY or (isinstance(value, str) and value.strip().lower() in _EMPTY):
            continue
        ok, out = _coerce(key, value, props[key])
        if ok:
            res.args[key] = out
        else:
            res.invalid.append(f"{key} {out}")
    res.missing = [r for r in required if r not in res.args and not any(i.startswith(r + " ") for i in res.invalid)]
    return res
