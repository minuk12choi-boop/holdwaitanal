# Fab Flow Monitor 웹페이지 프로젝트 설계도

## 1. 문서 목적

이 문서는 반도체 Fab 내 여러 Line의 생산 흐름을 비교하고, 각 지표별 원인을 Drill-down 할 수 있는 PC 전용 웹페이지의 화면 구조와 사용자 경험을 정의한다.

본 문서는 다음 항목만 다룬다.

- 화면 구성
- 시각화 방식
- 사용자 조작 방식
- 지표별 Drill-down 정보 구조
- 화면 간 상태 연동
- Lot Balance 표현 방식
- 컴포넌트 구조
- 사용자 친화적 설계 원칙

다음 항목은 본 문서의 범위에서 제외한다.

- MOVE 산식
- WIP TURN 산식
- HOLD 판정 기준
- WAIT성 진행불가율 산식
- 설비 Status 판정 방식
- TIP, Exception, FTP의 원천 데이터 정의
- 실제 테이블 및 컬럼 매핑
- 집계 주기
- 데이터 정합성 검증 규칙
- 손실량 추정 알고리즘의 구체 구현

실제 데이터 구조와 산식은 별도 데이터 설계 단계에서 연결한다.

---

# 2. 프로젝트 개요

## 2.1 프로젝트명

### 전체 페이지명

**Fab Flow Monitor**

반도체 Fab 내 Line별 생산 흐름과 진행 제약을 비교·진단하는 화면이라는 의미다.

### 좌측 공통 시계열 영역명

**FlowStack**

다섯 개 핵심 지표를 하나의 공통 시간축 위에 세로로 쌓아 보여주는 영역이다.

### 우측 상세 분석 영역명

**Cause Explorer**

FlowStack에서 선택한 지표, Line, 시점 또는 기간을 기준으로 상세 원인을 탐색하는 영역이다.

---

## 2.2 핵심 목적

이 화면의 목적은 단순 모니터링이 아니다.

사용자가 다음 순서로 문제를 탐색할 수 있어야 한다.

1. Line 간 생산 흐름 차이 확인
2. 특정 지표의 이상 시점 확인
3. 이상이 발생한 Line 선택
4. 관련 원인군 확인
5. Area, Proc, Layer, 설비, Lot 등 하위 단위로 Drill-down
6. 현재 지속 중인 문제와 과거 종료 문제 분리
7. 장기 정체 또는 장기 지속 문제 식별

---

# 3. 대상 지표

FlowStack에는 다음 다섯 개 지표를 배치한다.

1. MOVE
2. WIP TURN
3. 재공
4. HOLD율
5. WAIT성 진행불가율

배치 순서는 반드시 위 순서를 따른다.

이 순서는 다음의 생산 흐름을 반영한다.

```text
생산 결과
├─ MOVE
└─ WIP TURN

흐름 상태
└─ 재공

Lot 직접 제약
└─ HOLD

설비·조건·환경 및 기타 진행 제약
└─ WAIT성 진행불가
```

HOLD는 WAIT성 진행불가율보다 위에 둔다.

---

# 4. 화면 대상 환경

## 4.1 디바이스

- PC 전용
- 모바일 화면은 고려하지 않음
- 일반적인 가로형 업무용 모니터 기준

## 4.2 권장 해상도

- 최소 권장 너비: 1440px
- 권장: 1920px 이상
- 세로 스크롤은 허용
- 가로 스크롤은 원칙적으로 금지

## 4.3 전체 화면 비율

```text
좌측 FlowStack: 30~34%
우측 Cause Explorer: 66~70%
```

권장 기본값은 다음과 같다.

```text
FlowStack 32%
Cause Explorer 68%
```

FlowStack을 25% 이하로 줄이면 네 개 Line의 시계열과 Y축, 지표명, 상태값을 동시에 읽기 어려워질 수 있으므로 피한다.

---

# 5. 전체 페이지 레이아웃

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│ Fab Flow Monitor                                                            │
│ 전역 필터 및 기간 제어                                                      │
├──────────────────────────┬──────────────────────────────────────────────────┤
│ FlowStack                │ Cause Explorer                                   │
│                          │                                                  │
│ MOVE                     │ 선택 지표 상세 분석                             │
│ WIP TURN                 │                                                  │
│ 재공                     │ 요약 → 원인 → 하위 분석 → 상세 목록             │
│ HOLD율                   │                                                  │
│ WAIT성 진행불가율         │                                                  │
└──────────────────────────┴──────────────────────────────────────────────────┘
```

---

# 6. 상단 전역 제어 영역

상단에는 모든 차트와 Drill-down에 공통 적용되는 제어만 둔다.

## 6.1 기간 선택

```text
[7일] [30일] [90일] [직접 선택]
```

기본값은 최근 30일을 권장한다.

## 6.2 Line 선택

최종적으로 네 개 Line을 비교한다.

```text
☑ Line A
☑ Line B
☑ Line C
☑ Line D
```

기본값은 네 개 모두 선택이다.

## 6.3 Line 색상

Line 색상은 모든 화면에서 고정한다.

| Line | 색상 | 권장 HEX |
|---|---|---|
| Line A | 파란색 | `#2563EB` |
| Line B | 초록색 | `#16A34A` |
| Line C | 주황색 | `#EA580C` |
| Line D | 앰버색 | `#D19A00` |

일반적인 밝은 노란색은 흰 배경에서 식별력이 낮으므로 사용하지 않는다.

## 6.4 Lot Type 선택

Lot Type은 다음 세 가지 상태를 갖는다.

```text
[전체] [PP] [PG(TT)] [상세 비교]
```

기본값은 `전체`다.

### 선 표현 규칙

| Lot Type | 선 표현 |
|---|---|
| 전체 | 굵은 실선 |
| PP | 얇은 실선 |
| PG(TT) | 얇은 점선 |

권장 두께:

- 전체: 3px
- PP: 1.5px
- PG(TT): 1.5px 점선

### 상세 비교 모드

상세 비교를 켜면 전체, PP, PG(TT)를 동시에 표시할 수 있다.

다만 다음 문제를 고려해야 한다.

```text
4개 Line × 3개 Lot Type = 패널당 최대 12개 선
```

따라서 상세 비교는 기본 상태가 아니며, 사용자가 명시적으로 선택해야 한다.

## 6.5 시간 단위

```text
[일] [Shift] [시간]
```

실제 지원 범위는 데이터 해상도에 따라 결정한다.

## 6.6 비교 방식

```text
[절대값] [Line 평균 대비] [기준일=100]
```

기본값은 절대값이다.

---

# 7. FlowStack 설계

## 7.1 기본 구조

FlowStack은 다섯 개 지표 패널을 하나의 공통 시간축으로 연결한 세로형 시계열 차트다.

```text
┌ MOVE ────────────────────────────┐
│ Line A / B / C / D               │
└─────────────────────────────────┘

┌ WIP TURN ────────────────────────┐
│ Line A / B / C / D               │
└─────────────────────────────────┘

┌ 재공 ────────────────────────────┐
│ Line A / B / C / D               │
└─────────────────────────────────┘

┌ HOLD율 ──────────────────────────┐
│ Line A / B / C / D               │
└─────────────────────────────────┘

┌ WAIT성 진행불가율 ───────────────┐
│ Line A / B / C / D               │
└─────────────────────────────────┘

              공통 X축
```

## 7.2 축 규칙

- X축은 다섯 패널이 완전히 공유
- Y축은 지표별로 독립
- 이중 Y축 사용 금지
- 지표별 단위와 범위는 각 패널에 명확히 표시
- 하나의 패널에서 Line별 축 범위를 다르게 사용하지 않음

## 7.3 패널 크기

권장 상대 높이:

| 지표 | 상대 높이 |
|---|---:|
| MOVE | 1.2 |
| WIP TURN | 1.0 |
| 재공 | 1.2 |
| HOLD율 | 0.8 |
| WAIT성 진행불가율 | 0.8 |

MOVE와 재공은 변화 폭과 정보량이 많을 가능성이 높아 상대적으로 크게 둔다.

## 7.4 패널 헤더

각 패널에는 다음 정보를 압축해 표시한다.

```text
MOVE    12.4K    ▼ 8.2%
```

권장 구성:

- 지표명
- 현재값
- 선택 기간 내 증감
- 이상 상태 배지
- Drill-down 진입 아이콘

## 7.5 패널 선택

패널 전체 또는 지표명을 클릭하면 Cause Explorer가 해당 지표 분석 화면으로 변경된다.

예:

```text
MOVE 클릭 → Flow Loss Analysis
WIP TURN 클릭 → Flow Loss Analysis
재공 클릭 → WIP Analysis
HOLD율 클릭 → Hold Analysis
WAIT성 진행불가율 클릭 → Wait Block Analysis
```

## 7.6 Line 선택

특정 Line의 선을 클릭하면 Cause Explorer가 해당 Line을 기준으로 변경된다.

예:

```text
Line B의 HOLD율 선 클릭
→ Line B 기준 Hold Analysis
```

## 7.7 시점 또는 기간 선택

- 단일 클릭: 특정 시점 Snapshot
- 드래그: 특정 기간 분석
- 선택 범위는 모든 패널과 Cause Explorer에 동일 적용
- 다른 지표로 이동해도 기간 선택 상태 유지

## 7.8 동기화

다음 기능은 다섯 패널에서 반드시 동기화한다.

- 확대·축소
- 날짜 범위
- 마우스 세로 기준선
- Line 표시·숨김
- Lot Type 전환
- 선택 시점
- 선택 기간

## 7.9 결측치

결측 구간을 임의로 직선 연결하지 않는다.

결측은 공백 또는 명확한 결측 표시로 표현한다.

---

# 8. Cause Explorer 공통 구조

Cause Explorer는 지표별 화면이 달라도 동일한 골격을 유지해야 한다.

```text
┌ 선택 상태 ─────────────────────────────────────────┐
│ 지표 · Line · Lot Type · 기간                      │
└────────────────────────────────────────────────────┘

┌ 핵심 요약 ─────────────────────────────────────────┐
│ 현재값 │ 변화량 │ 영향 대상 │ 지속시간             │
└────────────────────────────────────────────────────┘

┌ 주 분석 차트 ──────────────────────────────────────┐
│ 선택 지표의 핵심 분석                              │
└────────────────────────────────────────────────────┘

┌ 세부 분석 ─────────────────────────────────────────┐
│ 원인별 추이 │ Area/Proc/Layer별 분포 │ Aging       │
└────────────────────────────────────────────────────┘

┌ 상세 목록 ─────────────────────────────────────────┐
│ Lot / 설비 / 원인 / 시작시각 / 지속시간            │
└────────────────────────────────────────────────────┘
```

## 8.1 선택 상태 표시

항상 상단에 현재 분석 상태를 표시한다.

예:

```text
HOLD율 > Line B > 전체 Lot > 최근 30일
```

## 8.2 Breadcrumb

Drill-down 단계가 깊어질 경우 Breadcrumb를 사용한다.

예:

```text
WAIT성 진행불가 > 설비·환경 > DOWN > ETCH Area
```

## 8.3 상태 유지

다음 상태는 다른 지표로 이동해도 유지한다.

- 선택 Line
- 선택 기간
- Lot Type
- 시간 단위
- 현재/종료 필터

---

# 9. MOVE 및 WIP TURN Drill-down

## 9.1 공통 Drill-down 사용

MOVE와 WIP TURN은 동일한 원인 체계를 공유한다.

두 지표는 Lot 흐름이 잘 진행되는지를 다음과 같이 다르게 표현한다.

- MOVE: 절대적인 생산량
- WIP TURN: 재공이 진행되는 속도

따라서 Cause Explorer에서는 동일한 **Flow Loss Analysis** 화면을 사용한다.

## 9.2 원인 기여도 표현 원칙

MOVE 및 WIP TURN 감소를 HOLD, DOWN, TIP 등으로 정확히 나누기 어려울 수 있으므로 원인별 감소 기여도를 확정적으로 표현하지 않는다.

다음 표현은 피한다.

```text
DOWN 40%
HOLD 30%
TIP 20%
```

대신 다음 세 단계를 분리한다.

1. 성과 Gap
2. 진행 제약 노출량
3. 추정 생산 손실

## 9.3 성과 Gap

```text
MOVE Gap = 기대 MOVE - 실제 MOVE
WIP TURN Gap = 기대 WIP TURN - 실제 WIP TURN
```

단, 실제 산식과 기대값 산정 방식은 데이터 설계 단계에서 정의한다.

화면에서는 다음처럼 표현한다.

```text
MOVE Gap       -1,700
WIP TURN Gap   -0.18
```

## 9.4 진행 제약 기본 지표

기본 원인 비교 지표는 다음을 권장한다.

### 진행제약 누적시간

개념:

```text
영향 Lot 수 × 진행 불가 시간
```

영문 보조 명칭:

```text
Blocked Lot-Hours
```

이 값은 단순 발생 건수보다 실제 Flow 영향 규모를 더 잘 보여준다.

## 9.5 보조 지표

다음 지표를 함께 제공한다.

- 영향 Lot 수
- 평균 정체시간
- 최대 정체시간
- 순 진행불가시간
- 중첩률
- 장기 정체 Lot 수
- 추정 MOVE 손실

## 9.6 중복 제약

동일 Lot에 여러 제약이 동시에 존재할 수 있으므로 다음 두 값을 구분한다.

### 총 노출시간

원인별 시간을 모두 합산하며 중복 포함

### 순 진행불가시간

Lot별 시간 구간의 합집합으로 중복 제거

화면에는 다음 값을 함께 표시한다.

```text
총 노출시간
순 진행불가시간
중첩률
```

## 9.7 기본 화면 구성

```text
┌ Flow 성과 Gap ────────────────────────────────┐
│ MOVE Gap │ WIP TURN Gap                       │
└───────────────────────────────────────────────┘

┌ 진행 제약 요약 ──────────────────────────────┐
│ 영향 Lot │ 순 진행불가시간 │ 장기정체 │ 중첩률 │
└───────────────────────────────────────────────┘

분석 기준:
[진행제약 누적시간] [영향 Lot] [평균시간] [추정 MOVE 손실]

┌ 주요 Flow 제약 ──────────────────────────────┐
│ HOLD / DOWN / PM / LOCAL / TIP / EXCEPTION / FTP │
└───────────────────────────────────────────────┘

┌ 시계열 연동 ─────────────────────────────────┐
│ MOVE 또는 WIP TURN Gap                        │
│ 진행 제약 추이                                │
└───────────────────────────────────────────────┘
```

## 9.8 표현 문구

실제 인과관계가 검증되지 않은 경우 다음 명칭을 사용한다.

권장:

- 관련 제약
- 동시 발생 제약
- 추정 영향
- Flow Loss 관련 요인

금지:

- 확정 원인
- 감소 기여율
- 손실 책임 비율

---

# 10. 재공 Drill-down

재공 Drill-down은 다음 두 가지 목적을 모두 충족해야 한다.

1. 투입량과 재공 변화의 시계열 확인
2. 현재 또는 선택 시점의 Layer/Proc별 재공 분포 확인

## 10.1 기본 구성

```text
┌ 재공 요약 ───────────────────────────────────┐
│ 현재 재공 │ 전일 대비 │ 정상 대비 │ 장기정체 │
└───────────────────────────────────────────────┘

┌ Lot Type별 투입량 시계열 ────────────────────┐
│ 전체 / PP / PG(TT)                           │
└───────────────────────────────────────────────┘

┌ 전체 재공 추이 ──────────────────────────────┐
│ 선택 Line 기준                               │
└───────────────────────────────────────────────┘

┌ Lot Balance ─────────────────────────────────┐
│ Layer 및 Proc별 재공 분포                    │
└───────────────────────────────────────────────┘

┌ Layer/Proc 상세 ─────────────────────────────┐
│ 유입 │ 유출 │ 순증감 │ 체류시간 │ HOLD │ WAIT │
└───────────────────────────────────────────────┘
```

---

# 11. Lot Balance 설계

## 11.1 분석 대상

Lot Balance는 Line 간 비교가 목적이 아니다.

반드시 **개별 Line 하나를 선택한 상태**에서 표시한다.

```text
선택 Line: Line A
```

FlowStack에서 Line을 선택하지 않은 경우 Lot Balance 진입 시 Line 선택을 요구하거나, 마지막으로 선택한 Line을 기본 적용한다.

## 11.2 목적

하나의 Line 내부에 여러 Proc가 존재할 때 다음 두 가지 수요를 모두 충족한다.

1. Proc를 Layer 기준으로 통합 정렬하여 전체 흐름 확인
2. 특정 Proc만 선택하여 세부 흐름 확인

## 11.3 보기 모드

```text
[Layer 통합 보기] [Proc별 보기]
```

### Layer 통합 보기

여러 Proc를 공통 Layer 기준으로 정렬하여 하나의 흐름으로 보여준다.

- X축: Layer
- Y축: 재공량
- 각 Layer 내부 구성: Proc별 재공
- 선택 시점 기준 Snapshot
- 한 개 Line만 표시

권장 차트:

```text
Layer별 누적 막대 차트
```

각 막대의 전체 높이는 Layer 총재공이며, 내부 조각은 해당 Layer를 구성하는 Proc별 재공이다.

```text
재공
│             ┌── Proc C
│      ┌──────┤
│      │Proc B│
│  ┌───┤      │
│  │Proc A    │
└────────────────── Layer
   L1  L2  L3  L4
```

### Proc별 보기

사용자가 특정 Proc를 선택하여 해당 Proc만 본다.

```text
Proc 선택:
[Proc A] [Proc B] [Proc C] [...]
```

Proc별 보기에서는 다음을 제공한다.

- 해당 Proc의 Layer별 재공
- Layer별 유입
- Layer별 유출
- Layer별 순증감
- 평균 체류시간
- 장기 정체 Lot
- HOLD Lot
- WAIT Lot

## 11.4 Layer 일치 규칙

화면 설계상 Layer는 여러 Proc 간 공통 비교축 역할을 한다.

실제 데이터에서 Layer 명칭 또는 단계 체계가 다를 수 있으므로 다음 기능을 고려한다.

- 공통 Layer 기준 정렬
- Layer가 없는 Proc는 빈 값으로 표시
- 동일 Layer에 여러 Proc가 존재할 경우 누적
- Layer 순서는 공정 순서를 따라 고정
- 사용자가 Layer 범위를 선택 가능
- Layer 클릭 시 해당 Layer의 Proc 상세로 Drill-down

실제 Layer 매핑 규칙은 별도 데이터 설계에서 정의한다.

## 11.5 색상 규칙

Lot Balance는 한 개 Line만 표시하므로 Line 색상을 기본색으로 사용한다.

예:

- Line A 선택: 파란색 계열
- Line B 선택: 초록색 계열
- Line C 선택: 주황색 계열
- Line D 선택: 앰버색 계열

Proc 구분은 같은 계열 내 명도 차이 또는 패턴으로 표현한다.

Proc별로 전혀 다른 강한 색상을 사용하면 Line 색상 체계와 충돌할 수 있으므로 피한다.

## 11.6 정상 범위

가능하면 Layer별 정상 범위를 함께 표시한다.

예:

```text
Layer 12
현재 재공      1,420
정상 범위      900~1,100
초과           +320
```

정상 범위의 실제 산정 방식은 데이터 설계 단계에서 결정한다.

## 11.7 보기 기준

```text
[재공량] [Proc 구성비]
```

### 재공량

- 절대 재공 확인
- 병목 Layer 확인
- 누적 위치 확인

### Proc 구성비

- 각 Layer의 Proc 구성 확인
- Proc Mix 변화 확인
- Layer별 구성 불균형 확인

## 11.8 시점 선택

Lot Balance는 기본적으로 현재 Snapshot을 보여준다.

추가 선택:

```text
[현재] [FlowStack 선택 시점]
```

FlowStack에서 특정 날짜 또는 시간을 선택한 경우 해당 시점의 Lot Balance를 볼 수 있어야 한다.

## 11.9 Drill-down 순서

```text
Line
→ Layer
→ Proc
→ Step
→ Lot Type
→ 개별 Lot
```

## 11.10 상세 정보

Layer 또는 Proc를 클릭하면 다음 정보를 보여준다.

- 현재 재공
- 유입
- 유출
- 순증감
- 평균 체류시간
- 최대 체류시간
- 장기 정체 Lot 수
- HOLD Lot 수
- WAIT Lot 수
- 개별 Lot 목록

---

# 12. HOLD율 Drill-down

HOLD는 개별 Lot에 직접 걸리는 제약으로 본다.

## 12.1 핵심 분석 항목

1. Hold 원인
2. Hold 유형
3. Hold 등록자 또는 파트원
4. Hold 지속시간
5. 영향 Lot 수
6. 장기 Hold
7. 현재 진행 중 여부

## 12.2 Hold 원인 Pareto

권장 차트:

```text
가로 막대
```

보기 기준:

```text
[건수] [영향 Lot 수] [누적 Hold 시간] [평균 지속시간]
```

기본값은 누적 Hold 시간을 권장한다.

## 12.3 Hold 유형 추이

시계열로 Hold 유형별 발생량과 지속량을 보여준다.

실제 Hold 유형 코드는 데이터 설계 단계에서 정의한다.

## 12.4 파트원별 분석

단순 건수 순위만 보여주지 않는다.

필수 컬럼:

- 등록 건수
- 영향 Lot 수
- 누적 Hold 시간
- 평균 해제 시간
- 장기 Hold 비율
- 현재 진행 중 Hold 수

예:

```text
파트원 | 등록 건수 | 영향 Lot | 누적시간 | 장기 비율
```

“가장 많이 등록한 사람”과 “가장 큰 영향을 준 사람”은 다를 수 있으므로 구분한다.

## 12.5 Aging

권장 구간:

```text
0~4시간
4~12시간
12~24시간
1~3일
3일 이상
```

## 12.6 상세 목록

- Lot ID
- Hold 유형
- Hold 원인
- 등록자
- 소속
- 시작 시각
- 지속시간
- 현재 상태
- Area
- Layer
- Proc
- Step

---

# 13. WAIT성 진행불가율 Drill-down

WAIT성 진행불가는 크게 두 범주로 나눈다.

## 13.1 설비·환경 기반 제약

- Down
- PM
- Local
- TIP

특정 설비 또는 조건 때문에 여러 Lot이 광범위하게 영향을 받는 항목이다.

## 13.2 개별 Lot 기반 제약

- Exception
- FTP

특정 Lot에 직접 적용되는 제약이다.

## 13.3 기본 화면

```text
┌ WAIT성 진행불가율 요약 ──────────────────────┐
│ 현재율 │ 영향 Lot │ 진행 중 건 │ 누적시간    │
└───────────────────────────────────────────────┘

┌ 제약 범주 ───────────────────────────────────┐
│ 설비·환경 제약 │ 개별 Lot 제약              │
└───────────────────────────────────────────────┘

┌ 설비·환경 분석 ──────────────────────────────┐
│ Status / TIP                                 │
└───────────────────────────────────────────────┘

┌ 개별 Lot 분석 ───────────────────────────────┐
│ Exception / FTP                             │
└───────────────────────────────────────────────┘

┌ 장기 진행불가 목록 ──────────────────────────┐
│ Area │ 설비 또는 Lot │ 원인 │ 시작 │ 지속시간 │
└───────────────────────────────────────────────┘
```

---

# 14. 설비 Status 분석

## 14.1 대상 Status

- Down
- PM
- Local

## 14.2 핵심 차트

권장:

```text
Area × Status Heatmap
```

보기 기준:

```text
[발생 건수] [누적 지속시간] [영향 Lot 수]
```

## 14.3 보조 정보

- Area별 발생 추이
- 현재 지속 중 건수
- 평균 지속시간
- 최대 지속시간
- 영향 설비 수
- 영향 Lot 수
- 장기 지속 설비 이슈

## 14.4 상세 목록

- Area
- 설비
- Status
- 시작 시각
- 지속시간
- 현재 진행 여부
- 영향 Lot 수

---

# 15. TIP 분석

## 15.1 구조

```text
TIP 원인
→ Area
→ 대상 설비 또는 조건
→ 영향 Lot
→ 지속시간
```

## 15.2 핵심 시각화

- TIP 원인 Pareto
- Area × TIP 원인 Heatmap
- TIP 발생 추이
- 장기 지속 TIP
- 현재 진행 중 TIP

실제 TIP 원인 분류는 데이터 설계 단계에서 연결한다.

---

# 16. Exception 및 FTP 분석

Exception과 FTP는 개별 Lot 기반이므로 HOLD 분석 UI를 재사용한다.

공통 분석 컴포넌트 개념:

```text
LotBlockerAnalysis
├─ HOLD
├─ Exception
└─ FTP
```

공통 항목:

- 원인별 건수
- 영향 Lot 수
- 누적 지속시간
- 평균 지속시간
- Area별 분포
- Layer별 분포
- Proc별 분포
- Step별 분포
- Aging
- 개별 Lot 목록

---

# 17. 사용자 상호작용 원칙

## 17.1 Line 선택 유지

Line을 선택한 뒤 다른 지표를 눌러도 해당 Line이 유지되어야 한다.

## 17.2 기간 유지

선택한 기간은 지표를 이동해도 유지한다.

## 17.3 Lot Type 유지

전체, PP, PG(TT) 선택 상태는 다른 지표로 이동해도 유지한다.

## 17.4 현재 진행 중과 종료 분리

모든 원인 분석 화면에 다음 필터를 둔다.

```text
[현재 진행 중] [기간 내 종료] [전체]
```

## 17.5 범례 클릭

Line 범례를 클릭하면 모든 FlowStack 패널에서 해당 Line을 동시에 숨기거나 표시한다.

## 17.6 이상 지점 클릭

FlowStack의 이상 지점 또는 이벤트 마커를 클릭하면 Cause Explorer의 관련 분석으로 이동한다.

## 17.7 상세 목록 연결

차트의 막대, 셀, 점 또는 구간을 클릭하면 하단 상세 목록이 필터링된다.

---

# 18. 시각적 계층 구조

## 18.1 좌측 FlowStack

역할:

- 비교
- 탐색
- 이상 시점 확인
- Line 선택
- 지표 선택

## 18.2 우측 Cause Explorer

역할:

- 원인 분석
- 하위 단위 Drill-down
- 현재 문제 식별
- 장기 지속 문제 확인
- 상세 Lot 또는 설비 추적

## 18.3 색상의 역할

- 색상: Line
- 선 종류: Lot Type
- 패널 위치: 지표
- 패턴 또는 명도: Proc
- 강조 배지: 이상 상태

한 가지 시각 속성에 여러 의미를 동시에 부여하지 않는다.

---

# 19. 화면 내 권장 용어

| 기능 | 권장 명칭 |
|---|---|
| 전체 화면 | Fab Flow Monitor |
| 좌측 지표 묶음 | FlowStack |
| 우측 상세 영역 | Cause Explorer |
| MOVE/WIP TURN 상세 | Flow Loss Analysis |
| 재공 상세 | WIP Analysis |
| Layer별 재공 분포 | Lot Balance |
| HOLD 상세 | Hold Analysis |
| WAIT 상세 | Wait Block Analysis |
| Lot 기반 제약 공통 분석 | Lot Blocker Analysis |
| 영향 Lot × 정체시간 | 진행제약 누적시간 |
| 중복 제거 정체시간 | 순 진행불가시간 |
| 원인 중첩 비율 | 중첩률 |

---

# 20. 권장 컴포넌트 구조

```text
FabFlowMonitorPage
├─ GlobalFilterBar
│  ├─ DateRangeSelector
│  ├─ LineSelector
│  ├─ LotTypeSelector
│  ├─ TimeGranularitySelector
│  └─ ComparisonModeSelector
│
├─ FlowStackPanel
│  ├─ MetricTrendPanel(MOVE)
│  ├─ MetricTrendPanel(WIP_TURN)
│  ├─ MetricTrendPanel(WIP)
│  ├─ MetricTrendPanel(HOLD_RATE)
│  └─ MetricTrendPanel(WAIT_BLOCK_RATE)
│
└─ CauseExplorer
   ├─ SelectionHeader
   ├─ SummaryCards
   ├─ FlowLossAnalysis
   ├─ WipAnalysis
   │  ├─ InputTrendChart
   │  ├─ WipTrendChart
   │  ├─ LotBalance
   │  │  ├─ LayerIntegratedView
   │  │  └─ ProcView
   │  └─ LotDetailTable
   ├─ HoldAnalysis
   ├─ WaitBlockAnalysis
   │  ├─ EquipmentStatusAnalysis
   │  ├─ TipAnalysis
   │  ├─ ExceptionAnalysis
   │  └─ FtpAnalysis
   └─ DetailTable
```

---

# 21. 화면 상태 모델

화면 상태는 최소한 다음 항목을 가진다.

```text
selectedMetric
selectedLines
focusedLine
selectedLotType
lotTypeDetailMode
dateRange
selectedTimestamp
selectedTimeRange
timeGranularity
comparisonMode
currentVsClosedFilter
selectedArea
selectedLayer
selectedProc
selectedStep
selectedReason
selectedEquipment
selectedLot
lotBalanceViewMode
```

## 상태 원칙

- 전역 상태와 Drill-down 상태를 분리한다.
- 지표 변경 시 전역 상태는 유지한다.
- 지표 전환 시 호환되지 않는 하위 선택만 초기화한다.
- URL Query Parameter로 주요 상태를 보존할 수 있도록 설계한다.
- 새로고침 후에도 분석 조건을 재현할 수 있도록 한다.

---

# 22. 성능 및 사용성 고려사항

## 22.1 초기 로딩

- FlowStack 우선 렌더링
- Cause Explorer는 선택 지표에 필요한 데이터만 지연 로딩
- 상세 테이블은 페이지네이션 또는 가상 스크롤 적용

## 22.2 대량 시계열

- 표시 구간에 따라 샘플링 또는 집계 수준 조절
- 화면 확대 시 원본 해상도로 재조회
- 마우스 이동 시 다섯 패널의 ToolTip을 동기화

## 22.3 Tooltip

한 번에 너무 많은 값을 보여주지 않는다.

기본 ToolTip:

- 선택 지표
- 해당 시점의 4개 Line 값

확장 ToolTip 또는 클릭 고정:

- 5개 지표 전체
- 선택 Line 상세
- 주요 이벤트

## 22.4 범례

Line 범례와 Lot Type 범례를 분리한다.

예:

```text
Line
■ A  ■ B  ■ C  ■ D

Lot Type
━━ 전체  ─ PP  ┄ PG(TT)
```

---

# 23. 금지해야 할 설계

## 23.1 다섯 지표를 하나의 Y축에 겹치기

단위가 달라 해석이 불가능하므로 금지한다.

## 23.2 다중 이중축

MOVE, WIP TURN, 재공, HOLD율, WAIT율을 하나의 차트에 여러 Y축으로 겹치지 않는다.

## 23.3 Line별 패널 분리

주요 목적은 동일 지표의 Line 간 비교이므로 Line별로 화면을 나누지 않는다.

## 23.4 Lot Type 전체 동시 노출을 기본값으로 설정

패널당 선이 과도하게 많아지므로 기본값은 전체만 표시한다.

## 23.5 확정되지 않은 인과관계 표시

MOVE 감소를 특정 원인의 확정 기여율로 표현하지 않는다.

## 23.6 일반 노란색 사용

흰 배경에서 식별성이 낮으므로 Line D는 앰버색을 사용한다.

## 23.7 Lot Balance에서 Line 간 비교

Lot Balance는 개별 Line 내부 분석용으로만 사용한다.

---

# 24. 1차 구현 우선순위

## Phase 1: 기본 비교 화면

- 전역 필터
- FlowStack
- 5개 지표
- 4개 Line
- Lot Type 전체/PP/PG(TT)
- 공통 시간축
- Line 클릭
- 지표 클릭
- 선택 기간 연동

## Phase 2: 기본 Drill-down

- MOVE/WIP TURN 공통 Flow Loss Analysis
- 재공 투입량 및 재공 추이
- HOLD 원인 및 파트원 분석
- WAIT 범주 분리
- 상세 목록

## Phase 3: Lot Balance

- 개별 Line 선택
- Layer 통합 보기
- Proc별 보기
- Layer 클릭 Drill-down
- Proc 클릭 Drill-down
- 선택 시점 Snapshot

## Phase 4: 고도화

- 이상 구간 자동 표시
- 진행제약 누적시간
- 순 진행불가시간
- 중첩률
- 추정 MOVE 손실
- 정상 범위
- 장기 정체 우선순위
- URL 상태 공유

---

# 25. Claude Code 작업 지시 요약

아래 요구사항을 기준으로 PC 전용 웹페이지를 설계하고 구현한다.

1. 전체 화면명은 `Fab Flow Monitor`로 한다.
2. 좌측 영역은 `FlowStack`, 우측 영역은 `Cause Explorer`로 명명한다.
3. 좌측 32%, 우측 68% 비율을 기본으로 한다.
4. FlowStack에는 MOVE, WIP TURN, 재공, HOLD율, WAIT성 진행불가율을 세로로 배치한다.
5. 다섯 패널은 공통 X축을 공유하고 Y축은 지표별 독립으로 둔다.
6. Line은 네 개이며 색상은 파랑, 초록, 주황, 앰버로 고정한다.
7. Lot Type은 전체=굵은 실선, PP=얇은 실선, PG(TT)=점선으로 표현한다.
8. 기본 Lot Type은 전체다.
9. 지표 패널 클릭 시 우측 Cause Explorer가 해당 지표 분석으로 바뀐다.
10. Line 선 클릭 시 우측 분석 대상 Line이 변경된다.
11. 선택 기간, Line, Lot Type은 지표를 바꿔도 유지한다.
12. MOVE와 WIP TURN은 같은 Flow Loss Analysis를 공유한다.
13. MOVE/WIP TURN 감소 원인은 확정 기여율로 표현하지 않고, 성과 Gap과 진행 제약 노출량을 분리한다.
14. 재공 Drill-down에는 Lot Type별 투입량 추이, 재공 추이, Lot Balance를 포함한다.
15. Lot Balance는 개별 Line 하나만 대상으로 한다.
16. Lot Balance에는 `Layer 통합 보기`와 `Proc별 보기` 두 가지 모드를 제공한다.
17. Layer 통합 보기에서는 X축을 Layer, Y축을 재공량으로 하고 Layer 내부를 Proc별 누적 구성으로 표현한다.
18. Proc별 보기에서는 선택 Proc의 Layer별 재공, 유입, 유출, 순증감, 체류시간, HOLD, WAIT를 보여준다.
19. HOLD Drill-down에는 원인, 유형, 파트원, 지속시간, Aging, Lot 목록을 포함한다.
20. WAIT Drill-down은 설비·환경 제약과 개별 Lot 제약으로 구분한다.
21. 설비·환경 제약은 Down, PM, Local, TIP으로 구성한다.
22. 개별 Lot 제약은 Exception, FTP로 구성한다.
23. 모든 분석에서 현재 진행 중과 종료 건을 구분할 수 있어야 한다.
24. 실제 데이터 산식과 테이블 매핑은 구현 전에 별도 정의가 필요하므로 임의로 추정하지 않는다.

---

# 26. 최종 설계 요약

이 화면은 다음의 2단 구조를 갖는다.

## 좌측 FlowStack

- 4개 Line 비교
- 5개 지표 동시 확인
- 공통 시계열
- 이상 시점 탐색
- 분석 대상 선택

## 우측 Cause Explorer

- 지표별 원인 분석
- Line별 Drill-down
- Area, Layer, Proc, Step, 설비, Lot 단위 탐색
- 현재 진행 중 문제와 장기 지속 문제 확인
- 최종 상세 목록 연결

Lot Balance는 Line 간 비교 차트가 아니라 개별 Line 내부의 공정 흐름 균형을 확인하는 분석 도구다.

Lot Balance는 다음 두 모드를 반드시 제공한다.

```text
Layer 통합 보기
Proc별 보기
```

이 구조를 통해 사용자는 전체 Line 비교에서 시작해 특정 Line, 특정 지표, 특정 Layer 또는 Proc, 최종 개별 Lot과 설비까지 일관된 흐름으로 내려갈 수 있다.
