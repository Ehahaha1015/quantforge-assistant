from __future__ import annotations

from _frontier_fixtures import complete_inputs

from quantforge.frontier import evaluate_frontier_eligibility

print(evaluate_frontier_eligibility(**complete_inputs()).model_dump_json())
