# KIC SignalForge

> 공개 금융·기업 자료를 수집·검증하여 **근거 / 반대 근거 / 불확실성**을 함께 제시하는
> 투자 리서치 보조 Agent (MVP)

- **팀명**: 도헌그는감히전설이라고할수있다
- **안전 범위**: 투자 권유 · 자동 매매 · 수익률 보장을 하지 않습니다.
  이 서비스는 **사용자가 검토할 조사 초안**만 만듭니다.
- **모델 추론과 데이터 수집은 분리**되어 있습니다. NVIDIA NIM 은 모델 추론 전용이며
  금융 데이터 공급원이 아닙니다. 금감원 공시·한국은행 통계·국가통계는 각각 별도 커넥터가
  직접 수집합니다.

---

## 1. 빠른 시작 (3분)

```bash
git clone <저장소> && cd KIC_SignalForge
python -m venv .venv && .venv\Scripts\activate      # Windows
pip install -r requirements.txt
copy .env.example .env                               # 키는 여기만 채웁니다(비우워도 실행됨)
python run.py
```

브라우저에서 <http://127.0.0.1:8000> 을 엽니다.

## Cloudflare 공개 데모 배포

이 프로젝트는 정적 화면과 FastAPI를 Cloudflare Worker + Container로 함께 제공합니다.
Containers를 사용하므로 Cloudflare Workers Paid 플랜이 필요합니다. Docker가 설치된 환경에서
`npm install` 후 `npm run deploy`를 실행합니다. Container 이미지가 Dockerfile에서 빌드됩니다.
Git 연동 자동 배포를 설정할 때는 Cloudflare Workers Builds에서 이 저장소를 연결하고,
배포 명령을 `npm run deploy`로 설정합니다.

API 키는 코드나 GitHub 저장소에 넣지 말고 Cloudflare Worker secrets에 등록하세요.
최소 `DART_API_KEY`, `KOSIS_API_KEY`, `NVIDIA_API_KEY`를 등록할 수 있으며,
ECOS는 키가 없어도 공식 샘플 키로 일부 실제 통계를 조회합니다. 배포 후 `workers.dev` 주소가
데모 URL입니다. 데모 데이터와 브리핑 저장소는 현재 Container의 임시 디스크를 사용하므로
Container가 재시작되면 초기화됩니다.

공개 저장소에는 `.env`, `data/`, SQLite 파일을 올리지 않습니다.

**키가 하나도 없어도 바로 동작합니다.** ECOS 는 공식 샘플 키(`sample`)로 실제 데이터를
가져오고, 나머지 커넥터는 "키 없음" 상태로 **명확히 표시**됩니다.
가짜 데이터로 빈칸을 메우지 않습니다.

### 실제 키를 넣었을 때 (2026-10-21 실측)

| 제공기관 | 상태 | 실측 결과 |
|---|---|---|
| NVIDIA NIM | 실제 API | `/v1/models` 83개, 실제 호출 가능 3개 확인, LLM 주장 4건이 원문 대조 통과 |
| 금융감독원 OpenDART | 실제 API | 기업코드 111,764개, 삼성전자 공시 2,782건 조회 |
| 국가데이터처 KOSIS | 실제 API | 산업생산지수 등 통계표 조회, 확정적 표제일 확보 |
| 한국은행 ECOS | 샘플 키 | 키 미설정 상태. 공식 샘플 키로 기준금리·환율 등 실제 데이터 |

### 확인용 명령

```bash
python scripts/probe_apis.py     # 각 기관 엔드포인트/인증/오류코드 실측 점검
python -m pytest tests/ -q       # 테스트 78개
python scripts/smoke_http.py     # 실행 중인 서버 대상 HTTP 스모크 테스트
```

---

## 2. 환경변수

`.env.example` 을 `.env` 로 복사해 값만 채웁니다. **모든 키는 환경변수에서만 읽으며,
코드·프론트엔드·저장소 어디에도 키를 넣지 않습니다.**

| 변수 | 필수 | 기본값 | 설명 |
|---|---|---|---|
| `NVIDIA_API_KEY` | 아니오 | (빈 값) | 없으면 LLM 미사용 모드로 동작 |
| `NVIDIA_BASE_URL` | 아니오 | `https://integrate.api.nvidia.com/v1` | 공식 NIM 호스팅 엔드포인트 |
| `NVIDIA_MODEL` | 아니오 | `nvidia/nemotron-3-super-120b-a12b` | 미지정 시 `/v1/models` 로 현재 모델 확인 |
| `NVIDIA_TIMEOUT_SECONDS` | 아니오 | `45` | LLM 호출 타임아웃 |
| `DART_API_KEY` | 아니오 | (빈 값) | 없으면 DART 수집을 건너뛰고 사유 표시 |
| `ECOS_API_KEY` | 아니오 | (빈 값) | 없으면 공식 샘플 키 `sample` 사용 |
| `KOSIS_API_KEY` | 아니오 | (빈 값) | 없으면 KOSIS 수집을 건너뛰고 사유 표시 |
| `HOST` / `PORT` | 아니오 | `127.0.0.1` / `8000` | 서버 바인딩 |
| `KIC_DATA_DIR` | 아니오 | `./data` | SQLite 저장 위치 (`.gitignore` 포함) |
| `KIC_MAX_RUN_SECONDS` | 아니오 | `90` | 리서치 1회 최대 wall-clock |
| `KIC_MAX_RETRIES` | 아니오 | `2` | 커넥터별 재시도 상한 |
| `KIC_STALE_AFTER_DAYS` | 아니오 | `400` | 이보다 오래된 문서는 최신성 경고 |
| `KIC_LOG_LEVEL` | 아니오 | `INFO` | 로그 레벨 |

---

## 3. 데이터 소스 접근 조건 (2026-10-21 실제 키로 실측 확인)

아래는 **실제 인증키로 응답을 직접 호출해 확인한** 사실입니다. 추정으로 채우지 않았습니다.
`python scripts/probe_apis.py` 로 언제든 재확인할 수 있습니다.

### 3.1 NVIDIA NIM — 모델 추론 (금융 데이터 아님)

| 항목 | 내용 |
|---|---|
| Base URL | `https://integrate.api.nvidia.com/v1` (공식 `build.nvidia.com/llms.txt`) |
| 인증 | `Authorization: Bearer $NVIDIA_API_KEY` (발급: <https://build.nvidia.com/settings>) |
| 엔드포인트 | `POST /v1/chat/completions` (OpenAI 호환), `GET /v1/models` |
| 실측 | `/v1/models` 는 **인증 없이도 200** 을 반환하며 2026-10-21 기준 83개 모델 노출 |
| 실측 | 잘못된 키 → **401** (`Invalid JWT serialization`) → Bearer 방식 확인 |
| 실측 | EOL 모델은 인증 검사 전에 **410 Gone** |
| **중요** | **`/v1/models` 목록에 있어도 해당 계정에서 호출되지 않을 수 있습니다.** 실측 결과 `nvidia/nemotron-3-super-120b-a12b` 등 다수 모델이 **404 (`Function ... Not found for account`)** 이었습니다. |
| **중요** | 대부분의 추천 모델은 **reasoning 모델**이라 `reasoning_content` 가 먼저 토큰 예산을 소비합니다. `max_tokens` 가 작으면 최종 `content` 가 `None` 이 됩니다. |
| 앱 대응 | ① 호출 가능한 모델을 **실제 최소 요청으로 검증**한 뒤 사용(목록만 믿지 않음) ② `chat_template_kwargs.enable_thinking=false` 로 추론 차단 ③ `response_format=json_object` 로 구조화 출력 ④ 응답이 비면 예산을 늘려 재시도 ⑤ 404 가 나면 다음 후보로 자동 전환 |
| 실측 | 이 계정에서 실제 호출 가능 확인: `nvidia/nemotron-3-ultra-550b-a55b`(7.3s), `nvidia/nemotron-3.5-lightning-30b-a3b`(2.9s), `deepseek-ai/deepseek-v4.1-flash`(4.6s) |

### 3.2 금융감독원 OpenDART — 기업 공시

| 항목 | 내용 |
|---|---|
| Base URL | `https://opendart.fss.or.kr/api` |
| 인증 | `crtfc_key` (발급된 40자리) — 질의 파라미터 |
| 신청 | <https://opendart.fss.or.kr> → 회원가입 → **인증키 신청/관리** → 즉시 발급 (무료) |
| 실측 | `/api/corpCode.xml?crtfc_key=...` → **200, 3.6MB ZIP** (Enterprise 111,764개 기업코드) |
| 실측 | `/api/list.json?crtfc_key=...&corp_code=00126380&bgn_de=...` → **status 000, 삼성전자 2,782건** |
| 실측 | 키가 틀리면 `{"status":"010"}` (미등록 키) → 키 유효성 판별 가능 |
| 한도 | 일 20,000회, 동시 조회 기업 100개 (공식 안내 기준) |
| 오류 코드 | `000` 정상 / `010` 미등록 키 / `011` 사용불가 / `012` IP 불가 / `013` 데이터없음 / `014` 파일없음 / `020` 호출한도 / `021` 기업수 초과 / `100` 값 미준수 / `101` 부적절한 접근 / `800` 점검 / `900` 미정의 / `901` 기한만료 |
| 라이선스 | 오픈API 이용약관 준수 (출처 표기 등). `https://opendart.fss.or.kr/intro/terms.do` |
| 앱 대응 | 기업코드 ZIP 을 **로컬 캐시**(TTL 24h)하여 반복 호출 비용을 줄임. 기업명→기업코드 자동 매칭 |

### 3.3 한국은행 ECOS — 거시 통계

| 항목 | 내용 |
|---|---|
| Base URL | `https://ecos.bok.or.kr/api` |
| 인증 | 인증키가 **URL 경로 세그먼트**로 전달 (`/StatisticSearch/{키}/json/...`) |
| 신청 | <https://ecos.bok.or.kr/api/> → 회원가입 → OPEN API 인증키 신청 (본인인증) |
| 샘플 키 | `sample` 로 동작. **1회 호출당 최대 10건** (`ERROR-301`). 일부 통계만 제공 |
| 조회 형식 | `/api/StatisticSearch/{키}/json/{lang}/{start}/{end}/{stat}/{freq}/{시작일}/{종료일}/{항목코드}/` |
| 주기별 날짜 형식 | `D`=YYYYMMDD, `M`=YYYYMM, `Q`=YYYYQn, `A`=YYYY (**형식이 다르면 `ERROR-101`**) |
| 실측 | `sample` 키로 `722Y001`(한국은행 기준금리) 조회 성공 → 2025-12 기준 2.5% |
| 실측 | `KeyStatisticList`(100대 통계지표)도 `sample` 키로 101개 중 10건 조회 성공 |
| 실측 | 동일 시점에 성별/연령 세부항목이 함께 내려오므로 2차 항목코드 `99988`(전체)를 지정해야 aggregate 가 나옴 |
| 오류 코드 | `ERROR-100` 필수값 누락 / `ERROR-101` 통계·날짜 형식 없음 / `ERROR-103` 인증키 / `ERROR-104` 일일 한도 / `ERROR-301` 조회건수 초과(sample 10건 제한) |
| 라이선스 | 한국은행 통계정보는 **출처를 명시하는 한 상업적 용도를 포함하여 무료 사용·가공·재배포 가능** (`ecos.bok.or.kr` 통계정보이용지침) |
| 앱 대응 | 샘플 키의 10건 제한 때문에 **구간을 나눠 역순(최신 우선) 호출**하여 최신값을 확보. 실제 키를 쓰면 1회 200건까지 조회 |

### 3.4 국가데이터처 KOSIS — 산업·국가 통계

| 항목 | 내용 |
|---|---|
| Base URL | `https://kosis.kr/openapi` |
| 인증 | `apiKey` 질의 파라미터 |
| 신청 | <https://kosis.kr/openapi/> → 회원가입 → **활용신청** → 즉시 발급 (무료, 승인 불필요) |
| 주요 엔드포인트 | `statisticsSearch.do?method=getList`, `Param/statisticsParameterData.do?method=getList`, `statisticsData.do?method=getMeta` |
| 실측 | `statisticsSearch.do` 응답은 **JSON 배열**(`{"result":[...]}` 가 아님) |
| 실측 | 데이터 행 필드: 값 `DT` / 시점 `PRD_DE` / 단위 `UNIT_NM` / 항목 `ITM_NM` / 분류 `C1_NM` / **최종갱신일 `LST_CHN_DE`** |
| 실측 | `objL1=ALL` 을 허용하지 않는 통계표가 있음 → `err 20`("요청변수의 값이 잘못되었습니다") |
| 실측 | `statisticsData.do?method=getMeta&type=ITM|OBJ` 로 유효 코드 사전 확보 가능 |
| 실측 | 키 없으면 `{"err":"10"}`, 잘못된 키면 `{"err":"11"}` |
| 오류 코드 | `10` 키 없음/무효 / `11` 키 무효 / `12` 파라미터 형식 / `20` 요청변수 값 오류 / `30` 결과 없음 / `31` 사용기한 만료 / `40` 호출한도 / `41` 응답셀 초과 |
| 제한 | 인증키당 분당 1,000건, 1회 40,000셀, HTTPS 전용 (공고 2026-03-05) |
| 라이선스 | 무료, 이용허락범위 제한 없음, 공공저작물 제1유형(출처표시) |
| 앱 대응 | `objL1`/`itmId` 조합을 **최대 4개만** 시도하고, 통계표당 HTTP 호출을 **12회로 제한**(쿼터 보호). 확정적 표제일(`LST_CHN_DE`)을 발행일로 사용 |

### 3.5 기업 공식 IR — 공개 웹페이지

- 키 불필요. **사용자가 직접 입력한 https URL 만** 조회합니다 (평문 URL 차단).
- 응답 본문 5MB 상한, 타임아웃 적용.

---

## 4. 아키텍처

```
app/
├─ config.py            환경변수 단일 진입점 (비밀값은 여기서만 읽음)
├─ models.py            도메인 모델 (SourceDoc / Claim / Uncertainty / Briefing)
├─ errors.py            오류 taxonomy + 기관별 실제 오류코드 매핑
├─ observability.py     로그 마스킹 필터 + 호출 통계
├─ llm/
│  ├─ base.py           LLMProvider 인터페이스
│  └─ nim.py            NVIDIA NIM 어댑터 (모델 검증 · 재시도 · JSON 파싱)
├─ connectors/          데이터 커넥터 (독립 모듈)
│  ├─ base.py           Connector 인터페이스
│  ├─ http_client.py    재시도/타임아웃/오류분류 공통 HTTP 래퍼
│  ├─ dart.py           금융감독원 OpenDART
│  ├─ ecos.py           한국은행 ECOS
│  ├─ kosis.py          국가데이터처 KOSIS
│  └─ ir.py             기업 공식 IR (공개 웹페이지)
├─ pipeline/
│  ├─ planner.py        1) 질문 분해 · 데이터 소스 선택
│  ├─ retriever.py      3) 수집 (타임아웃·재시도·전체 기한)
│  ├─ parser.py         4) 제목/URL/발행일/기준기간/단위/원문 구절 추출
│  ├─ claims.py         5) 핵심 주장·수치 구조화
│  ├─ evidence.py       6) 근거/반대 근거/상충/불확실성 분리
│  ├─ report.py         7) 출처 링크 포함 브리핑 생성
│  └─ orchestrator.py   전체 실행 + 종료 조건 + 상태 판정
├─ storage/sqlite_store.py   세션 단위 저장 (키·개인정보 미저장)
├─ api/routes.py        REST 엔드포인트
└─ web/static/          반응형 단일 페이지 UI (키 없음)
```

### 파이프라인 동작

1. **Planner** — 질문에 접속·병렬 표현으로 나누고, 키워드로 필요한 커넥터를 선택합니다.
2. **Retriever** — 커넥터별 호출. 실패해도 나머지는 계속됩니다. 전체 wall-clock 상한(`KIC_MAX_RUN_SECONDS`)이 지나면 이후 호출을 건너뜁니다.
3. **Parser** — 제목·URL·발행일·기준기간·단위·원문 구절·수치를 추출하고, 커넥터별 응답의 고유 구조를 정규화합니다.
4. **Claim Extractor** — 관측치를 지표명 그룹으로 묶고, 출처별로 값을 분리합니다.
5. **Evidence Checker** — 인용문 원문 대조, 단위/기간 불일치 분리, 상충 탐지, 최신성 경고, 샘플 경고.
6. **Report Writer** — 출처 표, 주장별 근거/반대 근거 카드, 경고 목록, 실패한 API, 데이터 공백을 담은 Markdown 브리핑.
7. **Orchestrator** — 상태 판정: `completed` / `partial` / `insufficient_evidence` / `failed`.

### 신뢰성 규칙과 구현 위치

| 규칙 | 구현 |
|---|---|
| LLM 수치·인용을 검증 없이 사실로 취급하지 않음 | `claims.merge_llm_claims()` 가 원문 대조 실패 시 주장을 **버림**. `evidence.check()` 가 인용문을 정규화 부분 문자열로 재검증 |
| 모든 핵심 주장에 출처 ID/URL | `Claim.supporting_source_ids` 필수, 보고서에서 출처 표와 링크로 렌더링 |
| 인용문은 실제 원문 구절만 | `parser.verify_quote()` + `evidence.check()` 의 `UNVERIFIED_QUOTE` 경고 |
| 날짜·단위·기간이 다른 수치는 직접 비교하지 않음 | `claims._build_claim()` 이 단위/기간 불일치 출처를 `incomparable_source_ids` 로 분리하고 `UNIT_MISMATCH` / `PERIOD_MISMATCH` 경고 생성 |
| 상충 출처는 양쪽을 제시하고 원인을 단정하지 않음 | `evidence._detect_conflicts()` 가 양쪽 값을 함께 적재, 원인은 추정하지 않음 |
| 검색 실패·API 실패는 원인과 공백을 제시 | `ConnectorError` 를 표준 코드(`timeout`/`quota`/`auth`/`not_found`/…)로 분류해 UI·보고서에 그대로 노출 |
| 재시도·전체 시간 제한 | `KIC_MAX_RETRIES`, `KIC_MAX_RUN_SECONDS`, 커넥터별 `_MAX_CALLS_PER_INDICATOR` |
| 최종 판단은 사용자에게, 자동 매매 없음 | 매매·권유 기능 없음. `Briefing.disclaimer` 상시 노출 |

---

## 5. 평가용 테스트 대응

`tests/test_evaluation_scenarios.py` 에 평가 항목 7종을 그대로 구현했습니다.

| 평가 항목 | 테스트 | 확인 내용 |
|---|---|---|
| 1. 정상 검색 | `test_scenario1_normal_search_each_claim_has_source` | 2개 이상 출처에서 모든 주장에 출처 연결, 보고서에 링크 포함 |
| 2. 출처 없음 | `test_scenario2_no_sources_creates_no_fabricated_evidence` | 주장 0건, 상태 `insufficient_evidence`, "확인 불가" 표기 |
| 3. 상충 자료 | `test_scenario3_conflicting_sources_show_both_sides` | 같은 지표·단위·기간 다른 값 → `contested`, 양쪽 값·출처 모두 표시 |
| 4. API 실패 | `test_scenario4_api_failures_are_transparent` | `timeout`·`quota` 가 코드와 메시지로 보고서에 노출, 커넥터 예외도 격리 |
| 5. 최신성 | `test_scenario5_stale_documents_are_flagged` | 2000일 전 자료에 `staleness` 경고, 발행일 불명은 `missing_date` |
| 6. 보안 | `test_scenario6_*` (3개) | `/api/health`·프론트엔드에 키 없음, 로그 마스킹, 저장소 전수 스캔 |
| 7. 샘플 모드 | `test_scenario7_sample_data_is_distinguishable` | 실제/샘플 건수 분리, 출처 표에 구분 배지, 샘플 의존 주장에 경고 |

추가로 파서·주장 추출·근거 검증·커넥터·API·보안 단위 테스트와, **실측으로 발견한 제약에 대한 회귀 테스트**
(	ests/test_real_world_parsing.py: NIM 404 모델 대체, ECOS 10건 제한·주기별 날짜 형식,
KOSIS 배열 응답·행 필드, 구분자 허용 인용 대조, 모델별 LLM 응답 형태)를 포함해 **총 78개**가 통과합니다.

---

## 6. 보안

- API 키는 **환경변수에서만** 읽습니다. 코드·HTML·JS·SQLite 어디에도 키를 쓰지 않습니다.
- 로그는 `SecretRedactingFilter` 를 거치며, 등록된 비밀값과 `crtfc_key`/`apiKey`/`Bearer` 패턴을 마스킹합니다.
- `/api/health` 는 커넥터별 **설정 여부(boolean)** 만 반환하고 키 값은 반환하지 않습니다.
- 프론트엔드는 키를 알 수 없으며, 모든 요청이 서버 `/api/*` 를 거칩니다.
- `.env`, `data/`, `*.sqlite3` 는 `.gitignore` 에 포함되어 있습니다.
- 테스트 `test_scenario6_no_real_key_material_in_repository` 가 저장소 전체를 스캔해 키로 보이는 값이 들어가지 않았는지 검사합니다.

---

## 7. API

| 메서드 | 경로 | 설명 |
|---|---|---|
| `GET` | `/api/health` | 커넥터 설정 상태·제한값 (키 값 미포함) |
| `POST` | `/api/research` | 리서치 1회 실행 → 브리핑 전체 |
| `GET` | `/api/runs` | 최근 실행 목록 |
| `GET` | `/api/runs/{run_id}` | 브리핑 원본 JSON |
| `GET` | `/api/runs/{run_id}/report.md` | Markdown 브리핑 |

```bash
curl -X POST http://127.0.0.1:8000/api/research \
  -H "Content-Type: application/json" \
  -d '{"question":"한국은행 기준금리와 원/달러 환율 영향은?","period":"2025"}'
```

---

## 8. 시연 순서 (약 6분)

1. `python run.py` 실행 → `http://127.0.0.1:8000` 접속.
2. **연결 상태** 패널 확인 — ECOS 가 "샘플 키", DART/KOSIS/NIM 이 "실제 API" 로 표시되는 것을
   보여줍니다. *(ECOS 키를 넣으면 해당 항목만 "실제 API" 로 바뀝니다.)*
3. 질문 입력: `삼성전자 2025년 실적과 반도체 산업생산 지수의 근거는?` / 기업 `삼성전자` /
   기간 `2025` → **조사 실행**.
4. **2. 조사 결과** 확인
   - 자료 출처 목록: ECOS·DART·KOSIS **실제 자료**가 발행일·기준기간·단위·구분과 함께 표시
   - 주장별 근거/반대 근거 카드: 각 주장에 지지 근거·반대 근거·직접 비교하지 않은 출처가 구분되어 표시
   - **LLM 이 만든 주장**도 인용문이 원문과 대조된 경우에만 포함됩니다(검증 실패 시 자동 제외)
   - 경고 목록: 최신성·자료 상충·발행일 불명·API 실패가 항목별로 표시
   - 데이터 공백: KOSIS 일부 통계표의 파라미터 오류가 사유와 함께 그대로 노출됨
5. 브리핑 원문 탭 → **복사** / **다운로드(.md)** 동작 확인.
6. `python scripts/probe_apis.py` 실행 → 각 기관의 실제 접근 상태와 오류 코드 매핑을 시연
   (NIM 은 "목록에 있으나 계정 미제공"인 모델까지 함께 보여줍니다).
7. `python scripts/smoke_http.py` 실행 → 서버 엔드포인트 전체 스모크 테스트.

### 실제 실행 결과 예시 (2026-10-21, 4개 기관 모두 실제 키 사용)

```
상태: partial | 출처 16건 | 주장 29건 | API 오류 3건 | 40초
출처: ECOS 4건(기준금리·환율·100대 지표·소비자동향), DART 10건(삼성전자 공시), KOSIS 1건(산업생산지수)
LLM : nvidia/nemotron-3-ultra-550b-a55b 사용, 인용 원문 대조 통과 4건
```

---

## 9. 알려진 제한사항 / 미구현 (솔직 고지)

- **DART 공시 원문 본문 파싱은 미구현.** 실제 키로 `corpCode.xml`(111,764개 기업코드)과
  `list.json`(삼성전자 2,782건) 수집은 **검증 완료**했으나, 공시 **본문**(`document.xml`)의
  XBRL/HTML 구조를 끝까지 파싱하지 못했습니다. 그래서 DART 출처는 현재
  **공시명·제출자·접수일·링크**까지 제공하며, 원문 인용은 사용자가 제공한 IR URL 에서만
  자동 추출됩니다.
- **ECOS 키가 없어 샘플 키 모드로 동작.** `ECOS_API_KEY` 를 넣으면 1회 200건 조회로 전환되고
  제공 통계 범위가 넓어집니다.
- **ECOS 샘플 키는 1회 10건 제한**(`ERROR-301`)이라, 앱은 구간을 나눠 역순 호출하여 최신값을
  확보합니다. 관측치 수는 월 12건/년 26건 수준으로 제한됩니다.
- **KOSIS 는 통계표마다 필요한 파라미터가 달라**(`objL1`/`itmId`/`prdSe`) 일부 표는
  여전히 조회에 실패합니다. 실패한 표는 사유와 함께 화면에 표시되며, 다른 검색어·다른 표로
  이어서 시도합니다. 전부 실패하면 해당 출처 없이 진행합니다.
- **IR 파싱은 규칙 기반**입니다. 표·차트가 많은 문서나 메뉴가 많은 홈페이지에서는
  지표 라벨이 부정확할 수 있습니다. 이 경우에도 값·단위·원문 구절은 그대로 보존되며,
  인용 대조로 검증되지 않은 인용은 근거에서 제외됩니다.
- **LLM 출력 형식은 모델마다 다릅니다.** 관측된 3가지 형태(평면형/중첩형/키 이름 상이)를
  모두 파싱하도록 구현했지만, 새 모델을 붙이면 파서를 보완해야 할 수 있습니다.
  또한 모델이 과하게 보수적으로 응답해 0건이 나오면 한 번 재시도하며, 그래도 0건이면
  **"LLM이 검증 가능한 후보 주장을 반환하지 않았다"** 고如实히 표시합니다.
- **장기 Memory 없음.** MVP 는 세션 단위 SQLite 저장만 사용합니다. 원문 텍스트와 메타데이터는
  함께 보관되지만, 벡터 검색·장기 기억은 구현 범위 밖입니다.
- **정량 평가(백테스트) 없음.** 근거·반대 근거 분리 규칙은 단위 테스트로 검증했지만,
  실제 포트폴리오/리서치 성과와의 대조 실험은 수행하지 않았습니다.
- **자동 매매·권유 기능 없음.** 의도적으로 구현하지 않았습니다.
- **1회 실행에 약 40초.** LLM 호출(7~12초)과 KOSIS 다중 파라미터 탐색이 대부분을 차지합니다.
  `KIC_MAX_RUN_SECONDS`(기본 90초) 안에서 종료됩니다.

---

## 10. 공식 기획서 10장 구조와의 정합성

이번 저장소에는 **기획서 원문이 포함되어 있지 않아** 10장 구조와의 문서 정합성을
직접 대조할 수 없었습니다. 구현 관점에서 각 장을支撑하는 요소를 아래에 적어 둡니다.
제출 전, 기획서 원문과 함께 아래 표를 확인해 갱신하십시오.

| 기획서 장 | 구현 근거 |
|---|---|
| 2 Summary / 4 서비스 제안 | `Briefing` 구조, 보고서 요약·출처·주장 섹션, UI |
| 3 문제 정의 | 근거 없는 단정 금지 규칙, 근거 부족 상태(`insufficient_evidence`) |
| 5 Agent Workflow | `app/pipeline/` 의 planner→retriever→parser→claims→evidence→report 6단계 |
| 6 AI 기술·Tool/API | `app/llm/nim.py` (NVIDIA NIM 어댑터) + `app/connectors/` (DART/ECOS/KOSIS/IR) |
| 7 데이터 활용 | 출처 메타데이터(발행일·기준기간·단위) 보존, 문서 청크 + 메타데이터 보관 |
| 8 MVP 개발 계획 | 본 README 1~8장, 테스트 60개 |
| 9 기대효과·확장성 | `Connector` 인터페이스 기반 커넥터 추가, SQLite→벡터DB 교체 지점 |
| 10 출처·AI 활용 내역 | 3장의 실측 근거 표, 9장의 미검증 항목 고지 |

---

## 11. 라이선스 / 배포 시 주의

- 이 저장소의 코드는 자유롭게 수정·사용할 수 있습니다.
- **수집 데이터의 권리·라이선스는 각 제공기관 조건을 따릅니다.** ECOS 는 출처 명시 시
  상업적 용도를 포함한 사용·가공·재배포가 허용되고, KOSIS 는 공공저작물 제1유형(출처표시)이며,
  DART 는 오픈API 이용약관이 적용됩니다. **재배포 시에도 각 기관의 약관을 그대로 준수하십시오.**
- DART·KOSIS·ECOS 의 실제 데이터를 그대로 재호스팅하는 것은 각 기관 약관 범위를 벗어날 수 있습니다.
  이 앱은 기본적으로 **원문 링크를 함께 제시**하는 방식으로 동작합니다.

---

## 12. 팀원 기재 안내

팀원 이름은 현재 미정이므로 **코드·문서에 실명을 임의로 넣지 않았습니다.**
`README`의 팀명 표기와 `app/` 어느 곳에도 개인 이름은 들어 있지 않습니다.
추가 시에는 `README` 10장 대응표와 일치하도록 반영하십시오.
