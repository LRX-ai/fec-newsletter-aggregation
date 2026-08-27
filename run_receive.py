#!/usr/bin/env python3
"""Money RECEIVED by each newsletter's industry, per quarter, broken down by sender.

    python run_receive.py              # use cached extracts if present
    python run_receive.py --refresh    # re-pull from Postgres first

Newsletter comes from the RECEIVING committee's catcode. Source is usa.fec_pac_to_pac only:
PAS2 recipients are candidates, and candidates carry no catcode.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from fec_newsletter import config, congress, extract, pipelines  # noqa: E402
from fec_newsletter.reporting import report  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refresh", action="store_true", help="re-pull extracts from Postgres")
    ap.add_argument("--parquet", action="store_true", help="also write Parquet output")
    ap.add_argument("--out", type=Path, default=config.OUT_DIR, help="output directory")
    args = ap.parse_args()

    print("== Extract ==")
    paths = extract.extract_all(refresh=args.refresh, extracts=config.RECEIVE_EXTRACTS)
    data = extract.load(paths)
    print(f"  OTH  {len(data['oth']):,} rows")

    print("\n== Transform ==")
    df, cov = pipelines.run_receive(data)
    agg = pipelines.aggregate(df)

    # The coverage limit is structural, not a bug, and it is large — surface it every run
    # rather than letting a reader assume the total is all candidate money.
    ledger = cov.to_frame().set_index("stage")
    to_cands = ledger.loc["money to candidates", "amount"]
    attributed = ledger.loc["split across committee newsletters", "amount"]
    cstats = congress.coverage(
        congress.candidate_newsletter_weights(data["legislators"], data["membership"]),
        data["legislators"], data["membership"],
    )
    notes = [
        "[warn] INCUMBENTS ONLY. Attribution is by congressional committee seat, so only\n"
        "  sitting members can be attributed. Challengers, open-seat candidates and\n"
        "  defeated incumbents sit on no committee and are dropped:\n"
        f"    money to candidates (all time)     ${to_cands:>16,.0f}\n"
        f"    ...reaching a sitting member       ${attributed:>16,.0f}"
        f"  ({attributed / to_cands * 100:.1f}%)\n"
        f"    excluded                           ${to_cands - attributed:>16,.0f}"
        f"  ({100 - attributed / to_cands * 100:.1f}%)\n"
        f"  Legislators: {cstats['legislators']} current, {cstats['with_fec_id']} with an FEC ID, "
        f"{cstats['with_mapped_committee']} on a mapped committee "
        f"({cstats['no_mapped_committee']} sit only on unmapped ones).",
        "[note] Amounts are SPLIT evenly across each member's mapped committees, then evenly\n"
        "  within a committee's newsletters — so totals ARE additive across newsletters.",
    ]

    return report(
        name="receive",
        df=df,
        agg=agg,
        coverage=cov.to_frame(),
        expected=config.RECEIVE_EXPECTED,
        out_dir=args.out,
        stem="receive_quarterly",
        counterparty_label="sender",
        parquet=args.parquet,
        show_sources=False,
        multi_newsletter_applies=False,   # receive splits evenly; totals are additive
        extra_notes=notes,
    )


if __name__ == "__main__":
    raise SystemExit(main())
