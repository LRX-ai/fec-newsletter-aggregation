-- catcode -> newsletter mapping (460 rows).
--
-- 76 catcodes have a blank newsletter -- mostly the Z*/J1*/J2* party, candidate and
-- leadership-PAC categories, which CRP deliberately leaves outside the industry
-- newsletters. 38 catcodes carry SEVERAL newsletters as one comma-separated string
-- (e.g. 'Oil & Gas, Utilities & Power, Environment'); see config.MULTI_NEWSLETTER.
select
    catcode,
    catorder,
    catname,
    industry,
    sector,
    newsletter
from usa.crp_l_crosswalk
