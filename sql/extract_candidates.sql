-- Candidate lookup for senders with an H.../S.../P... prefix.
select
    "CAND_ID",
    "CAND_NAME",
    "CAND_PTY_AFFILIATION"
from usa.fec_candidates_trim
