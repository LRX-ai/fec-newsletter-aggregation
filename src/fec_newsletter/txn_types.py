"""How every FEC TRANSACTION_TP code is treated, and why.

`DESCRIPTIONS` is verbatim from FEC's published code list:
https://www.fec.gov/campaign-finance-data/transaction-type-code-descriptions/

The direction sets live in `config`; this module only explains and classifies them, so the
generated map can never disagree with what the pipeline actually does.

Reading the descriptions is what caught the one direction bug we shipped: 30K/31K/32K are
Convention/HQ/Recount Account "receipt from registered filer" -- RECEIPTS, so the filer is
the recipient -- but they share a K suffix with 24K "Contribution MADE to nonaffiliated
committee" and had been grouped with the disbursements. When adding a code, classify it on
the description's verb ("made"/"out"/"to" vs "received"/"receipt"/"in"/"from"), never on
the shape of the code.
"""

from __future__ import annotations

from . import config

DESCRIPTIONS = {
    "11J": "Memo - Recipient committee's percentage of contribution from Native American Tribe given to joint fundraising committee",
    "13": "Inaugural donation accepted",
    "15J": "Memo - Recipient committee's percentage of contribution from an individual, partnership or limited liability company given to joint fundraising committee",
    "15Z": "In-kind contribution received from registered filer",
    "16C": "Loan received from the candidate",
    "16F": "Loan received from bank",
    "16G": "Loan from individual",
    "17R": "Contribution refund received from registered entity",
    "17Z": "Refund/Rebate/Return from candidate or committee",
    "18G": "Transfer in from affiliated committee",
    "18J": "Memo - Recipient committee's percentage of contribution from a registered committee given to joint fundraising committee",
    "18K": "Contribution received from registered filer",
    "18U": "Contribution received from unregistered committee",
    "20": 'Nonfederal disbursement - nonfederal party "soft money" accounts (1991-2002)',
    "20C": "Loan repayment made to candidate",
    "20F": "Loan repayment made to banks",
    "20G": "Loan repayment made to individual",
    "20R": "Loan repayment made to registered filer",
    "22Z": "Contribution refund to candidate or committee",
    "24A": "Independent expenditure opposing election of candidate",
    "24C": "Coordinated party expenditure",
    "24E": "Independent expenditure advocating election of candidate",
    "24F": "Communication cost for candidate (only for Form 7 filer)",
    "24G": "Transfer out to affiliated committee",
    "24K": "Contribution made to nonaffiliated committee",
    "24N": "Communication cost against candidate (only for Form 7 filer)",
    "24R": "Election recount disbursement",
    "24Z": "In-kind contribution made to registered filer",
    "30F": "Convention Account - Memo - Recipient committee's percentage of contributions from a registered committee given to joint fundraising committee",
    "30G": "Convention Account - transfer in from affiliated committee",
    "30J": "Convention Account - Memo - Recipient committee's percentage of contributions from an individual, partnership or limited liability company given to joint fundraising committee",
    "30K": "Convention Account receipt from registered filer",
    "31F": "Headquarters Account - Memo - Recipient committee's percentage of contributions from a registered committee given to joint fundraising committee",
    "31G": "Headquarters Account - transfer in from affiliated committee",
    "31J": "Headquarters Account - Memo - Recipient committee's percentage of contributions from an individual, partnership or limited liability company given to joint fundraising committee",
    "31K": "Headquarters Account receipt from registered filer",
    "32F": "Recount Account - Memo - Recipient committee's percentage of contributions from a registered committee given to joint fundraising committee",
    "32G": "Recount Account - transfer in from affiliated committee",
    "32J": "Recount Account - Memo - Recipient committee's percentage of contributions from an individual, partnership or limited liability company given to joint fundraising committee",
    "32K": "Recount Account receipt from registered filer",
    "40": "Convention Account disbursement",
    "41": "Headquarters Account disbursement",
    "42": "Recount Account disbursement",
    "42Z": "Recount Account refund to registered filer",
}

# Why an excluded code is excluded. Grouped so the reason is a category, not a one-off.
MEMO_JOINT_FUNDRAISING = (
    "Memo row: a joint-fundraising committee's percentage allocation of a receipt that is "
    "also reported as its own transaction elsewhere. Including it double-counts."
)
CANDIDATE_DIRECTED = (
    "OTHER_ID is a CANDIDATE, not a committee. Feeds BOTH pipelines: send attributes from "
    "the paying committee's catcode, and receive attributes from the recipient candidate's "
    "congressional committee seats. 98.2% of these rows (matched by SUB_ID) also appear in "
    "fec_pac_to_cand_current, which supplies most candidate money; the cross-source dedupe "
    "removes the overlap."
)
NO_COUNTERPARTY = (
    "OTHER_ID is null on these rows -- the counterparty is an individual, a vendor or an "
    "unregistered entity, so neither side of the transfer can be resolved."
)

EXCLUSION_REASONS = {
    "11J": MEMO_JOINT_FUNDRAISING,
    "15J": MEMO_JOINT_FUNDRAISING + " 96.5% of the table; excluded in SQL at extraction.",
    "18J": MEMO_JOINT_FUNDRAISING,
    "30F": MEMO_JOINT_FUNDRAISING,
    "30J": MEMO_JOINT_FUNDRAISING,
    "31F": MEMO_JOINT_FUNDRAISING,
    "31J": MEMO_JOINT_FUNDRAISING,
    "32F": MEMO_JOINT_FUNDRAISING,
    "32J": MEMO_JOINT_FUNDRAISING,
    "24A": CANDIDATE_DIRECTED,
    "24C": CANDIDATE_DIRECTED,
    "24E": CANDIDATE_DIRECTED,
    "24F": CANDIDATE_DIRECTED,
    "24N": CANDIDATE_DIRECTED,
    "40": NO_COUNTERPARTY,
    "41": NO_COUNTERPARTY,
    "42": NO_COUNTERPARTY,
    "24R": "Recount disbursement, not a contribution to a committee.",
    "42Z": "Recount-account refund; negligible volume and not a contribution.",
}


def classify(tp: str) -> tuple[str, str]:
    """Return (treatment, reason) for a transaction type code.

    treatment is one of: 'outflow', 'inflow', 'candidate-directed', 'excluded'.
    """
    if tp in config.OUTFLOW_TYPES:
        return "outflow", "CMTE_ID sent -> sender=CMTE_ID, recipient=OTHER_ID"
    if tp in config.INFLOW_TYPES:
        return "inflow", "CMTE_ID received -> sender=OTHER_ID, recipient=CMTE_ID"
    if tp in config.OTH_CANDIDATE_TYPES:
        return "candidate-directed", CANDIDATE_DIRECTED
    return "excluded", EXCLUSION_REASONS.get(
        tp, "Not classified. Unrecognised codes are excluded by default -- review before use."
    )


def pipelines_using(tp: str) -> str:
    """Which pipeline(s) consume a code.

    Inverted on 2026-08-13, when receive moved from catcode attribution to congressional
    committee seats:

    * **Candidate-directed** codes now feed BOTH. Send reads the paying committee's
      catcode; receive reads the recipient candidate's committee seats. (PAS2 supplies most
      of this money -- these are just the OTH-file codes that carry it.)
    * **Committee-to-committee** codes are now SEND ONLY. Receive attributes from a
      candidate, and these rows have no candidate on either end.
    """
    treatment, _ = classify(tp)
    if treatment == "candidate-directed":
        return "send + receive"
    if treatment in ("outflow", "inflow"):
        return "send only"
    return "neither"
