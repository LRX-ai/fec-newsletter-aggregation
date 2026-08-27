-- usa.fec_pac_to_cand_current (PAS2 file): committee -> candidate money.
--
-- Unlike the OTH file there is no directionality problem here: every TRANSACTION_TP in
-- this table is a disbursement, so CMTE_ID is always the payer. No 15J memo rows either,
-- so the whole table is extracted (820,784 rows).
--
-- ID semantics differ from the OTH file and from each other by transaction type:
--   24K, 24Z       OTHER_ID = the candidate's AUTHORIZED COMMITTEE (C...)
--   24A/C/E/F/N    OTHER_ID = the CANDIDATE (H.../S.../P...)
-- CAND_ID is the candidate in every case, and is the field to attribute against.
select
    "CMTE_ID",
    "AMNDT_IND",
    "RPT_TP",
    "TRANSACTION_PGI",
    "TRANSACTION_TP",
    "ENTITY_TP",
    "NAME",
    "TRANSACTION_DT",
    "TRANSACTION_AMT",
    "OTHER_ID",
    "CAND_ID",
    "TRAN_ID",
    "FILE_NUM",
    "MEMO_CD",
    "SUB_ID",
    cycle
from usa.fec_pac_to_cand_current
