# Azure Functions 기본 구조 + uv 개발

이 repo는 **Databricks Lakeflow Job 알림을 Discord 웹훅으로 중계하는 Azure Functions 앱**입니다.
이 문서는 (1) Azure Functions Python 앱의 기본 구조와 (2) `uv`로 개발하는 방법을 정리합니다.

배포 파이프라인(`azure-infra` GitHub Actions 패턴 적용)은 [02-apply-deploy-pipeline.md](02-apply-deploy-pipeline.md) 참고.

---

## 1. Azure Functions Python — 어떤 모델을 쓰나

Python은 현재 **v2 프로그래밍 모델**이 권장 방식입니다.

| | v1 (구식) | **v2 (권장)** |
|---|---|---|
| 함수 정의 | 폴더별 `__init__.py` + `function.json` | `function_app.py` 한 파일에 데코레이터 |
| 트리거 선언 | `function.json` (별도 설정 파일) | `@app.route(...)` 등 코드 데코레이터 |
| 진입점 | 폴더 = 함수 1개 | 전역·무상태 메서드, 한 파일에 여러 개 |

> 출처: Microsoft Learn — Python developer guide for Azure Functions / Python v2 programming model.

---

## 2. 디렉터리 구조 (v2 모델, uv 기반)

```text
python-discord-webhook-app/
├── function_app.py          # ★ 진입점 — 함수와 트리거 정의 (root)
├── discord_relay/           # 실제 로직 (root 레벨 패키지 — src/ 레이아웃 X)
│   ├── __init__.py
│   └── webhook.py
├── host.json                # 런타임 전역 설정 (모든 함수 공통)
├── local.settings.json      # 로컬 실행용 환경변수 (배포 X, .gitignore 대상)
├── pyproject.toml           # uv가 관리하는 의존성 (개발 기준)
├── uv.lock                  # 실제 설치 버전 잠금 (commit!)
├── .python-version          # Python 버전 고정 (commit)
├── .funcignore              # zip 패키징 제외 목록
├── .gitignore
└── .github/
    └── workflows/
        └── deploy.yml       # build + deploy (azure-infra 패턴)
```

> ⚠️ **`src/` 레이아웃은 피한다.** Functions 런타임은 배포된 폴더에서 `function_app.py`를 직접 import 하고, 이 패키지를 `pip install` 하지 않는다. 따라서 로직 패키지는 root 레벨(`discord_relay/`)에 두고 `function_app.py`에서 `from discord_relay.webhook import ...`로 가져온다.
>
> ⚠️ 현재 repo의 `[project.scripts]` CLI 진입점과 `uv_build` 빌드 백엔드는 Functions 배포에서 **사용되지 않는다**. (재구성 시 제거 — 자세한 격차는 [02 문서](02-apply-deploy-pipeline.md))

---

## 3. 각 파일의 역할

### `function_app.py` — 함수의 본체

v2 모델에서는 `function.json` 없이 데코레이터로 트리거를 코드 안에서 정의한다.

```python
import azure.functions as func
import logging

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)


@app.route(route="notify")
def relay_to_discord(req: func.HttpRequest) -> func.HttpResponse:
    logging.info("Lakeflow notification received.")
    payload = req.get_json()          # Lakeflow가 보낸 알림
    # ... Discord 웹훅으로 전송 (discord_relay 패키지) ...
    return func.HttpResponse("ok", status_code=200)
```

- `func.FunctionApp(http_auth_level=...)` — v2 진입점. 한 파일에 여러 함수 데코레이터 등록.
- `http_auth_level`:
  - `ANONYMOUS` — 인증 없이 호출 (공개 API)
  - `FUNCTION` — function key 필요 (외부 Webhook 수신용 권장)
  - `ADMIN` — master key 필요
- `@app.route(route="notify")` — `/api/notify` 경로로 매핑.
- `logging.info(...)` — App Insights에 자동 수집.

### `host.json` — 런타임 전역 설정

```json
{
    "version": "2.0",
    "logging": {
        "applicationInsights": {
            "samplingSettings": {
                "isEnabled": true,
                "excludedTypes": "Request"
            }
        }
    },
    "extensionBundle": {
        "id": "Microsoft.Azure.Functions.ExtensionBundle",
        "version": "[4.*, 5.0.0)"
    }
}
```

- `extensionBundle` — trigger/binding extension을 자동 관리. v4 bundle = Functions runtime v4.

### `local.settings.json` — 로컬 전용 비밀/설정

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

- **로컬 실행 시에만 사용. 배포 안 됨.**
- **반드시 `.gitignore`** (현재 repo는 이미 처리됨 ✓).
- `EnableWorkerIndexing` — Python v2 모델 활성화. **필수.**
- 운영 환경에서는 이 값들을 Function App **Application Settings**(Terraform `app_settings` 또는 `az functionapp config appsettings set`)에 등록하고, 코드에서 `os.environ[...]`로 읽는다.

### `.funcignore` — zip 패키징 제외

```text
.git*
.vscode
.venv
.python-version
local.settings.json
test
__pycache__
.pytest_cache
.ruff_cache
.mypy_cache
pyproject.toml
uv.lock
```

> ⚠️ `pyproject.toml`/`uv.lock`은 배포 zip에서 제외한다 — 런타임은 이 파일을 읽지 않고 `.python_packages/`에 미리 설치된 라이브러리만 사용. zip 사이즈 절감.

---

## 4. 핵심 개념

| 항목 | 설명 |
|---|---|
| **트리거(Trigger)** | 함수를 실행시키는 이벤트. 이 앱은 HTTP 트리거 (Lakeflow → HTTP POST) |
| **바인딩(Binding)** | 입출력 연결. Discord 호출은 외부 HTTP라 코드로 직접 처리 |
| **진입점** | `function_app.py` 하나로 통일 (v1 폴더별 방식은 구식) |
| **`.python_packages/`** | CI가 의존성을 미리 설치하는 디렉터리 → zip에 포함 → 배포 후 추가 빌드 없이 import |
| **비밀 관리** | 로컬은 `local.settings.json`, 운영은 App Settings / Key Vault |

---

## 5. uv로 개발하기

### 왜 uv

[Astral](https://astral.sh)의 Rust 기반 도구. pyenv + virtualenv + pip을 대체.

- 속도: pip의 10~100배
- Python 버전 관리: `uv python install 3.11`
- 의존성 관리: `uv add`, `uv sync`
- 재현성: `uv.lock` (npm `package-lock.json` 역할 — transitive 포함 정확한 버전 + 해시)

> `uv.lock`과 `.python-version`은 **반드시 commit** (팀/CI 동일 환경 보장).

### Azure Functions와의 긴장점

> Azure Functions 원격 빌드(Oryx)는 `requirements.txt`만 인식하고 `pyproject.toml`/`uv.lock`은 모른다.

이 repo는 **원격 빌드를 끄고**(`enable-oryx-build: false`, `scm-do-build-during-deployment: false`), CI에서 `uv`로 의존성을 `.python_packages/lib/site-packages`에 **미리 설치해 zip에 포함**하는 방식을 쓴다. (`azure-infra` 표준 — [02 문서](02-apply-deploy-pipeline.md) 참고.)

### pyproject.toml (목표 형태)

```toml
[project]
name = "python-discord-webhook-app"
version = "0.1.0"
description = "Databricks Lakeflow Job 알림을 Discord 웹훅으로 중계하는 Azure Functions 앱"
requires-python = ">=3.11,<3.12"
dependencies = [
    "azure-functions>=1.18.0",
    "httpx",                       # Discord 웹훅 전송
]

[tool.uv]
package = false                    # Functions 배포는 패키지 빌드 안 함
```

- `[build-system]`(`uv_build`)과 `[project.scripts]`는 **제거** — Functions 배포에 불필요하고 `--no-emit-project`와 충돌.
- `requires-python` / `.python-version` / Terraform `python_version` / `deploy.yml --python` 을 **한 값으로 통일**한다. (인프라 기본값이 3.11이므로 3.11 권장 — 가장 흔한 침묵 버그가 버전 불일치.)

### 일상 워크플로우

```bash
uv python install 3.11        # Python 3.11 설치 (이미 있으면 skip)
uv python pin 3.11            # .python-version 생성/갱신

uv add azure-functions httpx  # pyproject.toml + uv.lock 동시 갱신
uv sync                       # uv.lock 기준 .venv 동기화

uv run func start             # 로컬 Functions 호스트 실행
# 다른 터미널
curl "http://localhost:7071/api/notify" -d '{"...": "..."}'

uv remove <pkg>                       # 의존성 제거
uv lock --upgrade-package azure-functions   # 특정 패키지 업그레이드
```

`uv run`은 `.venv` 활성화 없이 한 번에 실행. 또는 `source .venv/bin/activate && func start`.

### 사전 도구

```bash
# uv
brew install uv            # 또는 curl -LsSf https://astral.sh/uv/install.sh | sh

# Azure Functions Core Tools (로컬 실행/디버깅용, 배포만이면 불필요)
brew tap azure/functions
brew install azure-functions-core-tools@4
```

---

## 6. 현재 repo 상태 (작성 시점 기준)

| 항목 | 현재 | 목표 |
|---|---|---|
| 구조 | `src/python_discord_webhook_app/__init__.py` + CLI | root `function_app.py` + `discord_relay/` |
| 빌드 | `[build-system] uv_build`, `[project.scripts]` | 제거, `[tool.uv] package=false` |
| Python | `.python-version`=3.12, `requires-python>=3.12` | 인프라(3.11)에 맞춰 3.11 통일 |
| `uv.lock` | 없음 | `uv add`로 생성 후 commit |
| 의존성 | `[]` | `azure-functions`, `httpx` |
| `host.json` / `.funcignore` | 없음 | 생성 |
| `.gitignore` | `local.settings.json` ✓ | `.python_packages/` 추가 권장 |

→ 재구성은 아직 진행하지 않음. 적용 격차·체크리스트·인프라 조율은 [02-apply-deploy-pipeline.md](02-apply-deploy-pipeline.md).
