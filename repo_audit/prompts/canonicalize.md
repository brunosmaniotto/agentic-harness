Task: find entity keys that are different names for the same thing.

Workers reading different files named the data they saw. Usually the same data got the same key, but not always: one file calls a table "orders", another "dw.orders"; one reads "data/orders.csv" and another writes "{}/orders.csv". Claims are compared per key, so two names for one thing means a disagreement between them is never seen. You receive the list of keys, how many claims each has, and one place that uses it.

Group only keys you are confident refer to the same data. A wrong merge makes unrelated claims look like contradictions, which is worse than a missed merge. Do not merge a raw table with a cleaned or aggregated version of it, a table with a file that merely feeds it, or two keys that only share a common word.

Reply with:
{"groups": [
  {"canonical": "the key to use, taken from the list",
   "aliases": ["other keys from the list that mean the same thing"],
   "reason": "why these are the same"}
]}

Use keys exactly as they appear in the list. If nothing should be merged, reply {"groups": []}.
