-- ---------------------------------------------------------------------------
-- f3_live / f3_history 에 컬럼 추가 (기존 데이터 유지)
--   마지막작업경과_일 : last_tkout_date 기준 경과일
--   fa_object4        : materialworkstatus
--
-- python getdata/db_common.py --init 이 같은 일을 자동으로 하므로
-- 보통은 이 파일을 쓸 필요가 없다. 수동 확인/실행용.
-- 이미 있는 컬럼에 실행하면 "Duplicate column name" 오류가 난다(무시해도 됨).
-- ---------------------------------------------------------------------------

ALTER TABLE `f3_live`
  ADD COLUMN `마지막작업경과_일` VARCHAR(128) NULL AFTER `스텝도착경과_일`,
  ADD COLUMN `fa_object4`       VARCHAR(128) NULL AFTER `ftp_reason`;

ALTER TABLE `f3_history`
  ADD COLUMN `마지막작업경과_일` VARCHAR(128) NULL AFTER `스텝도착경과_일`,
  ADD COLUMN `fa_object4`       VARCHAR(128) NULL AFTER `ftp_reason`;

-- 확인
-- SHOW COLUMNS FROM f3_live LIKE '%경과%';
-- SHOW COLUMNS FROM f3_live LIKE 'fa_object4';
