-- Non-15J rows from usa.fec_pac_to_pac (2025-2026 cycle).
--
-- 15J is 96.5% of the table (7,054,928 of 7,308,308 rows): earmarked-individual memo
-- rows with a null OTHER_ID. They have no resolvable sender and would double-count
-- money that is also reported as its own transaction, so they are excluded at the
-- source rather than filtered later -- it cuts the extract from 7.3M rows to 253k.
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
    "TRAN_ID",
    "FILE_NUM",
    "MEMO_CD",
    "SUB_ID",
    "FILING_DT",
    cycle
from usa.fec_pac_to_pac
where "TRANSACTION_TP" <> '15J'
