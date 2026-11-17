Task: describe what one branch of the data flow does to the data it starts from, as an ordered list of steps.

Several branches of this repository start from the same data. You see one of them: the files in data-flow order (the first reads the shared input, the later ones read what the earlier ones write), notes other workers made about these files, and code. Code sections are headed "### path (lines a-b of n)"; a heading may add which function the section is or who calls it.

Your description will be lined up against the descriptions of the other branches, step by step, by a worker who sees only the descriptions. So:

- List every step that changes the data or decides what is kept, in the order the data goes through them, from where the shared input is read to where results leave the block: filters and sample restrictions, joins, deduplication, unit and scale conversions, imputation, winsorizing or trimming, transformations (logs, differences, standardizing, detrending), lags and leads, train/test or tuning/evaluation splits, model fitting, scoring, aggregation, what is written out.
- Describe each step concretely enough that a reader could tell whether another branch does the same thing: "drop rows with missing price", not "clean the data".
- Put the values that would make two otherwise similar steps differ in "parameters": thresholds, percentiles, window lengths, sample dates, horizons, units, seeds, which columns. Use the names the code uses.
- Give evidence for every step: one to three lines copied exactly from the listing, with the path from the section heading.
- Do not invent steps the code does not show. If a step clearly happens in code you were not shown (a helper that is called but not listed), say so in the description and quote the call.

Reply with:
{"purpose": "one sentence: what this branch produces and for whom",
 "steps": [
   {"description": "what this step does to the data",
    "parameters": {"name": "value"},
    "evidence": [{"path": "path from the heading", "line_start": 10, "line_end": 11, "quote": "copied exactly"}]}
 ],
 "questions": [{"text": "...", "why": "..."}]}
