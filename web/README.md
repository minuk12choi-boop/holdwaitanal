# Fab Flow Monitor — 웹

```
pip install django pymysql
python -c "import pymysql; pymysql.install_as_MySQLdb()"   # mysqlclient 대신 pymysql 사용 시
cd web
python manage.py runserver              # 기본 8010, 나만 접속 (127.0.0.1)
python manage.py runserver 8020         # 포트만 바꿔서, 나만 접속

# 사내 다른 사람도 접속하게 하려면 0.0.0.0 으로 바인딩한다
python manage.py runserver 0.0.0.0:8020
```

`.env` 는 저장소 루트에서 읽는다(`db_common.load_env`).
테이블은 `build_f3.py` / `get_move.py` 가 만들므로 migrate 는 필요 없다.

- `/` FlowStack 화면 (상단 메뉴바 + 좌측 지표 / 우측 상세)
- `/api/move/` MOVE 차트 데이터(JSON)

Chart.js 는 `web/flowmonitor/static/flowmonitor/chart.umd.js` 에 포함돼 있다.
CDN 을 타지 않으므로 사내망에서도 동작한다(로드 실패 시에만 CDN 으로 폴백).

## 진단

차트가 비어 있으면 `/api/health/` 를 연다. 테이블 존재 여부, 행수, 기간,
패널별 데이터포인트 수를 JSON 으로 보여준다.

| 증상 | 원인 |
|---|---|
| 상단에 "아직 적재되지 않은 테이블" | 해당 테이블 미생성 → `python getdata/db_common.py --init` |
| "적재는 됐지만 그릴 데이터가 없습니다" | 조회 구간(최근 140일) 안에 데이터 없음 |
| "Chart.js 로드 실패" | static 파일 누락 |

## 다른 사람이 접속하게 하려면

```
python manage.py runserver 0.0.0.0:8020
```

접속 주소는 `http://<내 PC IP>:8020/` 이다. IP 확인:

```
ipconfig
```

`.env` 에 접속 허용 호스트를 등록해야 한다. 없으면 `DisallowedHost` 가 난다.

```
DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost,12.34.56.78
```

방화벽에서 해당 포트 인바운드를 열어야 한다(관리자 권한 명령 프롬프트).

```
netsh advfirewall firewall add rule name="FabMonitor 8020" dir=in action=allow protocol=TCP localport=8020
```

개발 서버(`runserver`)는 어디까지나 임시용이다. 상시 운영하려면
`waitress` 같은 WSGI 서버를 쓰는 편이 안정적이다.

```
pip install waitress
waitress-serve --listen=0.0.0.0:8020 config.wsgi:application
```

`runserver` 는 static 파일을 자동으로 서빙하지만 **waitress 는 하지 않는다.**
그대로 두면 `chart.umd.js` 가 404 나고 화면이 통째로 빈다.
그래서 `config/urls.py` 에서 `/static/` 을 항상 서빙하도록 했다
(사내망 전용이라 `insecure=True`).

```
Not Found: /static/flowmonitor/chart.umd.js   <- 이 로그가 보이면 위 설정이 빠진 것
```

## 포트

`python manage.py runserver` 는 인자가 없으면 **8010** 을 쓴다.
`.env` 에 `DJANGO_PORT=8020` 을 넣으면 그 값이 기본이 된다.

"you don't have permission to access that port" 는 해당 포트가 이미
점유돼 있거나 예약돼 있을 때 나온다. 점유 확인:

```
netstat -ano | findstr :8010        # Windows
```

Windows 는 Hyper-V 등이 포트 대역을 예약해 두는 경우가 있다. 확인:

```
netsh interface ipv4 show excludedportrange protocol=tcp
```

예약 대역에 걸렸다면 그 범위를 피해 포트를 고른다.
