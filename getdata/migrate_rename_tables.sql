-- ---------------------------------------------------------------------------
-- app_db 안에 여러 프로젝트의 테이블이 섞여 있어, 이 프로젝트 것은 모두
-- f3_ 접두를 붙인다. f3_ 로 시작하면 holdwaitanal 소유로 본다.
--
-- 실행 전 확인
--     USE app_db;
--     SHOW TABLES;
--
-- 이미 f3_ 인 것(f3_live, f3_history, f3_history_meta, f3_load_log)은 그대로 둔다.
-- 대상 테이블이 없으면 "Unknown table" 오류가 나는데, 그건 애초에 만든 적이
-- 없다는 뜻이므로 그 줄만 건너뛰면 된다.
-- ---------------------------------------------------------------------------

USE app_db;

RENAME TABLE move_shift    TO f3_move_shift;
RENAME TABLE move_daily    TO f3_move_daily;
RENAME TABLE move_lot      TO f3_move_lot;
RENAME TABLE std_module    TO f3_std_module;
RENAME TABLE std_holdtype  TO f3_std_holdtype;
RENAME TABLE cause_rules   TO f3_cause_rules;

-- 한 번에 하려면 아래 한 줄로도 된다(하나라도 없으면 전체가 실패한다).
-- RENAME TABLE move_shift TO f3_move_shift,
--              move_daily TO f3_move_daily,
--              move_lot TO f3_move_lot,
--              std_module TO f3_std_module,
--              std_holdtype TO f3_std_holdtype,
--              cause_rules TO f3_cause_rules;

-- 확인
-- SHOW TABLES LIKE 'f3\_%';
