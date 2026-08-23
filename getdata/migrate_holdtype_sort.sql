-- HOLD 유형에 적용 순서(sort_no)를 추가한다.
--   작을수록 먼저 걸린다. 사용자가 화면에서 행을 끌어 바꾼다.
--   기존 행에는 '조건이 많은 것이 위' 로 초기값을 넣는다.
--   이후에는 자동으로 다시 정렬하지 않는다(수동 순서가 절대 기준).
--
-- 실행:  mysql -u root -p app_db < getdata/migrate_holdtype_sort.sql

ALTER TABLE f3_std_holdtype
  ADD COLUMN IF NOT EXISTS sort_no INT NULL AFTER type_name;

ALTER TABLE f3_std_holdtype
  ADD INDEX IF NOT EXISTS ix_ht_sort (sort_no);

-- 초기값: 조건이 많은 행이 위(작은 번호), 같으면 기존 id 순
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
  ) t
  ORDER BY t.spec DESC, t.id
) o ON o.id = h.id
SET h.sort_no = o.sn;

SELECT COUNT(*) AS 행수, MIN(sort_no) AS 처음, MAX(sort_no) AS 마지막
FROM f3_std_holdtype;
