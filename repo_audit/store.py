"""The shared board: claims, questions, findings, probes, and bookkeeping.

Everything the agents know about the repository lives here, in one SQLite
file. Agents never talk to each other directly; they read and write rows.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS claims (
  n INTEGER PRIMARY KEY AUTOINCREMENT,
  fingerprint TEXT UNIQUE,
  stage TEXT, unit TEXT, source TEXT, model TEXT,
  kind TEXT, subject TEXT, entity TEXT, attribute TEXT, value TEXT,
  statement TEXT, evidence TEXT, evidence_ok INTEGER,
  confidence TEXT, created REAL);
CREATE INDEX IF NOT EXISTS claims_unit ON claims(stage, unit);
CREATE INDEX IF NOT EXISTS claims_entity ON claims(entity);
CREATE TABLE IF NOT EXISTS questions (
  n INTEGER PRIMARY KEY AUTOINCREMENT,
  fingerprint TEXT UNIQUE,
  stage TEXT, unit TEXT, text TEXT, why TEXT, subject TEXT, entity TEXT,
  evidence TEXT, finding TEXT, status TEXT DEFAULT 'open', merged_into TEXT,
  answer TEXT, answered REAL, created REAL);
CREATE TABLE IF NOT EXISTS findings (
  n INTEGER PRIMARY KEY AUTOINCREMENT,
  fingerprint TEXT UNIQUE,
  stage TEXT, unit TEXT, type TEXT, title TEXT, detail TEXT, severity TEXT,
  confidence TEXT, claims TEXT, files TEXT, entity TEXT, evidence TEXT,
  status TEXT DEFAULT 'candidate', notes TEXT, created REAL);
CREATE TABLE IF NOT EXISTS probes (
  n INTEGER PRIMARY KEY AUTOINCREMENT,
  fingerprint TEXT UNIQUE,
  file TEXT, entity TEXT, question TEXT, status TEXT DEFAULT 'pending');
CREATE TABLE IF NOT EXISTS units (
  stage TEXT, unit TEXT, input_hash TEXT, status TEXT, error TEXT, updated REAL,
  PRIMARY KEY (stage, unit));
CREATE TABLE IF NOT EXISTS aliases (alias TEXT PRIMARY KEY, canonical TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS calls (
  n INTEGER PRIMARY KEY AUTOINCREMENT, stage TEXT, model TEXT,
  prompt_tokens INTEGER, completion_tokens INTEGER, cost REAL,
  cached INTEGER, seconds REAL, ok INTEGER, created REAL);
"""

_JSON_COLS = ("evidence", "claims", "files")


def cid(n: int) -> str:
    return f"C-{n:05d}"


def qid(n: int) -> str:
    return f"Q-{n:04d}"


def fid(n: int) -> str:
    return f"F-{n:04d}"


def id_num(text: str) -> int | None:
    try:
        return int(str(text).split("-", 1)[1])
    except (IndexError, ValueError):
        return None


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        for ddl in ("ALTER TABLE questions ADD COLUMN priority TEXT",):
            try:  # columns added after the first release
                self.db.execute(ddl)
            except sqlite3.OperationalError:
                pass

    # -- low level ---------------------------------------------------------
    def rows(self, sql: str, args: tuple = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def run(self, sql: str, args: tuple = ()):
        with self.lock:
            return self.db.execute(sql, args)

    @staticmethod
    def _decode(row: dict, prefix) -> dict:
        row["id"] = prefix(row["n"])
        for col in _JSON_COLS:
            if col in row:
                row[col] = json.loads(row[col]) if row[col] else []
        return row

    # -- meta --------------------------------------------------------------
    def meta_get(self, key: str, default=None):
        rows = self.rows("SELECT value FROM meta WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def meta_set(self, key: str, value) -> None:
        self.run("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value)))

    # -- units (what has been done, for resume) ------------------------------
    def unit_hash(self, stage: str, unit: str) -> str | None:
        rows = self.rows(
            "SELECT input_hash FROM units WHERE stage=? AND unit=? AND status='done'",
            (stage, unit))
        return rows[0]["input_hash"] if rows else None

    def unit_set(self, stage: str, unit: str, input_hash: str, status: str, error: str = "") -> None:
        self.run("INSERT OR REPLACE INTO units VALUES (?,?,?,?,?,?)",
                 (stage, unit, input_hash, status, error[:500], time.time()))

    def unit_ids(self, stage: str) -> set[str]:
        return {r["unit"] for r in self.rows("SELECT unit FROM units WHERE stage=?", (stage,))}

    def clear_unit(self, stage: str, unit: str, forget: bool = False) -> None:
        """Remove what a unit produced before it is re-run (or because it is gone).

        Findings are handled separately with sync_findings so that a finding
        which is produced again keeps its id and its verification status.
        """
        self.run("DELETE FROM claims WHERE stage=? AND unit=?", (stage, unit))
        self.run("DELETE FROM questions WHERE stage=? AND unit=? AND status='open'", (stage, unit))
        if forget:
            self.run("DELETE FROM findings WHERE stage=? AND unit=?", (stage, unit))
            self.run("DELETE FROM units WHERE stage=? AND unit=?", (stage, unit))

    def sync_findings(self, stage: str, unit: str, keep: set) -> None:
        """Drop this unit's findings that it no longer produces."""
        for row in self.rows("SELECT n, fingerprint FROM findings WHERE stage=? AND unit=?",
                             (stage, unit)):
            if row["fingerprint"] not in keep:
                self.run("DELETE FROM findings WHERE n=?", (row["n"],))

    # -- claims ------------------------------------------------------------
    def add_claim(self, *, fingerprint, stage, unit, source, model="", kind, subject,
                  entity=None, attribute=None, value=None, statement, evidence=None,
                  evidence_ok=None, confidence=None) -> bool:
        cur = self.run(
            "INSERT OR IGNORE INTO claims (fingerprint,stage,unit,source,model,kind,subject,"
            "entity,attribute,value,statement,evidence,evidence_ok,confidence,created) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fingerprint, stage, unit, source, model, kind, subject, entity, attribute, value,
             statement, json.dumps(evidence or []), evidence_ok, confidence, time.time()))
        return cur.rowcount > 0

    def claims(self, where: str = "1=1", args: tuple = ()) -> list[dict]:
        return [self._decode(r, cid) for r in
                self.rows(f"SELECT * FROM claims WHERE {where} ORDER BY n", args)]

    # -- questions ---------------------------------------------------------
    def add_question(self, *, fingerprint, stage, unit, text, why="", subject=None,
                     entity=None, evidence=None, finding=None) -> bool:
        cur = self.run(
            "INSERT OR IGNORE INTO questions (fingerprint,stage,unit,text,why,subject,entity,"
            "evidence,finding,created) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (fingerprint, stage, unit, text, why, subject, entity,
             json.dumps(evidence or []), finding, time.time()))
        return cur.rowcount > 0

    def questions(self, where: str = "1=1", args: tuple = ()) -> list[dict]:
        return [self._decode(r, qid) for r in
                self.rows(f"SELECT * FROM questions WHERE {where} ORDER BY n", args)]

    # -- findings ----------------------------------------------------------
    def add_finding(self, *, fingerprint, stage, unit, type, title, detail, severity="medium",
                    confidence=None, claims=None, files=None, entity=None, evidence=None) -> bool:
        cur = self.run(
            "INSERT OR IGNORE INTO findings (fingerprint,stage,unit,type,title,detail,severity,"
            "confidence,claims,files,entity,evidence,created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fingerprint, stage, unit, type, title, detail, severity, confidence,
             json.dumps(claims or []), json.dumps(files or []), entity,
             json.dumps(evidence or []), time.time()))
        return cur.rowcount > 0

    def findings(self, where: str = "1=1", args: tuple = ()) -> list[dict]:
        return [self._decode(r, fid) for r in
                self.rows(f"SELECT * FROM findings WHERE {where} ORDER BY n", args)]

    # -- llm bookkeeping ---------------------------------------------------
    def cache_get(self, key: str) -> str | None:
        rows = self.rows("SELECT value FROM cache WHERE key=?", (key,))
        return rows[0]["value"] if rows else None

    def cache_set(self, key: str, value: str) -> None:
        self.run("INSERT OR REPLACE INTO cache VALUES (?,?)", (key, value))

    def log_call(self, stage, model, prompt_tokens, completion_tokens, cost, cached, seconds, ok) -> None:
        self.run("INSERT INTO calls (stage,model,prompt_tokens,completion_tokens,cost,cached,"
                 "seconds,ok,created) VALUES (?,?,?,?,?,?,?,?,?)",
                 (stage, model, prompt_tokens, completion_tokens, cost, int(cached), seconds,
                  int(ok), time.time()))

    def usage(self) -> dict:
        row = self.rows(
            "SELECT COUNT(*) calls, COALESCE(SUM(prompt_tokens),0) pt, "
            "COALESCE(SUM(completion_tokens),0) ct, COALESCE(SUM(cost),0) cost "
            "FROM calls WHERE cached=0")[0]
        return {"calls": row["calls"], "tokens": row["pt"] + row["ct"],
                "prompt_tokens": row["pt"], "completion_tokens": row["ct"], "usd": row["cost"]}

    def usage_by_stage(self) -> list[dict]:
        return self.rows(
            "SELECT stage, model, SUM(cached=0) calls, SUM(cached) cached, "
            "COALESCE(SUM(CASE WHEN cached=0 THEN prompt_tokens END),0) prompt_tokens, "
            "COALESCE(SUM(CASE WHEN cached=0 THEN completion_tokens END),0) completion_tokens, "
            "COALESCE(SUM(CASE WHEN cached=0 THEN cost END),0) usd "
            "FROM calls GROUP BY stage, model ORDER BY MIN(n)")
