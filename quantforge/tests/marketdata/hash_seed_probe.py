"""Fresh-process determinism probe for the frozen PRICE-001 identity rules."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _entry in (str(_ROOT), str(_ROOT / "src"), str(_ROOT / "tests" / "marketdata")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from _marketdata_fixtures import make_observation, make_target, produce

print(produce(make_observation(), target=make_target()).model_dump_json())
