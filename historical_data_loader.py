"""
Historical Data Loader - Descarga masiva de datos de Binance.

Optimized for top 20 validation first, then full 100.
Storage monitoring with 50GB limit and cleanup recommendations.
"""

import asyncio
import aiohttp
from typing import List, Dict, Optional
import pandas as pd
from datetime import datetime, timedelta
import os
import logging

logger = logging.getLogger(__name__)

BINANCE_API = "https://api.binance.com"
KLINES_LIMIT = 1000


class HistoricalDataLoader:
    """Manages bulk download of historical klines from Binance with storage monitoring."""

    def __init__(self, data_dir: str = './historical_data', max_storage_gb: float = 50.0):
        self.data_dir = data_dir
        self.max_storage_bytes = max_storage_gb * 1024**3
        os.makedirs(data_dir, exist_ok=True)

        # Track storage usage
        self.current_usage = self._calculate_storage_usage()
        self.max_storage_gb = max_storage_gb

    def _calculate_storage_usage(self) -> int:
        """Calculate current storage usage in bytes."""
        total = 0
        if os.path.exists(self.data_dir):
            for dirpath, dirnames, filenames in os.walk(self.data_dir):
                for f in filenames:
                    fp = os.path.join(dirpath, f)
                    if os.path.exists(fp):
                        total += os.path.getsize(fp)
        return total

    def check_storage_limit(self, estimated_addition_bytes: int = 0) -> bool:
        """Check if adding data would exceed limit. Alert at 90%."""
        projected = self.current_usage + estimated_addition_bytes
        usage_pct = (projected / self.max_storage_bytes) * 100

        if usage_pct > 90:
            logger.warning(
                f"⚠️  Storage alert: {usage_pct:.1f}% ({projected / (1024**3):.1f} GB / "
                f"{self.max_storage_gb:.1f} GB)"
            )
            return False

        if usage_pct > 75:
            logger.info(
                f"💡 Storage at {usage_pct:.1f}% - consider cleanup after learning complete"
            )

        return True

    def get_storage_stats(self) -> Dict:
        """Get detailed storage statistics."""
        stats = {
            'total_bytes': self.current_usage,
            'total_gb': self.current_usage / (1024**3),
            'max_gb': self.max_storage_gb,
            'usage_pct': (self.current_usage / self.max_storage_bytes) * 100,
            'available_gb': (self.max_storage_bytes - self.current_usage) / (1024**3),
            'by_extension': {}
        }

        # Breakdown by file type
        if os.path.exists(self.data_dir):
            for dirpath, dirnames, filenames in os.walk(self.data_dir):
                for f in filenames:
                    ext = os.path.splitext(f)[1].lower()
                    fp = os.path.join(dirpath, f)
                    if os.path.exists(fp):
                        size = os.path.getsize(fp)
                        stats['by_extension'][ext] = stats['by_extension'].get(ext, 0) + size

        return stats

    async def download_symbol_klines(
        self,
        session: aiohttp.ClientSession,
        symbol: str,
        interval: str = '1h',
        days: int = 1095,
    ) -> Optional[pd.DataFrame]:
        """Download klines for a single symbol with pagination."""
        end_time = datetime.utcnow()
        start_time = end_time - timedelta(days=days)

        all_klines = []
        current_start = int(start_time.timestamp() * 1000)
        final_end = int(end_time.timestamp() * 1000)

        retry_count = 0
        max_retries = 3

        while current_start < final_end:
            url = f"{BINANCE_API}/api/v3/klines"
            params = {
                'symbol': symbol,
                'interval': interval,
                'startTime': current_start,
                'limit': KLINES_LIMIT
            }

            try:
                async with session.get(url, params=params, timeout=30) as resp:
                    if resp.status == 429:
                        retry_count += 1
                        if retry_count > max_retries:
                            logger.error(f"Rate limit exceeded for {symbol}")
                            break
                        wait_time = 2 ** retry_count  # Exponential backoff
                        logger.warning(f"Rate limit for {symbol}, waiting {wait_time}s...")
                        await asyncio.sleep(wait_time)
                        continue

                    if resp.status != 200:
                        logger.error(f"HTTP {resp.status} for {symbol}")
                        break

                    data = await resp.json()

                    if not data or not isinstance(data, list):
                        break

                    all_klines.extend(data)
                    current_start = data[-1][0] + 1  # Next batch
                    retry_count = 0  # Reset on success

            except asyncio.TimeoutError:
                logger.error(f"Timeout downloading {symbol}")
                break
            except Exception as e:
                logger.error(f"Error downloading {symbol}: {e}")
                break

        if not all_klines:
            return None

        # Convert to DataFrame
        df = pd.DataFrame(all_klines, columns=[
            'open_time', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_volume', 'trades', 'taker_buy_base',
            'taker_buy_quote', 'ignore'
        ])

        # Convert types
        numeric_cols = ['open', 'high', 'low', 'close', 'volume', 'quote_volume']
        for col in numeric_cols:
            df[col] = pd.to_numeric(df[col], errors='coerce')

        df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
        df['close_time'] = pd.to_datetime(df['close_time'], unit='ms')

        return df

    async def download_bulk(
        self,
        symbols: List[str],
        interval: str = '1h',
        days: int = 1095,
        max_concurrent: int = 5,
    ) -> Dict[str, pd.DataFrame]:
        """Download data for multiple symbols with concurrency control."""
        semaphore = asyncio.Semaphore(max_concurrent)
        successful = {}
        failed = []

        async def bounded_download(symbol):
            async with semaphore:
                try:
                    async with aiohttp.ClientSession() as session:
                        df = await self.download_symbol_klines(session, symbol, interval, days)

                        if df is not None and not df.empty:
                            # Save to parquet
                            filepath = f"{self.data_dir}/{symbol}_{interval}.parquet"
                            df.to_parquet(filepath, compression='snappy')

                            # Update storage tracking
                            file_size = os.path.getsize(filepath)
                            self.current_usage += file_size

                            logger.info(
                                f"✅ {symbol}: {len(df)} candles, "
                                f"{df['open_time'].min().date()} to {df['open_time'].max().date()}, "
                                f"{file_size / (1024**2):.1f} MB"
                            )
                            return symbol, df, None
                        else:
                            return symbol, None, "No data returned"

                except Exception as e:
                    return symbol, None, str(e)

        # Progress tracking
        print(f"\nDownloading {len(symbols)} symbols with {max_concurrent} concurrent connections...")

        tasks = [bounded_download(s) for s in symbols]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                failed.append(("unknown", str(result)))
                continue

            symbol, df, error = result
            if error:
                failed.append((symbol, error))
                logger.error(f"❌ {symbol}: {error}")
            elif df is not None:
                successful[symbol] = df

        # Summary
        print(f"\n[Download Summary]")
        print(f"  ✅ Successful: {len(successful)}/{len(symbols)}")
        print(f"  ❌ Failed: {len(failed)}/{len(symbols)}")
        print(f"  💾 Storage: {self.current_usage / (1024**3):.2f} GB / {self.max_storage_gb:.1f} GB")

        if failed:
            print(f"\n  Failed symbols:")
            for symbol, error in failed[:5]:
                print(f"    - {symbol}: {error[:100]}")
            if len(failed) > 5:
                print(f"    ... and {len(failed) - 5} more")

        return successful

    def cleanup_raw_klines(self, keep_synthetic: bool = True) -> Dict:
        """
        Clean up raw kline data after learning is complete.

        Args:
            keep_synthetic: If True, preserve synthetic trade files

        Returns:
            Stats about cleaned data
        """
        stats = {'deleted': 0, 'preserved': 0, 'freed_bytes': 0}

        if not os.path.exists(self.data_dir):
            return stats

        for filename in os.listdir(self.data_dir):
            filepath = os.path.join(self.data_dir, filename)

            if not os.path.isfile(filepath):
                continue

            # Always preserve synthetic trades and manifests
            if 'synthetic' in filename or 'manifest' in filename:
                stats['preserved'] += 1
                continue

            # Preserve indicator cache if exists
            if 'indicators' in filename or 'features' in filename:
                stats['preserved'] += 1
                continue

            # Delete raw klines (can be re-downloaded if needed)
            if filename.endswith('.parquet') and '_1h' in filename:
                file_size = os.path.getsize(filepath)
                os.remove(filepath)
                stats['deleted'] += 1
                stats['freed_bytes'] += file_size
                self.current_usage -= file_size

        logger.info(
            f"Cleanup complete: {stats['deleted']} files deleted, "
            f"{stats['freed_bytes'] / (1024**3):.2f} GB freed"
        )
        return stats


if __name__ == '__main__':
    # Quick test
    loader = HistoricalDataLoader()
    stats = loader.get_storage_stats()
    print(f"Storage stats: {stats}")
