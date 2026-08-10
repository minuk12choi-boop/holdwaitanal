# Fab Flow Monitor — 웹

```
pip install django pymysql
python -c "import pymysql; pymysql.install_as_MySQLdb()"   # mysqlclient 대신 pymysql 사용 시
cd web
python manage.py runserver 0.0.0.0:8000
```

`.env` 는 저장소 루트에서 읽는다(`db_common.load_env`).
테이블은 `build_f3.py` / `get_move.py` 가 만들므로 migrate 는 필요 없다.

- `/` FlowStack 화면 (상단 메뉴바 + 좌측 지표 / 우측 상세)
- `/api/move/` MOVE 차트 데이터(JSON)

Chart.js 는 CDN 에서 받는다. 사내망이 막혀 있으면 `chart.umd.min.js` 를
`web/flowmonitor/static/` 에 두고 템플릿의 `<script src>` 를 교체할 것.
