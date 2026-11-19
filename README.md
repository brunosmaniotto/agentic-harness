# repo-audit

Point it at a repository and it works through the code on its own, in fixed stages, and comes back with two things: a short list of problems it could prove from the code, and a list of questions only a person can answer. Answer the questions and run it again; it picks up where it stopped.

It is built for repositories that many people have worked on over years: where parts were written under different assumptions, and nobody is left who knows all of it. It looks for files that assume different things about the same data, logic that exists in more than one place, code nothing uses, and documentation that no longer matches the code.

It never changes the repository. It writes only to its own output folder.

## Quick start

Needs Python 3.9 or later and nothing else (on 3.9 or 3.10, `pip install tomli`).

```bash
pip install -e .                      # or run as: python -m repo_audit
cd /somewhere/outside/the/target/repo
repo-audit init                       # writes repo-audit.toml
# edit repo-audit.toml: [repo].path, [models], and [baseline] if you have generated docs
export OPENROUTER_API_KEY=...

repo-audit run --static-only          # free: no model calls, static findings only
repo-audit plan                       # how many model calls a full run needs
repo-audit run                        # the full audit; safe to interrupt and re-run
```

For a first trial, set `[limits] max_files = 40` so the file wave reads only the most central files.

Output, in `.repo-audit/`:

| File | What it is |
|---|---|
| `report.md` | Confirmed findings with file and line, what is waiting on a person, and a map of the system |
| `cleanup.md`, `cleanup.json` | Potential clean up: files, functions, classes, methods, constants and outputs nothing uses, and commented-out code, in three tiers (also a tab in the dashboard) |
| `dashboard.html` | The confirmed findings folded into unique issues and ranked by urgency, in one self-contained page: severity filter, type filter, search, and rows that open to show the detail, the quoted evidence and every finding folded into them |
| `questions.md` | The questions worth a person's time: those that decide a finding, then those triage marked important |
| `questions_all.md` | The remaining open questions |
| `findings.json` | Every finding, including refuted ones, for other tools |
| `claims.jsonl` | The full board: every claim any stage recorded |
| `skeleton.json` | The static analysis: import graph, data touched, entry points |
| `store.db` | The SQLite database all of the above is generated from |

## The question loop

When the audit cannot settle something from the code, it asks instead of guessing. Type answers into `questions.md` under any question you can answer, then:

```bash
repo-audit answers .repo-audit/questions.md     # questions_all.md works the same way
repo-audit run
```

Only the stages that depend on an answer run again. You can also tell it things nobody asked about:

```bash
repo-audit note "Prices in the orders table are in cents since the 2021 migration." --entity table:orders#price
repo-audit note "This job is still run by hand at quarter end." --file scripts/quarter_close.py
```

Answers and notes are kept in the store and apply to every later run.

## Rules the code must obey

Some requirements live in people's heads and nowhere in the code: "no model choice may look at the holdout period", "every price is in cents", "nothing reads the raw table directly". State them once and every file is screened against them on each run:

```bash
repo-audit rule "No code path may read evaluation-period data before final scoring. Model selection, search spaces, scaling and imputation use the tuning split only."
```

A rule is a demand with no file behind it. Rules written in the documentation are picked up the same way. A suspected violation goes to the verifier like any other finding.

## Supply and demand

Each file is read for two things besides what it does: what it **requires** to be true of its inputs ("totals arrive already winsorized") and what it **ensures** about its outputs ("drops rows with missing dates"). The contracts stage then walks every shared table, file and parameter and matches each demand to a supplier. It reports a demand nobody meets, a supplier that does something different from what is demanded, and the same treatment applied twice. Before reporting an unmet demand it sends a second look into the files that write the data, and if nothing in the repository produces the data at all, it asks the maintainers instead.

## Potential clean up

Separate from the findings, because dead code is not a bug and a large repository has enough of it to bury the bugs. The static pass lists unused files, top-level functions and classes, methods, module constants, outputs nothing reads, and commented-out code. For each name it finds every occurrence in the repository and classifies it: Python code, a test, a string in the code (`getattr`, a registry), `__all__`, a config or script, a notebook, documentation. Comments and docstrings do not count. Each candidate gets a tier:

- **Safe to remove**: nothing refers to it anywhere.
- **Probably unused**: only tests or documentation refer to it, or it is public in a library other repositories import.
- **Check first**: it may be reached in a way the repository does not show: a framework decorator, a string lookup, a config file, a notebook, a method a library base class calls.

What static analysis cannot settle goes up a ladder, each step only for what the step before left open: a cheap model reads the occurrences (many candidates per call); the verifier reads the definition, each occurrence in context and every place the repository looks names up at run time; an optional outside checker (`[cleanup] external_check`, any command that takes candidates as JSON and returns verdicts); and finally maintainers, with one question per file, whose answer goes back to the verifier. Code that only tests reach never becomes "safe": removing it means removing its tests. Settings are under `[cleanup]`; `public_api = "yes"` for a library.

**With a decision model (Jev).** Set `[decisions] model = "~typesafe/jev-latest"` to use TypeSafe's Jev, served through OpenRouter with the same key. Jev answers typed questions (yes/no, pick one) with calibrated probabilities instead of writing text, at a fraction of a cent per call, and it is a different family from the chat models. It changes the ladder in two places:

- **First check.** One question per candidate (used / only tests / unused) replaces the packed cheap-model call. A candidate is settled only when the top answer reaches `accept` (default 0.9); anything less goes to the verifier.
- **Double check.** Before anything becomes "safe", whether static analysis or the verifier said so, Jev is asked whether anything can still reach it. At `veto` (default 0.5) or above, a verifier "unused" becomes "check first" with a question to maintainers, and a static "safe" drops to "probably unused".

## Branches from the same data

Some problems are invisible file by file. Training and scoring, a backtest and the production job, or every model in a horse race each start from the same data and are supposed to treat it the same way. When one branch winsorizes before imputing and the other after, or one restricts the sample to 1992Q1 and the other does not, each file looks fine on its own.

The block wave finds the points where the data lineage splits: a table or file read by several pieces of code. Each reader, plus what only it feeds downstream, is one branch. A worker describes each branch as an ordered list of steps applied to the shared input (filters, conversions, imputation, splits, fits), each step quoting its line and listing its parameters. A second worker lines the branches of one split up side by side, decides which are meant to match (from the code, the docs and any rules: competing models must see the same sample and preprocessing; a summary table and a model fit need not), and reports missing steps, different orders, different parameters and different definitions. Those go to the verifier like any other finding.

To say outright which branches must match, write it as a rule: `repo-audit rule "train.py and score.py apply the same transformations to features"`.

## How it works

No stage reads the whole repository. Each one does a narrow job and writes its results to a shared store (the "board"); later stages read the board, not each other.

| Stage | Uses a model | What it does |
|---|---|---|
| Skeleton | no | Import graph, entry points, which tables, files and environment variables each file reads and writes, unreachable files, duplicated functions |
| Docs (wave 0) | cheap | Records what the documentation claims (markdown and LaTeX, so a paper counts), as statements that can be checked against code |
| System (wave 1) | strong | The crude map: what goes in, what comes out, which paths to trace |
| Paths (wave 2) | strong | For each path, the ordered steps from entry point to output |
| Files (wave 3) | cheap | Reads every file and records what it does and what it assumes about the data it touches |
| Reconcile | strong | Lays all claims about one table, file or config key side by side and reports where they disagree |
| Contracts | strong | Matches what each file requires of its inputs to what upstream files ensure; reports unmet, mismatched and doubled treatment |
| Probe | strong | A second look at one file when reconciliation or contracts is missing a fact; then both run again |
| Supply | strong | For requirements the summaries could not settle, reads the producer's code next to the requirement |
| Blocks | strong | Where the data lineage splits, describes each branch as ordered steps applied to the shared input |
| Branches | strong | Lines up the branches of one split and reports where branches meant to match do not |
| Rules | strong | Screens every file against the rules maintainers stated and the rules written in the docs |
| Verify | verifier | A different model tries to refute each candidate against the code |
| Second opinion | verifier_2 | Optional: a model from another family checks the important verdicts again; disagreement goes to a person |
| Themes | strong | Groups confirmed findings by the cause behind them, so one fix is listed once |
| Questions | cheap | Triage: merges duplicates, drops what the code can answer, marks what matters most |

Three rules keep the output trustworthy:

1. **Every quote is checked.** A claim must quote the line it rests on, and a program looks for that quote in the file. If it is not there, the claim is discarded.
2. **A finding needs two independent sources.** Reconciliation can only report a disagreement between claims that come from different files or documents.
3. **Nothing is reported as confirmed until a second model has tried to refute it** and has itself quoted code that the program found.

Prompts are plain text files in `repo_audit/prompts/` and can be edited or overridden with `[prompts] dir`.

## Models and cost

Any OpenAI-compatible endpoint works; OpenRouter is the default. `provider = "queue"` writes each prompt to `<output>/queue/pending/` and reads replies from `<output>/queue/replies/`, pausing at each stage until they are there; anything that can read and write files can then act as the model (other agents, a batch job). Three tiers are set in `[models]`: `cheap` reads every file, `strong` traces and reconciles, `verifier` attacks the findings and is best set to a different model family from `strong`. `provider = "command"` pipes each prompt to a command-line tool instead of an HTTP endpoint.

`[llm] disable_reasoning` (on by default) sends OpenRouter's switch to turn off hidden reasoning. Every prompt asks for one JSON object, and a route with extended thinking can otherwise spend the whole output budget thinking and return nothing ("empty reply"), however high `max_output_tokens` is set. Turn it off for an endpoint that rejects unknown request fields.

`verifier_2` is optional. Set it to a model from a family different from `verifier`, and findings at or above `[verify] second_opinion_min_severity` (default `high`) are checked by both. Where they agree, the report says so; where one confirms and the other refutes, neither wins and the finding becomes a question for maintainers. Before relying on a new model as verifier, look at how many of its quotes the evidence check accepts: a model that paraphrases code cannot confirm anything.

**Which code a worker sees.** For Python, the static pass records every function and every call between functions of the repository. Workers that need code (the producer check, second looks, the block wave, the verifier) are shown the whole function a line sits in, the repository helpers it calls, and one or two places it is called from, rather than a window of lines around a name. Other languages fall back to windows around the matching lines. `[limits] call_graph`, `function_max_lines`, `context_callers` and `context_callees` control this.

`repo-audit plan` reports the size of a run before any money is spent. Measured on two open-source repositories:

| Repository | Lines analyzed | File-wave calls | File-wave input tokens | Static pass |
|---|---|---|---|---|
| Django | 718,000 | 1,146 | about 3.3 million | 10 s |
| Apache Superset | 2,190,000 | 6,788 | about 18.3 million | 22 s |

The file wave is the bulk of the tokens and runs on the cheap tier. Reconcile and verify run on the stronger tiers and grow with what the file wave finds, so `plan` can only give a floor for them. `[budget]` caps calls, tokens, and (where the endpoint reports cost) dollars; when a cap is hit the run stops, writes a partial report, and continues from there next time.

Settings that control cost on a large repository, all under `[verify]` and `[limits]`:

- `local_risk_min_severity`: single-file concerns below this are listed but not verified (default `medium`).
- `skip_low_redundant`: leave "same constant typed twice" findings unverified.
- `focus_terms`: findings mentioning any of these are verified whatever their severity, e.g. `["leak", "evaluation period"]`.
- `probe_rounds`, `max_probes_per_round`: how many second looks reconcile may ask for.
- `rule_min_severity`, `branch_min_severity`: suspected rule breaches and branch differences below these are listed but not verified.
- `max_forks`, `max_branches`, `block_max_files`, `block_code_lines`: how many splits in the data flow are described, and how much code each branch's worker sees.

## What it does not do

- It does not fix anything, and gives no effort estimates.
- It does not run the code or its tests.
- Static analysis is fullest for Python and SQL, partial for JavaScript and TypeScript (imports only), and limited to reference scanning for other languages. Every language is read by the file wave regardless. See `DESIGN.md` for adding a language.
- A disagreement is only found if both sides were written down under the same entity name. Files that touch the same data through names the audit could not connect will not be compared.

## Running with Claude Code subagents instead of an API

The `queue` provider writes each prompt to `<output>/queue/pending/` and pauses. `harness/subagents/step.py` runs the pipeline, groups the pending prompts into batch files by tier, and prints them. Give each batch to a subagent with the prompt in that script's docstring, run the script again, and repeat until nothing is pending. `examples/subagents.toml` is a starting configuration.
