-- Current-term identity for sitting members: party, chamber, state, district.
--
-- The person table carries no party -- party belongs to a TERM, because members
-- change party mid-career. This table holds every term, so the latest one per
-- bioguide is the member's current standing; congress.current_terms() does that
-- selection at read time.
select
    bioguide,
    type,
    party,
    state,
    district,
    start,
    "end"
from usa.c_github_legislators_current_terms
