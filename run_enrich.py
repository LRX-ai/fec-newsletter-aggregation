#!/usr/bin/env python3
"""Build enhanced_crp_l_crosswalk.csv — LLM coverage for committees CRP's crosswalk misses.

    python run_enrich.py --dry-run     # candidates + one sample prompt, no API calls
    python run_enrich.py --limit 50    # classify 50 committees (cheap smoke test)
    python run_enrich.py               # full run

Needs AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_KEY in .env (alongside the Postgres
credentials); AZURE_OPENAI_DEPLOYMENT_CHAT and AZURE_OPENAI_API_VERSION are optional.

Only committees that actually move money in the pipelines are classified — the other
~62,000 never carry a dollar, so paying to classify them would buy nothing.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import pandas as pd  # noqa: E402

from fec_newsletter import common, config, enrich, extract, pipelines, sources  # noqa: E402


def gather_candidates() -> tuple[pd.DataFrame, pd.DataFrame]:
    paths = {n: config.CACHE_DIR / c for n, (_, c) in config.EXTRACTS.items()}
    missing = [n for n, p in paths.items() if not p.exists()]
    if missing:
        raise SystemExit(f"missing cached extracts for {missing}; run `python run_send.py` first")
    data = extract.load(paths)

    cov = common.Coverage()
    oth = sources.from_oth(data["oth"], cov)
    pas2 = sources.from_pas2(data["pas2"], cov)
    txns = sources.dedupe(pd.concat([oth, pas2], ignore_index=True), cov)

    candidates = enrich.unresolved_committees(
        txns, data["committees"], data["crp_cmte_mapping"], data["crosswalk"],
        extra_names=enrich.counterparty_names(data["oth"], data["pas2"]),
    )
    return candidates, data["crosswalk"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="show candidates and a sample prompt, make no API calls")
    ap.add_argument("--limit", type=int, help="classify only the first N committees")
    ap.add_argument("--out", type=Path, default=config.REPO_ROOT / "enhanced_crp_l_crosswalk.csv")
    ap.add_argument("--checkpoint", type=Path,
                    default=config.REPO_ROOT / "cache" / "enrich_checkpoint.jsonl",
                    help="per-batch progress file; the run resumes from it by default")
    ap.add_argument("--restart", action="store_true",
                    help="ignore an existing checkpoint and classify everything again")
    ap.add_argument("--max-retries", type=int, default=4,
                    help="attempts per batch on rate limits and transient API errors")
    args = ap.parse_args()

    print("== Gathering candidates ==")
    candidates, crosswalk = gather_candidates()
    named = candidates["cmte_name"].notna() & (candidates["cmte_name"].fillna("").str.strip() != "")
    print(f"  {len(candidates):,} committees move money but resolve to no crosswalk row")
    print(f"  {named.sum():,} have a usable name; {(~named).sum():,} do not and are skipped")

    candidates = candidates[named].reset_index(drop=True)
    if args.limit:
        candidates = candidates.head(args.limit)

    vocab = enrich.Vocabulary.from_crosswalk(crosswalk)
    print(f"\n  vocabulary: {len(vocab.newsletters)} newsletters, "
          f"{len(vocab.sectors)} sectors, {len(vocab.industries)} industries")

    if args.dry_run:
        print("\n== Sample committees ==")
        print(candidates.head(8).to_string(index=False))
        print("\n== Sample request payload (first batch) ==")
        sample = [enrich._describe(r) for _, r in candidates.head(3).iterrows()]
        print(json.dumps(sample, indent=1))
        print(f"\n== Plan ==\n  {len(candidates):,} committees / {enrich.BATCH_SIZE} per batch "
              f"= {-(-len(candidates) // enrich.BATCH_SIZE):,} requests on Azure deployment "
              f"'{enrich.DEFAULT_DEPLOYMENT}' (override with AZURE_OPENAI_DEPLOYMENT_CHAT)")
        print("  Dry run: no API calls made, no file written.")
        return 0

    # ---- resume ----------------------------------------------------------------
    if args.restart and args.checkpoint.exists():
        args.checkpoint.unlink()
        print(f"\n  --restart: discarded {args.checkpoint.name}")
    done_rows, done_ids = enrich.load_checkpoint(args.checkpoint)
    if done_ids:
        print(f"\n  resuming: {len(done_ids):,} committees already classified in "
              f"{args.checkpoint.name}")
    todo = candidates[~candidates["cmte_id"].isin(done_ids)].reset_index(drop=True)

    failed = []
    if todo.empty:
        print("  nothing left to classify — writing output from the checkpoint")
        results = done_rows
        deployment = enrich.DEFAULT_DEPLOYMENT
    else:
        client, deployment = enrich.build_client()
        print(f"  Azure deployment: {deployment}")
        batches = [todo.iloc[i:i + enrich.BATCH_SIZE]
                   for i in range(0, len(todo), enrich.BATCH_SIZE)]
        print(f"\n== Classifying {len(todo):,} committees in {len(batches):,} batches ==")
        print("   Ctrl-C is safe: each batch is flushed to the checkpoint before the next "
              "request.\n")

        # Ctrl-C sets a flag rather than tearing the process down mid-request, so the
        # in-flight batch finishes and lands in the checkpoint before we stop.
        stopping = {"now": False}

        def _stop(signum, frame):
            if stopping["now"]:                    # second Ctrl-C: go now
                raise KeyboardInterrupt
            stopping["now"] = True
            print("\n  interrupt received — finishing this batch, then stopping cleanly")

        signal.signal(signal.SIGINT, _stop)

        results, interrupted = list(done_rows), False
        for i, batch in enumerate(batches, 1):
            out, err = None, None
            for attempt in range(1, args.max_retries + 1):
                try:
                    out = enrich.classify_batch(client, batch, vocab, deployment)
                    break
                except enrich.TransientError as exc:
                    err = exc
                    if attempt == args.max_retries:
                        break
                    wait = min(2 ** attempt, 30)
                    print(f"  batch {i}/{len(batches)}: {type(exc).__name__}, retry "
                          f"{attempt}/{args.max_retries - 1} in {wait}s")
                    time.sleep(wait)

            if out:
                enrich.append_checkpoint(args.checkpoint, out)   # durable before next call
                results.extend(out)
                print(f"  batch {i}/{len(batches)}: {len(out)} classified"
                      f"  ({len(results):,}/{len(candidates):,} total)", end="\r", flush=True)
            else:
                failed.append({"batch": i, "ids": batch["cmte_id"].tolist(),
                               "error": str(err) if err else "unusable response"})
                print(f"  batch {i}/{len(batches)}: FAILED — {err or 'unusable response'}")

            if stopping["now"]:
                interrupted = True
                break

        print()
        if failed:
            fp = args.checkpoint.with_suffix(".failed.json")
            fp.write_text(json.dumps(failed, indent=1))
            print(f"  {len(failed)} batch(es) failed; ids written to {fp.name}. "
                  f"Re-run to retry them — classified committees are skipped.")
        if interrupted:
            print(f"  stopped early: {len(results):,} of {len(candidates):,} classified. "
                  f"Re-run to continue from here.")

    inferred = enrich.to_crosswalk_rows(results, candidates, model=deployment)
    enhanced = enrich.merge(crosswalk, inferred)
    enhanced.to_csv(args.out, index=False)
    print(f"\n  wrote -> {args.out}")

    print("\n== Results ==")
    print(f"  CRP rows passed through : {(enhanced['source'] == 'crp').sum():,}")
    print(f"  committees classified   : {len(inferred):,}")
    if failed:
        print(f"  batches failed          : {len(failed):,}")
    if len(inferred):
        with_nl = inferred["newsletter"].str.strip() != ""
        print(f"  assigned a newsletter   : {with_nl.sum():,} ({with_nl.mean() * 100:.0f}%)")
        print(f"  no newsletter (expected): {(~with_nl).sum():,}")
        print("\n  by confidence:")
        print(inferred["confidence"].value_counts().to_string())
        print("\n  newsletters assigned:")
        counts = inferred.loc[with_nl, "newsletter"].value_counts()
        print(counts.head(20).to_string() if len(counts) else "   (none)")
        low = (inferred["confidence"] == "low") & with_nl
        if low.any():
            print(f"\n[warn] {low.sum()} low-confidence rows carry a newsletter — "
                  f"excluded from enrich.usable() by default. Review before promoting them.")

    print("\n[note] Inferred rows are committee-keyed (cmte_id), not catcode-keyed. CRP rows "
          "are unchanged. Review the reasoning column before wiring this into the pipelines.")
    print(f"[note] Progress is kept in {args.checkpoint.name}; delete it or pass --restart to "
          f"classify from scratch.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
