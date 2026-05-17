# Phase 1 — 로컬에서 동작하는 Functions 앱

Phase 1의 목표는 **배포·인프라를 건드리지 않고**, 로컬에서 `func start` → `curl POST` → Discord 채널 메시지 수신까지가 끝까지 이어지는지 검증하는 것이다.
구조·개념은 [01-azure-functions-structure.md](01-azure-functions-structure.md), 배포 파이프라인 적용은 [02-apply-deploy-pipeline.md](02-apply-deploy-pipeline.md) 참고.

> 완료일: 2026-05-17. 이 문서는 **무엇이 만들어졌고, 왜 그렇게 만들었는지**를 정리한다 (재구성/디버깅 시 회귀 방지용).

---

## 결과 요약

| 항목 | 상태 |
|---|---|
| `host.json` / `.funcignore` / `local.settings.json` | ✅ 생성 |
| 로직 패키지 `discord_relay/` (`webhook.py`) | ✅ Lakeflow 페이로드 → Discord embed 변환 + 전송 |
| 진입점 `function_app.py` | ✅ HTTP 트리거 `notify`, POST 전용, FUNCTION auth |
| `pyproject.toml` — 의존성·`requires-python`·`[tool.uv] package=false` | ✅ 3.11 통일, `[build-system]`/`[project.scripts]` 제거 |
| `uv.lock` commit, `.python-version=3.11`, `.gitignore`에 `.python_packages/` | ✅ |
| 로컬 smoke test (`uv run func start` + curl → Discord 수신) | ✅ |

→ 다음은 Phase 2 (azure-infra `app_settings` 추가 + OIDC 배포 자격증명). [02 문서](02-apply-deploy-pipeline.md) 체크리스트 B·C.

---

## 1. 최종 디렉터리 (Phase 1 종료 시점)

```text
python-discord-webhook-app/
├── function_app.py              # HTTP 트리거 진입점
├── discord_relay/
│   ├── __init__.py
│   └── webhook.py               # build_discord_payload + send_to_discord
├── host.json
├── local.settings.json          # gitignore ✓
├── pyproject.toml
├── uv.lock
├── .python-version              # 3.11
├── .funcignore
├── .gitignore                   # .python_packages/ 포함
└── docs/
    ├── 01-azure-functions-structure.md
    ├── 02-apply-deploy-pipeline.md
    └── 03-phase1-local-runtime.md   ← 이 문서
```

---

## 2. `function_app.py` — HTTP 진입점

```python
import logging
import os

import azure.functions as func
import httpx

from discord_relay.webhook import build_discord_payload, send_to_discord

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

@app.route(route="notify", methods=["POST"])
def relay_to_discord(req: func.HttpRequest) -> func.HttpResponse:
    logging.info("Lakeflow notification received.")

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        logging.error("DISCORD_WEBHOOK_URL is not configured.")
        return func.HttpResponse(
            "Server misconfigured: DISCORD_WEBHOOK_URL missing",
            status_code=500,
        )

    try:
        event = req.get_json()
    except ValueError:
        logging.warning("Invalid JSON body.")
        return func.HttpResponse("Invalid JSON body", status_code=400)

    payload = build_discord_payload(event)

    try:
        send_to_discord(webhook_url, payload)
    except httpx.HTTPStatusError as e:
        logging.error("Discord returned %d: %s", e.response.status_code, e.response.text)
        return func.HttpResponse(
            f"Discord rejected the webhook: {e.response.status_code}",
            status_code=502,
        )
    except httpx.HTTPError:
        logging.exception("Network error while calling Discord.")
        return func.HttpResponse("Network error calling Discord", status_code=502)

    return func.HttpResponse("ok", status_code=200)
```

### 설계 의사결정

| 결정 | 이유 |
|---|---|
| `http_auth_level=FUNCTION` | 외부(Lakeflow)에서 호출되므로 function key 필수. ANONYMOUS는 공개 노출 위험. |
| `methods=["POST"]` 명시 | 데코레이터 default는 GET+POST. 알림은 POST만 받으면 됨 — GET을 막아 우발 호출/스캐너 노이즈 차단. |
| `os.environ.get(...)` + 500 조기 반환 | 운영에서 App Setting 누락 시 빠르게 알기 위해. (Phase 2의 "인프라 → 코드 순서" 위반 시 여기서 잡힘.) |
| `req.get_json()` `ValueError` → 400 | 클라이언트 잘못 → 4xx, 우리 잘못 → 5xx 원칙. Discord 호출까지 가지 않고 즉시 거절. |
| `HTTPStatusError`(4xx/5xx) 와 `HTTPError`(네트워크) 분리 | 운영 로그에서 "Discord가 거절" vs "DNS/타임아웃"을 구분해야 대응이 달라짐. 둘 다 외부 의존성 실패이므로 우리 응답은 **502**. |
| `logging.exception(...)` (네트워크 분기) | 스택트레이스가 App Insights에 자동으로 같이 기록됨. `logging.error`만 쓰면 trace 손실. |

> ⚠️ **하지 않은 것**: 재시도. Discord 일시 5xx에 대한 retry는 Phase 1 범위 밖. Lakeflow가 호출한 webhook이 502를 받으면 Lakeflow 측 재시도/알림 정책에 위임. 자체 재시도를 도입할 거면 Durable Functions나 Queue 트리거로 분리하는 게 맞다 (지금은 stateless HTTP만).

---

## 3. `discord_relay/webhook.py` — 페이로드 변환 + 전송

```python
import logging
from typing import Any

import httpx

_EVENT_COLORS: dict[str, int] = {
    "jobs.on_success": 0x2ECC71,
    "jobs.on_failure": 0xE74C3C,
    "jobs.on_start": 0x3498DB,
    "jobs.on_duration_warning_threshold_exceeded": 0xF1C40F,
    "jobs.on_streaming_backlog_exceeded": 0xF1C40F,
}
_DEFAULT_COLOR = 0x95A5A6

_log = logging.getLogger(__name__)


def build_discord_payload(event: dict[str, Any]) -> dict[str, Any]:
    event_type = event.get("event", "unknown")
    job_name = event.get("job_name") or event.get("job_id") or "(unknown job)"

    fields: list[dict[str, Any]] = []
    for label, key in (("Run ID", "run_id"), ("Workspace", "workspace_id"), ("Task", "task_name")):
        value = event.get(key)
        if value:
            fields.append({"name": label, "value": str(value), "inline": True})

    embed: dict[str, Any] = {
        "title": f"[{event_type}] {job_name}",
        "color": _EVENT_COLORS.get(event_type, _DEFAULT_COLOR),
        "fields": fields,
    }

    if run_url := event.get("run_url"):
        embed["url"] = run_url
    if event_time := event.get("event_time"):
        embed["timestamp"] = event_time

    return {"embeds": [embed]}


def send_to_discord(webhook_url: str, payload: dict[str, Any], *, timeout: float = 10.0) -> None:
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(webhook_url, json=payload)
    resp.raise_for_status()
    _log.info("Discord webhook delivered: status=%d", resp.status_code)
```

### Lakeflow 이벤트 → Discord embed 매핑

| Lakeflow 필드 | Discord embed에서의 위치 | 비고 |
|---|---|---|
| `event` | `title` 접두 + `color` 결정 키 | success=초록, failure=빨강, start=파랑, 경고류=노랑, 그 외=회색 |
| `job_name` / `job_id` | `title` 본문 | 둘 다 없으면 `(unknown job)` |
| `run_id` / `workspace_id` / `task_name` | `fields` (inline) | 값이 있을 때만 추가 — null/빈 값으로 inline 칸 낭비 안 함 |
| `run_url` | `embed.url` (title 하이퍼링크) | 있으면 Databricks UI로 바로 이동 |
| `event_time` | `embed.timestamp` | ISO 8601 그대로 통과. 잘못된 값이면 Discord가 무시 (검증은 Phase 1 범위 밖) |

### 설계 의사결정

| 결정 | 이유 |
|---|---|
| `function_app.py`와 분리한 모듈 | Functions 진입점은 얇게 (트리거·에러 매핑만), 비즈니스 로직(=페이로드 변환·전송)은 단위테스트 가능한 순수 함수로. |
| 페이로드 변환과 전송을 **두 함수**로 | `build_discord_payload`는 순수 함수 — 입력 dict → 출력 dict. 네트워크 없이 테스트 가능. `send_to_discord`만 I/O. |
| `dict[str, Any]` 직접 다루기 (pydantic X) | Phase 1은 동작 확인 위주. Lakeflow 실제 페이로드 스키마가 굳어지면 그때 모델 도입. 지금 도입하면 추측한 필드명에 묶임. |
| event_type → color 매핑 테이블 | Discord 채널에서 색만 봐도 성공/실패 즉시 식별. 모르는 event_type은 회색으로 fallback (예외 대신). |
| `httpx.Client` context manager + 10s timeout | `httpx.post(...)` 직접 호출은 매번 connection pool 생성. Client로 묶고 timeout 명시 — Discord가 응답 안 줄 때 Function이 무한히 대기하지 않음. |
| `raise_for_status()` | 4xx/5xx를 호출자(`function_app.py`)로 던져서 거기서 502/로그 책임지게. 모듈 내부에서 삼키지 않음. |
| `_log = logging.getLogger(__name__)` | 모듈별 logger — App Insights에서 출처(`discord_relay.webhook` vs root) 구분 가능. |

---

## 4. 런타임 파일

### `host.json`
v4 extension bundle만 명시. App Insights 샘플링은 default 그대로 (요청 로그는 양이 많으므로 `Request` 제외).

### `local.settings.json` (로컬 전용, gitignore ✓)
```json
{
    "IsEncrypted": false,
    "Values": {
        "AzureWebJobsStorage": "UseDevelopmentStorage=true",
        "FUNCTIONS_WORKER_RUNTIME": "python",
        "AzureWebJobsFeatureFlags": "EnableWorkerIndexing",
        "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/..."
    }
}
```

- **`EnableWorkerIndexing` 없으면 v2 모델 인덱싱 0개 → `/api/notify` 404.** Phase 1에서 가장 먼저 부딪힐 가능성 있는 함정. 운영에서는 App Setting으로 동일하게 넣어야 함 (Phase 2 작업).
- `DISCORD_WEBHOOK_URL`은 Discord 채널 설정 → 통합 → 웹훅 → URL 복사. 로컬에서만 사용.

### `.funcignore`
```text
.git*
.github
.vscode
.idea
.venv
.python-version
local.settings.json
test
tests
__pycache__
.pytest_cache
.ruff_cache
.mypy_cache
pyproject.toml
uv.lock
docs
README.md
```

- `local.settings.json` 포함 — 로컬 비밀이 zip에 섞이는 사고 방지.
- `pyproject.toml` / `uv.lock` 제외 — 런타임은 이 파일을 읽지 않고 `.python_packages/`에 사전 설치된 라이브러리만 import. zip 사이즈 절감.
- **`.python_packages/`는 일부러 안 적음** — zip에 포함되어야 런타임이 import한다. `.gitignore`에는 있지만 `.funcignore`에는 없다는 게 의도된 비대칭. (회귀 위험 1순위.)

### `.gitignore` (관련 부분)
- `local.settings.json` ✓ — 비밀 유출 방지.
- `.python_packages/` ✓ — CI 산출물, 커밋 금지.

### `.python-version`
```
3.11
```
- 인프라 default(3.11)에 맞춤. `pyproject.toml`의 `requires-python = ">=3.11,<3.12"`, 향후 `deploy.yml --python 3.11`까지 한 값으로 정렬. (Phase 2의 가장 흔한 침묵 버그가 버전 불일치.)

### `pyproject.toml`
```toml
[project]
name = "python-discord-webhook-app"
version = "0.1.0"
description = "Databricks Lakeflow Job 알림을 Discord 웹훅으로 중계하는 Azure Functions 앱"
requires-python = ">=3.11,<3.12"
dependencies = [
    "azure-functions>=1.24.0",
    "httpx>=0.28.1",
]

[tool.uv]
package = false
```

- `[build-system]`(`uv_build`)·`[project.scripts]` **제거**. Functions 배포는 패키지 빌드를 하지 않고, `--no-emit-project`와도 충돌하기 때문.
- `[tool.uv] package = false` — 이 프로젝트 자체는 wheel로 빌드되지 않음을 명시. (의존성만 사용.)

---

## 5. 로컬 smoke test

```bash
# 1) 의존성 동기화
uv sync

# 2) Functions 호스트 기동 (이 터미널은 점유)
uv run func start
#   → 출력에 "Functions: notify: [POST] http://localhost:7071/api/notify" 가 한 줄로 떠야 정상.
#   → 함수 목록이 비어 있으면: EnableWorkerIndexing 누락 / function_app.py import 에러.

# 3) 다른 터미널에서 호출 — 성공 케이스
curl -X POST "http://localhost:7071/api/notify" \
  -H "Content-Type: application/json" \
  -d '{
    "event": "jobs.on_failure",
    "job_name": "daily-etl",
    "run_id": "12345",
    "workspace_id": "ws-001",
    "task_name": "load_silver",
    "run_url": "https://example.databricks.net/#job/runs/12345",
    "event_time": "2026-05-17T03:14:15Z"
  }'
# 기대: 200 "ok", Discord 채널에 빨간색 embed 도착.
```

### 의도적으로 확인한 실패 경로

| 입력 | 기대 응답 | 의미 |
|---|---|---|
| `curl -X GET .../api/notify` | 404 (method not allowed) | POST 외 차단 확인 |
| `-d 'not json'` | 400 "Invalid JSON body" | 잘못된 페이로드 거절 |
| `local.settings.json`에서 `DISCORD_WEBHOOK_URL` 제거 후 재기동 | 500 "Server misconfigured" | env 누락 조기 감지 |
| 잘못된 webhook URL (e.g. 끝자리 변조) | 502 "Discord rejected the webhook: 401" | `HTTPStatusError` 분기 작동 |

이 4가지 음성 케이스를 다 확인했으므로, Phase 1 = **로컬에서 happy path + 의도된 실패 path 모두 검증 완료** 상태.

---

## 6. 알게 된 것 / 다음에 주의할 것

- **로컬 happy path만으로는 운영 동작을 보장하지 않는다.** v2 모델 인덱싱·zip 패키징·OIDC subject·App Setting 등 운영 전용 함정은 Phase 2에서 별도로 검증 필요. ([02 문서](02-apply-deploy-pipeline.md) 체크리스트.)
- `.funcignore` / `.gitignore`의 `.python_packages/` 처리 비대칭은 **명시적 의도**. 통일하지 말 것.
- HTTP error 처리에서 `HTTPStatusError`와 `HTTPError`를 분리하지 않으면 운영에서 "Discord가 거절" vs "네트워크 단절"을 못 가린다 — App Insights 알림 룰을 만들 때 차이가 큼.
- `req.get_json()`은 본문이 비어 있어도 `ValueError`. `None` 체크 따로 안 해도 됨.
- `func.AuthLevel.FUNCTION`은 **운영**에서만 효과가 있다 (function key 검증). 로컬 `func start`는 키 없이도 호출 가능 — 보안 테스트는 배포 후 별도로.

---

## 7. Phase 2 진입 조건 (체크)

- [x] `uv run func start` → `/api/notify` POST → Discord 수신 확인
- [x] `uv.lock` commit 상태
- [x] `.python-version` = 3.11, `pyproject.toml` `requires-python` = `>=3.11,<3.12`
- [x] `local.settings.json`이 git에 안 올라옴
- [ ] (Phase 2) `azure-infra`의 Function App `app_settings`에 `EnableWorkerIndexing` + `DISCORD_WEBHOOK_URL` 추가 — **선행**
- [ ] (Phase 2) OIDC App Registration / SP / federated credential / `gh variable set`

→ Phase 2 작업은 [02 문서](02-apply-deploy-pipeline.md) §2·§3 참고.
