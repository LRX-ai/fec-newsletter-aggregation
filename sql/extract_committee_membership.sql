-- Current congressional committee memberships, BOTH levels.
--
-- 4-character codes are the ~48 top-level committees; 6-character codes are the 179
-- subcommittees (parent thomas_id + zero-padded subcommittee id, e.g. HSAG15). Attribution
-- runs at subcommittee level because "Cybersecurity and Infrastructure Protection" is a
-- far sharper industry signal than "House Homeland Security"; the top-level rows are the
-- fallback for the 17 members who sit on a committee without holding a mapped
-- subcommittee seat there.
--
-- The table spans several congresses, so `congress` must be filtered to the latest one at
-- read time -- otherwise a member's committee history counts as current seats and skews
-- the even split.
select
    committee,
    bioguide,
    chamber,
    congress,
    title,
    rank
from usa.c_github_committee_membership_current
