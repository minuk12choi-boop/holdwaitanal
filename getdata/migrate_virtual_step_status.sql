-- ---------------------------------------------------------------------------
-- 가상스텝 lot 의 상태를 WAIT -> WAIT(진행불가) 로 고친다.
--
-- 가상스텝은 실제 설비를 기다리는 게 아니라 진행이 막힌 것이다.
-- build_f3 는 이제 이 규칙을 반영하지만, 이미 적재된 스냅샷은 그대로다.
-- 아래를 한 번 실행하면 과거분까지 맞춰진다.
--
--   판정: 설비그룹 / CHAM그룹 / RCP / STEP / DESC 중 하나라도 'WAIT' 포함
--   대상: 현스텝 행의 상태가 'WAIT' 인 것
--
-- 실행 전 건수를 먼저 확인한다.
-- ---------------------------------------------------------------------------

USE app_db;

-- 1) 바뀔 건수 확인
SELECT COUNT(*) AS will_change
FROM   f3_live
WHERE  lot_status = 'WAIT'
  AND  `현스텝` = '현스텝'
  AND (UPPER(COALESCE(eqpgroup, ''))      LIKE '%WAIT%'
    OR UPPER(COALESCE(eqpgroup_cham, '')) LIKE '%WAIT%'
    OR UPPER(COALESCE(recipe_id, ''))     LIKE '%WAIT%'
    OR UPPER(COALESCE(step_seq, ''))      LIKE '%WAIT%'
    OR UPPER(COALESCE(step_desc, ''))     LIKE '%WAIT%');

-- 2) f3_live 수정
UPDATE f3_live
SET    lot_status = 'WAIT(진행불가)'
WHERE  lot_status = 'WAIT'
  AND  `현스텝` = '현스텝'
  AND (UPPER(COALESCE(eqpgroup, ''))      LIKE '%WAIT%'
    OR UPPER(COALESCE(eqpgroup_cham, '')) LIKE '%WAIT%'
    OR UPPER(COALESCE(recipe_id, ''))     LIKE '%WAIT%'
    OR UPPER(COALESCE(step_seq, ''))      LIKE '%WAIT%'
    OR UPPER(COALESCE(step_desc, ''))     LIKE '%WAIT%');

-- 3) f3_history 수정 (과거 스냅샷 전체)
UPDATE f3_history
SET    lot_status = 'WAIT(진행불가)'
WHERE  lot_status = 'WAIT'
  AND  `현스텝` = '현스텝'
  AND (UPPER(COALESCE(eqpgroup, ''))      LIKE '%WAIT%'
    OR UPPER(COALESCE(eqpgroup_cham, '')) LIKE '%WAIT%'
    OR UPPER(COALESCE(recipe_id, ''))     LIKE '%WAIT%'
    OR UPPER(COALESCE(step_seq, ''))      LIKE '%WAIT%'
    OR UPPER(COALESCE(step_desc, ''))     LIKE '%WAIT%');

-- 4) step_status 도 함께 맞춘다(있는 경우)
UPDATE f3_live
SET    step_status = 'WAIT(진행불가)'
WHERE  step_status = 'WAIT' AND lot_status = 'WAIT(진행불가)'
  AND  `현스텝` = '현스텝';

UPDATE f3_history
SET    step_status = 'WAIT(진행불가)'
WHERE  step_status = 'WAIT' AND lot_status = 'WAIT(진행불가)'
  AND  `현스텝` = '현스텝';

-- 5) 확인
-- SELECT lot_status, COUNT(*) FROM f3_live GROUP BY lot_status;
