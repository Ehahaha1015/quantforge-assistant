// Generated deterministic software fixture, never market evidence.
export const synthesisFixture = {
  "schema_version": "chart-analysis-v1",
  "id": "chart-cccccccccccccccccccccccccccccccc",
  "status": "SUCCEEDED",
  "request": {
    "symbol": "600519",
    "display_name": "",
    "period_start": "2026-08-15",
    "period_end": "2026-09-08",
    "horizon_sessions": 5,
    "timeframe": "1d",
    "horizon_bars": 5,
    "period_end_time": "15:00",
    "purchase_price": null,
    "held_bars": 0,
    "include_context": false,
    "position_reference_id": null,
    "question": "分析图中趋势，并给出未来走势的条件性情景。",
    "channel": "WEB"
  },
  "image": {
    "sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
    "bytes": 500,
    "width": 400,
    "height": 240,
    "mime": "image/png"
  },
  "created_at": "2026-09-08T08:00:00+00:00",
  "updated_at": "2026-09-08T08:00:00+00:00",
  "report": {
    "image_quality": "READABLE",
    "recognized_symbol": "软件夹具600519",
    "recognized_period": "2026-08-15 至 2026-09-08",
    "recognized_timeframe": "1d",
    "summary": "软件夹具的模型原始叙述：偏强展望不应覆盖后端震荡结论。",
    "observed_trend": "UNCLEAR",
    "outlook": "BULLISH",
    "observations": [
      {
        "observation": "夹具中只保留占位观察。",
        "image_region": "图像区域"
      }
    ],
    "support_zones": [],
    "resistance_zones": [],
    "volume_comment": "夹具未提供成交量。",
    "scenarios": [
      {
        "direction": "BULLISH",
        "trigger": "仅在条件成立时。",
        "interpretation": "软件夹具情景。",
        "invalidation": "条件未满足时失效。"
      },
      {
        "direction": "RANGE",
        "trigger": "仅在条件成立时。",
        "interpretation": "软件夹具情景。",
        "invalidation": "条件未满足时失效。"
      },
      {
        "direction": "BEARISH",
        "trigger": "仅在条件成立时。",
        "interpretation": "软件夹具情景。",
        "invalidation": "条件未满足时失效。"
      }
    ],
    "uncertainties": [
      "这是明确的软件夹具，不是模型或市场证据。"
    ]
  },
  "algorithm_analysis": {
    "strategies": [
      {
        "id": "sma-5-20",
        "label": "均线趋势5/20",
        "family": "SMA",
        "parameters": {
          "short_window": 5,
          "long_window": 20
        },
        "required_bars": 20,
        "entry_rule": "5周期均价高于20周期均价",
        "exit_rule": "5周期均价回落至20周期均价及以下",
        "status": "COMPUTED",
        "latest_state": 1,
        "previous_state": 1,
        "replay": [
          {
            "fee_bps": 10,
            "price_return": 0.1352272727272723,
            "max_drawdown": 0.0,
            "turnover": 1.0
          },
          {
            "fee_bps": 30,
            "price_return": 0.13295454545454488,
            "max_drawdown": 0.0,
            "turnover": 1.0
          }
        ]
      },
      {
        "id": "sma-20-60",
        "label": "均线趋势20/60",
        "family": "SMA",
        "parameters": {
          "short_window": 20,
          "long_window": 60
        },
        "required_bars": 60,
        "entry_rule": "20周期均价高于60周期均价",
        "exit_rule": "20周期均价回落至60周期均价及以下",
        "status": "COMPUTED",
        "latest_state": 1,
        "previous_state": 1,
        "replay": [
          {
            "fee_bps": 10,
            "price_return": 0.1352272727272723,
            "max_drawdown": 0.0,
            "turnover": 1.0
          },
          {
            "fee_bps": 30,
            "price_return": 0.13295454545454488,
            "max_drawdown": 0.0,
            "turnover": 1.0
          }
        ]
      },
      {
        "id": "momentum-20",
        "label": "20周期动量",
        "family": "MOMENTUM",
        "parameters": {
          "lookback": 20,
          "threshold_pct": "0.0"
        },
        "required_bars": 21,
        "entry_rule": "收盘价高于20根K线前收盘",
        "exit_rule": "20周期价格变化不再为正",
        "status": "COMPUTED",
        "latest_state": 1,
        "previous_state": 1,
        "replay": [
          {
            "fee_bps": 10,
            "price_return": 0.1352272727272723,
            "max_drawdown": 0.0,
            "turnover": 1.0
          },
          {
            "fee_bps": 30,
            "price_return": 0.13295454545454488,
            "max_drawdown": 0.0,
            "turnover": 1.0
          }
        ]
      },
      {
        "id": "donchian-20-10",
        "label": "通道突破",
        "family": "DONCHIAN",
        "parameters": {
          "entry_window": 20,
          "exit_window": 10
        },
        "required_bars": 21,
        "entry_rule": "收盘突破前20根K线最高价，不含当前K线",
        "exit_rule": "收盘跌破前10根K线最低价，不含当前K线",
        "status": "COMPUTED",
        "latest_state": 0,
        "previous_state": 0,
        "replay": [
          {
            "fee_bps": 10,
            "price_return": 0.0,
            "max_drawdown": 0.0,
            "turnover": 0.0
          },
          {
            "fee_bps": 30,
            "price_return": 0.0,
            "max_drawdown": 0.0,
            "turnover": 0.0
          }
        ]
      },
      {
        "id": "bollinger-20-2",
        "label": "布林带回归",
        "family": "BOLLINGER",
        "parameters": {
          "window": 20,
          "k_pct": "200.0"
        },
        "required_bars": 20,
        "entry_rule": "收盘触及20周期均价减2倍总体标准差",
        "exit_rule": "收盘回到20周期均价及以上",
        "status": "COMPUTED",
        "latest_state": 0,
        "previous_state": 0,
        "replay": [
          {
            "fee_bps": 10,
            "price_return": 0.0,
            "max_drawdown": 0.0,
            "turnover": 0.0
          },
          {
            "fee_bps": 30,
            "price_return": 0.0,
            "max_drawdown": 0.0,
            "turnover": 0.0
          }
        ]
      },
      {
        "id": "macd-12-26-9",
        "label": "MACD趋势",
        "family": "MACD",
        "parameters": {
          "fast": 12,
          "slow": 26,
          "signal": 9
        },
        "required_bars": 35,
        "entry_rule": "DIF高于DEA且DIF为正",
        "exit_rule": "DIF不再高于DEA或DIF不再为正",
        "status": "COMPUTED",
        "latest_state": 1,
        "previous_state": 1,
        "replay": [
          {
            "fee_bps": 10,
            "price_return": 0.1352272727272723,
            "max_drawdown": 0.0,
            "turnover": 1.0
          },
          {
            "fee_bps": 30,
            "price_return": 0.13295454545454488,
            "max_drawdown": 0.0,
            "turnover": 1.0
          }
        ]
      },
      {
        "id": "rsi-14-30-50",
        "label": "RSI回归",
        "family": "RSI",
        "parameters": {
          "window": 14,
          "entry": 30,
          "exit": 50
        },
        "required_bars": 15,
        "entry_rule": "14周期Wilder RSI不高于30",
        "exit_rule": "RSI回升至50及以上",
        "status": "COMPUTED",
        "latest_state": 0,
        "previous_state": 0,
        "replay": [
          {
            "fee_bps": 10,
            "price_return": 0.0,
            "max_drawdown": 0.0,
            "turnover": 0.0
          },
          {
            "fee_bps": 30,
            "price_return": 0.0,
            "max_drawdown": 0.0,
            "turnover": 0.0
          }
        ]
      },
      {
        "id": "volume-breakout-20",
        "label": "量价突破",
        "family": "VOLUME",
        "parameters": {
          "window": 20,
          "volume_multiple": "1.5"
        },
        "required_bars": 21,
        "entry_rule": "突破前20根K线最高价且量能高于前20根均量1.5倍",
        "exit_rule": "收盘跌破前10根K线最低价",
        "status": "COMPUTED",
        "latest_state": 0,
        "previous_state": 0,
        "replay": [
          {
            "fee_bps": 10,
            "price_return": 0.0,
            "max_drawdown": 0.0,
            "turnover": 0.0
          },
          {
            "fee_bps": 30,
            "price_return": 0.0,
            "max_drawdown": 0.0,
            "turnover": 0.0
          }
        ]
      }
    ],
    "baseline": [
      {
        "fee_bps": 10,
        "price_return": 0.1352272727272723,
        "max_drawdown": 0.0
      },
      {
        "fee_bps": 30,
        "price_return": 0.13295454545454488,
        "max_drawdown": 0.0
      }
    ],
    "trade_plan": {
      "version": "conditional-entry-exit-v2",
      "action": "WAIT",
      "basis": "固定规则的研究计划，不是个性化适当性评估或最佳买卖时点预测。",
      "reasons": [
        "趋势、MACD、追高或量能确认至少一项未满足，暂不购入；等待完整K线重新确认。"
      ],
      "indicators": {
        "reference_close": "200.0000",
        "sma20": "190.5000",
        "sma60": "170.5000",
        "atr14_sma": "3.0000",
        "rsi14_wilder": "100.0000",
        "macd_dif": "6.9939",
        "macd_dea": "6.9910",
        "volume_ratio_prior20": "1.0000"
      },
      "levels": {
        "reference": "200.0000",
        "stop_reference": "194.0000",
        "take_profit_reference": "209.0000",
        "rule": "参考价减2倍ATR与加3倍ATR；ATR为14周期TR简单均值；不是预测目标或保证成交价。"
      },
      "exit_rules": [
        "已持仓时，触及估算止损参考线应优先复核退出；跳空、停牌或跌停可能无法按该价卖出。",
        "到达估算止盈参考线时评估兑现；这不是模型预测会到达的价格。",
        "完整K线收盘跌破20周期均线时重新评估持有逻辑。",
        "自实际购入后最多观察5根所选周期完整K线，然后强制复评；不是承诺届时盈利或机械卖出日期。"
      ],
      "max_observation_bars": 5,
      "held_bars": 0,
      "remaining_observation_bars": 5,
      "position_basis": "HYPOTHETICAL_REFERENCE",
      "timeframe": "1d",
      "execution_constraint": "普通A股买入当日不能卖出；ETF回转规则未核验时按T+1保守处理。触发退出不保证可成交。",
      "trading_rules_source": "https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/c_20260424_10816482.shtml",
      "entry_action": "WAIT",
      "entry_reasons": [
        "趋势、MACD、追高或量能确认至少一项未满足，暂不购入；等待完整K线重新确认。"
      ],
      "position_action": "NONE",
      "levels_scope": "THIS_REPORT_ONLY",
      "position_note": "本次报告独立估算；未重建真实买入时ATR，不代表持续跟踪的止损线。",
      "as_of_bar": "2026-09-08"
    },
    "timeframe": "1d",
    "family_count": 7,
    "synthesis": {
      "schema_version": "chart-synthesis-v1",
      "profile": "day-v1",
      "timeframe": "1d",
      "as_of_bar": "2026-09-08T07:00:00+00:00",
      "risk_flags": [],
      "risk_level": "NORMAL",
      "market_state": "TREND",
      "indicators": {
        "atr14": "3.00000000",
        "atr_ratio": "0.01500000",
        "adx14_wilder": "100.00000000",
        "sma20_slope_3_atr": "1.00000000",
        "bollinger_width_ratio": "0.11532563",
        "volume_ratio_prior20": "1.00000000",
        "reference_close": "200.00000000"
      },
      "evidence": [
        {
          "id": "sma-5-20",
          "label": "均线趋势5/20",
          "family": "SMA",
          "status": "COMPUTED",
          "direction": "BULLISH",
          "research_state": 1,
          "transition": "CONTINUING",
          "reason": "均线价差按ATR归一化，阈值0.15ATR。"
        },
        {
          "id": "sma-20-60",
          "label": "均线趋势20/60",
          "family": "SMA",
          "status": "COMPUTED",
          "direction": "BULLISH",
          "research_state": 1,
          "transition": "CONTINUING",
          "reason": "均线价差按ATR归一化，阈值0.15ATR。"
        },
        {
          "id": "momentum-20",
          "label": "20周期动量",
          "family": "MOMENTUM",
          "status": "COMPUTED",
          "direction": "BULLISH",
          "research_state": 1,
          "transition": "CONTINUING",
          "reason": "20根价格变化超过0.5ATR才作为方向证据。"
        },
        {
          "id": "donchian-20-10",
          "label": "通道突破",
          "family": "DONCHIAN",
          "status": "COMPUTED",
          "direction": "RANGE",
          "research_state": 0,
          "transition": "WAITING",
          "reason": "排除当前K线的通道边界，突破幅度0.1ATR；量价证据另需1.5倍均量。"
        },
        {
          "id": "bollinger-20-2",
          "label": "布林带回归",
          "family": "BOLLINGER",
          "status": "COMPUTED",
          "direction": "RANGE",
          "research_state": 0,
          "transition": "WAITING",
          "reason": "前根越过布林带、当前回到带内并由价格方向确认。"
        },
        {
          "id": "macd-12-26-9",
          "label": "MACD趋势",
          "family": "MACD",
          "status": "COMPUTED",
          "direction": "RANGE",
          "research_state": 1,
          "transition": "CONTINUING",
          "reason": "DIF方向与超过0.03ATR的柱差方向一致。"
        },
        {
          "id": "rsi-14-30-50",
          "label": "RSI回归",
          "family": "RSI",
          "status": "COMPUTED",
          "direction": "RANGE",
          "research_state": 0,
          "transition": "WAITING",
          "reason": "RSI越过超跌/超买后的变化需价格确认；仅超卖不判断上涨。"
        },
        {
          "id": "volume-breakout-20",
          "label": "量价突破",
          "family": "VOLUME",
          "status": "COMPUTED",
          "direction": "RANGE",
          "research_state": 0,
          "transition": "WAITING",
          "reason": "排除当前K线的通道边界，突破幅度0.1ATR；量价证据另需1.5倍均量。"
        }
      ],
      "forecast_validated": false,
      "basis": "固定研究规则综合倾向；证据一致性和覆盖率不是上涨概率。",
      "direction": "RANGE",
      "groups": [
        {
          "id": "TREND",
          "label": "趋势与动量",
          "direction": "BULLISH",
          "members": [
            "sma-5-20",
            "sma-20-60",
            "momentum-20",
            "macd-12-26-9"
          ],
          "supporting": [
            "sma-5-20",
            "sma-20-60",
            "momentum-20"
          ],
          "opposing": [],
          "internal_conflict": false
        },
        {
          "id": "BREAKOUT",
          "label": "突破与量能",
          "direction": "RANGE",
          "members": [
            "donchian-20-10",
            "volume-breakout-20"
          ],
          "supporting": [],
          "opposing": [],
          "internal_conflict": false
        },
        {
          "id": "REVERSION",
          "label": "回归与超跌",
          "direction": "RANGE",
          "members": [
            "bollinger-20-2",
            "rsi-14-30-50"
          ],
          "supporting": [],
          "opposing": [],
          "internal_conflict": false
        }
      ],
      "supporting_groups": [
        "TREND"
      ],
      "opposing_groups": [],
      "conflict": false,
      "coverage": {
        "computed": 8,
        "total": 8
      },
      "agreement": "MIXED"
    },
    "evaluation_first": "2026-08-15",
    "evaluation_last": "2026-09-08",
    "evaluation_bars": 25,
    "replay_convention": "完整K线收盘信号在下一根开盘执行；空仓起步，10/30bp总摩擦。小时回放未模拟T+1，非可交易收益。",
    "schema_version": "core-chart-strategies-v3",
    "status": "AVAILABLE",
    "reason": null,
    "source": {
      "timeframe": "1d",
      "symbol": "sh600519",
      "url": "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh600519",
      "retrieved_at": "2026-09-08T08:00:00+00:00",
      "first": "2026-06-01",
      "last": "2026-09-08",
      "cutoff": "2026-09-08",
      "bars": 100,
      "raw_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "bars_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    },
    "limitations": [
      "SOFTWARE FIXTURE ONLY. 非真实行情或预测效果证据。"
    ],
    "forecast_validated": false,
    "screenshot_identity_verified": false,
    "cross_timeframe": {
      "status": "SINGLE_PERIOD",
      "items": []
    }
  },
  "image_alignment": {
    "status": "CONSISTENT",
    "note": "软件夹具"
  },
  "strategy_version": "chart-synthesis-report-v4",
  "error": null,
  "model_call": null,
  "live_authority": "NONE",
  "forecast_validated": false,
  "conclusion": {
    "schema_version": "chart-conclusion-v1",
    "status": "READY",
    "direction": "RANGE",
    "action": "WAIT",
    "headline": "日线后续5根日线K线：震荡/分歧；等待确认，暂不购入。",
    "alignment_note": "模型辨认的代码、日期、周期及截止时间一致；仍非独立数据认证。",
    "supporting_evidence": [
      "尚无足够独立证据支持单一方向。"
    ],
    "counter_evidence": [
      "趋势与动量：偏强"
    ],
    "coverage": {
      "computed": 8,
      "total": 8
    },
    "agreement": "MIXED",
    "risk_level": "NORMAL",
    "risk_flags": [],
    "market_state": "TREND",
    "groups": [
      {
        "id": "TREND",
        "label": "趋势与动量",
        "direction": "BULLISH",
        "members": [
          "sma-5-20",
          "sma-20-60",
          "momentum-20",
          "macd-12-26-9"
        ],
        "supporting": [
          "sma-5-20",
          "sma-20-60",
          "momentum-20"
        ],
        "opposing": [],
        "internal_conflict": false
      },
      {
        "id": "BREAKOUT",
        "label": "突破与量能",
        "direction": "RANGE",
        "members": [
          "donchian-20-10",
          "volume-breakout-20"
        ],
        "supporting": [],
        "opposing": [],
        "internal_conflict": false
      },
      {
        "id": "REVERSION",
        "label": "回归与超跌",
        "direction": "RANGE",
        "members": [
          "bollinger-20-2",
          "rsi-14-30-50"
        ],
        "supporting": [],
        "opposing": [],
        "internal_conflict": false
      }
    ],
    "cross_timeframe_status": "SINGLE_PERIOD",
    "timeframes": [
      {
        "timeframe": "1d",
        "status": "AVAILABLE",
        "direction": "RANGE",
        "last_bar": "2026-09-08",
        "raw_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "reason": null
      }
    ],
    "entry_action": "WAIT",
    "position_action": "NONE",
    "levels": {
      "reference": "200.0000",
      "stop_reference": "194.0000",
      "take_profit_reference": "209.0000",
      "rule": "参考价减2倍ATR与加3倍ATR；ATR为14周期TR简单均值；不是预测目标或保证成交价。"
    },
    "exit_conditions": [
      "已持仓时，触及估算止损参考线应优先复核退出；跳空、停牌或跌停可能无法按该价卖出。",
      "到达估算止盈参考线时评估兑现；这不是模型预测会到达的价格。",
      "完整K线收盘跌破20周期均线时重新评估持有逻辑。",
      "自实际购入后最多观察5根所选周期完整K线，然后强制复评；不是承诺届时盈利或机械卖出日期。"
    ],
    "reasons": [
      "趋势、MACD、追高或量能确认至少一项未满足，暂不购入；等待完整K线重新确认。"
    ],
    "scenarios": [
      {
        "direction": "BULLISH",
        "trigger": "至少两组偏强且无分歧，较长周期与风险确认。",
        "interpretation": "下一根完整K线仍满足时，才复评偏强情景与入场许可。",
        "invalidation": "转弱、分歧或风险触发时撤销；止盈线不是预测目标。"
      },
      {
        "direction": "RANGE",
        "trigger": "有效证据不足以支持单一方向，或不同周期尚未同向。",
        "interpretation": "等待完整K线确认；持仓按退出与时间条件复评。",
        "invalidation": "两组同向、风险与较长周期确认后重新判断。"
      },
      {
        "direction": "BEARISH",
        "trigger": "主周期至少两组证据偏弱且无反向分歧。",
        "interpretation": "暂停入场，持仓复核退出；不表示必跌或可即时成交。",
        "invalidation": "偏弱证据消失、出现反向确认或数据不足时撤销该情景。"
      }
    ],
    "model_commentary_status": "ARCHIVED_ONLY",
    "forecast_validated": false,
    "validation_note": "固定规则倾向，预测效果未验证；一致性与覆盖率不是概率。"
  }
} as const
