"""Entity keys: the shared vocabulary that lets claims from different files line up."""
from __future__ import annotations

ENTITY_KINDS = ("table", "file", "env", "config", "param", "api", "topic", "obj")

# obj: names too generic to mean the same thing in two files.
GENERIC_OBJ = {"df", "data", "result", "results", "config", "args", "row", "rows",
               "x", "y", "obj", "item", "items", "input", "output", "response", "request"}


def norm_entity(text) -> str | None:
    """Normalize 'kind:name#field' or return None if it is not a usable key."""
    if not isinstance(text, str) or ":" not in text:
        return None
    kind, _, rest = text.strip().partition(":")
    kind = kind.strip().lower()
    if kind not in ENTITY_KINDS:
        return None
    name, _, field = rest.partition("#")
    name = name.strip().strip("`\"'[] ")
    field = field.strip().strip("`\"'[] ").lower()
    if kind == "file":
        while name.startswith("./"):
            name = name[2:]
    elif kind != "env":
        name = name.lower()
    if not name or (kind == "obj" and name.lower() in GENERIC_OBJ):
        return None
    return f"{kind}:{name}" + (f"#{field}" if field else "")


def base_entity(key: str) -> str:
    return key.split("#", 1)[0]


def entity_field(key: str) -> str:
    return key.split("#", 1)[1] if "#" in key else ""
