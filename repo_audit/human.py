"""Getting knowledge that is not in the repository onto the board.

Answers and notes become claims with source "human". They are treated as
authoritative about intent, they persist across runs, and adding one causes
only the affected reconciliations and verifications to run again.
"""
from __future__ import annotations

import re
from pathlib import Path

from .core import sha
from .names import norm_entity
from .store import Store, id_num

PLACEHOLDER = "(write your answer here)"


def answer(store: Store, question_id: str, text: str) -> bool:
    n = id_num(question_id)
    rows = store.questions("n=?", (n,)) if n is not None else []
    text = text.strip()
    if not rows or not text:
        return False
    q = rows[0]
    import time
    store.run("UPDATE questions SET status='answered', answer=?, answered=? WHERE n=?",
              (text, time.time(), n))
    store.run("DELETE FROM claims WHERE stage='human' AND unit=?", (q["id"],))
    # The answer also covers questions that were merged into this one.
    targets = {(q["subject"] or "system", q["entity"])}
    for merged in store.questions("merged_into=?", (q["id"],)):
        targets.add((merged["subject"] or "system", merged["entity"]))
    for subject, entity in sorted(targets, key=str):
        store.add_claim(
            fingerprint=sha("human", q["id"], subject, entity), stage="human", unit=q["id"],
            source="human", kind="answer", subject=subject, entity=entity,
            statement=f"Maintainer answer. Q: {q['text']} A: {text}", evidence=[],
            evidence_ok=1, confidence="high")
    return True


def note(store: Store, text: str, entity: str | None = None, file: str | None = None) -> None:
    """Record something a maintainer knows, without it being an answer to a question."""
    text = text.strip()
    subject = f"file:{file}" if file else "system"
    store.add_claim(
        fingerprint=sha("note", subject, entity, text), stage="human",
        unit="note:" + sha(text)[:8], source="human", kind="answer", subject=subject,
        entity=norm_entity(entity) if entity else None,
        statement=f"Maintainer note: {text}", evidence=[], evidence_ok=1, confidence="high")


def rule(store: Store, text: str) -> None:
    """Record something that must hold everywhere, e.g. 'no code reads evaluation-period
    data before final scoring'. Every file is then checked against it."""
    text = text.strip()
    store.add_claim(
        fingerprint=sha("rule", text), stage="human", unit="rule:" + sha(text)[:8],
        source="human", kind="rule", subject="system", entity=None,
        statement=f"Rule from maintainer: {text}", evidence=[], evidence_ok=1, confidence="high")


_BLOCK = re.compile(r"^## (Q-\d+)\s*$", re.M)


def import_answers(store: Store, path: str) -> int:
    """Read answers typed into questions.md. Returns how many were recorded."""
    text = Path(path).read_text(encoding="utf-8")
    parts = _BLOCK.split(text)
    done = 0
    for i in range(1, len(parts) - 1, 2):
        qid, block = parts[i], parts[i + 1]
        if "**Answer:**" not in block:
            continue
        reply = block.split("**Answer:**", 1)[1].strip()
        reply = reply.replace(PLACEHOLDER, "").strip()
        if reply:
            done += answer(store, qid, reply)
    return done
