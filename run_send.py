#!/usr/bin/env python3
"""Money SENT by each newsletter's industry, per quarter, broken down by recipient.

    python run_send.py              # use cached extracts if present
    python run_send.py --refresh    # re-pull from Postgres first

Newsletter comes from the SENDING committee's catcode. Sources are
usa.fec_pac_to_cand_current and usa.fec_pac_to_pac, deduped against each other on SUB_ID.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import pandas as pd  # noqa: E402

from fec_newsletter import config, extract, pipelines  # noqa: E402
from fec_newsletter.reporting import report  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refresh", action="store_true", help="re-pull extracts from Postgres")
    ap.add_argument("--parquet", action="store_true", help="also write Parquet output")
    ap.add_argument("--out", type=Path, default=config.OUT_DIR, help="output directory")
    args = ap.parse_args()

    print("== Extract ==")
    paths = extract.extract_all(refresh=args.refresh, extracts=config.EXTRACTS)
    data = extract.load(paths)
    print(f"  OTH  {len(data['oth']):,} rows   PAS2 {len(data['pas2']):,} rows")

    print("\n== Transform ==")
    df, cov = pipelines.run_send(data)
    agg = pipelines.aggregate(df)

    return report(
        name="send",
        df=df,
        agg=agg,
        coverage=cov.to_frame(),
        expected=config.SEND_EXPECTED,
        out_dir=args.out,
        stem="send_quarterly",
        counterparty_label="recipient",
        parquet=args.parquet,
        show_sources=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
