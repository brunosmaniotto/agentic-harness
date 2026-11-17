"""Shared machinery for the stages: context, resumable units, claim intake."""
from __future__ import annotations

import hashlib
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import evidence
from .llm import BudgetExceeded, Deferred
from .names import norm_entity

KINDS = {"purpose", "input", "output", "step", "assumption", "behavior", "requires", "ensures",
         "rule", "answer"}
ATTRIBUTES = {"unit", "currency", "timezone", "grain", "key", "nullability", "dtype", "format",
              "filter", "dedup", "range", "default", "ordering", "schema", "freshness",
              "location", "encoding", "semantics", "other"}
SEVERITIES = ("high", "medium", "low")
CONFIDENCES = ("high", "medium", "low")


def sha(*parts) -> str:
    h = hashlib.sha1()
    for part in parts:
        h.update(str(part).encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


def as_list(value) -> list:
    return value if isinstance(value, list) else []


def dicts(value) -> list[dict]:
    return [v for v in as_list(value) if isinstance(v, dict)]


def text_of(value, limit: int = 2000) -> str:
    return str(value).strip()[:limit] if isinstance(value, (str, int, float)) else ""


def pick(value, allowed, default):
    value = str(value).strip().lower() if value is not None else ""
    return value if value in allowed else default


def norm_words(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


class PendingWork(Exception):
    """A stage has prompts waiting in the queue; the run stops until they are answered."""


class Ctx:
    """Everything a stage needs."""

    def __init__(self, cfg: dict, store, llm, skel: dict, log=print):
        self.cfg, self.store, self.llm, self.skel, self.log = cfg, store, llm, skel, log
        self.repo = Path(cfg["repo"]["path"])
        self.out = Path(cfg["output"]["dir"])
        self._texts: dict[str, list[str]] = {}
        self._lock = threading.Lock()
        self._prompts: dict[str, tuple[str, str]] = {}
        self.rerun_verify = False

    # -- file access (always redacted, always the same text the models saw) --
    @property
    def callgraph(self):
        if getattr(self, "_callgraph", None) is None:
            from .context import CallGraph
            self._callgraph = CallGraph(self.skel["files"])
        return self._callgraph

    def lines(self, path: str) -> list[str]:
        with self._lock:
            hit = self._texts.get(path)
        if hit is not None:
            return hit
        try:
            if path.startswith("docs:"):
                text = evidence.load_text(self.cfg["baseline"]["docs_dir"], path[5:],
                                          self.cfg["repo"]["redact_secrets"])
            elif path.startswith("leads:"):
                lead_file = Path(self.cfg["baseline"]["findings_file"])
                text = evidence.load_text(lead_file.parent, lead_file.name,
                                          self.cfg["repo"]["redact_secrets"])
            else:
                text = evidence.load_text(self.repo, path, self.cfg["repo"]["redact_secrets"])
            lines = text.splitlines()
        except OSError:
            lines = []
        with self._lock:
            if len(self._texts) > 256:
                self._texts.clear()
            self._texts[path] = lines
        return lines

    def resolve_path(self, text) -> str | None:
        """Map a path a model wrote to a real repository file, if unambiguous."""
        if not isinstance(text, str) or not text.strip():
            return None
        candidate = text.strip().strip("`'\" ")
        if candidate.startswith("file:"):
            candidate = candidate[5:]
        while candidate.startswith("./"):
            candidate = candidate[2:]
        files = self.skel["files"]
        if candidate in files:
            return candidate
        matches = [p for p in files if p.endswith("/" + candidate)]
        return matches[0] if len(matches) == 1 else None

    # -- prompts -----------------------------------------------------------
    def prompt(self, name: str) -> tuple[str, str]:
        if name not in self._prompts:
            package_dir = Path(__file__).parent / "prompts"
            custom = Path(self.cfg["prompts"]["dir"]) if self.cfg["prompts"]["dir"] else None

            def read(file: str) -> str:
                if custom and (custom / file).exists():
                    return (custom / file).read_text(encoding="utf-8")
                return (package_dir / file).read_text(encoding="utf-8")

            text = read("_common.md").strip() + "\n\n" + read(f"{name}.md").strip()
            self._prompts[name] = (text, sha(text))
        return self._prompts[name]

    # -- resumable units -----------------------------------------------------
    def run_units(self, stage: str, units: list[tuple[str, str, object]], worker, apply,
                  prune: bool = True) -> dict:
        """Run `worker(payload)` for every unit whose input changed since last time.

        units: (unit_id, input_hash, payload). `worker` runs in a thread and may
        call the model; `apply(unit_id, payload, result)` runs on the main thread
        and writes to the store. A unit that fails is retried on the next run.
        """
        current = {u[0] for u in units}
        if prune:
            for stale in self.store.unit_ids(stage) - current:
                self.store.clear_unit(stage, stale, forget=True)
        todo = [u for u in units if self.store.unit_hash(stage, u[0]) != u[1]]
        stats = {"total": len(units), "ran": 0, "failed": 0, "deferred": 0,
                 "skipped": len(units) - len(todo)}
        if not todo:
            return stats
        budget_error = None
        workers = max(1, int(self.cfg["llm"]["concurrency"]))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(worker, payload): (uid, h, payload) for uid, h, payload in todo}
            for future in as_completed(futures):
                uid, h, payload = futures[future]
                try:
                    result = future.result()
                except BudgetExceeded as exc:
                    budget_error = exc
                    for other in futures:
                        other.cancel()
                    continue
                except Deferred:
                    stats["deferred"] += 1
                    continue
                except Exception as exc:
                    stats["failed"] += 1
                    self.store.unit_set(stage, uid, h, "failed", f"{type(exc).__name__}: {exc}")
                    continue
                try:
                    apply(uid, payload, result if isinstance(result, dict) else {})
                    self.store.unit_set(stage, uid, h, "done")
                    stats["ran"] += 1
                except Exception as exc:
                    stats["failed"] += 1
                    self.store.unit_set(stage, uid, h, "failed", f"{type(exc).__name__}: {exc}")
        if stats["failed"]:
            self.log(f"  {stage}: {stats['failed']} unit(s) failed and will be retried next run")
        if budget_error:
            raise budget_error
        if stats["deferred"]:
            raise PendingWork(f"{stats['deferred']} prompt(s) from stage '{stage}' are waiting for replies")
        return stats

    # -- intake ------------------------------------------------------------
    def check_evidence(self, raw, default_path: str | None) -> list[dict]:
        """Keep only evidence whose quote really is in the file it names."""
        out = []
        for item in dicts(raw):
            path = default_path
            if item.get("path"):
                given = str(item["path"])
                path = given if given.startswith(("docs:", "leads:")) else self.resolve_path(given)
            if not path:
                continue
            hint = item.get("line_start") if isinstance(item.get("line_start"), int) else None
            span = evidence.locate_quote(self.lines(path), item.get("quote"), hint)
            if span:
                out.append({"path": path, "line_start": span[0], "line_end": span[1],
                            "quote": str(item["quote"]).strip()[:400]})
        return out

    def add_claims(self, stage: str, unit: str, raw, *, subject: str, path: str | None,
                   source: str = "llm", need_evidence: bool = True, subject_from=None) -> int:
        model = self.llm.model_for(stage) if source in ("llm", "docs") else ""
        added = 0
        for item in dicts(raw):
            statement = text_of(item.get("statement"), 1000)
            if not statement:
                continue
            ev = self.check_evidence(item.get("evidence"), path)
            ok = 1 if ev else (0 if need_evidence else None)
            claim_subject = subject_from(item) if subject_from else subject
            entity = norm_entity(item.get("entity"))
            kind = pick(item.get("kind"), KINDS, "behavior")
            attribute = pick(item.get("attribute"), ATTRIBUTES, "other") if item.get("attribute") else None
            value = text_of(item.get("value"), 200) or None
            added += self.store.add_claim(
                fingerprint=sha(stage, unit, kind, claim_subject, entity, attribute, norm_words(statement)),
                stage=stage, unit=unit, source=source, model=model, kind=kind,
                subject=claim_subject, entity=entity, attribute=attribute, value=value,
                statement=statement, evidence=ev, evidence_ok=ok,
                confidence=pick(item.get("confidence"), CONFIDENCES, "medium"))
        return added

    def add_questions(self, stage: str, unit: str, raw, *, subject: str | None,
                      path: str | None, entity: str | None = None, finding: str | None = None) -> int:
        added = 0
        for item in dicts(raw):
            question = text_of(item.get("text"), 800)
            if len(question) < 8:
                continue
            added += self.store.add_question(
                fingerprint=sha("q", norm_words(question)), stage=stage, unit=unit, text=question,
                why=text_of(item.get("why"), 800), subject=subject,
                entity=norm_entity(item.get("entity")) or entity,
                evidence=self.check_evidence(item.get("evidence"), path), finding=finding)
        return added

    def human_notes(self, limit: int = 40) -> list[dict]:
        return self.store.claims("source='human'")[-limit:]
