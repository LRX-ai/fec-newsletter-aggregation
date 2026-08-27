-- Current members of Congress, from the unitedstates/congress-legislators dataset.
--
-- `fec` is the bridge from FEC candidate IDs to bioguide IDs, and is stored as a
-- STRINGIFIED PYTHON LIST -- e.g. "['S8WA00194', 'H2WA01054']" -- because a legislator can
-- hold several FEC candidate IDs over a career (a House member who later runs for Senate
-- keeps both). congress.fec_id_map parses it; do not join on it raw.
select
    bioguide,
    official_full,
    first,
    last,
    fec
from usa.c_github_legislators_current_person
