"""Configuration: defaults merged with a TOML file."""
from __future__ import annotations

import copy
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ImportError:  # pragma: no cover - older interpreters
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        tomllib = None  # type: ignore

DEFAULTS: dict = {
    "repo": {
        "path": ".",
        "use_git_ls_files": True,
        "max_file_bytes": 400_000,
        "exclude": [
            ".git/**", "**/node_modules/**", "**/vendor/**", "**/third_party/**",
            "**/__pycache__/**", "**/.venv/**",
            "**/venv/**", "**/dist/**", "**/build/**", "**/.tox/**",
            "**/.mypy_cache/**", "**/.pytest_cache/**", "**/.ruff_cache/**",
            "**/*.egg-info/**", "**/.idea/**", "**/.vscode/**", "**/*.min.js",
            "**/*.lock", "**/package-lock.json", "**/.env", "**/.env.*",
            "**/*.pem", "**/*.key", "**/id_rsa*", "**/.repo-audit/**",
        ],
        "redact_secrets": True,
        "inactive_months": 12,
    },
    "baseline": {
        # Output of the dynamic-documentation system: a folder of markdown files.
        "docs_dir": "",
        # The "things worth investigating" file that system writes, if any.
        "findings_file": "",
        # Also treat markdown inside the repo (README etc.) as statements of intent.
        "include_repo_markdown": True,
    },
    "output": {"dir": ".repo-audit"},
    "llm": {
        # openai_compatible | command | mock
        "provider": "openai_compatible",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "timeout_s": 180,
        "max_retries": 4,
        "concurrency": 6,
        "json_mode": False,
        "temperature": 0,  # set to -1 to omit the parameter
        "max_output_tokens": 4000,
        # Every prompt asks for one JSON object. A route with extended thinking can spend the
        # whole max_output_tokens on hidden reasoning and return no content at all ("empty
        # reply"), however large the limit. This sends {"reasoning": {"enabled": false}}
        # (OpenRouter's switch). Set false for an endpoint that rejects unknown fields.
        "disable_reasoning": True,
        "extra_headers": {},
        # provider = "command": argv of a CLI that reads the prompt on stdin
        # and prints the reply on stdout, e.g. ["claude", "-p"].
        "command": [],
    },
    # Model ids for each tier. Any id the endpoint accepts.
    # verifier_2: optional second verifier, ideally another model family. When set,
    # findings at or above [verify] second_opinion_min_severity are checked by both,
    # and a disagreement goes to a person.
    "models": {"cheap": "", "strong": "", "verifier": "", "verifier_2": ""},
    # Which tier each stage uses.
    "stages": {
        "docs": "cheap", "leads": "cheap", "system": "strong", "paths": "strong",
        "files": "cheap", "canonicalize": "strong", "reconcile": "strong",
        "contracts": "strong", "supply": "strong", "rules": "strong", "themes": "strong",
        "probe": "strong", "verify": "verifier", "verify2": "verifier_2", "questions": "cheap",
        "blocks": "strong", "branches": "strong",
    },
    "budget": {"max_calls": 5000, "max_tokens": 50_000_000, "max_usd": 0.0},
    "limits": {
        "chunk_lines": 350,
        "chunk_overlap": 30,
        "max_files": 0,            # 0 = no cap on files given to the file wave
        "include_tests": False,
        "config_max_lines": 400,
        "path_files": 60,          # files shown to one path-tracing call
        "max_pipelines": 30,
        "max_entity_claims": 60,   # claims shown to one reconcile call
        "probe_rounds": 2,
        "max_probes_per_round": 25,
        "supplier_probes": 3,         # writers asked "do you ensure this?" per unmet requirement
        "max_doc_rules": 10,
        "themes_min_findings": 6,          # rules taken from documentation, on top of maintainers' own
        "canonicalize_min_new_keys": 15,
        "reconcile_pack_small": 12,   # subjects with this many claims or fewer share a call
        "reconcile_pack_claims": 45,  # claims per shared call
        "verify_context_lines": 30,
        # Code shown to workers is chosen from the call graph (Python): whole functions up to
        # function_max_lines, the repository helpers they call, and a few places they are called from.
        "call_graph": True,
        # Blocks and branches: forks in the data lineage and the branches that leave them.
        "max_forks": 20,
        "max_branches": 6,         # per fork
        "block_max_files": 8,      # per branch
        "block_code_lines": 500,   # code shown to one block worker, shared across its files
        "function_max_lines": 120,
        "context_callers": 2,
        "context_callees": 3,
        "verify_max_lines": 600,
        "max_static_findings": 150,  # per type
        "dup_function_min_lines": 8,
        "near_dup_jaccard": 0.7,
    },
    "verify": {"static": True, "local_risk_min_severity": "medium", "rule_min_severity": "low", "branch_min_severity": "medium",
               "second_opinion_min_severity": "high",
               "skip_low_redundant": False,
               # e.g. ["leak", "evaluation period"]: matching findings are verified whatever their severity
               "focus_terms": []},
    "prompts": {"dir": ""},
}


def _merge(base: dict, extra: dict) -> dict:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def load(path: str | None = None, overrides: dict | None = None) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    base = Path.cwd()
    if path:
        if tomllib is None:
            raise RuntimeError("Reading TOML needs Python 3.11+ or `pip install tomli`.")
        with open(path, "rb") as fh:
            _merge(cfg, tomllib.load(fh))
        base = Path(path).resolve().parent
    if overrides:
        _merge(cfg, overrides)

    def absolute(value: str) -> str:
        if not value:
            return ""
        p = Path(value).expanduser()
        return str(p if p.is_absolute() else (base / p).resolve())

    cfg["repo"]["path"] = absolute(cfg["repo"]["path"])
    cfg["output"]["dir"] = absolute(cfg["output"]["dir"])
    cfg["baseline"]["docs_dir"] = absolute(cfg["baseline"]["docs_dir"])
    cfg["baseline"]["findings_file"] = absolute(cfg["baseline"]["findings_file"])
    cfg["prompts"]["dir"] = absolute(cfg["prompts"]["dir"])
    return cfg


EXAMPLE = '''# repo-audit configuration. Paths are relative to this file.

[repo]
path = "."                 # the repository to audit (never written to)
# exclude = ["**/vendor/**"]   # replaces the default exclude list if set

[baseline]
docs_dir = ""              # folder of markdown from the dynamic-documentation system
findings_file = ""         # its "worth investigating" file, if any
include_repo_markdown = true

[output]
dir = ".repo-audit"        # store, report and questions are written here

[llm]
provider = "openai_compatible"            # or "command"
base_url = "https://openrouter.ai/api/v1"  # any OpenAI-compatible endpoint
api_key_env = "OPENROUTER_API_KEY"
concurrency = 6
# provider = "command"
# command = ["claude", "-p"]              # prompt on stdin, reply on stdout

[models]
# Fill these in. Any model id the endpoint accepts.
# cheap reads every file; strong traces paths and reconciles; verifier tries
# to refute each finding and should ideally be a different model family.
# Examples below were listed on OpenRouter on 2026-11-16; check openrouter.ai/models.
cheap = ""       # e.g. "anthropic/claude-haiku-5.5"
strong = ""      # e.g. "anthropic/claude-sonnet-5.5"
verifier = ""    # e.g. "openai/gpt-6.1-sol"
verifier_2 = ""   # optional: a second verifier from another family checks high-severity verdicts

[budget]
max_calls = 5000
max_tokens = 50000000
max_usd = 0.0              # 0 = no dollar cap (needs the endpoint to report cost)

[limits]
max_files = 0              # set to e.g. 40 for a first trial run
probe_rounds = 2
'''
