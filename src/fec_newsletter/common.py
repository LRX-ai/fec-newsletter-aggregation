"""Stages shared by the send and receive pipelines.

The two pipelines differ only in WHICH SIDE of a transaction supplies the newsletter:

* **send**    -- newsletter from the sender's catcode; "money this industry put out"
* **receive** -- newsletter from the recipient's catcode; "money this industry took in"

Everything else -- catcode resolution, the crosswalk join, multi-newsletter fan-out,
quarter bucketing, name lookup -- is identical, so it lives here and is parameterised by
which ID column to attribute from.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from . import config


@dataclass
class Coverage:
    """Row/dollar ledger recorded as a pipeline narrows the data."""

    stages: list[dict] = field(default_factory=list)

    def record(
        self, stage: str, df: pd.DataFrame, note: str = "", starts_branch: bool = False
    ) -> None:
        """Record a stage. `starts_branch` marks a stage that does not follow on from the
        previous one -- the start of an independent source branch -- so the drop columns
        are left blank there instead of differencing two unrelated row counts.
        """
        self.stages.append(
            {
                "stage": stage,
                "rows": len(df),
                "amount": float(df["amount"].sum()) if "amount" in df else float("nan"),
                "note": note,
                "_branch": starts_branch,
            }
        )

    def to_frame(self) -> pd.DataFrame:
        out = pd.DataFrame(self.stages)
        rows_dropped = -out["rows"].diff()
        amount_dropped = -out["amount"].diff()
        # No meaningful "drop" at a branch start, nor at the very first stage.
        blank = out["_branch"] | (out.index == 0)
        out["rows_dropped"] = rows_dropped.mask(blank)
        out["amount_dropped"] = amount_dropped.mask(blank)
        return out.drop(columns="_branch")


def norm_catcode(s: pd.Series) -> pd.Series:
    """Uppercase and trim.

    211 committees store lowercase catcodes ('j2400', 'z1200', ...) that silently fail the
    crosswalk join but match perfectly once uppercased. Applied to BOTH sides of every
    catcode join.
    """
    return s.fillna("").str.strip().str.upper()


def resolve_catcode(
    df: pd.DataFrame,
    id_col: str,
    committees: pd.DataFrame,
    crp_mapping: pd.DataFrame,
    cov: Coverage,
) -> pd.DataFrame:
    """Map `id_col` (a committee ID) to a catcode, with a gap-filling fallback source.

    fec_committees_trim_mapped is authoritative; crp_fec_cmte_mapping fills only the nulls
    (9,779 committees). The two disagree on a large minority of shared committees, so
    `catcode_source` is retained to make that auditable.
    """
    primary = committees[["CMTE_ID", "catcode"]].copy()
    primary["catcode"] = norm_catcode(primary["catcode"])
    primary = primary[primary["catcode"] != ""].rename(
        columns={"CMTE_ID": id_col, "catcode": "_cc_primary"}
    ).drop_duplicates(id_col)

    fallback = crp_mapping[["cmte_id", "catcode"]].copy()
    fallback["catcode"] = norm_catcode(fallback["catcode"])
    fallback = fallback[fallback["catcode"] != ""].rename(
        columns={"cmte_id": id_col, "catcode": "_cc_fallback"}
    ).drop_duplicates(id_col)

    out = df.merge(primary, on=id_col, how="left").merge(fallback, on=id_col, how="left")
    out["catcode"] = out["_cc_primary"].fillna(out["_cc_fallback"])
    out["catcode_source"] = "none"
    out.loc[out["_cc_fallback"].notna(), "catcode_source"] = "crp_fec_cmte_mapping"
    out.loc[out["_cc_primary"].notna(), "catcode_source"] = "fec_committees_trim_mapped"
    out = out.drop(columns=["_cc_primary", "_cc_fallback"])

    cov.record(f"{id_col} resolved to a catcode", out[out["catcode"].notna()])
    return out


def inferred_lookup(
    enhanced: pd.DataFrame, min_confidence: str = None
) -> pd.DataFrame:
    """The committee-keyed half of the enhanced crosswalk, filtered to what may attribute.

    Three filters, each of which a row must survive to move a dollar:
      * committee-keyed  -- the catcode-keyed rows in the same table are CRP's own and are
                            already handled by the catcode join;
      * at or above the confidence floor -- config.MIN_CROSSWALK_CONFIDENCE, "medium" being
                            "everything except low";
      * a non-blank newsletter -- most inferred rows deliberately have none, because the
                            committee belongs to no industry newsletter.
    """
    floor = config.CONFIDENCE_ORDER[min_confidence or config.MIN_CROSSWALK_CONFIDENCE]
    out = enhanced[enhanced["cmte_id"].fillna("").str.strip() != ""].copy()
    out["confidence"] = out["confidence"].fillna("")
    out = out[out["confidence"].map(config.CONFIDENCE_ORDER).fillna(-1) >= floor]
    out["newsletter"] = out["newsletter"].fillna("").str.strip()
    out = out[out["newsletter"] != ""]
    out["cmte_id"] = out["cmte_id"].str.strip()
    return out.drop_duplicates("cmte_id")


def attach_newsletter(
    df: pd.DataFrame,
    crosswalk: pd.DataFrame,
    cov: Coverage,
    enhanced: pd.DataFrame | None = None,
    id_col: str | None = None,
) -> pd.DataFrame:
    """catcode -> newsletter, dropping catcodes that map to no newsletter.

    Catcodes with a blank newsletter are the Z*/J1*/J2* party, candidate and
    leadership-PAC categories, which CRP deliberately leaves outside the industry
    newsletters. Dropping them is correct, not lossy.

    `enhanced` adds a SECOND, committee-keyed pass over the rows the first pass left
    without a newsletter, using `id_col` as the committee ID. It runs strictly after the
    catcode join and only fills blanks, so CRP's mapping is never overwritten -- an
    inferred row can add a newsletter to money that had none, never move money CRP placed.

    Note this cannot rescue a Z*/J1*/J2* committee: those resolve to a real CRP catcode
    whose newsletter is deliberately blank, so they are not in the inferred set at all.
    """
    xw = crosswalk[["catcode", "catname", "newsletter"]].copy()
    xw["catcode"] = norm_catcode(xw["catcode"])
    xw["newsletter"] = xw["newsletter"].fillna("").str.strip()

    out = df.merge(xw.drop_duplicates("catcode"), on="catcode", how="left")
    cov.record("catcode found in crosswalk", out[out["catname"].notna()])

    out["newsletter_source"] = ""
    out.loc[out["newsletter"].fillna("") != "", "newsletter_source"] = "crp"

    if enhanced is not None and id_col is not None:
        inferred = inferred_lookup(enhanced)
        gap = out["newsletter"].fillna("") == ""
        filled = out.loc[gap, id_col].map(inferred.set_index("cmte_id")["newsletter"])
        names = out.loc[gap, id_col].map(inferred.set_index("cmte_id")["catname"])
        out.loc[gap, "newsletter"] = filled
        out.loc[gap, "catname"] = out.loc[gap, "catname"].fillna(names)
        out.loc[gap & filled.notna(), "newsletter_source"] = "llm_inferred"
        cov.record(
            f"{id_col} matched an inferred committee row",
            out[out["newsletter_source"] == "llm_inferred"],
            f"{len(inferred):,} committees usable at confidence "
            f">= {config.MIN_CROSSWALK_CONFIDENCE!r}",
        )

    out = out[out["newsletter"].fillna("") != ""].copy()
    cov.record("catcode maps to a newsletter", out)
    return out


def fan_out_newsletters(df: pd.DataFrame, cov: Coverage) -> pd.DataFrame:
    """Expand comma-separated multi-newsletter catcodes per config.MULTI_NEWSLETTER."""
    mode = config.MULTI_NEWSLETTER
    if mode == "raw":
        cov.record("newsletter fan-out", df, "mode=raw (no expansion)")
        return df

    out = df.copy()
    out["_parts"] = out["newsletter"].str.split(",")
    out["_n"] = out["_parts"].str.len()
    if mode == "split":
        out["amount"] = out["amount"] / out["_n"]
    elif mode != "duplicate":
        raise ValueError(f"Unknown MULTI_NEWSLETTER mode: {mode!r}")

    multi = int((out["_n"] > 1).sum())
    out = out.explode("_parts")
    out["newsletter"] = out["_parts"].str.strip()
    out = out.drop(columns=["_parts", "_n"])

    note = f"mode={mode}, {multi} multi-newsletter rows expanded"
    if mode == "duplicate":
        note += " -- TOTALS ARE NON-ADDITIVE ACROSS NEWSLETTERS"
    cov.record("newsletter fan-out", out, note)
    return out


def resolve_names(
    df: pd.DataFrame,
    id_col: str,
    out_prefix: str,
    committees: pd.DataFrame,
    candidates: pd.DataFrame,
    cov: Coverage,
) -> pd.DataFrame:
    """Resolve an FEC ID to a name and entity type, branching on the ID prefix.

    Unresolved IDs keep the bare ID rather than being dropped; losing a real dollar to a
    missing lookup row would be worse than showing an opaque party.
    """
    cmte = committees[["CMTE_ID", "CMTE_NM"]].rename(
        columns={"CMTE_ID": id_col, "CMTE_NM": "_cmte_nm"}
    ).drop_duplicates(id_col)
    cand = candidates[["CAND_ID", "CAND_NAME"]].rename(
        columns={"CAND_ID": id_col, "CAND_NAME": "_cand_nm"}
    ).drop_duplicates(id_col)

    out = df.merge(cmte, on=id_col, how="left").merge(cand, on=id_col, how="left")

    prefix = out[id_col].str[0]
    out[f"{out_prefix}_type"] = "unknown"
    out.loc[prefix == "C", f"{out_prefix}_type"] = "committee"
    out.loc[prefix.isin(["H", "S", "P"]), f"{out_prefix}_type"] = "candidate"

    name = out["_cmte_nm"].fillna(out["_cand_nm"])
    unresolved = int(name.isna().sum())
    out[f"{out_prefix}_name"] = name.fillna(out[id_col])
    out = out.drop(columns=["_cmte_nm", "_cand_nm"])

    pct = 100 * (1 - unresolved / len(out)) if len(out) else 0.0
    cov.record(f"{out_prefix} identity resolved", out, f"{pct:.1f}% named, {unresolved} bare IDs")
    return out


def add_quarter(
    df: pd.DataFrame, cov: Coverage, start: str, end: str, date_col: str = "transaction_dt"
) -> pd.DataFrame:
    """Bucket by transaction date, never by RPT_TP.

    RPT_TP describes the filing schedule, not when the money actually moved. Dates arrive
    here already normalised to ISO by the source adapters, because the two source tables
    store them in different formats.
    """
    out = df.copy()
    parsed = pd.to_datetime(out[date_col], format="%Y-%m-%d", errors="coerce")
    unparseable = int(parsed.isna().sum())
    out["quarter"] = parsed.dt.to_period("Q")
    out = out[out["quarter"].notna()]
    cov.record("date parsed to a quarter", out, f"{unparseable} unparseable dates dropped")

    lo, hi = pd.Period(start, freq="Q"), pd.Period(end, freq="Q")
    out = out[(out["quarter"] >= lo) & (out["quarter"] <= hi)]
    cov.record("within reporting window", out, f"{start}..{end}")
    return out
