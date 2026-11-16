Task: triage the questions before they reach a person.

Workers reading different parts of the repository raised questions for its maintainers. Maintainers answer these by hand, in whatever time they can spare, so the list has to be short and every question on it has to deserve a human. You receive the open questions with their ids, which wave raised each, and what each is about. Workers saw only one file or one summary, so many of their questions are about things that are written down elsewhere in the repository.

Do three things:

"merge": group questions that a single answer would fully answer. Two questions about the same file are not duplicates if they ask about different properties of it. Keep the clearest, most specific one.

"drop": questions that should not go to a person at all:
- the answer is in the repository's code or configuration (where a name is defined, what a function returns, which file a script reads, whether a value matches the one in another file, what a default is)
- an artifact of the worker seeing only part of a file ("where is X imported", "what is above line 321")
- about how a well-known library behaves
- of no consequence for whether the results are right
Never drop a question marked "decides a finding".

"important": of the questions that remain, the ones whose answer most affects whether the repository's results are correct or reproducible: intent behind a choice that changes numbers, guarantees of external data, whether something is still used. Be selective: aim for the fifth of the remaining questions that matter most.

Reply with:
{"merge": [{"keep": "Q-0003", "duplicates": ["Q-0011", "Q-0027"]}],
 "drop": ["Q-0004", "Q-0019"],
 "important": ["Q-0003", "Q-0031"]}
