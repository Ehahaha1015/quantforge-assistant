"""Research candidates never auto-promote on development scores."""

POLICIES = {"STOCK": "BASELINE_V2", "ETF": "BASELINE_V2"}
EXITS = {"STOCK": "ATR_TREND_TIME", "ETF": "ATR_TREND_TIME"}
STATUS = "NOT_PROMOTED"
EVALUATION_SUMMARY = {
    "development_assets": 16,
    "sealed_assets": 8,
    "sealed_windows": 3,
    "candidate": "REGIME_ROUTER",
    "stock_promoted": False,
    "etf_promoted": False,
    "failed_gates": [
        "同覆盖率准确率超过多数类基线2个百分点",
        "每段上涨召回率至少10%",
        "30bp回撤恶化不超过1个百分点",
    ],
    "note": "覆盖率提高，但股票和ETF均未通过封存检验，保留原有保守默认规则。",
}
