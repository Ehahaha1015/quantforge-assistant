"""Fresh-process determinism probe for oracle AC."""

from __future__ import annotations

from _credential_fixtures import (
    canonical_json,
    facts_payload,
    make_facts,
    make_reference,
    make_scope,
)

_scope = make_scope()
_reference = make_reference()

print(make_facts(_scope, _reference).facts_id)
print(canonical_json(facts_payload(_scope, _reference)))
