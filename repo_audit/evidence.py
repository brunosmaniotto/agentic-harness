"""Reading files for analysis, and checking that quoted evidence is real.

Every quote a model returns is located in the file it claims to come from.
A claim whose quote cannot be found is not trusted downstream. This is the
main defence against a model inventing plausible-sounding facts.
"""
from __future__ import annotations

import bisect
import json
import re
from pathlib import Path

_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)
_TOKENS = [
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b(?:sk|rk)-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
]
_ASSIGN = re.compile(
    r"""(?i)\b([a-z0-9_]*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|"""
    r"""private[_-]?key)[a-z0-9_]*)(\s*[:=]\s*)(['"])([^'"\n]{6,})\3""")


def redact(text: str) -> str:
    """Mask obvious credentials. Line count is preserved so line numbers still hold."""
    text = _PRIVATE_KEY.sub(
        lambda m: "<REDACTED PRIVATE KEY>" + "\n" * m.group(0).count("\n"), text)
    for pattern in _TOKENS:
        text = pattern.sub("<REDACTED>", text)
    return _ASSIGN.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}<REDACTED>{m.group(3)}", text)


def notebook_to_text(raw: str) -> str:
    """Flatten a Jupyter notebook to its code cells so it can be read like a script."""
    try:
        nb = json.loads(raw)
    except ValueError:
        return raw
    out: list[str] = []
    for i, cell in enumerate(nb.get("cells", []) if isinstance(nb, dict) else []):
        if cell.get("cell_type") != "code":
            continue
        src = cell.get("source", "")
        if isinstance(src, list):
            src = "".join(src)
        out.append(f"# %% cell {i}")
        for line in src.splitlines():
            out.append("# " + line if line.lstrip().startswith(("%", "!")) else line)
    return "\n".join(out)


def load_text(base: str | Path, rel: str, do_redact: bool = True) -> str:
    raw = (Path(base) / rel).read_bytes().decode("utf-8", errors="replace")
    if rel.endswith(".ipynb"):
        raw = notebook_to_text(raw)
    return redact(raw) if do_redact else raw


def numbered(lines: list[str], start: int, end: int) -> str:
    end = min(end, len(lines))
    return "\n".join(f"{i:>5}| {lines[i - 1]}" for i in range(max(1, start), end + 1))


def chunk_ranges(n_lines: int, size: int, overlap: int) -> list[tuple[int, int]]:
    if n_lines <= 0:
        return []
    if n_lines <= size:
        return [(1, n_lines)]
    out, start = [], 1
    step = max(1, size - overlap)
    while start <= n_lines:
        end = min(n_lines, start + size - 1)
        out.append((start, end))
        if end == n_lines:
            break
        start += step
    return out


def merge_windows(spans: list[tuple[int, int]], pad: int, n_lines: int) -> list[tuple[int, int]]:
    """Pad line spans and merge the ones that touch."""
    padded = sorted((max(1, a - pad), min(n_lines, b + pad)) for a, b in spans if n_lines)
    out: list[tuple[int, int]] = []
    for a, b in padded:
        if out and a <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


_PREFIX = re.compile(r"^\s*\d+\|\s?")
_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _WS.sub(" ", text).strip()


def locate_quote(lines: list[str], quote, hint: int | None = None) -> tuple[int, int] | None:
    """Find `quote` in `lines`, ignoring whitespace differences.

    Returns the 1-based (start, end) line span, preferring the occurrence
    nearest `hint`, or None when the quote is not in the file.
    """
    if not isinstance(quote, str) or not lines:
        return None
    cleaned = "\n".join(_PREFIX.sub("", part) for part in quote.splitlines())
    needle = _norm(cleaned)
    if len(needle.replace(" ", "")) < 6:
        return None

    offsets: list[int] = []
    line_nos: list[int] = []
    parts: list[str] = []
    pos = 0
    for i, line in enumerate(lines, 1):
        norm = _norm(line)
        if not norm:
            continue
        offsets.append(pos)
        line_nos.append(i)
        parts.append(norm)
        pos += len(norm) + 1
    hay = " ".join(parts)

    def spans(n: str) -> list[tuple[int, int]]:
        found, at = [], hay.find(n)
        while at != -1:
            first = line_nos[bisect.bisect_right(offsets, at) - 1]
            last = line_nos[bisect.bisect_right(offsets, at + len(n) - 1) - 1]
            found.append((first, last))
            at = hay.find(n, at + 1)
        return found

    found = spans(needle)
    if not found:
        # Models sometimes stitch non-adjacent lines together; accept the
        # longest single line of the quote if it is distinctive enough.
        longest = max((_norm(p) for p in cleaned.splitlines()), key=len, default="")
        if len(longest.replace(" ", "")) >= 16 and longest != needle:
            found = spans(longest)
    if not found:
        return None
    if isinstance(hint, int):
        found.sort(key=lambda s: abs(s[0] - hint))
    return found[0]
