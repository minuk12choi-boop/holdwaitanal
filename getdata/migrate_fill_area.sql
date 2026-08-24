-- 과거 스냅샷의 AREA 를 채운다.
--   설비그룹 첫 글자로 가른다. build_f3.py 의 AREA_MAP 과 같은 규칙이다.
--   이미 값이 있는 행은 건드리지 않는다.
--
-- 실행: mysql -u root -p app_db  로 들어간 뒤 이 파일 내용을 붙여넣거나
--       source D:/PERSONAL_SPACE/SW/python/7_holdwaitanal/getdata/migrate_fill_area.sql

USE app_db;
SET NAMES utf8mb4;

UPDATE f3_history
SET AREA = CASE UPPER(LEFT(TRIM(eqpgroup), 1))
             WHEN 'E' THEN 'ETCH'  WHEN 'P' THEN 'PHOTO'
             WHEN 'M' THEN 'METRO' WHEN 'I' THEN 'IMP'
             WHEN 'D' THEN 'DIFF'  WHEN 'W' THEN 'CLN'
             WHEN 'F' THEN 'IMP'   WHEN 'T' THEN 'CVD'
             WHEN 'S' THEN 'METAL' WHEN 'C' THEN 'CMP'
           END
WHERE (AREA IS NULL OR AREA = '')
  AND eqpgroup IS NOT NULL AND eqpgroup <> ''
  AND UPPER(LEFT(TRIM(eqpgroup), 1))
      IN ('E','P','M','I','D','W','F','T','S','C');

UPDATE f3_live
SET AREA = CASE UPPER(LEFT(TRIM(eqpgroup), 1))
             WHEN 'E' THEN 'ETCH'  WHEN 'P' THEN 'PHOTO'
             WHEN 'M' THEN 'METRO' WHEN 'I' THEN 'IMP'
             WHEN 'D' THEN 'DIFF'  WHEN 'W' THEN 'CLN'
             WHEN 'F' THEN 'IMP'   WHEN 'T' THEN 'CVD'
             WHEN 'S' THEN 'METAL' WHEN 'C' THEN 'CMP'
           END
WHERE (AREA IS NULL OR AREA = '')
  AND eqpgroup IS NOT NULL AND eqpgroup <> ''
  AND UPPER(LEFT(TRIM(eqpgroup), 1))
      IN ('E','P','M','I','D','W','F','T','S','C');

SELECT 'f3_history' AS tbl,
       SUM(AREA IS NULL OR AREA = '') AS still_empty,
       COUNT(*) AS total_rows
FROM f3_history
UNION ALL
SELECT 'f3_live', SUM(AREA IS NULL OR AREA = ''), COUNT(*) FROM f3_live;
