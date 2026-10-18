from __future__ import annotations

import hashlib
import json

from _fixtures import make_artifact

artifact = make_artifact()
payload = json.loads(artifact.model_dump_json())["payload"]
canonical = json.dumps(
    payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
).encode("utf-8")
print(
    json.dumps(
        {
            "payload_sha256": hashlib.sha256(canonical).hexdigest(),
            "portfolio": artifact.portfolio_commitment,
            "price": artifact.price_set_commitment,
            "provenance": artifact.provenance_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
)
