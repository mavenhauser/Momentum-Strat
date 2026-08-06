import pandas as pd


def bars_to_df(bars):
    if not bars:
        raise ValueError(
            "No bars returned - almost always an IBKR historical-data timeout "
            "or pacing-limit rejection (see TWS/API logs for a reqHistoricalData "
            "error), not a genuine empty result. Retry, possibly after a short "
            "pause if multiple requests were made in quick succession."
        )
    return pd.DataFrame(
        [
            {
                "date": b.date,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
            }
            for b in bars
        ]
    )
