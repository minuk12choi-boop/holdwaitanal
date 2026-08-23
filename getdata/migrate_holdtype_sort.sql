-- HOLD 유형에 적용 순서(sort_no)를 추가한다.
--   작을수록 먼저 걸린다. 사용자가 화면에서 행을 끌어 바꾼다.
--   기존 행에는 '조건이 많은 것이 위' 로 초기값을 넣는다.
--   이후에는 자동으로 다시 정렬하지 않는다(수동 순서가 절대 기준).
--
-- 실행 (MySQL 프롬프트 안):
--   USE app_db;
--   source D:/PERSONAL_SPACE/SW/python/7_holdwaitanal/getdata/migrate_holdtype_sort.sql
--
-- MySQL 8.0 은 ALTER TABLE ... IF NOT EXISTS 를 지원하지 않는다.
-- 이미 있는지 먼저 보고 없을 때만 실행한다.

SET NAMES utf8mb4;

-- 1) sort_no 컬럼 추가 (없을 때만)
SET @has := (SELECT COUNT(*) FROM information_schema.COLUMNS
             WHERE TABLE_SCHEMA = DATABASE()
               AND TABLE_NAME = 'f3_std_holdtype'
               AND COLUMN_NAME = 'sort_no');
SET @sql := IF(@has = 0,
  'ALTER TABLE f3_std_holdtype ADD COLUMN sort_no INT NULL AFTER type_name',
  'SELECT ''sort_no 이미 있음'' AS note');
PREPARE st FROM @sql; EXECUTE st; DEALLOCATE PREPARE st;

-- 2) 인덱스 추가 (없을 때만)
SET @has := (SELECT COUNT(*) FROM information_schema.STATISTICS
             WHERE TABLE_SCHEMA = DATABASE()
               AND TABLE_NAME = 'f3_std_holdtype'
               AND INDEX_NAME = 'ix_ht_sort');
SET @sql := IF(@has = 0,
  'ALTER TABLE f3_std_holdtype ADD INDEX ix_ht_sort (sort_no)',
  'SELECT ''ix_ht_sort 이미 있음'' AS note');
PREPARE st FROM @sql; EXECUTE st; DEALLOCATE PREPARE st;

-- 3) 초기값: 조건이 많은 행이 위(작은 번호), 같으면 기존 id 순
--    이미 sort_no 가 있는 행은 건드리지 않는다.
SET @n := 0;
UPDATE f3_std_holdtype h
JOIN (
  SELECT id, (@n := @n + 10) AS sn
  FROM (
    SELECT id,
           (CASE WHEN condition1 IS NULL OR condition1 = '' THEN 0 ELSE 1 END)
         + (CASE WHEN condition2 IS NULL OR condition2 = '' THEN 0 ELSE 1 END)
         + (CASE WHEN condition3 IS NULL OR condition3 = '' THEN 0 ELSE 1 END)
         + (CASE WHEN line IS NULL OR line = '' THEN 0 ELSE 1 END)
         + (CASE WHEN type IS NULL OR type = 'ALL' THEN 0 ELSE 1 END) AS spec
    FROM f3_std_holdtype
    ORDER BY spec DESC, id
  ) t
) o ON o.id = h.id
SET h.sort_no = o.sn
WHERE h.sort_no IS NULL;

-- 4) CONDITION 을 대문자로 통일 (대소문자 구분 없이 걸리게)
UPDATE f3_std_holdtype
SET condition1 = UPPER(condition1),
    condition2 = UPPER(condition2),
    condition3 = UPPER(condition3);

SELECT COUNT(*) AS total_rows,
       MIN(sort_no) AS first_no,
       MAX(sort_no) AS last_no,
       SUM(sort_no IS NULL) AS not_set
FROM f3_std_holdtype;
