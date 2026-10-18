"""Host CLI for the paper-only CN small-capital pilot.

Every command is local and paper-only: no network fetch, no credentials and no
broker transport. ``real_transport`` remains LOCKED for all pilot operations.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from quantforge.risk import RiskLimits

from .broker import (
    broker_reconcile,
    broker_status,
    import_broker_fills,
    open_broker_account,
)
from .data import (
    corpus_snapshot,
    extend_bars_csv,
    import_bars_csv,
    list_snapshots,
)
from .fees import CN_FEE_PROFILES, CnFeeProfile, CnTradability
from .service import PilotDeclaration, PilotService


def _fee_profile(args: argparse.Namespace) -> CnFeeProfile:
    if args.commission_bps is None:
        if args.fee_profile not in CN_FEE_PROFILES:
            raise ValueError("unknown fee profile")
        return CN_FEE_PROFILES[args.fee_profile]
    return CnFeeProfile(
        name="operator-override",
        commission_bps=args.commission_bps,
        min_commission=args.min_commission,
        stamp_duty_bps=args.stamp_duty_bps,
        transfer_fee_bps=args.transfer_fee_bps,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Paper-only CN small-capital pilot (real transport locked)"
    )
    parser.add_argument("--root", required=True, type=Path)
    sub = parser.add_subparsers(dest="command", required=True)

    import_csv = sub.add_parser("import-csv")
    import_csv.add_argument("--csv", required=True, type=Path)
    import_csv.add_argument("--symbol", required=True)
    import_csv.add_argument("--as-of", required=True)
    import_csv.add_argument("--declared-adjustment", required=True)

    extend_csv = sub.add_parser("extend-csv")
    extend_csv.add_argument("--snapshot", required=True)
    extend_csv.add_argument("--csv", required=True, type=Path)
    extend_csv.add_argument("--symbol", required=True)
    extend_csv.add_argument("--as-of", required=True)
    extend_csv.add_argument("--declared-adjustment", required=True)

    import_corpus = sub.add_parser("import-corpus")
    import_corpus.add_argument("--provider-root", required=True, type=Path)
    import_corpus.add_argument("--universe", required=True, type=Path)
    import_corpus.add_argument("--symbol", required=True)
    import_corpus.add_argument("--as-of", required=True)

    declare = sub.add_parser("declare")
    declare.add_argument("--program", required=True)
    declare.add_argument("--purpose", required=True)
    declare.add_argument("--snapshot", required=True)
    declare.add_argument("--symbol", required=True)
    declare.add_argument("--short", type=int, required=True)
    declare.add_argument("--long", type=int, required=True)
    declare.add_argument("--capital", required=True)
    declare.add_argument(
        "--strategy",
        choices=("sma", "canary_hold"),
        default="sma",
        help="sma = the fixed crossover demo; canary_hold = buy one lot at the "
        "first affordable session and hold (requires --sizing one_lot)",
    )
    declare.add_argument(
        "--sizing", choices=("all_in", "fraction", "one_lot"), default="all_in"
    )
    declare.add_argument("--fraction", default="1")
    declare.add_argument("--max-lots", type=int, default=None)
    declare.add_argument(
        "--fee-profile", choices=tuple(CN_FEE_PROFILES), default="stock-default"
    )
    declare.add_argument("--commission-bps", default=None)
    declare.add_argument("--min-commission", default=None)
    declare.add_argument("--stamp-duty-bps", default=None)
    declare.add_argument("--transfer-fee-bps", default=None)
    declare.add_argument("--lot-size", type=int, default=100)
    declare.add_argument("--price-limit-pct", default="0.10")
    declare.add_argument("--no-t-plus-one", action="store_true")
    declare.add_argument(
        "--risk-order",
        default=None,
        help="fat-finger order-notional sanity bound; defaults to 10x capital "
        "(the frozen risk cap also applies to sells of appreciated positions)",
    )
    declare.add_argument(
        "--risk-position",
        default=None,
        help="position sanity bound; defaults to 10x capital because exposure "
        "is structurally cash-funded and the account can grow past its "
        "declared starting capital",
    )
    declare.add_argument(
        "--risk-gross",
        default=None,
        help="gross-exposure sanity bound; same default rationale as --risk-position",
    )

    broker_open = sub.add_parser("broker-open")
    broker_open.add_argument("--cash", required=True)
    broker_open.add_argument("--declared-source", required=True)

    broker_import = sub.add_parser("broker-import")
    broker_import.add_argument("--csv", required=True, type=Path)
    broker_import.add_argument("--declared-source", required=True)
    broker_import.add_argument("--as-of", required=True)

    sub.add_parser("broker-status")
    sub.add_parser("broker-reconcile")

    sub.add_parser("snapshots")
    sub.add_parser("start")
    sub.add_parser("status")
    sub.add_parser("stop")
    sub.add_parser("history")
    sub.add_parser("tickets")
    sub.add_parser("reconcile")
    run = sub.add_parser("run")
    run.add_argument("--steps", type=int, default=0)
    run.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    value: Any
    try:
        if args.command == "import-csv":
            args.root.mkdir(parents=True, exist_ok=True)
            value = import_bars_csv(
                args.root,
                args.csv,
                args.symbol,
                as_of=datetime.fromisoformat(args.as_of),
                declared_adjustment=args.declared_adjustment,
            )
        elif args.command == "extend-csv":
            value = extend_bars_csv(
                args.root,
                args.snapshot,
                args.csv,
                args.symbol,
                as_of=datetime.fromisoformat(args.as_of),
                declared_adjustment=args.declared_adjustment,
            )
        elif args.command == "import-corpus":
            universe = json.loads(args.universe.read_text(encoding="utf-8"))
            args.root.mkdir(parents=True, exist_ok=True)
            value = corpus_snapshot(
                args.root,
                args.provider_root,
                universe,
                args.symbol,
                as_of=datetime.fromisoformat(args.as_of),
            )
        elif args.command == "broker-open":
            value = open_broker_account(
                args.root, cash=args.cash, declared_source=args.declared_source
            )
        elif args.command == "broker-import":
            value = import_broker_fills(
                args.root,
                args.csv,
                declared_source=args.declared_source,
                as_of=datetime.fromisoformat(args.as_of),
            )
        elif args.command == "broker-status":
            value = broker_status(args.root)
        elif args.command == "broker-reconcile":
            value = broker_reconcile(args.root)
        else:
            service = PilotService(args.root)
            try:
                if args.command == "declare":
                    limits = RiskLimits(
                        max_order_notional=args.risk_order
                        or str(Decimal(args.capital) * 10),
                        max_position_notional=args.risk_position
                        or str(Decimal(args.capital) * 10),
                        max_gross_exposure=args.risk_gross
                        or str(Decimal(args.capital) * 10),
                    )
                    declaration = PilotDeclaration(
                        program_id=args.program,
                        purpose=args.purpose,
                        snapshot_id=args.snapshot,
                        symbol=args.symbol,
                        short_window=args.short,
                        long_window=args.long,
                        initial_cash=args.capital,
                        strategy=args.strategy,
                        sizing=args.sizing,
                        allocation_fraction=args.fraction,
                        max_lots_per_order=args.max_lots,
                        fee_profile=_fee_profile(args),
                        tradability=CnTradability(
                            lot_size=args.lot_size,
                            price_limit_pct=args.price_limit_pct,
                            t_plus_one=not args.no_t_plus_one,
                        ),
                        risk_limits=limits,
                    )
                    value = service.declare(declaration)
                elif args.command == "start":
                    value = service.start()
                elif args.command == "status":
                    value = service.status()
                elif args.command == "stop":
                    value = service.stop()
                elif args.command == "history":
                    value = service.history()
                elif args.command == "tickets":
                    value = service.tickets()
                elif args.command == "reconcile":
                    value = service.reconcile()
                elif args.command == "run":
                    if args.steps < 0 or args.steps > 10000:
                        raise ValueError("BOUNDED_PILOT_STEPS_REQUIRED")
                    value = service.run(steps=args.steps, watch=args.watch)
                else:  # snapshots
                    value = list_snapshots(args.root)
            finally:
                service.close()
        print(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False))
    except (ValueError, ArithmeticError, OSError, KeyError, TypeError):
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "reason": "Pilot operation denied; inspect the declaration, retained snapshots, journal and declared rule profiles. No implicit retry, no network and no real order exists.",
                }
            )
        )
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
