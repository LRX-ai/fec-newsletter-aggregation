-- Primary catcode source. One row per CMTE_ID (64,805, unique).
--
-- `cycle` is deliberately not selected: it is a brace-list of every cycle the committee
-- appeared in, not a single cycle, so catcode here is a CURRENT SNAPSHOT and cannot be
-- resolved point-in-time. Historical newsletter totals can therefore shift retroactively
-- if a committee is reclassified.
select
    "CMTE_ID",
    "CMTE_NM",
    "CMTE_TP",
    "CMTE_DSGN",
    -- CONNECTED_ORG_NM is the sponsor a committee discloses, and it points BOTH ways
    -- for an affiliated pair (Fairshake names Protect Progress; Protect Progress names
    -- Fairshake). A reverse lookup over it turns one field into a disclosed network
    -- edge, which is what lets a description explain a transfer between two arms of
    -- the same operation without guessing.
    "CONNECTED_ORG_NM",
    "ORG_TP",
    catcode
from usa.fec_committees_trim_mapped
