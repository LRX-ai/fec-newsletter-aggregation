-- Candidate race identity: which seat each candidate is contesting, and whether
-- they hold it. This is what lets the receive edition surface the CHALLENGERS to
-- the members it ranks -- money the pipeline otherwise drops, because a
-- challenger sits on no committee.
--
-- CAND_ICI is the FEC's own incumbency flag: I = incumbent, C = challenger,
-- O = open seat. A race is (CAND_OFFICE, CAND_OFFICE_ST, CAND_OFFICE_DISTRICT,
-- CAND_ELECTION_YR); the master carries one row per candidate per cycle, so a
-- DISTINCT is needed.
select distinct
    "CAND_ID",
    "CAND_NAME",
    "CAND_PTY_AFFILIATION",
    "CAND_OFFICE",
    "CAND_OFFICE_ST",
    "CAND_OFFICE_DISTRICT",
    "CAND_ELECTION_YR",
    "CAND_ICI"
from usa.fec_candidate_master
where "CAND_ELECTION_YR" in ('2025', '2026')
