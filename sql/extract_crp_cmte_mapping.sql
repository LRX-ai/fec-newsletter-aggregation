-- Fallback catcode source, used only to fill gaps where
-- fec_committees_trim_mapped.catcode is null (covers 9,779 such committees).
--
-- Not authoritative: the two sources agree on only 44,429 of 74,584 shared committees.
-- transform.resolve_catcode records which source supplied each catcode so that
-- disagreement can be audited later.
select
    cmte_id,
    catcode
from usa.crp_fec_cmte_mapping
