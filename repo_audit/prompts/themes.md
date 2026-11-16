Task: group confirmed findings by what lies behind them.

You receive the findings that survived verification, one line each. Many of them are symptoms of a few underlying causes: a parameter that is not read from one place, a convention never written down, a module that was copied instead of shared, a boundary (training versus evaluation data, raw versus cleaned) that is not enforced. A maintainer who sees fifty findings needs to know the five things to fix.

Group findings that would be settled by the same change, or that exist for the same reason. Give each group a title that names the cause, not the symptom, and one or two sentences on what the findings have in common and what kind of change would settle them (name the kind of change; do not design it). A finding belongs to at most one group. Leave a finding ungrouped if it stands alone; do not build a group of unrelated leftovers.

Reply with:
{"themes": [
  {"title": "the underlying cause, in a few words",
   "summary": "what these findings share and what would settle them together",
   "finding_ids": ["F-0012", "F-0040"]}
]}
