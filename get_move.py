move_query = f"""
select
line_id,
sys_line_id,
current_line_id as cur_line_id,
lot_id,
lot_type,
(case when lot_type in ('PP', 'PB', 'PG') then component_qty end) as move,
ppid,
process_eqp_id,
process_id,
step_seq,
step_desc,
eqp_type,
from_unixtime(unix_timestamp(recent_tkout_time, 'yyyyMMdd HHmmss')) as recent_tkout_date,
tkin_date,
process_start_date,
process_finish_date,
from_unixtime(unix_timestamp(lot_transn_time, 'yyyyMMdd HHmmss')) as tkout_date
from FAB.M_LOT_TRANSN_HIST
where line_id in ('KFR7', 'PFR1')
    and lot_transn_type = 'TrackOut'
    and lot_type in ('PP', 'PB', 'PG')
    and tkin_date >= days_sub(now(), {move_days})
