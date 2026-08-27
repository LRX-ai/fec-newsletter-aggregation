"""Shared console reporting and file output for both runners.

The warnings here are the point, not decoration. The headline totals of both pipelines are
easy to misread -- non-additive across newsletters, non-additive across flow types, and in
the send pipeline built from sources with different date spans -- so every run restates
those caveats next to the numbers they apply to.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from . import config, pipelines


def _fmt(df: pd.DataFrame) -> str:
    with pd.option_context("display.max_columns", None, "display.width", 220):
        return df.to_string(index=False)


def _money(df: pd.DataFrame, col: str = "amount") -> pd.DataFrame:
    return df.assign(**{col: df[col].map("{:,.0f}".format)})


def report(
    *,
    name: str,
    df: pd.DataFrame,
    agg: pd.DataFrame,
    coverage: pd.DataFrame,
    expected: dict,
    out_dir: Path,
    stem: str,
    counterparty_label: str,
    parquet: bool = False,
    show_sources: bool = False,
    multi_newsletter_applies: bool = True,
    extra_notes: list[str] | None = None,
) -> int:
    def _num(s):
        return s.map(lambda v: "" if pd.isna(v) else f"{v:,.0f}")

    print(_fmt(coverage.assign(
        amount=lambda d: _num(d["amount"]),
        rows_dropped=lambda d: _num(d["rows_dropped"]),
        amount_dropped=lambda d: _num(d["amount_dropped"]),
    )))

    print("\n== Aggregate ==")
    summary = pipelines.summarise(agg)
    for key, value in summary.items():
        print(f"  {key:<15} {value:,}" if isinstance(value, (int, float)) else f"  {key:<15} {value}")

    out_dir.mkdir(parents=True, exist_ok=True)
    agg.to_csv(out_dir / f"{stem}.csv", index=False)
    coverage.to_csv(out_dir / f"{stem}_coverage.csv", index=False)
    if parquet:
        agg.to_parquet(out_dir / f"{stem}.parquet", index=False)
    print(f"\n  wrote -> {out_dir}/{stem}.csv")

    print("\n== By flow type ==")
    flows = (
        agg.groupby(["flow_type", "support_oppose"], observed=True)
        .agg(rows=("amount", "size"), amount=("amount", "sum"))
        .reset_index().sort_values("amount", ascending=False)
    )
    print(_fmt(_money(flows)))
    print("  These are different KINDS of money. Do not sum across them.")

    print("\n== By newsletter ==")
    print(_fmt(_money(
        agg.groupby("newsletter", observed=True)
        .agg(rows=("amount", "size"), amount=("amount", "sum"))
        .reset_index().sort_values("amount", ascending=False)
    )))

    print("\n== By quarter ==")
    by_q = (
        agg.groupby("quarter", observed=True)
        .agg(rows=("amount", "size"), amount=("amount", "sum")).reset_index()
    )
    if show_sources:
        comp = (
            df.assign(quarter=df["quarter"].astype(str))
            .groupby("quarter")["source"]
            .agg(lambda s: "+".join(sorted(set(s))))
            .rename("sources").reset_index()
        )
        by_q = by_q.merge(comp, on="quarter", how="left")
    print(_fmt(_money(by_q)))

    exit_code = 0

    if show_sources and by_q["sources"].nunique() > 1:
        print("\n[warn] source composition CHANGES across quarters -- the trend line is not "
              "comparable end to end:")
        for _, r in by_q.iterrows():
            print(f"    {r['quarter']}  {r['sources']}")
        print("  Quarters built from fewer sources will look artificially low.")
        exit_code = 1

    negatives = pipelines.negative_groups(agg)
    if len(negatives):
        print(f"\n[warn] {len(negatives)} group(s) net negative (${negatives['amount'].sum():,.0f}) "
              f"-- unmatched amendment reversals, mostly conduit refunds.")

    if not config.DROP_SELF_TRANSFERS and "sender_id" in df and "recip_id" in df:
        self_rows = df[df["sender_id"] == df["recip_id"]]
        if len(self_rows):
            share = 100 * self_rows["amount"].sum() / df["amount"].sum()
            print(f"\n[note] {len(self_rows)} row(s) where sender == recipient "
                  f"(${self_rows['amount'].sum():,.0f}, {share:.2f}%) are INCLUDED. "
                  f"Set config.DROP_SELF_TRANSFERS=True to exclude.")

    for note in extra_notes or []:
        print(f"\n{note}")

    if multi_newsletter_applies and config.MULTI_NEWSLETTER == "duplicate":
        print("\n[note] MULTI_NEWSLETTER='duplicate': per-newsletter totals are correct, but "
              "summing ACROSS newsletters double-counts multi-newsletter catcodes.")

    if not expected:
        print(f"\n[note] config.{name.upper()}_EXPECTED is empty -- nothing to check this run "
              f"against. Pin these numbers once you trust them.")
    else:
        drift = pipelines.check_expected(summary, expected)
        if drift:
            print(f"\n[warn] diverged from the verified run "
                  f"(tolerance {config.EXPECTED_TOLERANCE_PCT}%):")
            for line in drift:
                print(f"  - {line}")
            exit_code = 1
        else:
            print("\n[ok] output matches the verified run")

    return exit_code
