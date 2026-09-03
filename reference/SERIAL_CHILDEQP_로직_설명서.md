# SERIAL 설비(CHILDEQP) 경로 전개 로직 — 이식 설명서

FACTS 프로젝트(`my_def.py`)에서 쓰는 SERIAL 설비 처리 방식을 다른 프로젝트로
옮기기 위한 설명서. 실제 동작 코드에서 추출했으며, 함정 위주로 적었다.

---

## 1. 한 줄 요약

`CHILDEQP` 문자열은 **설비 내부를 웨이퍼가 지나가는 경로**를 표현한다.
이 문자열 하나를 파싱해 **가능한 모든 경로 조합**으로 펼치고, 각 조합마다
진행 가능 여부(`prevent`)를 계산한다.

---

## 2. 문법 — 여기서 대부분 틀린다

```
a;b : c;d : e
└─┬─┘ └─┬─┘ └┬┘
 stage1 stage2 stage3
```

| 기호 | 의미 | 논리 |
|---|---|---|
| `:` | **stage 구분자** | **AND** — 모든 stage를 통과해야 진행 가능 |
| `;` | stage 안의 챔버 구분자 | **OR** — 그중 하나만 살아 있으면 그 stage는 통과 |

### 파싱 순서가 계약이다

```python
raw_stages = [s.strip() for s in expr.split(":") if s.strip()]   # ① ':' 먼저
for stage in raw_stages:
    raw_tokens = [t.strip() for t in stage.split(";") if t.strip()]   # ② 그 다음 ';'
```

**`:` 를 먼저 자른다.** 순서를 바꾸면 의미가 정반대가 된다.

`a;b:c;d:e;f:g;h` 를 넣으면:

```
(a;b) : (c;d) : (e;f) : (g;h)      ← 올바름. stage 4개
```

이걸 `;` 먼저 자르면 `a ; (b:c) ; (d:e) ; (f:g) ; h` 가 되어 완전히 다른 구조가
된다. 리뷰할 때 이 지점을 가장 먼저 확인할 것.

---

## 3. 진행 가능 판정 — 3단 롤업

상태값은 `DOING` / `PREVENT` / `미등록` 세 가지다.
(`DOING` = 진행 중/가능, `PREVENT` = 진입 금지, `미등록` = 정보 없음)

### 3-1. 챔버 단위 (`get_chamber_prevent`)

같은 챔버에 행이 여러 개일 수 있어 먼저 요약한다.

```
DOING 이 하나라도 있으면      → DOING
없고 PREVENT 가 있으면        → PREVENT
둘 다 없으면                  → 미등록
```

### 3-2. stage 단위 (`get_stage_prevent`) — `;` = OR

```
챔버 결과 중 DOING 이 하나라도 있으면   → DOING
DOING 없고 PREVENT 가 있으면            → PREVENT
그 외                                    → 미등록
```

OR 이므로 **하나만 살아 있어도 통과**다.

### 3-3. path 단위 (`get_path_prevent`) — `:` = AND

```
모든 stage 가 DOING 이면                → DOING
아니고 하나라도 PREVENT 면              → PREVENT
그 외                                    → 미등록
```

AND 이므로 **하나라도 막히면 전체가 막힌다.**

> 세 단계의 우선순위가 서로 다르다는 점에 주의.
> 챔버·stage 는 `DOING` 우선(낙관적), path 는 `모두 DOING` 요구(비관적)다.

---

## 4. 존재하지 않는 챔버 처리

토큰이 실제 설비 목록(`existing_chambers`)에 있는지 먼저 거른다.

```python
available_tokens = [t for t in raw_tokens if t in existing_chambers]
missing_tokens   = [t for t in raw_tokens if t not in existing_chambers]

if len(available_tokens) == 0:
    raise ValueError(f"stage '{stage}' 의 모든 토큰이 현재 그룹 eqpcham에 없음")
```

- stage 안에 **하나라도 살아 있으면** 그 stage 는 계속 진행
- **전멸한 stage 가 하나라도 있으면** 그 경로 자체가 성립 불가 → 에러 행 생성

에러는 예외를 그대로 던지지 않고 `path_error` 컬럼에 사유를 적은 행으로
남긴다. 배치가 죽지 않으면서 원인 추적이 가능하다.

---

## 5. 경로 조합 생성

각 stage 에서 **살아 있는 토큰의 모든 비어있지 않은 부분집합**을 만들고,
stage 간에 곱집합(cartesian product)을 취한다.

```python
def non_empty_subsets_keep_order(tokens):
    out = []
    for r in range(1, len(tokens) + 1):
        out.extend(combinations(tokens, r))     # 입력 순서 유지
    return out

path_combos = list(product(*stage_available_subsets))
```

`(a;b) : (c)` 라면:

| stage1 부분집합 | stage2 부분집합 | 생성 경로 |
|---|---|---|
| `(a)` | `(c)` | `(a) \| (c)` |
| `(b)` | `(c)` | `(b) \| (c)` |
| `(a,b)` | `(c)` | `(a, b) \| (c)` |

### ⚠ 조합 폭발

한 stage 에 토큰 n 개면 부분집합이 `2^n - 1` 개다. stage 가 여러 개면 곱해진다.

```
stage 3개, 각 4토큰 → 15 × 15 × 15 = 3,375 경로
stage 3개, 각 6토큰 → 63 × 63 × 63 = 250,047 경로
```

**이식 전에 실제 데이터의 stage 수와 stage 당 토큰 수 분포를 반드시 확인할 것.**
필요하면 부분집합 크기 상한(예: `r <= 2`)이나 경로 수 상한을 두어야 한다.
FACTS 는 현재 상한이 없다.

### 중복 제거와 정렬

```python
uniq_combo_map = {}                      # 튜플 키로 중복 제거
for combo in path_combos:
    key = tuple(tuple(x) for x in combo)
    uniq_combo_map.setdefault(key, combo)

uniq_combos.sort(key=path_sort_key)
```

```python
def path_sort_key(stage_combo):
    total_cnt  = sum(len(s) for s in stage_combo)          # ① 전체 챔버 수
    stage_lens = tuple(len(s) for s in stage_combo)        # ② stage별 길이
    stage_text = tuple(",".join(s) for s in stage_combo)   # ③ 문자열
    return (total_cnt, stage_lens, stage_text)
```

가장 단순한 경로(챔버 수가 적은 것)가 먼저 온다. 결정적 정렬이라
같은 입력이면 항상 같은 순서가 나온다 — 회귀 비교에 중요하다.

### 표기 형식

```python
def format_path(stage_combo):
    return " | ".join([f"({', '.join(stage)})" for stage in stage_combo])
```

→ `(a, b) | (c)`

`|` 가 stage 경계(AND), 괄호 안 `,` 가 OR 후보다.
**입력 문법(`:`/`;`)과 출력 표기(`|`/`,`)가 다르다.** 혼동 주의.

---

## 6. CHILDEQP 가 없는 경우

경로가 아니라 **챔버 단위**로 처리한다. 별도 분기다.

```python
if len(child_vals) == 0:
    out["path"] = out["eqpcham_norm"].apply(lambda x: f"({x})" if pd.notna(x) else np.nan)
    # prevent 는 챔버 단위(get_chamber_prevent)로만 계산
```

경로가 `(챔버명)` 하나짜리로 나오고, 3단 롤업 없이 챔버 상태가 곧 결과다.

## 6-2. CHILDEQP 값이 여러 개인 경우

같은 path group 안에서 `childeqp` 가 2개 이상이면 **에러 행**을 만든다.
어느 것을 따를지 결정할 근거가 없기 때문이다. 임의로 하나를 고르면 안 된다.

---

## 7. 상류 필터 — 놓치기 쉬운 함정

FACTS 는 원천 조회 단계에서 **`:` 가 없는 `childeqp` 를 버린다.**

```sql
AND INSTR(childeqp, ':') > 0
```

`;` 만 있고 `:` 가 없는 값(= stage 가 하나뿐인 경우)은 여기서 걸러져
`childeqp` 없음 경로(§6)로 빠진다.

**증상**: "분명 childeqp 가 있는데 경로 전개가 안 된다."
→ 파싱 로직이 아니라 이 필터를 먼저 의심할 것.

이식하는 프로젝트에서 stage 1개짜리도 경로로 다루려면 이 조건을 빼야 한다.

---

## 8. 다른 문법과 헷갈리지 말 것

같은 회사 안에서도 시스템마다 구분자 의미가 다르다.

| 시스템 | `_` | `;` | `#` | `:` |
|---|---|---|---|---|
| **FACTS `childeqp`** | — | stage 내 OR | — | **stage 구분 (AND)** |
| PPOS `CHAMBERID` | 호환 설비 구분 | 그룹 내 챔버 구분 | Body–챔버 연결 | Serial 구분 |

`:` 가 Serial/AND 라는 점만 공통이다. 나머지는 다르다.
이식 대상 시스템의 원천이 어느 문법을 쓰는지 먼저 확정할 것.

---

## 9. 이식 체크리스트

- [ ] 원천 `childeqp` 문자열이 `:` / `;` 문법인지 확인 (다른 문법이면 §8)
- [ ] `:` → `;` **순서로** 파싱하는지 확인 (§2)
- [ ] 상류에 `INSTR(childeqp, ':') > 0` 같은 필터가 있는지 확인 (§7)
- [ ] 실제 데이터의 stage 수 / stage당 토큰 수 분포 측정 → 조합 폭발 위험 평가 (§5)
- [ ] 상태값 3종(`DOING`/`PREVENT`/`미등록`)의 원천 컬럼명과 값 매핑 확인
- [ ] 챔버 목록(`existing_chambers`)을 무엇으로 잡을지 결정
      — FACTS 는 같은 path group 안에 실제로 존재하는 `eqpcham` 값
- [ ] 에러를 예외로 던질지, 에러 행으로 남길지 결정
      (배치라면 행으로 남기는 쪽을 권장)
- [ ] 정렬 키를 그대로 쓸지 결정 — 결정적 정렬이라 회귀 비교에 유리

---

## 10. 검증용 최소 케이스

```
입력:  a;b:c;d
챔버:  a, b, c, d 모두 존재

기대 경로 (9개, path_sort_key 순):
  (a) | (c)
  (a) | (d)
  (b) | (c)
  (b) | (d)
  (a) | (c, d)
  (a, b) | (c)
  (b) | (c, d)
  (a, b) | (d)
  (a, b) | (c, d)
```

```
입력:  a;b:c;d
챔버:  a, b 만 존재 (c, d 없음)
기대:  ValueError → stage 'c;d' 의 모든 토큰이 현재 그룹 eqpcham에 없음
       → path_error 행 생성
```

```
prevent 판정:
  a=DOING, b=PREVENT, c=DOING, d=PREVENT
  경로 (a)|(c)     → stage1 DOING, stage2 DOING       → DOING
  경로 (b)|(c)     → stage1 PREVENT, stage2 DOING     → PREVENT
  경로 (a,b)|(c)   → stage1 DOING(OR), stage2 DOING   → DOING
```
