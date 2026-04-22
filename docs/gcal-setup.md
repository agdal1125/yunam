# Google Calendar (M3) — setup guide

Yunam ↔ [nspady/google-calendar-mcp](https://github.com/nspady/google-calendar-mcp) 연결을 처음부터 끝까지 따라 할 수 있게 쓴 가이드. 총 4 단계, 각 단계마다 **"이 로그/출력이 보이면 다음으로"** 표시가 있습니다.

- Part 1. Google Cloud 콘솔에서 OAuth 크레덴셜 발급 (브라우저, ~15분)
- Part 2. VPS에 파일 배치 (SSH, ~5분)
- Part 3. SSH 포트 포워딩으로 1회성 OAuth consent (~10분, 경로 B)
- Part 4. 일상 배포 + 동작 확인

---

## Part 1 — Google Cloud 콘솔 세팅

laptop 브라우저에서 진행. 모든 단계는 jaekeun의 개인 Google 계정으로 수행.

### 1.1 프로젝트 생성

1. [console.cloud.google.com](https://console.cloud.google.com/) 접속 → 로그인
2. 상단 헤더의 **프로젝트 선택** 드롭다운 → **새 프로젝트**
3. 프로젝트 이름: `yunam-gcal` (아무거나 OK), 조직은 비워둠 → **만들기**
4. 생성 완료 토스트 뜨면 다시 드롭다운에서 해당 프로젝트 선택

✅ 확인: 헤더에 `yunam-gcal` 이 표시되면 OK.

### 1.2 Calendar API 활성화

1. 좌측 햄버거 메뉴 → **API 및 서비스** → **라이브러리**
2. 검색창에 `Google Calendar API` → 클릭 → **사용** 버튼

✅ 확인: 버튼이 **"API 관리"** 로 바뀌면 활성화 완료.

### 1.3 OAuth 동의 화면 구성

1. 좌측 메뉴 → **API 및 서비스** → **OAuth 동의 화면**
2. User Type: **외부(External)** → **만들기**
3. 앱 등록 1단계 (앱 정보):
   - 앱 이름: `Yunam`
   - 사용자 지원 이메일: jaekeun Gmail
   - 개발자 연락처 이메일: jaekeun Gmail
   - **나머지 전부 비워두고** → **저장 후 계속**
4. 앱 등록 2단계 (범위):
   - **범위 추가 또는 삭제** 클릭
   - 필터에 `calendar` 검색 → 다음 **2개 체크**:
     - `https://www.googleapis.com/auth/calendar`
     - `https://www.googleapis.com/auth/calendar.events`
   - **업데이트** → **저장 후 계속**
5. 앱 등록 3단계 (테스트 사용자):
   - **+ ADD USERS** → jaekeun Gmail 입력 → **추가**
   - **저장 후 계속**
6. 요약 확인 → **대시보드로 돌아가기**

✅ 확인: OAuth 동의 화면 > "게시 상태: **테스트** / 사용자 유형: **외부**" 표시.

### 1.4 OAuth Client ID 발급

1. 좌측 메뉴 → **API 및 서비스** → **사용자 인증 정보**
2. **+ 사용자 인증 정보 만들기** → **OAuth 클라이언트 ID**
3. 애플리케이션 유형: **데스크톱 앱 (Desktop app)**
4. 이름: `yunam-gcal-desktop` → **만들기**
5. 팝업에서 **JSON 다운로드** 클릭 → `client_secret_xxxxx.json` 같은 파일 저장됨
6. 파일 이름을 `gcp-oauth.keys.json` 으로 rename

✅ 확인: `gcp-oauth.keys.json` 내용이 아래 모양이어야 함:
```json
{
  "installed": {
    "client_id": "xxxxxxx.apps.googleusercontent.com",
    "project_id": "yunam-gcal",
    "client_secret": "GOCSPX-...",
    "redirect_uris": ["http://localhost"],
    ...
  }
}
```

`"installed"` 키로 시작하면 정답 (Desktop 앱 타입). `"web"` 으로 시작하면 애플리케이션 유형이 잘못된 것 — 1.4를 다시.

---

## Part 2 — VPS에 파일 배치

laptop 터미널에서 실행.

### 2.1 OAuth 키 파일 업로드

```bash
# Laptop에서
scp ~/Downloads/gcp-oauth.keys.json yunam:~/yunam/gcp-oauth.keys.json
```

### 2.2 nspady clone + 권한/gitignore

```bash
ssh yunam
cd ~/yunam

# nspady clone
mkdir -p mcp-servers
git clone https://github.com/nspady/google-calendar-mcp.git mcp-servers/google-calendar-mcp

# 크레덴셜 파일 권한 조이기
chmod 600 gcp-oauth.keys.json

# gitignore — 절대 커밋되면 안 됨
grep -qxF 'gcp-oauth.keys.json' .gitignore || echo 'gcp-oauth.keys.json' >> .gitignore
grep -qxF 'mcp-servers/' .gitignore || echo 'mcp-servers/' >> .gitignore
```

### 2.3 .env 업데이트

VPS의 `~/yunam/.env` 파일 편집 (vim / nano 어느 쪽이든). 맨 밑에 추가:

```bash
# Google Calendar MCP
YUNAM_GCAL_MCP_URL=http://calendar-mcp:3000/mcp
```

✅ 확인:
```bash
ls -la ~/yunam/gcp-oauth.keys.json ~/yunam/mcp-servers/google-calendar-mcp/docker-compose.yml
grep YUNAM_GCAL_MCP_URL ~/yunam/.env
# 각 라인이 "No such file" 없이 떠야 정상
```

---

## Part 3 — OAuth Consent (경로 B: SSH 포트 포워딩)

이 Part는 **처음 한 번만** 필요. 이후 일상 운영에서는 반복하지 않음.

### 전체 그림 먼저

```
 [Laptop 브라우저]  ↔  [SSH -L 터널]  ↔  [VPS localhost:3500~]  ↔  [calendar-mcp 컨테이너]
      ↑                                                                      ↓
      └─────────────── Google OAuth 리디렉트 (http://localhost:3500/...) ───────┘
```

터미널을 **2개** 엽니다:
- **터미널 A**: SSH 터널 유지 (아무것도 안 함, Ctrl+C 할 때까지 돈다)
- **터미널 B**: VPS 들어가서 calendar-mcp 포그라운드 기동, 로그 보면서 진행

### 3.1 진행 전 점검

VPS에서 현재 gateway가 돌고 있다면 굳이 내릴 필요는 없음 — calendar-mcp 컨테이너만 새로 뜨는 거라 충돌 없음. 하지만 **아직 존재하지 않는 `YUNAM_GCAL_MCP_URL` 로 연결 시도하다 gateway가 죽는 것은 문제**. 안전하게:

```bash
# VPS에서 (터미널 B가 될 세션)
ssh yunam
cd ~/yunam

# 만약 gateway가 이미 running이면 잠깐 내려둠 (consent 후 다시 올림)
docker compose stop gateway
```

### 3.2 터미널 A — SSH 터널 열기

**laptop에서 새 터미널** 열고:

```bash
ssh -N -L 3000:localhost:3000 yunam
```

- `-N` = 커맨드 없이 터널만 유지. 프롬프트가 안 돌아오는 게 정상.
- `-L 3000:localhost:3000` = "내 laptop의 3000 포트를 VPS의 localhost:3000 으로 연결".
- nspady의 HTTP 모드에서는 **포트 3000 하나만** 필요합니다 — auth 랜딩, OAuth URL 발급, Google callback 모두 3000에서 처리.
- 이 터미널은 **consent가 끝날 때까지 그대로 둠**. Ctrl+C 하면 터널 끊김.

✅ 확인: 커맨드 실행 후 프롬프트가 돌아오지 않고 대기 상태면 OK.

### 3.3 터미널 B — calendar-mcp 포그라운드 기동

**또 다른 laptop 터미널**에서:

```bash
ssh yunam
cd ~/yunam

# consent override 파일을 같이 적용해서 포트 3500-3505를 VPS localhost에 바인드.
# `--profile gcal` 은 calendar-mcp가 profile-gated이기 때문에 명시적으로 켜는 것.
docker compose \
  -f docker-compose.yml \
  -f docker-compose.consent.yml \
  --profile gcal \
  up --build calendar-mcp
```

로그가 흘러나옵니다. 컨테이너가 빌드되는 동안 기다림 (첫 빌드는 2~3분).

✅ 확인: 로그 마지막에 다음 비슷한 줄이 보여야 함:
```
calendar-mcp | No token file found at: /home/nodejs/.config/google-calendar-mcp/tokens.json
calendar-mcp | ⚠️  No valid normal user authentication tokens found.
calendar-mcp | Visit the server URL in your browser to authenticate, or run "npm run auth" separately.
calendar-mcp | Google Calendar MCP Server listening on http://0.0.0.0:3000
```

서버가 토큰 없이도 올라오는 상태 — 이제 브라우저로 인증 붙일 차례.

### 3.4 laptop 브라우저에서 consent

1. laptop 브라우저 주소창에: **`http://localhost:3000/`** 열기 (터미널 A의 SSH 터널을 타고 VPS로 도달)
2. nspady의 **계정 관리 페이지**가 열림 — "Add Account" 같은 버튼
3. 계정 ID 입력 (예: `normal` 또는 `jaekeun` 아무 문자열) → Submit → Google OAuth URL로 자동 리디렉트
4. Google 로그인 → jaekeun 계정 선택
5. **"이 앱은 확인되지 않았습니다"** 경고 뜨면 → **"고급"** → **"Yunam (안전하지 않음)으로 이동"**
   - 테스트 모드라서 뜨는 경고, 정상
6. 권한 요청 화면: Calendar + Calendar Events 접근 권한 → **허용**
7. Google이 `http://localhost:3000/oauth2callback?code=...&account=...` 로 리디렉트
   - laptop 브라우저가 localhost:3000 을 치면 SSH 터널 → VPS localhost:3000 → calendar-mcp 컨테이너로 전달
8. "Authentication successful" 비슷한 최종 페이지

✅ 확인: 터미널 B 로그에:
```
calendar-mcp | Tokens saved successfully for normal account to: /home/nodejs/.config/google-calendar-mcp/tokens.json
```

### 3.5 정리

- **터미널 B**: `Ctrl+C` 로 calendar-mcp 중지
- **터미널 A**: `Ctrl+C` 로 SSH 터널 끊기 (이제 3000 포트 host 노출 필요 없음)

✅ 토큰이 저장됐는지 확인:
```bash
ssh yunam
docker run --rm -v yunam_calendar-tokens:/data alpine ls -la /data
# tokens.json 파일이 보이면 성공
```

---

## Part 4 — 일상 배포 + 동작 확인

### 4.1 정상 기동

```bash
ssh yunam
cd ~/yunam

# gcal profile을 포함해서 calendar-mcp + gateway 둘 다 기동
docker compose --profile gcal up -d --build

docker compose logs -f gateway
```

> 💡 gcal을 잠시 쉬게 하고 싶으면 `--profile gcal` 을 빼고 `docker compose up -d --build`. 그러면 calendar-mcp는 안 뜨고 gateway는 `YUNAM_GCAL_MCP_URL` 값과 무관하게 gcal 스킬을 비활성화 — wait, 이 경우 gateway는 여전히 URL을 보고 connect 시도하다 실패함. **gcal을 끄려면 .env에서 `YUNAM_GCAL_MCP_URL=` (빈 값) 으로 바꾼 뒤 gateway 재기동**.

✅ 확인: gateway 로그에 다음 라인:
```
yunam.gateway: gcal MCP configured at http://calendar-mcp:3000/mcp — connecting
yunam.mcp.gcal: gcal MCP connected url=... tools=12 (create-event, delete-event, get-current-time, get-event, get-freebusy, list-calendars, list-colors, list-events...)
yunam.gateway: gateway running
```

`tools=12` 가 찍히면 nspady의 모든 도구가 정상 discovered + sorted된 것.

### 4.2 Telegram 테스트

3가지 시나리오 권장:

1. **Read-only** — "이번 주 일정 알려줘"
2. **FreeBusy** — "내일 오후에 비는 시간 찾아줘"
3. **Write (확인 플로우 필수)** — "내일 오후 3시부터 1시간 치과 예약 넣어줘"
   - Claude가 먼저 freebusy로 확인 후보 제시 → 사용자 승인 기다림 → 그 다음 `create-event`
   - 확인 없이 바로 create하면 프롬프트 fragment 강화 필요

### 4.3 감사 로그

```bash
sqlite3 data/yunam/yunam.db "
SELECT name, skill_id, scope, is_error, elapsed_ms
FROM tool_calls
WHERE skill_id='gcal'
ORDER BY id DESC LIMIT 10;
"
```

`skill_id=gcal`, `scope=calendar:read` 또는 `calendar:write` 로 기록돼야 정상.

---

## 문제 해결

| 증상 | 원인 / 조치 |
|---|---|
| 터미널 B 빌드가 `ENOSPC` / `ENOMEM` | VPS 디스크/메모리 부족 → `df -h` `free -m` 확인 |
| 브라우저가 `This site can't be reached` (localhost:3500) | SSH 터널(터미널 A) 이 끊겨 있거나 3500이 이미 점유됨 → `lsof -i :3500` 로 확인 |
| nspady 로그: `invalid_client` | `gcp-oauth.keys.json` 내용이 `"web"` 타입 → Part 1.4 다시, Desktop 타입으로 |
| 브라우저 경고 "앱이 확인되지 않음" 에서 Advanced 버튼 안 보임 | Test user에 현재 로그인한 Gmail이 안 들어있음 → Part 1.3 5번으로 |
| gateway 로그: `gcal MCP ... connection refused` | calendar-mcp가 healthy 아님 → `docker compose logs calendar-mcp` 확인 |
| gateway 로그: `invalid_grant` / `token expired` | refresh token이 revoke됨 → Part 3부터 다시 consent |
| `tools=0` 로 뜸 | 토큰이 있지만 Google API 호출이 실패 — Calendar API가 비활성화된 경우 → Part 1.2 확인 |

재인증이 필요하면 Part 3만 다시. Part 1, 2는 한 번 한 뒤에는 유지됨.
