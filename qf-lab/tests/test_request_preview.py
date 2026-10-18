from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from qf_lab.evaluation import EvaluationBatch
from qf_lab.gateway import GatewayError
from qf_lab.request_preview import RequestPreview
from qf_lab.room_gateway import FakeRoomProvider, RoomGateway


def test_preview_sizes_actual_payload_without_network_or_credentials() -> None:
    preview = RequestPreview(
        protocol="OPENAI_COMPATIBLE", model="preview", generation_mode="NON_THINKING"
    )
    bounds = preview.estimate(
        {
            "phase": "task_evaluation",
            "reference_ids": [],
            "questions": [["T01", "Run SMA"]],
        },
        EvaluationBatch,
        max_output=256,
    )
    assert bounds["input"] > 2000 and bounds["output"] == 256


def test_lower_output_caps_are_exact_and_do_not_expand_existing_limit(
    tmp_path: Path,
) -> None:
    class Provider(FakeRoomProvider):
        def complete_structured(self, context, schema, *, max_output=1800):
            assert max_output == 256
            return schema.model_validate(
                {
                    "evidence_refs": [],
                    "answers": [{"q": "T01", "op": "insufficient", "a": "", "c": []}],
                }
            ), {"real_model_call": False, "input_tokens": 0, "output_tokens": 0}

    gateway = RoomGateway(tmp_path, calls=1, tokens=20000, cost=Decimal(1))
    provider = Provider()
    for cap in (True, 1801, 1):
        with pytest.raises(GatewayError, match="OUTPUT_BUDGET"):
            gateway.call("researcher", provider, {}, EvaluationBatch, max_output=cap)
    assert not gateway.ledger()
    gateway.call("researcher", provider, {}, EvaluationBatch, max_output=256)
    assert gateway.ledger()[0]["reserved_tokens"] > 256
