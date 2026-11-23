import copy
import hashlib
import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from quantforge.data.bars import logical_content_hash
from quantforge.strategies.chart_evaluation import label_threshold

from qf_platform.chart_analysis import ChartAnalysisService
from qf_platform.chart_api import create_app
from qf_platform.chart_demo import OfflineChartRuntime, prepare_demo
from qf_platform.chart_timeframes import ZONE
from qf_platform.chart_visualization import visualization


@pytest.fixture
def workspace(tmp_path):
    ids = prepare_demo(tmp_path)
    service = ChartAnalysisService(tmp_path, runtime=OfflineChartRuntime(tmp_path))
    while service.tick():
        pass
    return service, ids


def test_visualization_reuses_exact_archived_ohlc_and_existing_threshold(workspace):
    service, ids = workspace
    snapshot = service.get(ids[0])
    before, budget = copy.deepcopy(snapshot), service.status()
    view = visualization(snapshot, service.market_data)
    assert view.status == "AVAILABLE", view.reason
    assert view.offline and view.model_calls == view.market_fetches == 0
    assert view.forecast_validated is False
    source = snapshot["algorithm_analysis"]["source"]
    rows = json.loads((service.market_data / source["artifact"]).read_bytes())["bars"]
    assert len(view.candles) <= 120
    assert view.candles[-1].close == str(rows[-1]["close"])
    assert (
        Decimal(view.candles[-1].sma20)
        == sum(Decimal(str(r["close"])) for r in rows[-20:]) / 20
    )
    reference, atr = Decimal(view.reference_close), Decimal(view.atr14)
    last = view.boundaries[-1]
    span = reference * label_threshold(reference, atr, view.horizon_bars)
    assert abs(Decimal(last.upper) - reference - span) < Decimal("1e-24")
    assert last.step == view.horizon_bars == 5
    assert (
        view.levels.reference
        == snapshot["algorithm_analysis"]["trade_plan"]["levels"]["reference"]
    )
    assert snapshot == before == service.get(ids[0]) and service.status() == budget


def test_invalid_images_quality_and_corrupt_archives_cannot_produce_chart(workspace):
    service, ids = workspace
    assert visualization(service.get(ids[1]), service.market_data).status == "BLOCKED"
    snapshot = service.get(ids[0])
    broken = copy.deepcopy(snapshot)
    broken["algorithm_analysis"]["synthesis"]["risk_flags"].append("SESSION_GAPS")
    view = visualization(broken, service.market_data)
    assert (
        view.status == "BLOCKED"
        and not view.candles
        and not view.boundaries
        and view.levels is None
    )
    for key in ("bars_sha256", "artifact"):
        broken = copy.deepcopy(snapshot)
        broken["algorithm_analysis"]["source"][key] = "wrong"
        assert visualization(broken, service.market_data).status == "UNAVAILABLE"
    broken = copy.deepcopy(snapshot)
    broken["algorithm_analysis"]["synthesis"]["indicators"]["atr14"] = "NaN"
    assert visualization(broken, service.market_data).status == "UNAVAILABLE"


@pytest.mark.parametrize("timeframe,horizon", [("1d", 20), ("60m", 4), ("1w", 4)])
def test_real_archive_parsing_supports_every_frequency_without_network(
    workspace, timeframe, horizon
):
    service, ids = workspace
    snapshot = service.get(ids[0])
    source = snapshot["algorithm_analysis"]["source"]
    rows = []
    if timeframe == "60m":
        days = [date(2026, 8, 1) + timedelta(days=i) for i in range(61)]
        timestamps = [
            datetime.combine(d, t, ZONE)
            for d in days
            if d.weekday() < 5
            for t in (time(10, 30), time(11, 30), time(14), time(15))
        ]
    elif timeframe == "1w":
        timestamps = [
            datetime.combine(date(2024, 1, 5) + timedelta(weeks=i), time(15), ZONE)
            for i in range(140)
        ]
    else:
        timestamps = [
            datetime.combine(date(2026, 5, 1) + timedelta(days=i), time(15), ZONE)
            for i in range(130)
        ]
    for i, at in enumerate(timestamps):
        value = 100 + i * 0.1
        rows.append(
            [
                at.strftime("%Y%m%d%H%M")
                if timeframe == "60m"
                else at.date().isoformat(),
                str(value),
                str(value),
                str(value + 1),
                str(value - 1),
                "10000",
            ]
        )
    from qf_platform.chart_algorithms import parse_daily
    from qf_platform.chart_timeframes import parse_period

    raw = json.dumps(
        {
            "code": 0,
            "data": {
                "sh600519": {{"1d": "day", "1w": "week", "60m": "m60"}[timeframe]: rows}
            },
        }
    ).encode()
    start, end = timestamps[0].date(), timestamps[-1]
    frame = (
        parse_daily(raw, "sh600519", start, end.date())
        if timeframe == "1d"
        else parse_period(raw, "sh600519", start, end, timeframe)[0]
    )
    sha = hashlib.sha256(raw).hexdigest()
    (service.market_data / (sha + ".json")).write_bytes(raw)
    snapshot["request"].update(timeframe=timeframe, horizon_bars=horizon)
    source.update(
        provider="TENCENT_PUBLIC_HTTPS",
        symbol="sh600519",
        timeframe=timeframe,
        raw_sha256=sha,
        artifact=sha + ".json",
        bars_sha256=logical_content_hash(frame),
        first=start.isoformat(),
        last=end.isoformat() if timeframe == "60m" else end.date().isoformat(),
        cutoff=end.date().isoformat(),
        cutoff_at=end.isoformat(),
    )
    snapshot["algorithm_analysis"]["synthesis"]["indicators"]["reference_close"] = str(
        frame["close"][-1]
    )
    snapshot["algorithm_analysis"]["synthesis"]["indicators"]["atr14"] = "2"
    snapshot["report"]["recognized_timeframe"] = timeframe
    snapshot["request"].update(
        period_start=start.isoformat(), period_end=end.date().isoformat()
    )
    snapshot["report"]["recognized_period"] = f"{start} 至 {end.date()}" + (
        " 15:00" if timeframe == "60m" else ""
    )
    view = visualization(snapshot, service.market_data)
    assert view.status == "AVAILABLE", view.reason
    assert view.timeframe == timeframe and view.horizon_bars == horizon
    assert view.candles[-1].label == source["last"]


def test_api_offline_visualization_is_read_only_and_explicitly_synthetic(workspace):
    service, ids = workspace
    before, budget = service.get(ids[0]), service.status()
    app = create_app(service.root.parent, port=4192, runtime=service.runtime, demo=True)
    with TestClient(app, base_url="http://127.0.0.1:4192") as client:
        response = client.get(f"/api/chart-analyses/{ids[0]}/visualization")
        assert response.status_code == 200
        value = response.json()
        assert value["status"] == "AVAILABLE" and value["offline"]
        assert value["model_calls"] == value["market_fetches"] == 0
        assert (
            client.get(f"/api/chart-analyses/{ids[1]}/visualization").json()["status"]
            == "BLOCKED"
        )
        assert (
            client.get(
                f"/api/chart-analyses/{ids[0]}/visualization",
                headers={"Origin": "https://foreign.example"},
            ).status_code
            == 403
        )
    assert service.get(ids[0]) == before and service.status() == budget
