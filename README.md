# FEC Newsletter Quarterly Aggregation

Quarterly aggregates of FEC money attributed to QSA newsletters. Two pipelines, organised
by **what the number means** rather than which file it came from.

| | `run_send.py` | `run_receive.py` |
|---|---|---|
| Question | trends in money this industry **put out** | trends in money reaching this **policy area** |
| Newsletter from | the **sender's** catcode | the recipient **candidate's congressional committee seats** |
| Broken down by | recipient | sender |
| Sources | `fec_pac_to_cand_current` + `fec_pac_to_pac` | candidate money from both |
| Output | `out/send_quarterly.csv` | `out/receive_quarterly.csv` |
| Result | 60,271 rows · 19 newsletters · **$1,003,253,305** | 135,309 rows · 18 newsletters · **$358,467,209** |

Both cover **2025Q1–2026Q2**.

Receive does **not** use a catcode: candidates don't have one. Verified — **0** of 728,836
PAS2 rows whose recipient is a committee resolve to a non-blank newsletter; they're all
`Z*` candidate-committee catcodes. Instead the industry signal is *which congressional
committees the recipient sits on*, so money to a member of House Agriculture is money
reaching agricultural policy.

**The two outputs are not two halves of one total — do not union them.** They attribute the
same candidate money from opposite ends: a `24E` independent expenditure backing a member of
House Agriculture is *the paying PAC's industry sent it* in one file and *Agri-Food policy
received it* in the other.

## How receive attributes (`congress.py`)

Three hops, each of which loses rows:

```
FEC CAND_ID  ->  bioguide        c_github_legislators_current_person.fec
bioguide     ->  committee code  c_github_committee_membership_current  (48 top-level)
committee    ->  newsletter(s)   data/congressional_committee_newsletter.csv
```

- **`fec` is a stringified Python list** (`"['S8WA00194', 'H2WA01054']"`) — a legislator can
  hold several FEC IDs across a career. Parse it; don't join on it raw.
- **Membership is filtered to the latest congress.** The table spans several; including all
  would count committee *history* as current seats.
- **Only top-level committees** (4-char codes). The 179 subcommittees are finer-grained than
  the newsletter scheme and would fragment attribution.
- **`data/congressional_committee_newsletter.csv` is hand-drafted and human-reviewed** — 48
  rows with a rationale column, derived from each committee's published jurisdiction text.
  35 map to at least one newsletter; 13 are deliberately blank (Appropriations, Budget,
  Rules, Ethics, Oversight — government-wide or procedural, no industry). This file is the
  receive pipeline's entire industry signal; treat edits to it as edits to the deliverable.

**Weighting is an even split, and totals are additive.** Each of a member's *mapped*
committees gets an equal share, then a committee's share divides equally among its
newsletters. A member on Agriculture (1 newsletter) and Energy & Commerce (5) splits 50/50
between the committees — not 1/6 vs 5/6, which would let a broad-jurisdiction committee
dominate purely by being broad. Unmapped committees are dropped *before* normalising, so
money redistributes across the member's industry-relevant seats instead of evaporating.
Weights sum to 1.0 per transaction, asserted by `test_receive_split_is_additive`.

### Receive is incumbents-only, and that is most of the money

Attribution by committee seat only works for people who hold one:

| | |
|---|---:|
| Money to candidates (all time) | $5,806,738,122 |
| …reaching a sitting member | **$1,720,934,330 (29.6%)** |
| Excluded — challengers, open seats, defeated incumbents | $4,085,803,792 (70.4%) |

538 current legislators, all with an FEC ID; 489 sit on a mapped committee (43 sit only on
unmapped ones). The runner prints this every run so the limit is never invisible.

- `docs/transaction_type_map.md` — how every `TRANSACTION_TP` is treated, generated
- `PLAN.md` — design and decisions
- `handoff.md` — the original handoff (pre-dates DB inspection; several claims are wrong,
  see *Corrections* in `PLAN.md`)

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env    # POSTGRES_USER, POSTGRES_PASSWORD, PGHOST
```

## Run

```bash
python run_send.py                       # money sent by each industry
python run_receive.py                    # money received by each industry
python run_send.py --refresh             # re-pull from Postgres first
python -m pytest tests/                  # 59 stage assertions
python tools/build_transaction_map.py    # regenerate the transaction type map
python run_enrich.py --dry-run           # preview the crosswalk enrichment (no API calls)
```

Extracts are cached deliberately. The FEC tables carry **no indexes** and the server dropped
connections twice under moderate server-side joins, so both pipelines extract once and
transform locally in pandas. Don't move the aggregation back into SQL. The pipelines share
lookup extracts by cache filename, so running one warms the other.

## Three things to know before using the numbers

**Never sum across `flow_type`.** A direct contribution lands in the recipient's account; an
independent expenditure is money spent *about* a candidate that they never receive and
cannot coordinate with. `oppose` money is spent *against* its target and is never netted
against `support`. Loans and repayments are borrowed money moving, not industry money
backing a cause. Send-pipeline breakdown:

| flow_type | stance | amount |
|---|---|---:|
| direct_contribution | support | $514,073,346 |
| committee_transfer | support | $284,308,395 |
| independent_expenditure | support | $129,788,609 |
| independent_expenditure | oppose | $73,913,361 |
| communication_cost | support | $1,169,594 |

**Totals are not additive across newsletters — in `send` only.** 38 catcodes map to several
newsletters (`Social Issues, Healthcare`) and each gets the full amount; set
`config.MULTI_NEWSLETTER = "split"` to make the grand total additive. **Receive is already
additive** — it splits evenly rather than duplicating.

**The send pipeline's two sources have different spans**, so its window defaults to the
overlap. `fec_pac_to_pac` is a single cycle (2025Q1+) while PAS2 reaches back to 2023Q1.
Widening `config.SEND_QUARTER_START` gains eight quarters that contain *candidate money
only* — and because 2024 was an election year with heavy IE spending, those quarters are
actually **larger** than the 2025–26 ones, so you cannot separate the cycle effect from the
coverage effect by eye. The runner prints per-quarter source composition and warns when it
is not uniform.

## Deduplication — the part that matters most

The send pipeline unions two files that overlap heavily. Three distinct duplicate paths,
collapsed in two passes:

1. **The same row in both files** — 139,744 rows / **$828M**. `SUB_ID` is FEC's global row
   key and is shared across tables, so this pass is *exact*, not heuristic.
2. **Both sides of an OTH transfer** — the payer files an outflow, the recipient an inflow,
   under different `SUB_ID`s. Normalising to `(sender, recip)` first makes them identical.
3. **The same contribution in both files under different `SUB_ID`s** — caught by the
   `(sender, recip, date, amount)` fingerprint. 244,707 rows / **$651M**.

Without these, the send total would be roughly **$1.48B too high**.

## Two joins that fail silently

- **Catcodes must be uppercased on both sides.** 211 committees store lowercase catcodes
  (`j2400`, `z1200`) that fail the crosswalk join unnormalised.
- **`crp_fec_cmte_mapping` fills catcode gaps** that `fec_committees_trim_mapped` leaves
  null (9,779 committees), but is *not* authoritative — the two disagree on a large
  minority of shared committees, so `catcode_source` records which was used.

## Verification

`config.SEND_EXPECTED` / `RECEIVE_EXPECTED` pin every headline number; both runners exit
non-zero on >2% drift.

**Direction verified against FEC's own records**, matched by `SUB_ID` — the check that
catches a reversed sender/recipient, which otherwise looks plausible in aggregate:

- *OTH outflow* — `SUB_ID 4070820261533622675`: FAIRSHAKE (`C00835959`) → PROTECT PROGRESS
  (`C00848440`), $9,000,000 on 2026-05-05. FEC Schedule B: identical.
- *OTH inflow* — `SUB_ID 4030120261360240970`: 1199 SEIU (`C00348540`) → SEIU COPE
  (`C00004036`), $1,812,870 on 2025-11-20. FEC Schedule A: identical (FEC shows $1,812,871;
  the bulk file truncates cents).
- *PAS2* — verified across **all 725,939** `24K` rows rather than by sample: the PAS2 `NAME`
  field matches the `OTHER_ID` committee 87.9% of the time and the filer 0.0%.

### A direction bug this caught

`30K`/`31K`/`32K` are Convention/HQ/Recount Account **"receipt from registered filer"** —
receipts, so the filer is the *recipient*. They were initially grouped with the disbursement
codes on the strength of the shared `K` suffix with `24K` "Contribution **made** to
nonaffiliated committee", which swapped sender and recipient on 824 rows worth $30.1M.
**Classify on the description's verb, not the shape of the code.** Guarded by
`test_receipt_codes_are_classified_as_inflows`.

## Crosswalk enrichment (`run_enrich.py`)

**2,933 committees move money but resolve to no crosswalk row** — $434M on the send side,
$691M measured before receive was re-architected — dropped today. `run_enrich.py` asks an
Azure OpenAI deployment to classify them from committee names and FEC metadata, and writes
`enhanced_crp_l_crosswalk.csv`. Note this now benefits the **send** pipeline only: receive
no longer uses catcodes at all, so enrichment cannot widen its coverage.

```bash
python run_enrich.py --dry-run    # candidates + sample prompt, no API calls
python run_enrich.py --limit 50   # cheap smoke test
python run_enrich.py              # 2,390 committees / 96 requests
```

Needs `AZURE_OPENAI_ENDPOINT` and `AZURE_OPENAI_KEY` in `.env`
(`AZURE_OPENAI_DEPLOYMENT_CHAT` and `AZURE_OPENAI_API_VERSION` are optional). Note the
`model` argument is the Azure **deployment name**, not a model name. Only committees that
*move money* are classified — the other ~62,000 never carry a dollar. 543 of the 2,933 have
no name anywhere and are skipped rather than guessed at.

Four properties make the output safe to join against money, each covered by a test:

- **"No newsletter" is the default answer, not a fallback.** Most unmapped committees are
  party, candidate, or leadership PACs that CRP deliberately excludes. The schema makes an
  empty newsletter list first-class and the prompt names it as expected.
- **The vocabulary is closed** — newsletters, sectors, and industries are enums built from
  the live crosswalk, so a hallucinated category cannot enter the join.
- **CRP always wins.** Real crosswalk rows pass through byte-identical; inferences only
  fill committees CRP has nothing for, and are keyed by `cmte_id` rather than `catcode`.
- **Low confidence never attributes.** Every row ships with confidence, one-line reasoning,
  and the model that produced it; `enrich.usable()` excludes anything below `medium`.

### Wired into the send pipeline

The enriched crosswalk is uploaded to its own table and read back as an extract:

```bash
python tools/upload_enhanced_crosswalk.py --dry-run   # validate the CSV, write nothing
python tools/upload_enhanced_crosswalk.py             # -> usa.crp_l_crosswalk_enhanced
python run_send.py                                    # picks it up via the new extract
```

`usa.crp_l_crosswalk` is never written to. The new table holds both row kinds and a CHECK
constraint keeps them apart: a CRP row has a `catcode` and a null `cmte_id`, an inferred row
the reverse.

`common.attach_newsletter` joins the catcode rows exactly as before, then makes a **second,
committee-keyed pass over only the rows the first left without a newsletter**. So the join is
purely additive — it can give a newsletter to money that had none, never move money CRP
placed. `newsletter_source` (`crp` / `llm_inferred`) records which pass produced each row, and
the coverage ledger reports what the second pass matched.

Two switches in `config.py`: `USE_ENHANCED_CROSSWALK` (`True`) turns the second pass off for a
directly comparable run, and `MIN_CROSSWALK_CONFIDENCE` (`"medium"`, i.e. everything except
`low`) sets the floor. Receive is unaffected — it attributes by congressional committee seat
and reads no crosswalk at all.

Measured effect on the send pipeline, whole window:

| | CRP only | + enriched | delta |
|---|---|---|---|
| output rows | 60,273 | 60,636 | +363 |
| total | $1,003,253,305 | $1,007,073,369 | **+$3,820,064 (+0.38%)** |
| counterparties | 4,021 | 4,041 | +20 |

Only 84 of the 2,390 classified committees carry a newsletter at all, which is why the delta
is small: the other 2,306 are correctly classified as belonging to no industry newsletter.
Every low-confidence row happens to be one of those, so the confidence floor currently
excludes nothing the blank-newsletter filter did not already exclude.

The `reasoning` column is still worth reading before treating any single inferred
attribution as fact.

## Open decisions

Each is a one-line config change:

1. **`MULTI_NEWSLETTER`** (`"duplicate"`) — pending verification of `crp_l_crosswalk.csv`.
2. **`DROP_SELF_TRANSFERS`** (`False`) — 55 rows / $2,540,274 where a committee names itself
   as the other party. Real dollars, but not a transfer between *distinct* parties.
3. **`INCLUDE_FLOW_TYPES`** (`None` = all) — set to `{"direct_contribution"}` to report
   donations only.
4. **`SEND_QUARTER_START`** (`"2025Q1"`) — widen to `"2023Q1"` only with the composition
   caveat above firmly in mind.
5. **Social Issues leads both pipelines** — driven by catcode `J1000` (General Ideological)
   and the `LB100` building-trades mapping. A crosswalk question, not a pipeline one. Its
   dominance is far less extreme in send ($363M of $1.0B). It does not affect receive,
   which now attributes from congressional committee seats rather than catcodes.
6. **Negatives are netted in** — driven by WinRed (`C00694323`), which alone files 94.6% of
   the negative rows in PAS2 as conduit earmark reversals. 90.9% pair with a positive
   between the same committee and candidate; net effect -0.78%.

## Known limits

- **Six quarters.** Extending needs `fec_pac_to_pac_full` (2022Q1–2025Q2, dates stored as
  `YYYY-MM-DD` not `MMDDYYYY`) plus an overlap check on `SUB_ID`.
- **5,634 rows ($127M) are dropped for having no `TRANSACTION_DT`.** `RPT_TP` is not an
  acceptable substitute — it describes the filing window, not when the money moved.
- **PAS2 2023Q1–Q2 look under-loaded** (1,446 and 2,362 rows vs 47,158 in 2023Q3), which
  matters only if you widen the send window.
- **Catcodes are a current snapshot**, so reclassifying a committee retroactively rewrites
  historical totals.
- **The fingerprint dedupe is a heuristic** — two genuinely distinct same-day transfers of
  the same amount between the same pair would collapse into one.
