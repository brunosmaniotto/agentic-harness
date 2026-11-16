Task: screen files against rules that must hold everywhere.

The people responsible for this repository, or its documentation, have stated rules: things that must be true of the code as a whole, whichever file does the work. A rule has no file of its own, so the usual comparison between files never tests it. You receive the rules, and for a group of files the notes other workers recorded about each one: what it reads, what it requires and ensures, and what it does, each with the line it rests on.

For each file, ask whether anything recorded about it could break a rule. You are screening from notes, not reading code: everything you flag goes to a verifier who will read the code itself. So flag what a careful reviewer would want to look at: behaviour that would break the rule if the notes are accurate, and behaviour that touches what the rule protects in a way the notes do not show to be safe. Do not flag a file merely for being in the same area as a rule. Most files break no rule.

Think about indirect ways a rule can be broken, not only the literal one. A rule that evaluation data must not influence design choices is broken by selecting a model on evaluation error, by fitting a scaler or an imputation on all rows, by choosing features with the full sample, and by settings whose comments say they were picked for how they performed.

Reply with:
{"suspects": [
  {"rule_id": "R-1",
   "claim_ids": ["C-00123", "C-00130"],
   "title": "one line: what this file does that may break the rule, naming the file",
   "why": "how what is recorded would break the rule, and what the verifier should check in the code",
   "severity": "high" | "medium" | "low"}
]}

Cite the claims you are relying on; they must all be from the same file. If nothing is suspect, reply {"suspects": []}.
