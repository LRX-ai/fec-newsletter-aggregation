-- Committee reference data (names, type, jurisdiction text).
--
-- One row per subcommittee in the source, so a DISTINCT on the parent fields collapses it
-- to one row per top-level committee. Used for display names and to document how the
-- newsletter mapping in data/congressional_committee_newsletter.csv was derived.
select distinct
    thomas_id,
    type,
    name,
    jurisdiction
from usa.c_github_committees_current_combined
where thomas_id is not null
