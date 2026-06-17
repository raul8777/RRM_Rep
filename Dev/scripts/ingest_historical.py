#!/usr/bin/env python3
"""
Historical Data Ingestion Pipeline

Descarga datos históricos de Binance y simula trades para el Learning Engine.

Uso:
    python ingest_historical.py --phase top20 --days 1095 --simulate --learn
    python ingest_historical.py --phase full --days 1095 --simulate

Fases:
    top20: Descarga top 20 símbolos para validación (rápido, ~3-4GB)
    full: Descarga top 100 símbolos completos (~40-50GB)
"""

import argparse
import asyncio
import sys
from pathlib import Path
from datetime import datetime, timedelta
import json
import os

# Add parent to path (crypto-agent root)
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from historical_data_loader import HistoricalDataLoader
from pair_screener import get_screened_pairs
from config import get_settings


async def main():
    parser = argparse.ArgumentParser(
        description='Ingest historical data from Binance for ML training',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Phase 1: Top 20 for validation
  python ingest_historical.py --phase top20 --days 365 --simulate

  # Phase 2: Full 100 after validation
  python ingest_historical.py --phase full --days 1095 --simulate --learn

  # Check storage without downloading
  python ingest_historical.py --check-storage
        """
    )
    parser.add_argument('--phase', choices=['top20', 'full', 'check'], default='top20',
                        help='Download phase: top20 (validation), full (complete), check (storage only)')
    parser.add_argument('--days', type=int, default=1095,
                        help='Days of history to download (max ~1500 days for 1h klines)')
    parser.add_argument('--concurrent', type=int, default=5,
                        help='Max concurrent downloads (default: 5)')
    parser.add_argument('--simulate', action='store_true',
                        help='Run historical simulation after download')
    parser.add_argument('--learn', action='store_true',
                        help='Feed simulated trades to LearningEngine')
    parser.add_argument('--check-storage', action='store_true',
                        help='Check current storage usage without downloading')
    parser.add_argument('--data-dir', type=str, default='./historical_data',
                        help='Directory to store historical data (default: ./historical_data)')

    args = parser.parse_args()

    print(f"[{datetime.now()}] Historical Data Ingestion Pipeline")
    print(f"  Phase: {args.phase}")
    print(f"  Days: {args.days}")
    print(f"  Data directory: {args.data_dir}")

    # Initialize loader with 50GB limit
    loader = HistoricalDataLoader(data_dir=args.data_dir, max_storage_gb=50.0)

    # Check storage mode
    if args.check_storage or args.phase == 'check':
        usage_gb = loader.current_usage / (1024**3)
        print(f"\n[Storage Check]")
        print(f"  Current usage: {usage_gb:.2f} GB / 50.00 GB ({(usage_gb/50)*100:.1f}%)")
        print(f"  Available: {50 - usage_gb:.2f} GB")

        if usage_gb > 45:
            print("  ⚠️  WARNING: Approaching 90% storage limit!")
            print("  💡 Tip: Run cleanup after learning is complete to free ~90% of space")
        return

    # Get symbols from screener
    print(f"\n[1/4] Getting symbols from screener...")
    settings = get_settings()
    screener = get_screened_pairs(max_spot=120)  # Get extra for filtering

    if args.phase == 'top20':
        symbols = screener['spot'][:20]
        print(f"  Selected: Top 20 symbols for validation")
    else:  # full
        symbols = screener['spot'][:100]
        print(f"  Selected: Top 100 symbols (full universe)")

    print(f"  Symbols: {', '.join(symbols[:10])}{'...' if len(symbols) > 10 else ''}")

    # Check storage before starting
    estimated_bytes_per_symbol = 500 * 1024 * 1024  # ~500MB per symbol (3 years 1h data)
    estimated_total = len(symbols) * estimated_bytes_per_symbol

    print(f"\n[Storage] Estimated requirement: {estimated_total / (1024**3):.1f} GB")

    if not loader.check_storage_limit(estimated_total):
        print("⚠️  WARNING: Storage limit would be exceeded!")
        print("  Options:")
        print("    1. Reduce --days (e.g., 365 instead of 1095)")
        print("    2. Use --phase top20 first")
        print("    3. Clean up existing data")
        response = input("\nContinue anyway? (y/n): ")
        if response.lower() != 'y':
            return

    # Download phase
    print(f"\n[2/4] Starting bulk download of {len(symbols)} symbols...")
    print(f"  Concurrent: {args.concurrent}")
    print(f"  This may take 10-30 minutes depending on network speed...")

    results = await loader.download_bulk(
        symbols=symbols,
        interval='1h',
        days=args.days,
        max_concurrent=args.concurrent
    )

    print(f"\n[Download Summary]")
    print(f"  Successful: {len(results)}/{len(symbols)}")
    print(f"  Failed: {len(symbols) - len(results)}")
    print(f"  Storage used: {loader.current_usage / (1024**3):.2f} GB / 50.00 GB")

    # Save manifest
    manifest = {
        'phase': args.phase,
        'symbols_requested': symbols,
        'symbols_successful': list(results.keys()),
        'total_requested': len(symbols),
        'successful': len(results),
        'failed': len(symbols) - len(results),
        'days': args.days,
        'storage_gb': loader.current_usage / (1024**3),
        'date': datetime.now().isoformat(),
    }

    manifest_path = f"{loader.data_dir}/manifest_{args.phase}_{datetime.now():%Y%m%d_%H%M%S}.json"
    with open(manifest_path, 'w') as f:
        json.dump(manifest, f, indent=2)
    print(f"  Manifest saved: {manifest_path}")

    # Simulation phase (optional)
    if args.simulate and results:
        print(f"\n[3/4] Running historical simulation...")

        try:
            from historical_simulator import HistoricalSimulator

            simulator = HistoricalSimulator(data_dir=args.data_dir)
            all_synthetic_trades = []

            # Limit to successful downloads
            sim_symbols = list(results.keys())[:50] if args.phase == 'full' else list(results.keys())

            for i, symbol in enumerate(sim_symbols, 1):
                try:
                    print(f"  [{i}/{len(sim_symbols)}] Simulating {symbol}...", end=' ')

                    trades = simulator.simulate_trades(
                        symbol=symbol,
                        start_date=datetime.now() - timedelta(days=min(args.days, 365)),
                        end_date=datetime.now(),
                        volspike_params={
                            'min_strength': settings.volspike_override_min_strength,
                            'max_positions': settings.volspike_override_max_positions,
                            'daily_limit': settings.volspike_override_daily_limit
                        } if settings.volspike_override_enabled else None
                    )

                    all_synthetic_trades.extend(trades)
                    print(f"{len(trades)} trades")

                except Exception as e:
                    print(f"ERROR: {e}")
                    continue

            print(f"\n[Simulation Summary]")
            print(f"  Total synthetic trades: {len(all_synthetic_trades)}")

            # Save synthetic trades
            trades_path = f"{loader.data_dir}/synthetic_trades_{args.phase}_{datetime.now():%Y%m%d_%H%M%S}.json"
            with open(trades_path, 'w') as f:
                # Convert to serializable format
                serializable_trades = []
                for trade in all_synthetic_trades:
                    trade_dict = vars(trade) if hasattr(trade, '__dict__') else trade
                    # Convert datetime to string
                    for key in ['entry_time', 'exit_time', 'opened_at', 'closed_at']:
                        if key in trade_dict and isinstance(trade_dict[key], datetime):
                            trade_dict[key] = trade_dict[key].isoformat()
                    serializable_trades.append(trade_dict)
                json.dump(serializable_trades, f, indent=2, default=str)
            print(f"  Trades saved: {trades_path}")

            # Learning phase (optional)
            if args.learn and all_synthetic_trades:
                print(f"\n[4/4] Feeding to LearningEngine...")

                try:
                    from init_components import get_components
                    from learning_engine import LearningEngine

                    # Load the saved synthetic trades file
                    engine = LearningEngine(db_client=get_components().db_client)
                    loaded = engine.load_synthetic_trades(trades_path)

                    print(f"  Loaded {len(loaded)} synthetic trades")
                    print(f"  Running learning cycle (synthetic + real combined)...")

                    result = engine.run_with_synthetic(loaded)

                    if result['status'] == 'completed':
                        print(f"  ✅ Learning complete:")
                        print(f"     Trades analyzed: {result['trades_analyzed']}")
                        print(f"       - Synthetic: {result['synthetic_count']}")
                        print(f"       - Real: {result['real_count']}")
                        print(f"     Symbols updated: {len(result['symbols_updated'])}")
                        if result['symbols_updated']:
                            print(f"       - {result['symbols_updated'][:10]}")
                        acc = result.get('accuracy', {})
                        if acc:
                            print(f"     Prediction accuracy: {acc.get('prediction_accuracy', 0):.1%}")
                    else:
                        print(f"  ⚠️  Learning skipped: {result.get('reason', 'unknown')}")

                except Exception as e:
                    print(f"  ❌ Error: {e}")

        except ImportError as e:
            print(f"  Skipping simulation: {e}")
            print("  💡 Ensure historical_simulator.py is available")

    print(f"\n[{datetime.now()}] Pipeline completed!")
    print(f"  Next steps:")
    print(f"    1. Verify data integrity: check {args.data_dir}/")
    print(f"    2. Review manifest files")
    if not args.simulate:
        print(f"    3. Run simulation: python ingest_historical.py --phase {args.phase} --simulate")
    if not args.learn and args.simulate:
        print(f"    4. Feed to learning: python ingest_historical.py --phase {args.phase} --simulate --learn")


if __name__ == '__main__':
    asyncio.run(main())
