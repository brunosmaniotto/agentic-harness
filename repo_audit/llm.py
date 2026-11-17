"""Model access: one small client for any OpenAI-compatible endpoint.

Responses are cached by (model, prompt), so re-running a stage after a crash
or a config change only pays for what actually changed.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


class BudgetExceeded(Exception):
    pass


class LLMError(Exception):
    pass


class Deferred(Exception):
    """provider 'queue': the prompt was written to disk and is waiting for a reply."""


def extract_json(text: str) -> dict:
    """Parse a JSON object out of a model reply, tolerating fences and stray prose."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```\s*$", "", t)
    try:
        value = json.loads(t)
    except ValueError:
        i, j = t.find("{"), t.rfind("}")
        if i < 0 or j <= i:
            raise ValueError("no JSON object in reply")
        value = json.loads(t[i:j + 1])
    if not isinstance(value, dict):
        raise ValueError("reply is not a JSON object")
    return value


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class LLM:
    def __init__(self, cfg: dict, store, responder=None):
        self.cfg = cfg
        self.store = store
        self.responder = responder  # callable(stage, system, user) -> str, for provider "mock"
        self.lock = threading.Lock()
        used = store.usage()
        self.calls, self.tokens, self.usd = used["calls"], used["tokens"], used["usd"]

    # -- configuration -----------------------------------------------------
    def model_for(self, stage: str) -> str:
        tier = self.cfg["stages"].get(stage, "strong")
        model = self.cfg["models"].get(tier, "")
        if not model and self.cfg["llm"]["provider"] == "openai_compatible":
            raise LLMError(f"No model set for tier '{tier}' (stage '{stage}'). Fill in [models] in the config.")
        return model or self.cfg["llm"]["provider"]

    def preflight(self) -> None:
        """Fail before any work starts if the model setup cannot possibly work."""
        llm = self.cfg["llm"]
        if llm["provider"] == "openai_compatible":
            if not os.environ.get(llm["api_key_env"], ""):
                raise LLMError(f"environment variable {llm['api_key_env']} is not set")
            for tier in sorted(set(self.cfg["stages"].values()) - {"verifier_2"}):  # optional
                if not self.cfg["models"].get(tier):
                    raise LLMError(f"[models].{tier} is empty in the config")
        elif llm["provider"] == "command" and not llm["command"]:
            raise LLMError("provider 'command' needs llm.command")
        elif llm["provider"] == "mock" and self.responder is None:
            raise LLMError("provider 'mock' needs a responder")

    def _check_budget(self) -> None:
        b = self.cfg["budget"]
        with self.lock:
            if b["max_calls"] and self.calls >= b["max_calls"]:
                raise BudgetExceeded(f"call budget reached ({self.calls})")
            if b["max_tokens"] and self.tokens >= b["max_tokens"]:
                raise BudgetExceeded(f"token budget reached ({self.tokens})")
            if b["max_usd"] and self.usd >= b["max_usd"]:
                raise BudgetExceeded(f"dollar budget reached (${self.usd:.2f})")

    # -- public ------------------------------------------------------------
    def complete_json(self, stage: str, system: str, user: str) -> dict:
        model = self.model_for(stage)
        key = hashlib.sha256("\x00".join((model, system, user)).encode("utf-8", "replace")).hexdigest()
        hit = self.store.cache_get(key)
        if hit is not None:
            self.store.log_call(stage, model, 0, 0, 0.0, True, 0.0, True)
            return json.loads(hit)
        self._check_budget()
        if self.cfg["llm"]["provider"] == "queue":
            return self._queued(stage, model, key, system, user)
        text = self._raw(stage, model, system, user)
        try:
            data = extract_json(text)
        except ValueError:
            self._check_budget()
            text = self._raw(stage, model, system, user +
                             "\n\nYour previous reply was not a valid JSON object. "
                             "Reply with a single JSON object and nothing else.")
            try:
                data = extract_json(text)
            except ValueError as exc:
                raise LLMError(f"model did not return JSON: {exc}") from exc
        self.store.cache_set(key, json.dumps(data))
        return data

    # -- providers ---------------------------------------------------------
    def _queued(self, stage: str, model: str, key: str, system: str, user: str) -> dict:
        """Prompts go to <out>/queue/pending/, replies are read from <out>/queue/replies/.

        Lets anything that can read a file and write a file act as the model:
        another agent, a batch API, a person. The run stops at the first stage
        with unanswered prompts and continues when it is run again.
        """
        root = Path(self.cfg["output"]["dir"]) / "queue"
        name = f"{stage}-{key[:16]}"
        pending, reply = root / "pending" / f"{name}.md", root / "replies" / f"{name}.json"
        if reply.exists():
            text = reply.read_text(encoding="utf-8", errors="replace")
            try:
                data = extract_json(text)
            except ValueError:
                reply.rename(reply.with_suffix(".invalid"))
            else:
                pt, ct = _estimate_tokens(system + user), _estimate_tokens(text)
                with self.lock:
                    self.calls += 1
                    self.tokens += pt + ct
                self.store.log_call(stage, model, pt, ct, 0.0, False, 0.0, True)
                self.store.cache_set(key, json.dumps(data))
                pending.unlink(missing_ok=True)
                return data
        pending.parent.mkdir(parents=True, exist_ok=True)
        reply.parent.mkdir(parents=True, exist_ok=True)
        tier = self.cfg["stages"].get(stage, "strong")
        pending.write_text(
            f"REPLY PATH: {reply}\nSTAGE: {stage}\nTIER: {tier}\n\n"
            f"################ INSTRUCTIONS ################\n{system}\n\n"
            f"################ INPUT ################\n{user}\n", encoding="utf-8")
        raise Deferred(name)

    def _raw(self, stage: str, model: str, system: str, user: str) -> str:
        provider = self.cfg["llm"]["provider"]
        started = time.time()
        usage = {}
        try:
            if provider == "mock":
                if self.responder is None:
                    raise LLMError("provider 'mock' needs a responder")
                text = self.responder(stage, system, user)
            elif provider == "command":
                text = self._command(system, user)
            elif provider == "openai_compatible":
                text, usage = self._http(model, system, user)
            else:
                raise LLMError(f"unknown provider '{provider}'")
        except Exception:
            self.store.log_call(stage, model, 0, 0, 0.0, False, time.time() - started, False)
            raise
        pt = int(usage.get("prompt_tokens") or _estimate_tokens(system + user))
        ct = int(usage.get("completion_tokens") or _estimate_tokens(text))
        cost = float(usage.get("cost") or 0.0)
        with self.lock:
            self.calls += 1
            self.tokens += pt + ct
            self.usd += cost
        self.store.log_call(stage, model, pt, ct, cost, False, time.time() - started, True)
        return text

    def _command(self, system: str, user: str) -> str:
        argv = self.cfg["llm"]["command"]
        if not argv:
            raise LLMError("provider 'command' needs llm.command")
        proc = subprocess.run(argv, input=system + "\n\n" + user, capture_output=True,
                              text=True, timeout=self.cfg["llm"]["timeout_s"])
        if proc.returncode != 0:
            raise LLMError(f"command failed ({proc.returncode}): {proc.stderr[:300]}")
        return proc.stdout

    def _http(self, model: str, system: str, user: str) -> tuple[str, dict]:
        llm = self.cfg["llm"]
        key = os.environ.get(llm["api_key_env"], "")
        if not key:
            raise LLMError(f"environment variable {llm['api_key_env']} is not set")
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "max_tokens": llm["max_output_tokens"],
        }
        if llm["temperature"] is not None and llm["temperature"] >= 0:
            body["temperature"] = llm["temperature"]
        if llm["json_mode"]:
            body["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        headers.update(llm.get("extra_headers") or {})
        url = llm["base_url"].rstrip("/") + "/chat/completions"
        payload = json.dumps(body).encode("utf-8")

        last = "no attempt made"
        for attempt in range(llm["max_retries"] + 1):
            wait = min(60.0, 2.0 ** attempt + random.random())
            try:
                req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=llm["timeout_s"]) as resp:
                    data = json.loads(resp.read().decode("utf-8", "replace"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:300]
                last = f"HTTP {exc.code}: {detail}"
                if exc.code not in (408, 409, 425, 429) and exc.code < 500:
                    raise LLMError(last) from exc
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                if retry_after and retry_after.isdigit():
                    wait = min(120.0, float(retry_after))
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                last = f"{type(exc).__name__}: {exc}"
            else:
                choices = data.get("choices") if isinstance(data, dict) else None
                if choices:
                    content = (choices[0].get("message") or {}).get("content")
                    if isinstance(content, list):  # some gateways return content parts
                        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
                    if content:
                        return content, data.get("usage") or {}
                    last = "empty reply"
                else:
                    last = f"no choices in reply: {json.dumps(data)[:300]}"
            if attempt < llm["max_retries"]:
                time.sleep(wait)
        raise LLMError(f"request failed after retries: {last}")
