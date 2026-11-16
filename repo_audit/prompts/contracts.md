Task: match what consumers require against what producers ensure.

Think of each piece of shared data as having a demand side and a supply side. Files that read it wrote down what they require of it: things that must already be true for their code to be correct. Files that produce it wrote down what they ensure: what their code makes true before handing it on. Each worker saw only its own file, so nobody has yet checked that the two sides meet. A requirement that no producer meets, or that a producer meets on different terms, is one of the main ways a codebase that many people have worked on goes quietly wrong.

You receive, for one subject (or several, each in its own SUBJECT block, to be treated separately): which files write it, the DEMAND claims, and the SUPPLY claims (what producers ensure, plus other recorded behaviour and outputs that may bear on it).

First merge demand claims that ask for the same thing into one requirement. Then, for each requirement, decide:

- "met": a producer ensures it, on the same terms. Cite the supply claim. Nothing more is needed.
- "mismatched": a producer does address it, on different terms: winsorized at 5/95 where 1/99 is required; dollars where cents are required; deduplicated on a different key; a date convention that differs.
- "double": the requirement is established more than once along the way, so the operation is applied twice (two files each winsorize, each convert units, each lag by a quarter). Cite the supply claims. Use this only when applying it twice changes the result.
- "unmet": the supply claims describe what the producers do in enough detail that the requirement would be visible if it were met, and it is not there.
- "unclear": the supply claims do not say enough about the producers to tell. The pipeline will then ask the producing files directly. Prefer this to "unmet" whenever the supply side is thin; an unmet verdict on thin evidence wastes a reviewer's time.

A requirement that the consumer enforces itself (it raises an error if the property fails) is still a requirement: report it only if it is mismatched or unmet, with low severity, since the failure would be loud.

Reply with:
{"requirements": [
  {"subject": "the SUBJECT line",
   "requirement": "what is required, in one precise sentence",
   "demand_ids": ["C-00012", "C-00044"],
   "status": "met" | "mismatched" | "double" | "unmet" | "unclear",
   "supply_ids": ["C-00031"],
   "title": "for anything other than met: one line a maintainer would understand, naming the files",
   "explanation": "what the consumer needs, what the producer does, and what goes wrong",
   "severity": "high" | "medium" | "low",
   "confidence": "high" | "medium" | "low"}
]}

Use claim ids exactly as given, and only from the same SUBJECT block. Severity: high if the mismatch probably produces wrong numbers that nobody would notice; medium if it would under plausible conditions; low if it would fail loudly or is cosmetic.
