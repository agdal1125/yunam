# Yunam Agent 고도화 — Milestone

> 작성일: 2026-05-22
> 대상 단계: Phase 2.x (현 코드 기준 신규 capabilities 추가 + 운영성 강화)
> 작성자: 사용자 요건 정리 + Claude (Opus 4.7) 실행 계획

---

## 0. 현재 상태 재점검 (CLAUDE.md와 실제 코드의 격차)

CLAUDE.md는 "Phase 1.5까지 끝, Phase 2 아직 시작 안 함"으로 적혀 있으나 실제 repo에는 다음이 이미 있습니다.

| 이미 존재 | 위치 |
|---|---|
| Stock-Agent MCP skill (`build_stock_mcp_skill`) | [gateway/yunam/mcp/stock.py](gateway/yunam/mcp/stock.py) |
| Google Calendar MCP skill | [gateway/yunam/mcp/gcal.py](gateway/yunam/mcp/gcal.py) |
| Memory skill (semantic recall) | [gateway/yunam/skills/memory.py](gateway/yunam/skills/memory.py) |
| Web search skill (Jina) | [gateway/yunam/skills/web.py](gateway/yunam/skills/web.py) |
| Reminders / nudge sweeper | [gateway/yunam/skills/reminders.py](gateway/yunam/skills/reminders.py), [gateway/yunam/scheduler.py](gateway/yunam/scheduler.py) |
| Multi-principal (visibility + privacy) | [gateway/yunam/auth.py](gateway/yunam/auth.py), [gateway/yunam/skills/privacy.py](gateway/yunam/skills/privacy.py) |
| Obsidian graph skill | [gateway/yunam/skills/obsidian_graph.py](gateway/yunam/skills/obsidian_graph.py) |
| Deep-think sub-agent (Opus 4.7, `/think`) | [gateway/yunam/subagents/deep_think.py](gateway/yunam/subagents/deep_think.py) |
| Per-turn preference primer | [gateway/yunam/context_primer.py](gateway/yunam/context_primer.py) |
| Parcel / AirQuality skills | [skills/parcel.py](gateway/yunam/skills/parcel.py), [skills/airquality.py](gateway/yunam/skills/airquality.py) |
| 사용량 로깅 (DB 영구화 X) | [orchestrator.py:322-333](gateway/yunam/orchestrator.py#L322-L333) — `logger.info`만, 영속화 없음 |
| 현재 DB user_version | `DB_USER_VERSION = 6` ([sessions.py:160](gateway/yunam/sessions.py#L160)) |
| 현재 SkillRegistry 순서 (cache key) | `obsidian → files → web → airquality → parcel → (gcal?) → (stock?) → reminders → memory → obsidian_graph → privacy` |

**그래서 이 milestone은 "Phase 2 시작"이 아니라 "이미 진행된 Phase 2.x 위에 4개 capability를 append"** 하는 것으로 다시 정의합니다. CLAUDE.md는 Phase 2.4 마지막에 같이 업데이트.

---

## 0.5 — `/help` 명령 (가장 시급, 같은 turn에서 처리)

큐레이션 PR로 `/newsletter`가 추가되면서 슬래시 명령이 5개를 넘었고,
사용자(jaekeun + yoolim)가 검색 없이 한 번에 볼 수 있는 entry point가 필요.

**비용 제약: Anthropic 토큰 0건.** orchestrator를 거치지 않고 handler가
모듈 상수 문자열을 PTB로 바로 전송. `/chatid` 패턴 그대로.

구현:
- `gateway/handlers/commands.py`에 `HELP_TEXT` 상수 + `on_help` 핸들러.
- `gateway/handlers/__init__.py`에 `CommandHandler("help", on_help)` 등록.
- DB, scope, skill, 큐레이션 모두 변경 없음.

Exit 조건:
- `/help` 응답이 100ms 이내 (DB도 안 거침).
- `api_usage` row 증가 0건.
- 새 슬래시 명령 추가 시 HELP_TEXT 한 줄만 수정.

분리 유지 이유: agent용 `SYSTEM_PROMPT_FRAGMENT`는 "언제 어떤 도구를 쓰는가"를
풍부하게 설명하고, 사용자용 HELP_TEXT는 "어떤 게 가능한가"만 간결하게 보여줌 —
독자가 다르므로 자동 합성하면 양쪽 다 어색해짐.

---

## 1. 사용자 요건 4건에 대한 답

### Q1. X / Threads / Toss Invest 비공식 소스 가능성

| 소스 | 현실적 옵션 | 안정성 | 권장 |
|---|---|---|---|
| **Naver 뉴스** | 검색 OpenAPI (CLIENT_ID/SECRET) + RSS (`/rss/economy.xml` 등) | 높음 (공식) | **Tier-A (필수)** |
| **Toss Invest** (`tossinvest.com/feed/news`) | (a) Next.js 페이지 안의 내부 JSON API 역추적 → 직접 호출, (b) `playwright`로 헤드리스 렌더링 후 DOM 수집 | 중간 (API 노출 변경시 깨짐) | **Tier-A (필수)** — 한국 증권 뉴스 가장 양질, 단 (a) 우선 시도 / (b)는 비용↑ |
| **X (Twitter)** | (a) RSSHub 셀프호스트 (`/twitter/user/<id>`), (b) Nitter 셀프호스트 — 모두 X 차단 회피용이라 깨짐 잦음. 공식 API v2 Basic은 월 $200 | 낮음 (frequent breakage) | **Tier-B (선택)** — RSSHub 셀프호스트를 sibling 컨테이너로 시도, 깨지면 1주 이상 disable |
| **Threads** | 공식 API 미공개, 비공식 wrapper(`threads-py`) 존재하나 ToS 위반 / 차단 위험 | 매우 낮음 | **Tier-C (보류)** — Meta가 Threads API 공개할 때까지 대기. 무리하지 않음 |
| **그 외 RSS** | 한국경제·조선비즈·연합인포맥스 RSS, ZeroHedge, MarketWatch RSS | 높음 | **Tier-A 보강** |
| **MoneyFlow (Stock-Agent)** | 이미 MCP로 연결됨 → 큐레이션 워커가 *결과를 소비*하면 됨 | 본인 소유 | **Tier-A (이미 있음)** |

**결론**: Phase 2.1에서는 Tier-A (Naver + Toss + 기타 RSS + 기존 Stock-Agent)를 1차 출시. Tier-B (X via RSSHub)는 별도 sibling 컨테이너로 분리해서 *깨져도 본체에 영향 없게* circuit-breaker 뒤에 둠. Tier-C는 명시적으로 보류.

### Q2. 1시간 tick + 중요도별 푸시/뉴스레터 분기

채택. 구현 패턴:

```
매 정시 (cron 0 * * * *)
  ├─ fetcher  : 병렬 RSS/API 호출, 새 아이템만 적재
  ├─ summarizer (Haiku) : 각 아이템 2~3줄 요약
  ├─ scorer (Voyage)    : interest_profile 벡터들과 cosine
  └─ router  :
        score >= URGENT_THRESHOLD (e.g. 0.82)  → 즉시 PTBSender.push (출처 URL 포함)
        score >= DIGEST_THRESHOLD (e.g. 0.55)  → digest_queue에 보관
        else                                    → 폐기 (저장은 함, 푸시 안 함)

매일 21:00 (newsletter cron)
  └─ digester (Haiku) : 그날 digest_queue 항목들을 섹션별로 묶어 뉴스레터 1통 작성
                        → PTBSender.push (단일 메시지, 카테고리별 정리)
```

세부사항:
- 임계치는 환경변수로 노출 (`YUNAM_CURATION_URGENT_THRESHOLD=0.82` 등) — 초기 한 달은 튜닝 필요
- "최근 4시간 내 같은 토픽으로 push했으면 dedupe" 규칙 (clustering by URL host + 제목 임베딩 유사도)
- 뉴스레터는 토요일/일요일은 "주말 모드" → 정치/시장 카테고리 압축, 관심사 카테고리 우대 (옵션)
- **푸시는 user_id별로 분리되지 않음** (현재 single-principal owner 운영) — 다만 multi-principal 지원이 코드에 이미 있으니 `chat_id` 키로 저장하고 owner에게만 발송

### Q3. Stock-Agent (MoneyFlow) — 이미 MCP로 통합됨, 확인 완료

- [mcp/stock.py](gateway/yunam/mcp/stock.py)에 `StockMCPClient` + `build_stock_mcp_skill` 팩토리 구현
- `YUNAM_STOCK_MCP_URL` 환경변수로 SSE 연결, 도구는 MCP `list_tools`로 동적 발견 (정렬됨 → 캐시 순서 안정)
- declared scope: `Scope.STOCK_SUPPLY_READ` (governance 정합성 확보 완료 — 2026-05-22 버그 수정 라운드에서 처리, 기존 `Scope.KNOWLEDGE`는 enum에 존재하지도 않던 stale 참조였음)
- → **재구현 없음**. Phase 2.3의 Finance Guardrail은 Stock-Agent를 *소비*하는 sub-agent로 설계.

### Q4. 왜 profile/* 자동 적용이 위험한가

**근본 원인: LLM 추출 → 영구 self-model 자동 쓰기는 한 방향 누적 오류**

5가지 실패 모드:

1. **추론 오류 누적**: Reflection은 `messages` 표본에서 추상 인사이트를 *추출*하는 작업이고, Haiku가 잘못 일반화할 수 있음 (e.g. "오늘 NVDA 짜증난다" → profile에 "사용자는 반도체 섹터에 부정적" 영구 기록). 한 번 잘못 쓰이면 이후 모든 advice가 그 잘못된 self-model에 정렬됨. *자기실현적 편향*.
2. **검출 어려움**: profile은 평소 obsidian 앱에서 사용자가 직접 보는 파일이지만, 자동 수정 빈도가 높아지면 사용자가 모든 변경을 검수하지 않게 됨. 점진적 드리프트가 가장 위험.
3. **프롬프트 인젝션 표면**: 큐레이션이 가져온 외부 텍스트가 그대로 messages로 들어가고, reflection이 그걸 다시 self-model에 쓰면 → 외부 콘텐츠가 영구 시스템 상태에 침투할 경로가 생김. (예: 뉴스 본문에 "사용자는 high-yield bond에 적극적이다"라는 문장이 우연히 섞여있고 reflection이 화자 식별 실패)
4. **롤백 비용**: obsidian vault는 사용자의 영구 메모 공간. 자동 수정 후 잘못된 줄을 찾아내려면 `git log` 또는 Obsidian Sync 히스토리를 뒤져야 함. *Draft → 승인 → confirm* 패턴은 잘못된 줄을 confirm 직전에 끊을 수 있음.
5. **에이전시 침해**: 사용자의 *자기 이해(self-model)*는 사용자가 편집하는 것이고, agent는 *제안*만 한다는 경계가 핵심. agent가 사용자의 정체성 파일을 자율 수정하기 시작하면 "내가 누군지를 agent가 정한다"는 역전이 발생. 멘토링 도구가 멘토링 대상의 정의를 쓴다는 것은 거버넌스 문제.

**채택 패턴**: nightly reflector는 *draft만 작성* → `~/obsidian/inbox/reflections/YYYY-MM-DD.md`에 둠 → 사용자가 텔레그램에서 `/review-reflections` 또는 자동 morning ping을 통해 승인 → 승인분만 profile/*.md에 append-only로 병합. **삭제는 절대 자동화하지 않음**. 사용자가 obsidian에서 직접 지우거나 수정.

---

## 2. 추가 요건 — Usage / Cost Tracking Skill

별도 skill로 분리. **Phase 2.0에 포함** (가장 먼저 만들어야 다른 Phase 비용을 측정 가능).

핵심 설계:
- 외부 API 호출이 일어나는 모든 지점에 *얇은 wrapper / decorator* 삽입:
  - Anthropic: `orchestrator.py`의 `messages.create` 호출 직후, `response.usage`를 DB에 영구화 (현재는 `logger.info`만)
  - Voyage: `embeddings.py` 호출 시 input bytes/요청 수 카운트
  - Naver, Jina, Sweet Tracker 등 외부 REST: HTTP wrapper가 (provider, endpoint, status, cost_estimate)를 기록
  - MCP (gcal, stock): MCP 자체는 무료지만 *호출 빈도*를 카운트해서 sibling 컨테이너 사용량 추적
- 단가는 코드 안 상수로 (e.g. `RATES = {"claude-sonnet-4-6": {"input": 3/1e6, "output": 15/1e6, ...}}`) → 단가 변경시 한 군데만 손봄
- Skill tools (전부 read-only, `Scope.USAGE_READ`):
  - `usage_summary(period)` — 일/주/월 단위 토큰·비용 합계
  - `usage_breakdown(period, group_by)` — provider/model/skill별 분해
  - `cost_alert_status()` — 일·월 한도 대비 진행도 (한도는 ENV에서)
- 일별 cost가 ENV 한도의 80%를 넘으면 retrospective 메시지에 자동 경고

---

## 3. 전체 로드맵 (수정판)

```
Phase 2.0  Audit & Usage Tracking         (3~5일,  필수 선행)
Phase 2.1  Curation Pipeline              (1.5주,  Naver+Toss+RSS+Stock-Agent 통합)
Phase 2.2  Reflection / Digital Twin      (1.5주,  draft-then-approve 패턴)
Phase 2.3  Finance Guardrail Sub-agent    (1.5주,  Stock-Agent 위에 얹기)
Phase 2.4  Hardening / Eval / CLAUDE.md   (3~4일,  회귀 + 문서 갱신)
```

**각 Phase는 CLAUDE.md governance 체크리스트를 답하고 들어감**: ① scope, ② skill/MCP/subagent, ③ migration.

---

## 4. Phase 2.0 — Audit & Usage Tracking

### 목적
1. 현 운영의 비용·토큰·캐시 hit rate baseline 확보
2. Phase 2.1~2.3 비용 측정을 가능하게 만드는 인프라 선설치

### Scope 결정
```python
# capabilities.py에 추가
USAGE_READ = "usage:read"
```

### 분류 — In-process skill + decorator/wrapper

### DB 마이그레이션 (v6 → v7)
```sql
CREATE TABLE IF NOT EXISTS api_usage (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    provider        TEXT NOT NULL,           -- 'anthropic'|'voyage'|'naver'|'jina'|'sweettracker'|'mcp:stock'|...
    model_or_endpoint TEXT NOT NULL,         -- 'claude-sonnet-4-6'|'embed-multimodal-v1'|'/v1/search/news.json'
    chat_id         INTEGER,                 -- nullable (background jobs는 NULL)
    skill_id        TEXT,                    -- 어느 skill의 호출이었는지 (nullable for orchestrator-level calls)
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    cache_read_tokens   INTEGER,
    cache_create_tokens INTEGER,
    units           INTEGER,                 -- 토큰이 없는 호출(REST)의 수량 단위(e.g. 1 = 1 request)
    cost_usd_micro  INTEGER NOT NULL,        -- µUSD (정수, decimal 회피)
    elapsed_ms      INTEGER,
    status          TEXT NOT NULL DEFAULT 'ok',  -- 'ok'|'error'|'partial'
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_api_usage_created ON api_usage(created_at);
CREATE INDEX IF NOT EXISTS idx_api_usage_provider ON api_usage(provider, created_at);
```

### 산출물
1. `gateway/yunam/usage/__init__.py` — `UsageRecorder` 클래스 (싱글톤). 모든 wrapper가 여기로 push.
2. `gateway/yunam/usage/rates.py` — 단가 상수 (모델별, 외부 API별). 변경시 한 곳만.
3. `orchestrator.py` 수정 — `response.usage` 영구화 (한 줄 추가)
4. `embeddings.py` 수정 — `embed_*` 후 `recorder.record_voyage(...)` 호출
5. `tools/web.py`, `tools/parcel.py`, `tools/airquality.py` 같은 외부 REST 호출 지점에 동일 wrapper 적용
6. MCP 호출 지점 (`stock.py`, `gcal.py`)에 호출 카운트
7. `gateway/yunam/skills/usage.py` — 신규 skill (3개 tool)
8. **베이스라인 리포트** (별도 산출물, 채팅으로):
   - 최근 7일 cost/token 분포
   - cache hit rate (`cache_read / (cache_read + cache_create + input)`)
   - 가장 비싼 skill, 가장 잦은 skill
   - 현재 SkillRegistry 순서 스냅샷 → Phase 2.4까지 변경 금지 베이스라인

### SkillRegistry 등록 순서
`...→ privacy → usage` (append 끝)

### Exit 조건
- 기존 한 턴을 재현시켜 `api_usage`에 정확히 1 row (anthropic) + (있다면) voyage/web/parcel 행이 추가되는지 검증
- `usage_summary("today")` 응답이 텔레그램 메시지에서 정상 렌더
- cost_alert 임계치 ENV로 설정 가능, 임계 도달시 경고 동작 확인 (mock으로)

### 위험요소 / 가드
- **wrapper가 throw하면 본 함수가 죽으면 안 됨** → 모든 record 호출은 `try/except`로 감싸고 실패는 WARNING만
- **DB write가 hot path를 느리게 하면 안 됨** → `asyncio.create_task(...)`로 fire-and-forget (단, 종료시 await로 flush)
- **prompt cache prefix 영향 0** — 본 skill은 마지막에 append, prompt fragment는 짧고 byte-stable

---

## 5. Phase 2.1 — Curation Pipeline

### Scope 결정
```python
CURATION_FETCH  = "curation:fetch"    # 외부 소스 수집
CURATION_RANK   = "curation:rank"     # 임베딩 기반 관련도
CURATION_READ   = "curation:read"     # 대화 중 큐레이션 히스토리 조회
CURATION_ADMIN  = "curation:admin"    # 관심사 프로파일 수정
```
*Note: `CURATION_NOTIFY`는 별도 scope로 두지 않고, push 자체는 background runner가 PTBSender를 직접 호출 — agent의 tool surface에서 push를 *못* 하게 막는 게 안전.*

### 분류 — Background runner + In-process skill (no sub-agent)

### 모듈 배치
```
gateway/yunam/runners/
  __init__.py
  curator.py          # 메인 워커. 1시간 tick.
  sources/
    base.py           # FeedSource Protocol (async def fetch() -> list[CuratedItem])
    naver_news.py     # 검색 OpenAPI 기반
    toss_invest.py    # Next.js 내부 JSON API 역추적 (옵션 a) / 실패시 playwright fallback (옵션 b)
    rss_generic.py    # 일반 RSS/Atom 어댑터 (한경, 조선비즈, 연합인포맥스 등)
    rsshub_x.py       # RSSHub via 사이드카 컨테이너 (Tier-B, circuit-breaker)
    moneyflow_pull.py # Stock-Agent MCP 호출로 "오늘 수급 핫이슈" 받아오기
  summarizer.py       # Haiku 호출, 2~3줄 요약 일괄
  scorer.py           # Voyage 임베딩 + sqlite-vec KNN vs interest_profile
  router.py           # URGENT/DIGEST/DROP 분기
  digester.py         # 21:00 뉴스레터 작성
gateway/yunam/skills/
  curation.py         # 대화 중 조회/관심사 편집 tool
```

### DB 마이그레이션 (v7 → v8)
```sql
CREATE TABLE IF NOT EXISTS curated_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL,           -- 'naver'|'toss'|'rss:한경'|'x:rsshub'|'moneyflow'|...
    external_id  TEXT NOT NULL,           -- 소스의 고유 id (URL hash 등) — UNIQUE constraint for dedup
    url          TEXT NOT NULL,
    title        TEXT NOT NULL,
    raw_excerpt  TEXT,                    -- 본문 일부 (요약 전 원문 일부)
    summary      TEXT,                    -- Haiku 요약
    score        REAL,                    -- best matching interest의 cosine
    matched_interest TEXT,                -- 어떤 관심사 라벨에 매칭됐는지
    routed_as    TEXT,                    -- 'urgent'|'digest'|'drop'
    fetched_at   TEXT NOT NULL,
    pushed_at    TEXT,                    -- urgent push 발생 시각
    digested_at  TEXT,                    -- newsletter 포함 시각
    UNIQUE(source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_curated_fetched ON curated_items(fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_curated_routed ON curated_items(routed_as, pushed_at, digested_at);

CREATE TABLE IF NOT EXISTS interest_profile (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    label        TEXT NOT NULL,           -- 'AI 인프라', '한국 금리', '연준 발언', '북한 도발'...
    anchor_text  TEXT NOT NULL,           -- 임베딩 만들 본문
    weight       REAL NOT NULL DEFAULT 1.0,
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    UNIQUE(label)
);

-- vec0 virtual tables — curated_items와 interest_profile 각각의 임베딩
-- (기존 file_embeddings 패턴과 동일)
CREATE VIRTUAL TABLE IF NOT EXISTS curated_item_vectors USING vec0(
    item_id INTEGER PRIMARY KEY,
    embedding FLOAT[1024]
);
CREATE VIRTUAL TABLE IF NOT EXISTS interest_vectors USING vec0(
    interest_id INTEGER PRIMARY KEY,
    embedding FLOAT[1024]
);
```

### 환경변수 추가
| Name | 기본값 | 용도 |
|---|---|---|
| `YUNAM_CURATION_ENABLED` | unset | truthy 시 runner 활성화 |
| `YUNAM_CURATION_INTERVAL_MINUTES` | 60 | tick 간격 |
| `YUNAM_CURATION_URGENT_THRESHOLD` | 0.82 | 즉시 푸시 임계치 |
| `YUNAM_CURATION_DIGEST_THRESHOLD` | 0.55 | 뉴스레터 포함 임계치 |
| `YUNAM_CURATION_NEWSLETTER_TIME` | 21:00 | 뉴스레터 발송 시각 (로컬) |
| `NAVER_CLIENT_ID` / `NAVER_CLIENT_SECRET` | — | Naver OpenAPI |
| `YUNAM_RSSHUB_URL` | unset | Tier-B X 소스 (선택) |
| `YUNAM_TOSS_FETCH_MODE` | `api` | `api` 또는 `playwright` |

### Toss Invest 접근 전략 — 단계별 fallback
1. **1차**: 브라우저로 `https://www.tossinvest.com/feed/news` 열어 DevTools → 어떤 XHR/fetch 가 새 아이템 JSON을 가져오는지 확인 → 그 endpoint를 `sources/toss_invest.py`에서 `httpx.get`으로 직접 호출. User-Agent와 (있다면) referer 헤더 모방.
2. **2차 (1차 깨졌을 때)**: `playwright` 사이드카로 페이지 렌더 → DOM에서 카드 수집. 컨테이너 메모리 증가 큼 (Chromium ~150MB) → VPS 2GB에서는 큐레이션 워커 전용 컨테이너로 분리 권장.
3. **3차**: 토스 차단 / 약관 이슈 명백시 disable. Naver + RSS만으로 운영.

### 캐시 무결성 체크
- `curation` skill의 `SYSTEM_PROMPT_FRAGMENT`는 module-level 리터럴. 임계치·시각은 *프롬프트가 아니라 코드 상수/ENV*.
- SkillRegistry 등록: `...→ usage → curation` (Phase 2.0 뒤에 append)
- background runner는 SkillRegistry를 *읽기만*; tool surface 미변경

### Exit 조건
- Naver OpenAPI fixture로 5건 → 1차 요약 → 점수 → 1건 URGENT 푸시, 3건 digest 큐 적재 검증
- 21:00 디지스터 모의 실행 → 단일 텔레그램 메시지로 카테고리별 정리 출력
- `curated_items` UNIQUE 제약으로 동일 URL 재수집시 INSERT 무시 동작 확인
- `api_usage`에 Naver/Toss/Voyage/Haiku 호출이 다 기록되는지 검증 (Phase 2.0 의존)
- system prompt 바이트 변화: *마지막 fragment 추가분만* — 다른 fragment 0 바이트 변경

---

## 6. Phase 2.2 — Reflection / Digital Twin (Draft-Then-Approve)

### Scope 결정
```python
MEMORY_WRITE      = "memory:write"        # 단기 episodic note 작성
MEMORY_REFLECT    = "memory:reflect"      # nightly 작업 전용 (agent tool로는 노출 안 함)
MEMORY_DRAFT_READ = "memory:draft_read"   # 사용자가 draft 검토할 때
```
(기존 `MEMORY_READ`는 그대로)

### 분류
- **In-process skill** `memory` 확장 — 기존 `recall`에 `note(content, kind)` 추가
- **Background reflector** `runners/reflector.py` — agent tool 아닌 사이드 작업
- **Draft 검토용 skill** `skills/reflections.py` — `list_pending_reflections`, `confirm_reflection`, `discard_reflection` (Scope.MEMORY_DRAFT_READ)

### 메모리 계층 (확정)
```
L0  messages                 (기존, raw 대화)
L1  episodic_notes           (신규, 즉석 메모)
L2  reflections              (신규, nightly 추출, draft 상태로 시작)
L3  ~/obsidian/profile/*.md  (기존, 수동 승인 후 append-only로만 병합)
```

### DB 마이그레이션 (v8 → v9)
```sql
CREATE TABLE IF NOT EXISTS episodic_notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    user_id     INTEGER,
    kind        TEXT NOT NULL,        -- 'decision'|'worry'|'plan'|'mood'|'misc'
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_episodic_chat ON episodic_notes(chat_id, created_at DESC);

CREATE TABLE IF NOT EXISTS reflections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id         INTEGER NOT NULL,
    user_id         INTEGER,
    content         TEXT NOT NULL,
    source_kind     TEXT NOT NULL,    -- 'message_window'|'note_cluster'|'mood_pattern'
    source_window_from TEXT,
    source_window_to   TEXT,
    confidence      REAL,
    status          TEXT NOT NULL DEFAULT 'draft',  -- 'draft'|'confirmed'|'discarded'
    confirmed_at    TEXT,
    obsidian_path   TEXT,             -- confirm 시 어디로 병합되었는지 ('profile/worries.md' 등)
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reflections_status ON reflections(status, created_at);

CREATE VIRTUAL TABLE IF NOT EXISTS reflection_vectors USING vec0(
    reflection_id INTEGER PRIMARY KEY,
    embedding FLOAT[1024]
);
```

### 자동 적용 금지 / Draft-then-approve 흐름
1. nightly `reflector` (예: 03:00) → `messages` + `episodic_notes` 최근 24h 분석 → Haiku로 3~5개 reflection 추출 → `reflections` 테이블에 `status='draft'`로 INSERT, vector 동기 작성
2. 다음 날 morning ping (예: 09:00, 옵션) — "검토할 reflection 3개 있어요: [list]" 텔레그램 메시지
3. 사용자가 텔레그램에서 한 줄씩 `/confirm <id>` 또는 `/discard <id>` (별도 command 또는 inline button)
4. `confirm` 호출 시: obsidian의 **inbox/reflections/YYYY-MM-DD.md**에 append (profile/* 직접 수정 X — *inbox에 던지고* 사용자가 obsidian 앱에서 profile/에 직접 이동)
5. `recall_reflection(query)` tool은 *confirmed만* 검색 대상

### Context injection
**기존 `context_primer.py` 패턴 유지** — 자동 reflection 주입 *안 함*. 사용자가 멘토링이 필요할 때는 agent가 `recall` 또는 `recall_reflection`을 *명시적으로* 호출. 자동 primer를 시스템 프롬프트에 욱여넣는 안티패턴은 피함.

### 캐시 무결성 체크
- `memory` skill 기존 fragment는 *수정 없이* 그대로 (`recall` 도구만 사용). 신규 `note`/`recall_reflection` 도구는 같은 skill 안에 append되지만 그러면 fragment 변경 발생 → **별도 skill `memory_v2`로 분리** 또는 `reflections` skill로 신규 모듈화 (후자 권장).
- SkillRegistry 순서: `...→ curation → reflections` append

### Exit 조건
- 30개 가짜 messages → reflector 1회 실행 → `reflections.status='draft'` 3~5건 생성
- `/confirm <id>` 호출 시 `obsidian/inbox/reflections/2026-05-22.md` append + 상태 변경
- `recall_reflection("최근 투자 고민")` → confirmed만 반환
- *어떤 nightly job도 profile/*.md를 직접 쓰지 않음* 검증 (테스트 fixture로)

---

## 7. Phase 2.3 — Finance Guardrail Sub-agent

### Scope 결정
```python
SUBAGENT_FINANCE    = "subagent:finance"        # outer agent → ask_finance 호출 권한
FINANCE_RULES_READ  = "finance:rules_read"      # sub-agent 내부, 투자 원칙 vault 읽기
FINANCE_LEDGER_READ = "finance:ledger_read"     # sub-agent 내부, mistake 조회
FINANCE_LEDGER_WRITE= "finance:ledger_write"    # sub-agent 내부, mistake 기록
```
*Stock MCP scope 정리는 2026-05-22 버그 수정 라운드에 선행 처리됨 (`STOCK_SUPPLY_READ`). Phase 2.3는 finance-specific scope (`subagent:finance`, `finance:rules_read`, `finance:ledger_*`)만 추가.*

### 분류 — Sub-agent + 별도 mistake_ledger skill
- **Sub-agent** `subagents/finance_advisor.py`:
  - 외부 노출: `ask_finance(query)` 단일 tool, scope `SUBAGENT_FINANCE`
  - 모델: 기본 `claude-sonnet-4-6`. *매수/매도 의향 감지된 turn*만 내부에서 Opus 4.7 + adaptive thinking으로 승격
  - 자체 tool set (sub-agent 내부에서만 접근):
    - `read_investment_rules()` — `~/obsidian/finance/rules/` 읽기
    - `read_mistake_ledger(ticker_or_topic?)` — mistake_ledger DB 조회
    - `write_mistake_ledger(...)` — 위반 정황 기록
    - Stock MCP의 `analyze_supply`, `get_historical_supply` — 기존 MCP를 sub-agent에 *위임*
  - 자체 `SYSTEM_PROMPT`: 본체와 분리. **Yunam 본체 prompt에 결합되지 않음** (sub-agent의 핵심 분리 포인트)
- **Mistake Ledger skill** `skills/mistake_ledger.py` — 외부에서도 조회 가능 ("내 실수 보여줘"는 본체 agent도 답할 수 있어야 함)

### 가드레일 강제 구조 — XML prefill X, system prompt 행동 규약 + thinking
Sub-agent SYSTEM_PROMPT 골자 (실제 문구는 구현 시 다듬음):
```
역할: jaekeun의 투자 원칙 집행자. 거절하는 게 기본값.
필수 행동 순서 (모든 답변 전):
  1) read_investment_rules() — 항상 호출
  2) 사용자 발화에 ticker/섹터가 보이면 read_mistake_ledger(...)
  3) 수급/펀더멘털 신호가 필요하면 analyze_supply(...)
이 호출들의 결과 없이는 매수/매도/보류 결론 금지.

답변 구성:
  ① 원칙 평가 — 어떤 rule에 부합/위배. rule 인용 필수.
  ② 과거 실수 대조 — ledger에 유사 패턴이 있으면 명시. 없으면 "관련 기록 없음" 명시.
  ③ 수급/시장 데이터 평가 — analyze_supply 결과 인용.
  ④ 결론 — 매수/매도/보류/리밸런싱 권고 + 위배시 거절 이유.

"매수/매도" 의향이 보이는 turn에서는 ②와 ④에서 위배 가능성을 *먼저* 평가하고,
ledger에 기록할 만한 정황이면 write_mistake_ledger(...) 호출.
```
- Forced reasoning은 *행동 규약*으로 강제 (Opus 4.7 prefilling 금지 우회)
- 외부 tool 호출 자체가 reflection 역할 — ledger가 비어 있어도 조회 자체로 사용자의 과거 행동이 모델 context에 들어옴

### Mistake Ledger 저장소 — Hybrid
- **검색·자동 조회용 정형 데이터**: DB 테이블 `mistake_ledger`
- **사람이 읽는 narrative**: `~/obsidian/finance/mistakes/YYYY-MM-DD-<ticker>.md` (사용자가 obsidian에서 직접 가독)
- write 시 양쪽 동시 (DB는 sub-agent용, MD는 본인용)

### DB 마이그레이션 (v9 → v10)
```sql
CREATE TABLE IF NOT EXISTS mistake_ledger (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    ticker          TEXT,
    sector          TEXT,
    rule_violated   TEXT NOT NULL,      -- 'no FOMO', 'position size > 10%', ...
    narrative       TEXT NOT NULL,
    outcome_pnl_pct REAL,               -- nullable, 사후 기입 가능
    obsidian_path   TEXT,
    created_at      TEXT NOT NULL,
    tags_json       TEXT                -- JSON array
);
CREATE INDEX IF NOT EXISTS idx_mistakes_ticker ON mistake_ledger(ticker, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_mistakes_user ON mistake_ledger(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS finance_intents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    user_id     INTEGER,
    ticker      TEXT,
    action      TEXT NOT NULL,           -- 'buy_intent'|'sell_intent'|'hold_intent'
    captured_at TEXT NOT NULL,
    advised     TEXT,                    -- 'allowed'|'blocked'|'qualified'
    resolved_at TEXT,
    pnl_pct     REAL
);
```

### 캐시 무결성 체크
- Sub-agent의 system prompt는 **별도 모듈 상수**. Yunam 본체 prompt와 결합 안 됨.
- 본체 SkillRegistry에는 *단일 ToolSpec* `ask_finance`만 추가됨 → byte 변화 최소
- 본체 등록 순서: `...→ reflections → mistake_ledger → finance` (3개 append)
- Stock MCP scope 변경(`KNOWLEDGE` → `STOCK_SUPPLY_READ`)은 cache key가 아닌 *DB 컬럼*에만 영향 → 안전 (단 일관성 확인 후 적용)

### Exit 조건
- 시나리오 1: "NVDA 3주만 살까?" → sub-agent가 `read_investment_rules()` + `read_mistake_ledger("NVDA")` + `analyze_supply("NVDA")` 자동 호출 → 원칙 위배 케이스면 거절 + ledger 기록 + `finance_intents`에 `advised='blocked'` row
- 시나리오 2: "내 투자 실수 보여줘" → 본체 agent가 `mistake_ledger.list_recent()` 호출 (sub-agent 안 거침)
- *outer messages 테이블에 sub-agent 내부 tool_use/tool_result 누출 없음* 검증
- `api_usage`에 outer + inner의 두 Anthropic 호출이 분리 기록

---

## 8. Phase 2.4 — Hardening & Eval & CLAUDE.md 갱신

### 회귀 테스트
- 각 Phase 핵심 시나리오 5~10건씩 fixture (가짜 Claude로 dispatch)
- 단일 명령 `python scripts/repl.py --regression`으로 전부 돌릴 수 있게
- 회귀 통과 조건:
  - `tool_calls`에 정확한 `skill_id` + `scope` 기록
  - `api_usage`에 정확한 호출 수 / 비용 기록
  - sub-agent 케이스: outer messages 테이블에 inner 누출 0건

### 캐시 hit rate 모니터링
- Phase 2.0 baseline 대비 ±5%p 이내 유지 확인
- 초과 하락시 어떤 fragment가 원인인지 git bisect

### Cost 알람 운영화
- `cost_alert_status()` 호출 → 일일 retrospective 메시지 본문에 한 줄 (해당 일이 임계치 50% 이상일 때만)

### DB 백업 cron (Phase 1.5에 비어있던 부분)
- `~/data/yunam/yunam.db` VACUUM + 압축 + 원격 (B2/S3 또는 SSH로 다른 호스트)
- 별도 컨테이너로 분리하거나 sibling cron — 본체와 분리

### 문서 갱신
- `CLAUDE.md`를 *실제 상태*로 재작성 (현재 Phase 1.5에 머물러 있는 문장들을 Phase 2.x 종료 시점에 맞게)
- 새 governance 항목: "외부 텍스트(curation)는 절대 system prompt에 결합 금지"
- 새 governance 항목: "self-model (profile/*) 자동 수정 금지 — draft → confirm only"
- 새 governance 항목: "background runner는 SkillRegistry 읽기만, 등록 X"

---

## 9. Hard Invariants — 매 PR 체크리스트

- [ ] `SkillRegistry([...])` 등록 순서는 **append-only**. 기존 위치 변경 0.
- [ ] 새 skill의 `SYSTEM_PROMPT_FRAGMENT`는 module-level 리터럴, 보간 없음.
- [ ] 새 ToolSpec마다 정확히 하나의 `Scope`. 새 scope는 [capabilities.py](gateway/yunam/capabilities.py)에 enum으로 추가.
- [ ] 새 DB 변경마다 `DB_USER_VERSION` bump + 컬럼 존재 체크 가드 + 비파괴.
- [ ] tool handler는 async + `str` 반환. 실패는 `VaultError` (또는 동등한 typed error).
- [ ] `data/obsidian`, `data/filevault` chown 금지 (`entrypoint.sh` 손대지 말 것).
- [ ] Sub-agent의 inner tool 호출은 outer session `messages`에 누출 금지.
- [ ] 외부 API 키는 `.env.example`에 placeholder로만, 실제 값은 사용자가 직접 입력.
- [ ] 큐레이션 외부 텍스트는 *user message로만* 진입, 절대 system prompt에 결합 금지.
- [ ] `profile/*.md` 자동 수정 금지. inbox/reflections 경유 + 명시적 confirm만 허용.
- [ ] 새 외부 API 호출 지점은 `UsageRecorder`를 반드시 통과.

---

## 10. 사용자 결정 사항 (착수 전 확정)

다음 항목들은 milestone 확정 후 코드 변경 *전*에 한 번에 답이 와야 Phase 2.0이 깔끔히 출발합니다.

1. **Toss 접근 방식**: 1차로 내부 JSON API 역추적 시도 OK? (실패시 playwright fallback) → 또는 처음부터 playwright?
2. **RSSHub (Tier-B X 소스)**: Phase 2.1에 포함할지, Phase 2.5로 미룰지?
3. **Cost 알람 임계치**: 일일 한도 (e.g. $5/day, $100/month) 기준값?
4. **뉴스레터 시각**: 21:00 KST OK? 주말 모드(토·일은 다른 시간/카테고리) 도입?
5. **Morning reflection ping**: 매일 09:00에 "검토할 reflection 있어요" 자동 ping을 받고 싶은지, 아니면 사용자가 `/reflections`로 명시 조회만 할지?
6. ~~Stock MCP scope 분리~~ → **이미 처리됨** (2026-05-22 버그 수정 라운드).

이 5개에 답이 오면 → Phase 2.0 audit + Usage Tracking부터 PR 단위로 착수합니다.
