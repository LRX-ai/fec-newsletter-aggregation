"""Stage assertions for the send and receive pipelines.

Run with:  python -m pytest tests/ -v
Requires cached extracts (run `python run_send.py` once first).

These are data assertions as much as code assertions. Several encode facts measured against
the live tables that the transforms silently depend on -- if one starts failing, the source
data changed shape and the pipelines need re-verifying, not the test loosening.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fec_newsletter import common, config, extract, pipelines, sources  # noqa: E402


@pytest.fixture(scope="module")
def data():
    paths = {n: config.CACHE_DIR / c for n, (_, c) in config.EXTRACTS.items()}
    missing = [n for n, p in paths.items() if not p.exists()]
    if missing:
        pytest.skip(f"no cached extracts for {missing}; run `python run_send.py` first")
    return extract.load(paths)


@pytest.fixture(scope="module")
def send(data):
    df, cov = pipelines.run_send(data)
    return df, cov.to_frame(), pipelines.aggregate(df)


@pytest.fixture(scope="module")
def receive(data):
    df, cov = pipelines.run_receive(data)
    return df, cov.to_frame(), pipelines.aggregate(df)


# --- Source data shape ----------------------------------------------------------------

def test_sub_id_unique_within_each_table(data):
    assert data["oth"]["SUB_ID"].is_unique
    assert data["pas2"]["SUB_ID"].is_unique


def test_sub_id_is_shared_across_tables(data):
    """The cross-source dedupe depends on SUB_ID being FEC's GLOBAL row key.

    If the two tables ever stopped sharing SUB_IDs for the same transaction, the send
    pipeline would double-count ~$828M and this is the assertion that catches it.
    """
    overlap = set(data["oth"]["SUB_ID"]) & set(data["pas2"]["SUB_ID"])
    assert len(overlap) > 100_000


def test_no_15j_rows_survive_extraction(data):
    assert (data["oth"]["TRANSACTION_TP"] == "15J").sum() == 0


def test_pas2_filer_is_always_the_payer(data):
    assert (data["pas2"]["CMTE_ID"].str[:1] == "C").all()


def test_pas2_name_identifies_the_recipient_not_the_filer(data):
    """Confirms PAS2 direction across the whole table rather than by sample.

    In the PAS2 file NAME is the recipient's name. If it matched the FILER's name instead,
    sender and recipient would be the wrong way round.
    """
    d = data["pas2"]
    names = data["committees"].set_index("CMTE_ID")["CMTE_NM"]
    sub = d[(d["TRANSACTION_TP"] == "24K") & (d["OTHER_ID"].str[:1] == "C")]

    def norm(s):
        return s.fillna("").str.upper().str.replace(r"[^A-Z0-9]", "", regex=True)

    assert (norm(sub["OTHER_ID"].map(names)) == norm(sub["NAME"])).mean() > 0.80
    assert (norm(sub["CMTE_ID"].map(names)) == norm(sub["NAME"])).mean() < 0.01


def test_pas2_recipients_never_carry_a_newsletter(data):
    """Why receive cannot attribute PAS2 money from a CATCODE.

    PAS2 recipients are candidates and their authorized committees, which sit under Z*
    catcodes CRP leaves outside the industry newsletters. Receive therefore attributes from
    the recipient's congressional committee seats instead — this test is what establishes
    that the catcode route was a dead end, not a missed opportunity.
    """
    cm = data["committees"].copy()
    cm["catcode"] = common.norm_catcode(cm["catcode"])
    xw = data["crosswalk"].copy()
    xw["catcode"] = common.norm_catcode(xw["catcode"])
    xw["newsletter"] = xw["newsletter"].fillna("").str.strip()

    recip = data["pas2"]["OTHER_ID"]
    catcode = recip.map(cm.set_index("CMTE_ID")["catcode"])
    newsletter = catcode.map(xw.set_index("catcode")["newsletter"]).fillna("")
    assert (newsletter != "").sum() == 0


# --- Direction ------------------------------------------------------------------------

def test_direction_sets_are_disjoint():
    assert not (config.OUTFLOW_TYPES & config.INFLOW_TYPES)
    assert not (config.OUTFLOW_TYPES & config.OTH_CANDIDATE_TYPES)
    assert not (config.INFLOW_TYPES & config.OTH_CANDIDATE_TYPES)


def test_receipt_codes_are_classified_as_inflows():
    """Regression guard for the 30K/31K/32K bug.

    These are Convention/HQ/Recount "receipt from registered filer" -- receipts. Filing
    them as outflows swapped sender and recipient on 824 rows worth $30.1M.
    """
    for code in ("30K", "31K", "32K", "30G", "31G", "32G"):
        assert code in config.INFLOW_TYPES, code
        assert code not in config.OUTFLOW_TYPES, code


def test_oth_self_transfers_come_from_the_source(data):
    """Sender == recipient must always trace to a source row with CMTE_ID == OTHER_ID.

    A self-transfer invented by the derivation would mean the direction logic collapsed.
    """
    cov = common.Coverage()
    oth = sources.from_oth(data["oth"], cov)
    self_rows = oth[oth["sender_id"] == oth["recip_id"]]
    src = data["oth"]
    source_self = set(src.loc[src["CMTE_ID"] == src["OTHER_ID"], "SUB_ID"])
    assert set(self_rows["sub_id"]) <= source_self


# --- Dedupe ---------------------------------------------------------------------------

def test_send_dedupes_across_sources(send):
    """The union must remove the rows both files report, or $828M is counted twice."""
    _, cov, _ = send
    removed = cov.loc[cov["stage"] == "deduped on SUB_ID across sources", "rows_dropped"].iloc[0]
    assert removed > 100_000


def test_no_duplicate_sub_ids_survive(send):
    df, _, _ = send
    assert df.drop_duplicates(["sub_id", "newsletter"])["sub_id"].notna().all()
    one_newsletter = df[df["newsletter"] == df["newsletter"].iloc[0]]
    assert not one_newsletter.duplicated(subset=["sub_id"]).any()


def test_fingerprint_is_unique_after_dedupe(send):
    df, _, _ = send
    key = ["sender_id", "recip_id", "transaction_dt", "amount", "newsletter"]
    assert not df.duplicated(subset=key).any()


# --- Attribution direction ------------------------------------------------------------

def test_send_attributes_from_the_sender(send, data):
    df, _, _ = send
    cm = data["committees"].copy()
    cm["catcode"] = common.norm_catcode(cm["catcode"])
    lookup = cm.set_index("CMTE_ID")["catcode"]
    sample = df[df["catcode_source"] == "fec_committees_trim_mapped"].head(2000)
    assert (sample["sender_id"].map(lookup) == sample["catcode"]).all()


def test_receive_carries_no_catcode_attribution(receive):
    """Receive attributes by committee seat, never by catcode.

    Replaces the pre-2026-08-13 assertion that receive read the recipient's catcode — a
    candidate has none, which is why the architecture changed.
    """
    df, _, _ = receive
    assert "catcode" not in df.columns or df["catcode"].isna().all()


def test_receive_uses_candidate_money_from_both_sources(receive):
    """Candidate money lives mostly in PAS2; OTH contributes the candidate-directed codes."""
    df, _, _ = receive
    assert set(df["source"]) <= {"oth", "pas2"}
    assert "pas2" in set(df["source"])


def test_receive_keeps_only_money_to_candidates(receive):
    """Committee-to-committee money has no candidate and cannot be attributed here."""
    df, _, _ = receive
    assert df["cand_id"].str[:1].isin(["H", "S", "P"]).all()


def test_send_uses_both_sources(send):
    df, _, _ = send
    assert set(df["source"]) == {"oth", "pas2"}


# --- Flow types -----------------------------------------------------------------------

def test_flow_types_are_from_the_known_set(send):
    df, _, _ = send
    assert set(df["flow_type"]) <= {
        "direct_contribution", "independent_expenditure",
        "coordinated_expenditure", "communication_cost", "committee_transfer",
        "loan", "loan_repayment",
    }
    assert set(df["support_oppose"]) <= {"support", "oppose", "neutral"}


def test_loans_are_tagged_separately(data):
    """Loans and repayments must never merge into contribution or transfer totals.

    They reach $0 today only because the committee end has no catcode -- $362.5M across
    4,890 rows. run_enrich.py targets exactly those committees, so this tag is what stops
    a successful enrichment run from quietly turning borrowed money into industry money.
    """
    loan_codes = {c for c, (f, _) in config.FLOW_TYPES.items() if f.startswith("loan")}
    assert loan_codes, "no loan codes are tagged"
    assert loan_codes <= (config.OUTFLOW_TYPES | config.INFLOW_TYPES)
    for code in loan_codes:
        assert config.FLOW_TYPES[code][1] == "neutral", code


def test_oppose_money_is_never_netted_against_support(send):
    _, _, agg = send
    assert (agg[agg["support_oppose"] == "oppose"]["amount"] >= 0).mean() > 0.95


def test_flow_type_is_an_output_dimension(send):
    """Keeps IE money from being silently folded into contributions."""
    _, _, agg = send
    assert agg.groupby(["quarter", "newsletter", "counterparty_id"])["flow_type"].nunique().max() > 1


# --- Newsletter attribution -----------------------------------------------------------

def test_catcode_case_hazard_still_exists_in_source(data):
    """Guards the uppercase normalisation by proving the raw data still needs it."""
    raw = data["committees"]["catcode"].fillna("")
    non_empty = raw[raw != ""]
    assert (non_empty != non_empty.str.upper()).sum() > 0


def test_no_blank_newsletters(send, receive):
    for df, _, _ in (send, receive):
        assert (df["newsletter"].str.strip() == "").sum() == 0


def test_no_comma_in_newsletter_after_fan_out(send):
    if config.MULTI_NEWSLETTER == "raw":
        pytest.skip("raw mode keeps the combined string")
    df, _, _ = send
    assert df["newsletter"].str.contains(",").sum() == 0


def test_split_mode_conserves_the_total(data):
    original = config.MULTI_NEWSLETTER
    config.MULTI_NEWSLETTER = "split"
    try:
        split_total = pipelines.run_send(data)[0]["amount"].sum()
        config.MULTI_NEWSLETTER = "raw"
        raw_total = pipelines.run_send(data)[0]["amount"].sum()
    finally:
        config.MULTI_NEWSLETTER = original
    assert split_total == pytest.approx(raw_total, rel=1e-9)


# --- Quarter bucketing ----------------------------------------------------------------

def test_quarters_inside_configured_windows(send, receive):
    _, _, s = send
    _, _, r = receive
    assert s["quarter"].min() >= config.SEND_QUARTER_START
    assert s["quarter"].max() <= config.SEND_QUARTER_END
    assert r["quarter"].min() >= config.RECEIVE_QUARTER_START
    assert r["quarter"].max() <= config.RECEIVE_QUARTER_END


def test_send_window_has_uniform_source_coverage(send):
    """Every quarter in the default window must draw on BOTH sources.

    A quarter missing one source would look artificially low and corrupt the trend, which
    is the whole purpose of this pipeline.
    """
    df, _, _ = send
    per_q = df.groupby(df["quarter"].astype(str))["source"].nunique()
    assert (per_q == 2).all(), per_q[per_q != 2].to_dict()


def test_dates_are_iso_normalised(send):
    """The two tables store dates differently; adapters must normalise before bucketing."""
    df, _, _ = send
    assert df["transaction_dt"].str.match(r"^\d{4}-\d{2}-\d{2}$").all()


# --- Output ---------------------------------------------------------------------------

def test_outputs_match_verified_runs(send, receive):
    _, _, s = send
    _, _, r = receive
    assert pipelines.check_expected(pipelines.summarise(s), config.SEND_EXPECTED) == []
    assert pipelines.check_expected(pipelines.summarise(r), config.RECEIVE_EXPECTED) == []


def test_no_duplicate_output_groups(send, receive):
    for _, _, agg in (send, receive):
        assert not agg.duplicated(subset=pipelines.GROUP_KEYS).any()


def test_negative_groups_stay_immaterial(send, receive):
    for _, _, agg in (send, receive):
        negatives = pipelines.negative_groups(agg)["amount"].sum()
        assert abs(negatives) < 0.01 * agg["amount"].sum()


# --- Receive: congressional committee attribution ---------------------------------------

def test_receive_attributes_by_committee_not_catcode(receive):
    """Receive must not use a catcode at all — candidates do not have one."""
    df, _, _ = receive
    assert "bioguide" in df.columns
    assert df["bioguide"].notna().all()
    assert (df["cand_id"].str[:1].isin(["H", "S", "P"])).all()


def test_receive_weights_sum_to_one_per_candidate(data):
    """The even split conserves dollars: a candidate's weights must total 1.0.

    If they didn't, the receive total would silently inflate or shrink relative to the
    money that actually reached those candidates.
    """
    from fec_newsletter import congress
    w = congress.candidate_newsletter_weights(data["legislators"], data["membership"])
    totals = w.groupby("cand_id")["weight"].sum()
    assert totals.round(9).eq(1.0).all(), totals[totals.round(9) != 1.0].head().to_dict()


def test_receive_split_is_additive(receive):
    """Each transaction's weights must total 1.0 across its newsletter rows.

    This is what makes receive additive: summing every newsletter reproduces the money
    that reached those candidates, rather than a multiple of it.
    """
    df, _, _ = receive
    per_txn = df.groupby("sub_id")["weight"].sum()
    assert per_txn.round(6).eq(1.0).all(), per_txn[per_txn.round(6) != 1.0].head().to_dict()


def test_receive_uses_only_newsletters_from_the_mapping(receive):
    """A newsletter can only enter receive via the authored subcommittee crosswalk."""
    from fec_newsletter import congress
    allowed = set(congress.committee_newsletters()["newsletter"])
    df, _, _ = receive
    assert set(df["newsletter"]) <= allowed


def test_every_label_in_the_mapping_is_recognised():
    """An unaliased label attributes NOTHING — it must fail loudly, not silently.

    The source file uses its own shorthand ("Defence", "ICT", "Agri", "Energy"). A new
    label with no alias would be dropped without trace, quietly shrinking a newsletter.
    """
    from fec_newsletter import congress
    assert congress.unknown_labels() == set()


def test_mapping_normalises_onto_the_crosswalk_vocabulary(data):
    """Receive and send must share one newsletter vocabulary to stay comparable."""
    from fec_newsletter import congress
    used = set(congress.committee_newsletters()["newsletter"])
    valid = {p.strip() for v in data["crosswalk"]["newsletter"].fillna("")
             for p in v.split(",") if p.strip()}
    assert used <= valid, used - valid


def test_foreign_affairs_is_reachable(data):
    """Regression guard: the source labelled every HSFA/SSFR subcommittee "Social Issues".

    Left as authored, the Foreign Affairs newsletter would receive $0 despite the foreign
    affairs committees being its most obvious source.
    """
    from fec_newsletter import congress
    cn = congress.committee_newsletters()
    assert "Foreign Affairs" in set(cn["newsletter"])
    for committee in ("HSFA", "SSFR"):
        reach = set(cn[cn["committee"] == committee]["newsletter"])
        assert "Foreign Affairs" in reach, committee


def test_subcommittee_codes_match_the_membership_table(data):
    """The join key is parent thomas_id + zero-padded subcommittee id (HSAG + 15 -> HSAG15).

    If the padding or concatenation were wrong the join would silently return nothing.
    """
    from fec_newsletter import congress
    cn = congress.committee_newsletters()
    codes = set(cn[cn["level"] == "subcommittee"]["code"])
    live = set(congress.current_memberships(data["membership"])["committee"])
    live_sub = {c for c in live if len(c) == 6}
    assert live_sub <= codes, live_sub - codes


def test_subcommittee_seats_take_precedence_over_the_fallback():
    """Within a committee, a mapped subcommittee seat wins over the derived top-level union.

    Otherwise the sharper signal would be diluted by the committee's whole jurisdiction.
    """
    from fec_newsletter import congress
    legislators = pd.DataFrame([{"bioguide": "X000003", "official_full": "T",
                                 "first": "T", "last": "T", "fec": "['H0TEST0003']"}])
    membership = pd.DataFrame([
        {"committee": "HSHM", "bioguide": "X000003", "chamber": "house",
         "congress": "119", "title": "", "rank": "1"},        # top-level seat
        {"committee": "HSHM08", "bioguide": "X000003", "chamber": "house",
         "congress": "119", "title": "", "rank": "1"},        # Cybersecurity -> ICT only
    ])
    w = congress.candidate_newsletter_weights(legislators, membership)
    assert set(w["newsletter"]) == {"ICT & Cybersecurity"}
    assert w["weight"].sum() == pytest.approx(1.0)


def test_top_level_fallback_applies_without_a_subcommittee_seat():
    """A member on a committee with no mapped subcommittee gets that committee's union."""
    from fec_newsletter import congress
    legislators = pd.DataFrame([{"bioguide": "X000004", "official_full": "T",
                                 "first": "T", "last": "T", "fec": "['H0TEST0004']"}])
    membership = pd.DataFrame([
        {"committee": "HSHM", "bioguide": "X000004", "chamber": "house",
         "congress": "119", "title": "", "rank": "1"},
    ])
    w = congress.candidate_newsletter_weights(legislators, membership)
    union = set(congress.committee_newsletters()
                .query("committee == 'HSHM' and level == 'subcommittee'")["newsletter"])
    assert set(w["newsletter"]) == union
    assert w["weight"].sum() == pytest.approx(1.0)


def _one_seat(bioguide: str, cand_id: str, committee: str, chamber: str = "senate"):
    return (
        pd.DataFrame([{"bioguide": bioguide, "official_full": "T", "first": "T",
                       "last": "T", "fec": f"['{cand_id}']"}]),
        pd.DataFrame([{"committee": committee, "bioguide": bioguide, "chamber": chamber,
                       "congress": "119", "title": "", "rank": "1"}]),
    )


def test_committees_marked_none_attribute_nothing():
    """The ethics committees, Printing and the Library carry the NO_MAPPING sentinel.

    Their jurisdiction is procedural, so they must yield nothing rather than have an
    industry silently invented for them.
    """
    from fec_newsletter import congress
    legislators, membership = _one_seat("X000005", "H0TEST0005", "HSSO", "house")
    assert len(congress.candidate_newsletter_weights(legislators, membership)) == 0


def test_committee_absent_from_the_crosswalk_attributes_nothing():
    """A code the file has never heard of is not guessed at."""
    from fec_newsletter import congress
    legislators, membership = _one_seat("X000015", "H0TEST0015", "ZZNONE")
    assert len(congress.candidate_newsletter_weights(legislators, membership)) == 0


def test_authored_top_level_label_attributes_without_subcommittees():
    """Senate Indian Affairs has no subcommittees, so its label is authored on the row.

    Nothing can be derived for such a committee, so a blank cell means it attributes
    nothing at all -- the label has to be readable straight off the top-level row.
    """
    from fec_newsletter import congress
    legislators, membership = _one_seat("X000016", "S0TEST0016", "SLIA")
    w = congress.candidate_newsletter_weights(legislators, membership)
    assert set(w["newsletter"]) == {"Tribal Affairs"}
    assert w["weight"].sum() == pytest.approx(1.0)


def test_authored_top_level_label_replaces_the_derived_union():
    """An authored label is a statement about the whole committee, not an addition to it."""
    from fec_newsletter import congress
    mapping = pd.DataFrame([
        {"name": "C", "thomas_id": "HSAG", "jurisdiction_source": "",
         "subcommittee_name": "Sub", "subcommittee_thomas_id": "15",
         "newsletters": "Agri", "notes": ""},
        {"name": "C", "thomas_id": "HSAG", "jurisdiction_source": "",
         "subcommittee_name": "", "subcommittee_thomas_id": "",
         "newsletters": "Finance", "notes": ""},
    ])
    cn = congress.committee_newsletters(mapping)
    top = cn[(cn["code"] == "HSAG") & (cn["level"] == "committee")]
    assert set(top["newsletter"]) == {"Finance & Insurance"}


def test_committees_are_weighted_equally_regardless_of_breadth():
    """A narrow and a broad committee split 50/50, not by newsletter count."""
    from fec_newsletter import congress
    legislators = pd.DataFrame([{"bioguide": "X000006", "official_full": "T",
                                 "first": "T", "last": "T", "fec": "['H0TEST0006']"}])
    membership = pd.DataFrame([
        # Cybersecurity and Infrastructure Protection -> ICT only (1 newsletter)
        {"committee": "HSHM08", "bioguide": "X000006", "chamber": "house",
         "congress": "119", "title": "", "rank": "1"},
        # Commerce, Manufacturing, and Trade -> Manufacturing / Retail / Finance (3)
        {"committee": "HSIF17", "bioguide": "X000006", "chamber": "house",
         "congress": "119", "title": "", "rank": "1"},
    ])
    w = congress.candidate_newsletter_weights(legislators, membership).set_index("newsletter")
    assert w["weight"].sum() == pytest.approx(1.0)
    assert w.loc["ICT & Cybersecurity", "weight"] == pytest.approx(0.5)
    assert w.loc["Manufacturing", "weight"] == pytest.approx(0.5 / 3)


def test_energy_expands_to_both_energy_newsletters():
    """The source's "Energy" label has no single crosswalk equivalent."""
    from fec_newsletter import congress
    assert set(congress.parse_newsletters("Energy")) == {"Oil & Gas", "Utilities & Power"}


def test_fec_id_map_handles_multiple_ids_per_legislator(data):
    """A legislator who moved House -> Senate carries several FEC IDs; both must map."""
    from fec_newsletter import congress
    legislators = pd.DataFrame([{"bioguide": "C000127", "official_full": "Maria Cantwell",
                                 "first": "M", "last": "C",
                                 "fec": "['S8WA00194', 'H2WA01054']"}])
    m = congress.fec_id_map(legislators)
    assert set(m["cand_id"]) == {"S8WA00194", "H2WA01054"}
    assert m["bioguide"].nunique() == 1


def test_membership_is_filtered_to_the_latest_congress(data):
    """Including past congresses would count committee history as current seats.

    The live table happens to hold a single congress today, so the guard is that the
    filter collapses to exactly one — and that it is the newest, not an arbitrary one.
    """
    from fec_newsletter import congress
    raw = data["membership"].copy()
    current = congress.current_memberships(raw)
    assert current["congress"].nunique() == 1
    assert current["congress"].iloc[0] == pd.to_numeric(raw["congress"], errors="coerce").max()

    # With two congresses present, only the newer one survives.
    older = raw.head(3).copy()
    older["congress"] = "1"
    mixed = pd.concat([raw, older], ignore_index=True)
    assert len(congress.current_memberships(mixed)) == len(current)


# --- Enhanced crosswalk ---------------------------------------------------------------
# The enhanced crosswalk adds committee-keyed newsletters for committees CRP does not
# cover. The invariant that makes it safe to turn on is that it is purely ADDITIVE: it can
# give a newsletter to money that had none, and can never move money CRP already placed.

def _enhanced():
    return pd.DataFrame({
        "catcode":    ["A1000", None,        None,        None,        None],
        "cmte_id":    [None,    "C00000001", "C00000002", "C00000003", "C00000004"],
        "catorder":   [None] * 5,
        "catname":    ["Crops", "High PAC", "Medium PAC", "Low PAC", "No-letter PAC"],
        "industry":   [""] * 5,
        "sector":     [""] * 5,
        "newsletter": ["Agri-Food", "Defense", "Mining", "Oil & Gas", ""],
        "source":     ["crp"] + ["llm_inferred"] * 4,
        "confidence": ["high", "high", "medium", "low", "high"],
        "reasoning":  [""] * 5,
        "model":      [""] * 5,
    })


def test_inferred_lookup_applies_the_confidence_floor():
    keep = set(common.inferred_lookup(_enhanced())["cmte_id"])
    assert keep == {"C00000001", "C00000002"}, "low confidence and blank newsletter must go"


def test_inferred_lookup_keeps_only_committee_keyed_rows():
    """A CRP row reaching the committee pass would attribute the same money twice."""
    assert common.inferred_lookup(_enhanced())["catcode"].isna().all()


def test_enhanced_fills_only_gaps_crp_left():
    enhanced = _enhanced()
    df = pd.DataFrame({
        "sender_id": ["C00000001", "C00000002", "C00000003", "C00000009"],
        "catcode":   ["A1000",     "",          "",          ""],
        "amount":    [10.0, 20.0, 30.0, 40.0],
    })
    out = common.attach_newsletter(df, enhanced[enhanced["catcode"].notna()],
                                   common.Coverage(), enhanced=enhanced, id_col="sender_id")
    got = dict(zip(out["sender_id"], out["newsletter"]))
    # C00000001 has a real catcode, so CRP wins even though an inferred row exists for it.
    assert got["C00000001"] == "Agri-Food"
    assert got["C00000002"] == "Mining"          # gap filled from the inferred row
    assert "C00000003" not in got                # low confidence: still no newsletter
    assert "C00000009" not in got                # not in the crosswalk at all


def test_enhanced_marks_where_each_newsletter_came_from():
    enhanced = _enhanced()
    df = pd.DataFrame({
        "sender_id": ["C00000001", "C00000002"],
        "catcode":   ["A1000", ""],
        "amount":    [10.0, 20.0],
    })
    out = common.attach_newsletter(df, enhanced[enhanced["catcode"].notna()],
                                   common.Coverage(), enhanced=enhanced, id_col="sender_id")
    assert dict(zip(out["sender_id"], out["newsletter_source"])) == {
        "C00000001": "crp", "C00000002": "llm_inferred"}


def test_enhanced_crosswalk_is_additive_on_real_data(data):
    """Every CRP-attributed transaction must survive turning the enhanced crosswalk on,
    unchanged and in the same newsletter.

    Asserted per TRANSACTION, not per output group. A group total can legitimately fall:
    newly-attributed committees bring their refunds with them, so an added row is
    sometimes negative. What must never happen is a CRP-attributed transaction moving
    newsletter, changing amount, or disappearing -- the second pass only fills blanks.
    """
    original = config.USE_ENHANCED_CROSSWALK
    try:
        config.USE_ENHANCED_CROSSWALK = False
        base = pipelines.run_send(data)[0]
        config.USE_ENHANCED_CROSSWALK = True
        enh = pipelines.run_send(data)[0]
    finally:
        config.USE_ENHANCED_CROSSWALK = original

    def by_txn(df):
        return df.groupby(["sub_id", "newsletter"], observed=True)["amount"].sum()

    b, e = by_txn(base), by_txn(enh)
    missing = b.index.difference(e.index)
    assert missing.empty, f"{len(missing)} CRP-attributed transaction(s) lost"
    pd.testing.assert_series_equal(b, e.reindex(b.index), check_names=False)

    assert e.sum() >= b.sum()
    assert set(enh["newsletter"]) <= set(base["newsletter"]), \
        "an inferred row invented a newsletter CRP does not define"
    assert (enh.loc[enh["newsletter_source"] == "crp", "sub_id"].nunique()
            == base["sub_id"].nunique())


def test_inferred_rows_never_carry_a_catcode_newsletter(send):
    """An inferred attribution means the committee resolved to no CRP newsletter."""
    df = send[0]
    if "newsletter_source" not in df:
        pytest.skip("enhanced crosswalk not enabled")
    inferred = df[df["newsletter_source"] == "llm_inferred"]
    if inferred.empty:
        pytest.skip("no inferred rows in this run")
    assert inferred["catcode_source"].isin(["none", "crp_fec_cmte_mapping",
                                            "fec_committees_trim_mapped"]).all()
    assert not inferred["newsletter"].str.strip().eq("").any()
