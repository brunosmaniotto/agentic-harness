Task: try to refute one candidate finding.

Another worker proposed the finding below. It worked from short notes written by workers who each saw one file, so it may rest on a misreading, a note taken out of context, or a difference that the code already handles. Everything that survives you is shown to the repository's maintainers as a confirmed problem, and their time is the scarce resource here: a false alarm costs them an investigation and costs the audit their trust. So start from the position that the finding is wrong, and let the code change your mind.

You receive the finding, the claims it cites, anything maintainers have told the audit that bears on it, and the code around each cited location. Check:
- Do the quoted lines actually say what the claims say they do?
- Are the two places really dealing with the same data, at the same point in its life? A value converted on the way from one place to the other is not a conflict.
- Is there a guard, conversion, or normalization in the shown code that already reconciles the difference?
- For a file said to be unused: do the text-search results show it being loaded, scheduled, or referred to in a way static analysis would miss (a config file, a plugin table, a string-built import, a command in a script)?
- For a requirement said to be unmet: you are shown where the data is produced. Does anything there, or anywhere else shown, establish what the consumer requires, perhaps in other words or one step earlier? Does the consumer in fact establish it itself before relying on it? If the producer's code is not among the excerpts, ask for it.
- For supply that is said not to match the demand: are the two really about the same property of the same data, and do the terms really differ?
- For a rule said to be broken: does this code actually do what the rule forbids, on a path that runs, or does it only look that way from a summary? A rule is broken by code, not by a variable name.
- For duplicated code: are the copies really the same, and are both live?
- Has a maintainer already said this is intended?

Verdicts:
- "confirmed": the code shown establishes the problem. You must quote the lines that establish it.
- "refuted": the code shown contradicts the finding, or shows that it is handled, or the cited claims misread the code.
- "needs_human": the code is consistent with the finding, but whether it is a problem depends on a fact that is not in the repository: which of two behaviours is intended, whether something is still run by hand, what an outside system guarantees. Ask the one question whose answer would settle it.
- "needs_code": you cannot decide because code you were not shown would settle it: the caller, the definition of a name, the place a file is written. Say which file and what to look for in "requests"; the pipeline fetches it and asks you again, once. Do not use needs_human for something the repository can answer.

Do not confirm on plausibility. If you cannot point at lines that establish the problem and you cannot refute it either, the verdict is needs_code when more code would settle it, otherwise needs_human.

When you confirm, correct the title and detail so they claim exactly what the code shows and no more. If the original overstated, narrow it.

Reply with:
{"verdict": "confirmed" | "refuted" | "needs_human" | "needs_code",
 "reasoning": "two to five sentences: what you checked and what decided it",
 "severity": "high" | "medium" | "low",
 "title": "the finding in one line, corrected if the original was imprecise",
 "detail": "what is wrong and where, for a maintainer who has not seen the claims",
 "evidence": [{"path": "repository path as shown in the excerpt headings", "line_start": 0, "line_end": 0, "quote": "exact code"}],
 "question": {"text": "...", "why": "..."} or null,
 "requests": [{"path": "repository path", "find": "a function name, variable name, or exact text to locate in that file"}]}

"requests" is only for needs_code; otherwise use an empty list.

Severity: high if the result is probably wrong numbers or lost data that nobody would notice; medium if it would be under plausible conditions, or is a trap for the next person to change this code; low for dead and duplicated code.
