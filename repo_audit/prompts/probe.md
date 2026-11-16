Task: take a second look at one file to answer one question.

A worker comparing claims about a subject could not tell whether two files disagree, because a fact was missing, and it believes this file holds that fact. You receive the question and the parts of the file that mention the subject. Answer it from the code shown.

Produce:

"claims": what the code shows that bears on the question, in the same form as any other claim. Use the subject's entity key so the new claims join the others.
  {"kind": "requires" | "ensures" | "assumption" | "behavior" | "input" | "output",
   "entity": entity key, "attribute": attribute or null, "value": short normalized value or null,
   "statement": "one self-contained sentence",
   "evidence": [{"line_start": 0, "line_end": 0, "quote": "exact code"}],
   "confidence": "high" | "medium" | "low"}

"questions": usually none. If the question asks you to compare this file with another one, you cannot, and should not try: record precisely what this file shows (exact names, values, column headers) and the comparison step will do the rest. If the answer is simply not in the code shown, return no claims. Raise a question for a maintainer only when the answer cannot be in any file of the repository.
  {"text": "...", "why": "...", "entity": entity key or null}

Reply with {"claims": [...], "questions": [...]}.
