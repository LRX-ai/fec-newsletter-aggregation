"""The send and receive pipelines.

Both reduce to the same six stages over a normalised transaction set; they differ only in
which side supplies the newsletter and which side becomes the breakdown dimension:

                    newsletter from      broken down by       sources
    send            sender_id            recipient            OTH + PAS2
    receive         recip_id             sender               OTH only
"""

from __future__ import annotations

import pandas as pd

from . import common, config, congress, sources
from .common import Coverage


def _attribute(
    df: pd.DataFrame,
    data: dict[str, pd.DataFrame],
    attribute_from: str,
    counterparty: str,
    cov: Coverage,
) -> pd.DataFrame:
    """Shared tail: catcode -> newsletter -> fan out -> name both parties."""
    out = common.resolve_catcode(df, attribute_from, data["committees"],
                                 data["crp_cmte_mapping"], cov)
    # The enhanced crosswalk only ever fills gaps CRP left, and only on the side being
    # attributed from, so it is keyed to `attribute_from` rather than the counterparty.
    enhanced = data.get("crosswalk_enhanced") if config.USE_ENHANCED_CROSSWALK else None
    out = common.attach_newsletter(out, data["crosswalk"], cov,
                                   enhanced=enhanced, id_col=attribute_from)
    out = common.fan_out_newsletters(out, cov)
    out = common.resolve_names(out, counterparty, "counterparty",
                               data["committees"], data["candidates"], cov)
    out = common.resolve_names(out, attribute_from, "attributed",
                               data["committees"], data["candidates"], cov)
    return out


def _filter_flow_types(df: pd.DataFrame, cov: Coverage) -> pd.DataFrame:
    if config.INCLUDE_FLOW_TYPES is None:
        return df
    out = df[df["flow_type"].isin(config.INCLUDE_FLOW_TYPES)]
    cov.record("flow types included", out, str(sorted(config.INCLUDE_FLOW_TYPES)))
    return out


def run_send(data: dict[str, pd.DataFrame]) -> tuple[pd.DataFrame, Coverage]:
    """Money SENT by each newsletter's industry, broken down by who received it."""
    cov = Coverage()
    oth = sources.from_oth(data["oth"], cov)
    pas2 = sources.from_pas2(data["pas2"], cov)

    combined = pd.concat([oth, pas2], ignore_index=True)
    cov.record("OTH + PAS2 combined", combined, starts_branch=True)

    combined = sources.dedupe(combined, cov)
    combined = _filter_flow_types(combined, cov)

    # For the send side, the interesting counterparty is the candidate where one is named.
    combined["counterparty_id"] = combined["recip_cand_id"].fillna(combined["recip_id"])

    out = _attribute(combined, data, "sender_id", "counterparty_id", cov)
    out = common.add_quarter(out, cov, config.SEND_QUARTER_START, config.SEND_QUARTER_END)
    return out, cov


def run_receive(data: dict[str, pd.DataFrame]) -> tuple[pd.DataFrame, Coverage]:
    """Money reaching each newsletter's POLICY AREA, broken down by who sent it.

    Candidate money only, attributed by the recipient's congressional committee seats
    rather than by a catcode — a candidate has no catcode. Each candidate's money is split
    evenly across their mapped committees, then evenly again within a committee's
    newsletters, so the amounts stay additive.

    Coverage is inherently incumbent-only: 68% of candidate dollars go to challengers and
    open-seat candidates who sit on no committee. Those rows are dropped, and the ledger
    records exactly how much.
    """
    cov = Coverage()
    oth = sources.from_oth(data["oth"], cov)
    pas2 = sources.from_pas2(data["pas2"], cov)

    combined = pd.concat([oth, pas2], ignore_index=True)
    cov.record("OTH + PAS2 combined", combined, starts_branch=True)
    combined = sources.dedupe(combined, cov)

    # The candidate is the attribution target. recip_cand_id is populated by PAS2; OTH
    # candidate-directed rows carry the candidate in recip_id itself.
    combined["cand_id"] = combined["recip_cand_id"].fillna(combined["recip_id"])
    combined = combined[combined["cand_id"].str[:1].isin(["H", "S", "P"])]
    cov.record("money to candidates", combined, "committee-to-committee money excluded")

    combined = _filter_flow_types(combined, cov)

    weights = congress.candidate_newsletter_weights(
        data["legislators"], data["membership"], terms=data.get("terms"))
    out = combined.merge(weights, on="cand_id", how="inner")
    cov.record(
        "recipient sits on a mapped committee", out,
        f"{combined['cand_id'].nunique() - out['cand_id'].nunique()} candidates dropped "
        f"(not sitting members)",
    )

    # Even split: weights sum to 1.0 per candidate, so the total is conserved.
    out["amount"] = out["amount"] * out["weight"]
    cov.record("split across committee newsletters", out, "weights sum to 1.0 per candidate")

    out["counterparty_id"] = out["sender_id"]
    out = common.resolve_names(
        out, "counterparty_id", "counterparty", data["committees"], data["candidates"], cov
    )
    out["attributed_name"] = out["legislator_name"]
    out["attributed_type"] = "candidate"
    if "party_short" in out:
        # "Rep. Andy Barr (R-KY-06)" -- party belongs beside a legislator's name.
        out["attributed_label"] = (
            out["chamber"].fillna("") + " " + out["legislator_name"].fillna("")
            + " (" + out["party_short"].fillna("?") + "-" + out["seat"].fillna("") + ")"
        ).str.strip()
    out = common.add_quarter(out, cov, config.RECEIVE_QUARTER_START, config.RECEIVE_QUARTER_END)
    return out, cov


# --- Aggregation ----------------------------------------------------------------------

GROUP_KEYS = [
    "quarter", "newsletter", "flow_type", "support_oppose",
    "counterparty_id", "counterparty_name", "counterparty_type",
]
OUTPUT_COLUMNS = GROUP_KEYS + ["amount", "txn_count", "sources"]


def aggregate(df: pd.DataFrame) -> pd.DataFrame:
    out = (
        df.groupby(GROUP_KEYS, observed=True, dropna=False)
        .agg(
            amount=("amount", "sum"),
            txn_count=("amount", "size"),
            sources=("source", lambda s: "+".join(sorted(set(s)))),
        )
        .reset_index()
        .sort_values(["quarter", "newsletter", "flow_type", "amount"],
                     ascending=[True, True, True, False])
    )
    out["quarter"] = out["quarter"].astype(str)
    return out[OUTPUT_COLUMNS]


def summarise(agg: pd.DataFrame) -> dict:
    return {
        "output_rows": len(agg),
        "newsletters": agg["newsletter"].nunique(),
        "counterparties": agg["counterparty_id"].nunique(),
        "total_amount": float(agg["amount"].sum()),
        "quarters": f"{agg['quarter'].min()}..{agg['quarter'].max()}" if len(agg) else "-",
    }


def check_expected(summary: dict, expected: dict) -> list[str]:
    warnings = []
    for key in ("output_rows", "newsletters", "counterparties", "total_amount"):
        want = expected.get(key)
        if not want:
            continue
        drift = abs(summary[key] - want) / want * 100
        if drift > config.EXPECTED_TOLERANCE_PCT:
            warnings.append(
                f"{key}: got {summary[key]:,.0f}, expected ~{want:,.0f} ({drift:.1f}% drift)"
            )
    return warnings


def negative_groups(agg: pd.DataFrame) -> pd.DataFrame:
    """Groups whose netted total is negative -- unmatched amendment reversals."""
    return agg[agg["amount"] < 0].sort_values("amount")
