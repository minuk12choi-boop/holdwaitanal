-- NRDSEND · NRDMEAS 설비그룹을 가상스텝으로 본다.
--   실제 설비를 기다리는 것이 아니므로 Bottleneck 이 아니라
--   WAIT(진행불가) 로 둔다.
--
-- 실행 (MySQL 프롬프트 안):
--   USE app_db;
--   source D:/PERSONAL_SPACE/SW/python/7_holdwaitanal/getdata/migrate_virtual_eqp.sql

SET NAMES utf8mb4;

UPDATE f3_live
SET lot_status = 'WAIT(진행불가)',
    step_status = 'WAIT(진행불가)'
WHERE `현스텝` = '현스텝'
  AND lot_status = 'WAIT'
  AND UPPER(TRIM(COALESCE(eqpgroup, ''))) IN ('NRDSEND', 'NRDMEAS');

UPDATE f3_history
SET lot_status = 'WAIT(진행불가)',
    step_status = 'WAIT(진행불가)'
WHERE `현스텝` = '현스텝'
  AND lot_status = 'WAIT'
  AND UPPER(TRIM(COALESCE(eqpgroup, ''))) IN ('NRDSEND', 'NRDMEAS');

SELECT UPPER(TRIM(eqpgroup)) AS eqp_group,
       lot_status,
       COUNT(*) AS rows_cnt
FROM f3_live
WHERE UPPER(TRIM(COALESCE(eqpgroup, ''))) IN ('NRDSEND', 'NRDMEAS')
GROUP BY 1, 2;
