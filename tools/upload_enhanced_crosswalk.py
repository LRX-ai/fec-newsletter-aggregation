#!/usr/bin/env python3
"""Upload enhanced_crp_l_crosswalk.csv to usa.crp_l_crosswalk_enhanced.

    python tools/upload_enhanced_crosswalk.py            # create/replace the table
    python tools/upload_enhanced_crosswalk.py --dry-run  # validate only, no writes

A SEPARATE table: usa.crp_l_crosswalk is CRP's own mapping and is never written to here.
This one carries both -- the 460 CRP rows unchanged (source='crp') plus the LLM-inferred
committee rows (source='llm_inferred') -- so the send pipeline can read one relation.

The two row kinds are keyed differently and the schema says so: a CRP row has a catcode
and a null cmte_id, an inferred row has a cmte_id and a null catcode. The CHECK constraint
holds that line, because a row with neither key joins to nothing and a row with both would
be attributed twice.
"""
from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from fec_newsletter import config, enrich, extract  # noqa: E402

TABLE = "usa.crp_l_crosswalk_enhanced"

DDL = f"""
drop table if exists {TABLE};
create table {TABLE} (
    catcode     text,
    cmte_id     text,
    catorder    text,
    catname     text,
    industry    text,
    sector      text,
    newsletter  text,
    source      text not null,
    confidence  text,
    reasoning   text,
    model       text,
    constraint crp_l_crosswalk_enhanced_key check (
        (catcode is not null and cmte_id is null)
        or (catcode is null and cmte_id is not null)
    )
);
create index on {TABLE} (catcode);
create index on {TABLE} (cmte_id);
comment on table {TABLE} is
    'CRP catcode->newsletter crosswalk plus LLM-inferred committee->newsletter rows for '
    'committees CRP does not cover. Built by run_enrich.py; uploaded by '
    'tools/upload_enhanced_crosswalk.py. source=crp rows are a verbatim copy of '
    'usa.crp_l_crosswalk. Inferred rows carry confidence/reasoning/model; the pipelines '
    'read only those at or above config.MIN_CROSSWALK_CONFIDENCE.';
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", type=Path,
                    default=config.REPO_ROOT / "enhanced_crp_l_crosswalk.csv")
    ap.add_argument("--dry-run", action="store_true", help="validate only, write nothing")
    args = ap.parse_args()

    df = pd.read_csv(args.csv, dtype=str)
    missing = [c for c in enrich.ENHANCED_COLUMNS if c not in df.columns]
    if missing:
        raise SystemExit(f"{args.csv.name} is missing column(s): {missing}")
    df = df[enrich.ENHANCED_COLUMNS]

    # Blank and NA both mean "no key here"; the CHECK constraint only understands NULL.
    for col in ("catcode", "cmte_id"):
        df[col] = df[col].fillna("").str.strip().replace("", None)

    both = df[df["catcode"].notna() & df["cmte_id"].notna()]
    neither = df[df["catcode"].isna() & df["cmte_id"].isna()]
    if len(both) or len(neither):
        raise SystemExit(
            f"{len(both)} row(s) carry both keys and {len(neither)} carry neither; "
            f"the table's CHECK constraint would reject them. Fix the CSV first."
        )

    print(f"== {args.csv.name} ==")
    print(f"  {len(df):,} rows")
    print(df["source"].value_counts().to_string())
    print("\n  inferred rows by confidence:")
    inf = df[df["source"] != "crp"]
    print(inf["confidence"].value_counts().to_string() if len(inf) else "   (none)")
    usable = enrich.usable(df)
    print(f"\n  usable at min_confidence={enrich.MIN_CONFIDENCE!r}: {len(usable):,} rows "
          f"({(usable['source'] != 'crp').sum():,} inferred)")

    if args.dry_run:
        print("\n  --dry-run: nothing written")
        return 0

    conn = extract.connect()
    try:
        buf = io.StringIO()
        df.to_csv(buf, index=False, header=False)
        buf.seek(0)
        with conn.cursor() as cur:
            cur.execute(DDL)
            cur.copy_expert(
                f"COPY {TABLE} ({', '.join(enrich.ENHANCED_COLUMNS)}) "
                f"FROM STDIN WITH (FORMAT CSV)", buf)
            cur.execute(f"select count(*), count(catcode), count(cmte_id) from {TABLE}")
            total, by_cat, by_cmte = cur.fetchone()
        conn.commit()
    finally:
        conn.close()

    print(f"\n  wrote -> {TABLE}: {total:,} rows "
          f"({by_cat:,} catcode-keyed, {by_cmte:,} committee-keyed)")
    if total != len(df):
        raise SystemExit(f"row count mismatch: sent {len(df):,}, table holds {total:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
