Task: turn investigation notes into leads.

You receive notes written by an earlier documentation pass over the repository: things it noticed in passing and thought someone should look at. They were not verified. Each one you extract will be handed to a verifier together with the code of the files it names, so a lead is only useful if it says what to check and where.

Reply with:
{"leads": [
  {"title": "short statement of the suspected problem",
   "detail": "what the note says, in one or two sentences, including what would make it true or false",
   "files": ["repository paths the note names; empty if it names none"],
   "severity": "high" | "medium" | "low"}
]}

Keep separate suspicions separate. Do not add suspicions of your own, and do not drop a note because it seems minor.
