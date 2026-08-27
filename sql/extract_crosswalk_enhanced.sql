-- catcode -> newsletter (CRP's own 460 rows) UNION committee -> newsletter (LLM-inferred).
--
-- Built by run_enrich.py and uploaded by tools/upload_enhanced_crosswalk.py. The two row
-- kinds are keyed differently and never both: source='crp' rows carry a catcode and a null
-- cmte_id, source='llm_inferred' rows the reverse. common.attach_newsletter joins the first
-- kind on catcode and uses the second only as a FALLBACK for committees that resolved to no
-- CRP row, so CRP's mapping always wins.
--
-- The confidence floor is applied in Python (config.MIN_CROSSWALK_CONFIDENCE), not here, so
-- the same extract can be re-read at a different floor without a re-pull.
select
    catcode,
    cmte_id,
    catorder,
    catname,
    industry,
    sector,
    newsletter,
    source,
    confidence,
    reasoning,
    model
from usa.crp_l_crosswalk_enhanced
