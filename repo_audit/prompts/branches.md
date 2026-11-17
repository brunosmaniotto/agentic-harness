Task: line up branches that start from the same data and report where branches that should treat it the same way do not.

You receive the shared input, any rules maintainers or the documentation state, and two or more BRANCHES. Each branch is a chain of files with its steps numbered B1.S1, B1.S2, ..., written by workers who each saw one branch. Every step points at the line that does it.

First decide, for the branches as a group, which of them are meant to treat the shared input the same way:

- "should_match": they compute the same kind of thing and differ only in a part that is supposed to differ. Typical cases: training and scoring of one model; a backtest and the production job it stands for; competing models or methods evaluated against each other, which must all see the same data, the same sample, the same splits and the same preprocessing; two reports of one quantity. A rule above may say so outright.
- "may_differ": they serve different purposes, so different treatment is expected (a summary-statistics table and a model fit, say).
- "unclear": you cannot tell from what is shown. Ask one question per unclear group.

Then, for every group that should match or is unclear, compare the branches step by step and report each difference that would change results:

- "missing_step": one branch does something (drops outliers, deflates, deduplicates, lags) that the other does not.
- "different_order": the same steps in an order that changes the outcome (impute then winsorize vs winsorize then impute; scale before vs after the split).
- "different_parameter": the same step with different values (percentiles, windows, sample start, horizon, units).
- "different_definition": the same name computed differently.

Each difference names the branches and the steps involved, as "B1.S3"; for a missing step, name the step in the branch that has it. Do not report a difference that is explained by the part meant to differ (the model itself in a horse race), and do not report differences in what is plotted or printed unless they change a reported number. Be specific in the title: name the step and the branches' files.

Reply with:
{"groups": [{"branches": ["B1", "B2"], "expectation": "should_match" | "may_differ" | "unclear", "why": "one sentence"}],
 "divergences": [
   {"branches": ["B1", "B2"], "steps": ["B1.S2", "B2.S2"],
    "kind": "missing_step" | "different_order" | "different_parameter" | "different_definition",
    "title": "one line a maintainer would understand",
    "explanation": "what each branch does, and what goes wrong because they differ",
    "severity": "high" | "medium" | "low",
    "confidence": "high" | "medium" | "low"}
 ],
 "questions": [{"text": "...", "why": "..."}]}

Severity: high if a reported comparison or a production result is probably wrong and nobody would notice; medium if it would be under plausible conditions; low if minor.
