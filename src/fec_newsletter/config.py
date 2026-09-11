"""Configuration for the FEC newsletter aggregations.

Two pipelines, organised by what the number MEANS rather than which file it came from:

* **send**    (`run_send.py`)    -- newsletter from the SENDER's catcode.
                                    "trends in money this industry put out"
                                    Sources: fec_pac_to_cand_current + fec_pac_to_pac
* **receive** (`run_receive.py`) -- newsletter from the recipient CANDIDATE'S
                                    CONGRESSIONAL COMMITTEE SEATS.
                                    "trends in money reaching this policy area"
                                    Source: candidate-directed money (PAS2 + OTH)

The two pipelines attribute from opposite ends of the same candidate money, but receive
does NOT use a catcode: a candidate has no catcode. Its industry signal is which
congressional committees the recipient sits on, joined FEC CAND_ID -> bioguide ->
committee -> newsletter. Only sitting members appear in that source, so receive covers
incumbents only -- see RECEIVE_* below.

Every value a product decision could plausibly change lives here.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SQL_DIR = REPO_ROOT / "sql"
CACHE_DIR = REPO_ROOT / "cache"
OUT_DIR = REPO_ROOT / "out"

DB_NAME = "postgres"

OTH_TABLE = "fec_pac_to_pac"
PAS2_TABLE = "fec_pac_to_cand_current"

# --- Extracts -------------------------------------------------------------------------
# Cached on disk because the FEC tables carry no indexes and the server dropped connections
# twice under moderate server-side joins. Re-pull only with --refresh.
EXTRACTS = {
    "oth": ("extract_transactions.sql", "transactions.csv"),
    "pas2": ("extract_pac_to_cand.sql", "pac_to_cand.csv"),
    "committees": ("extract_committees.sql", "committees.csv"),
    "crp_cmte_mapping": ("extract_crp_cmte_mapping.sql", "crp_cmte_mapping.csv"),
    "candidates": ("extract_candidates.sql", "candidates.csv"),
    "crosswalk": ("extract_crosswalk.sql", "crosswalk.csv"),
    "crosswalk_enhanced": ("extract_crosswalk_enhanced.sql", "crosswalk_enhanced.csv"),
    "legislators": ("extract_legislators.sql", "legislators.csv"),
    "membership": ("extract_committee_membership.sql", "committee_membership.csv"),
    "terms": ("extract_legislator_terms.sql", "legislator_terms.csv"),
    "congress_committees": ("extract_congress_committees.sql", "congress_committees.csv"),
}

# Receive now needs PAS2 (that is where candidate money lives) and the congress tables.
# It no longer needs crp_cmte_mapping, since it does not attribute from a catcode at all.
RECEIVE_EXTRACTS = EXTRACTS

# Authored by the team: 184 congressional SUBCOMMITTEES -> newsletters. This is the receive
# pipeline's entire industry signal, so treat edits to it as edits to the deliverable.
#
# It uses its own shorthand vocabulary ("Defence", "ICT", "Agri", "Energy"), which
# congress.LABEL_ALIASES maps onto the 19 crosswalk newsletters. Add an alias when adding a
# label — congress.unknown_labels() lists anything unrecognised, and unrecognised labels
# attribute nothing.
COMMITTEE_NEWSLETTER_MAP = REPO_ROOT / "congress_cmte_crosswalk.csv"

# Which committees matter to a channel, and how much -- the reviewed ranking. The
# crosswalk decides where a dollar is counted; this decides whose seat is worth naming.
COMMITTEE_RANKS = REPO_ROOT / "newsletter_committee_ranks.csv"

# --- Transaction direction (OTH file only) --------------------------------------------
# The OTH file is bidirectional: TRANSACTION_TP decides whether the filer sent or received.
# Both parties file, so the same dollar appears twice -- normalising to (sender, recip)
# and then deduping is what makes combining them safe.
#
# CLASSIFY ON THE DESCRIPTION'S VERB, NOT THE SHAPE OF THE CODE. 30K/31K/32K are
# Convention/HQ/Recount Account "receipt from registered filer" -- RECEIPTS -- but share a
# K suffix with 24K "Contribution MADE to nonaffiliated committee". Filing them as
# outflows swapped sender and recipient on 824 rows worth $30.1M.

# CMTE_ID sent the money, OTHER_ID received it. All read "made" / "out" / "to".
OUTFLOW_TYPES = frozenset({"24K", "24G", "24Z", "22Z", "20", "20C", "20F", "20G", "20R"})

# OTHER_ID sent the money, CMTE_ID received it. All read "received" / "receipt" / "in" / "from".
INFLOW_TYPES = frozenset(
    {"13", "15Z", "16C", "16F", "16G", "16R", "17R", "17Z", "18G", "18K", "18U",
     "30G", "30K", "31G", "31K", "32G", "32K"}
)

# Candidate-directed disbursements. CMTE_ID pays; OTHER_ID is a CANDIDATE, so these can
# never carry a recipient catcode -- useless to the receive pipeline, fine for send.
# ~98% of them are also in PAS2, and the cross-source dedupe removes those.
OTH_CANDIDATE_TYPES = frozenset({"24A", "24C", "24E", "24F", "24N"})

# Excluded from both pipelines:
#   *J memo codes (10J 11J 15J 18J 19J 30J 31J 32J, and the F variants) -- joint-fundraising
#       percentage allocations, also reported as their own transaction elsewhere.
#       15J alone is 96.5% of the OTH table and is filtered in SQL at extraction.
#   40 41 42 40Z 41Z 42Z -- convention/HQ disbursements with a null OTHER_ID.
#   24R -- recount disbursement, not a contribution.

# --- Flow types -----------------------------------------------------------------------
# What KIND of money a transaction is. NEVER summed across: a direct contribution lands in
# the recipient's account, while an independent expenditure is money spent ABOUT a
# candidate that they never receive and cannot coordinate with. IEs are ~2.7x direct
# contributions, so conflating them would swamp the real donation figures. `oppose` money
# is spent AGAINST its target and is never netted against `support`.
FLOW_TYPES = {
    "24K": ("direct_contribution", "support"),
    "24Z": ("direct_contribution", "support"),
    "24C": ("coordinated_expenditure", "support"),
    "24E": ("independent_expenditure", "support"),
    "24A": ("independent_expenditure", "oppose"),
    "24F": ("communication_cost", "support"),
    "24N": ("communication_cost", "oppose"),
    # Loans and loan repayments -- borrowed money moving, not industry money backing a
    # cause. 16C is a candidate self-funding their own campaign; 20C is that campaign
    # paying them back. Tagged separately so they can never merge into contribution or
    # transfer totals.
    #
    # These contribute $0 today, but only by accident: $362.5M across 4,890 rows reaches
    # zero because the committee end has no catcode, not because anything excludes it.
    # run_enrich.py targets exactly those committees, so without this tag a successful
    # enrichment run would silently move loan money into newsletter totals.
    "16C": ("loan", "neutral"),
    "16F": ("loan", "neutral"),
    "16G": ("loan", "neutral"),
    "16R": ("loan", "neutral"),
    "20C": ("loan_repayment", "neutral"),
    "20F": ("loan_repayment", "neutral"),
    "20G": ("loan_repayment", "neutral"),
    "20R": ("loan_repayment", "neutral"),
}
# OTH types absent from FLOW_TYPES (transfers, loans, refunds between committees) fall
# through to flow_type='committee_transfer'.

# None keeps every flow type. Set e.g. {"direct_contribution"} to restrict an output.
INCLUDE_FLOW_TYPES = None

# --- Self-transfers -------------------------------------------------------------------
# 456 OTH rows have CMTE_ID == OTHER_ID: a committee naming ITSELF as the other party. This
# is in the source data, not an artefact of the direction logic -- typically a committee
# moving money between its own accounts. Kept because they are genuinely reported dollars;
# set True if the deliverable means money moving between DISTINCT parties.
DROP_SELF_TRANSFERS = False

# --- Newsletter attribution -----------------------------------------------------------
# 38 catcodes map to several newsletters as one comma-separated string.
#   "duplicate" -- full amount to each. Per-newsletter totals correct, grand total
#                  double-counts, so output is NON-ADDITIVE across newsletters.
#   "split"     -- amount divided evenly. Grand total correct, each newsletter
#                  under-reports the dollars touching its industry.
#   "raw"       -- keep the combined string as its own newsletter value.
# Pending Natalie's verification of crp_l_crosswalk.csv.
MULTI_NEWSLETTER = "duplicate"

# --- Enhanced crosswalk ---------------------------------------------------------------
# CRP's crosswalk covers catcodes; 2,933 committees that move money carry no catcode it
# knows. usa.crp_l_crosswalk_enhanced adds an LLM-inferred newsletter for those, keyed by
# CMTE_ID rather than catcode (run_enrich.py builds it, tools/upload_enhanced_crosswalk.py
# uploads it).
#
# When True the send pipeline reads that table and uses an inferred row ONLY where the
# committee resolved to no CRP crosswalk row. CRP always wins, so turning this on can add
# dollars to a newsletter but can never move a dollar CRP already placed.
#
# Set False to fall back to usa.crp_l_crosswalk alone -- the two runs are directly
# comparable, which is how the added dollars are audited.
USE_ENHANCED_CROSSWALK = True

# Inferred rows below this are kept in the table for review but never attribute money.
# "medium" means: everything except confidence='low'.
MIN_CROSSWALK_CONFIDENCE = "medium"
CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}

# --- Quarter windows ------------------------------------------------------------------
# OTH covers 2025Q1-2026Q2 (single cycle). PAS2 covers 2023Q1-2026Q2.
#
# The send pipeline unions both, so its sources have DIFFERENT SPANS. Defaulting it to the
# common window keeps the trend line honest: widening to 2023Q1 gives eight more quarters,
# but those quarters contain candidate money only, so the series would step up sharply at
# 2025Q1 purely because the OTH source switches on. Widen deliberately, and read the
# per-quarter source composition the runner prints before trusting the shape.
SEND_QUARTER_START = "2025Q1"
SEND_QUARTER_END = "2026Q2"

RECEIVE_QUARTER_START = "2025Q1"
RECEIVE_QUARTER_END = "2026Q2"

# --- Default build period -------------------------------------------------------------
# Monthly is the default build mode: an edition is read monthly, and a quarter is three
# months of hindsight by the time it closes. A tool given no --period builds this one.
#
# Derived from the window above rather than written out again, so the window moves in ONE
# place. It is deliberately the last COMPLETE month of that window and not "the latest
# month with any data": the extracts carry a thin tail past the window -- 57 transactions
# in 2026-07 and a single one in 2026-11 -- and chasing the maximum date would build a
# near-empty edition that looks like a collapse in giving.
def _last_month_of_quarter(q: str) -> str:
    year, quarter = int(q[:4]), int(q[-1])
    return f"{year}-{3 * quarter:02d}"


DEFAULT_PERIOD = _last_month_of_quarter(RECEIVE_QUARTER_END)

# --- Expected results -----------------------------------------------------------------
# Measured by verified runs. The runners warn on material divergence; treat a mismatch as a
# pipeline bug, not an improvement. Update only alongside a deliberate, understood change.
EXPECTED_TOLERANCE_PCT = 2.0

SEND_EXPECTED = {
    "output_rows": 60_271,
    "newsletters": 19,
    "counterparties": 4_021,
    "total_amount": 1_003_253_305,
}
# Receive was re-architected on 2026-08-13: attribution moved from the recipient
# committee's catcode to the recipient CANDIDATE's congressional committee seats, so these
# numbers are not comparable to the earlier catcode-based ones.
RECEIVE_EXPECTED = {
    "output_rows": 162_029,
    "newsletters": 19,
    "counterparties": 3_745,
    "total_amount": 389_519_200,
}
