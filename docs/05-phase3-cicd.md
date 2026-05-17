# Phase 3 — CI/CD (GitHub Actions 배포 + 운영 smoke test)

Phase 3의 목표는 **`git push origin main`만으로 Function App에 코드가 배포되고, 운영 환경에서 동일 동작이 확인**되는 것.

전제 (모두 Phase 2에서 충족):
- Function App `func-dataplay-lab-kc` 생성 + `EnableWorkerIndexing` / `DISCORD_WEBHOOK_URL` App Setting 박힘
- App Registration + SP + Function App scope `Website Contributor` role
- Federated credential subject = `repo:hellojin97/python-discord-webhook-app:ref:refs/heads/main`
- GitHub variables 4개 (`AZURE_CLIENT_ID/TENANT_ID/SUBSCRIPTION_ID/FUNCTIONAPP_NAME`)

구조·표준 패턴은 [02-apply-deploy-pipeline.md](02-apply-deploy-pipeline.md), Phase 1/2 회고는 [03](03-phase1-local-runtime.md)/[04](04-phase2-deployment-infra.md).

> 완료일: 2026-05-18. 이 문서는 **첫 운영 배포가 통과한 워크플로우 형태와 거기에 도달하기까지 마주친 함정**을 정리한다.

---

## 결과 요약

| 단계 | 산출물 | 상태 |
|---|---|---|
| 3-1 | `.github/workflows/deploy.yml` — OIDC 로그인 + uv 사전 빌드 + Oryx 끄고 zip 배포 | ✅ |
| 3-2 | `main` push → GitHub Actions `Deploy Function` 워크플로우 | ✅ 첫 배포 1m04s 성공 |
| 3-3 | 운영 smoke test (function key + `POST /api/notify` → Discord 채널 수신) | ✅ happy path 200 + Discord embed 도착, 음성 케이스 401/404/400 의도대로 |

→ 다음 후보 단계 (선택): **Phase 4 — Key Vault 이전** (Discord webhook URL을 Terraform var/GitHub secret에서 빼고 KV 참조로 교체). [§5](#5-다음-단계-후보--key-vault-이전)

---

## 1. 최종 `deploy.yml`

`/Users/dawn/Workspace/GitHub/python-discord-webhook-app/.github/workflows/deploy.yml`:

```yaml
name: Deploy Function

on:
    push:
        branches: [main]
        paths-ignore:
            - "**.md"
            - ".gitignore"
    workflow_dispatch:

permissions:
    id-token: write
    contents: read

concurrency:
    group: deploy-function
    cancel-in-progress: false

jobs:
    deploy:
        name: build & deploy
        runs-on: ubuntu-latest

        steps:
            - name: Checkout
              uses: actions/checkout@v4

            - name: Install uv
              uses: astral-sh/setup-uv@v3
              with:
                enable-cache: true

            - name: Install Python (from .python-version)
              run: uv python install

            - name: Install dependencies into .python_packages
              run: |
                uv export \
                  --no-hashes \
                  --no-emit-project \
                  --frozen \
                  --output-file requirements-deploy.txt
                uv pip install \
                  --python 3.11 \
                  --target=".python_packages/lib/site-packages" \
                  -r requirements-deploy.txt

            - name: Azure Login (OIDC)
              uses: azure/login@v2
              with:
                client-id:       ${{ vars.AZURE_CLIENT_ID }}
                tenant-id:       ${{ vars.AZURE_TENANT_ID }}
                subscription-id: ${{ vars.AZURE_SUBSCRIPTION_ID }}

            - name: Deploy to Function App
              uses: Azure/functions-action@v1
              with:
                app-name: ${{ vars.AZURE_FUNCTIONAPP_NAME }}
                package: "."
                scm-do-build-during-deployment: false
                enable-oryx-build: false
```

### 줄별 의사결정

| 블록 | 결정 | 이유 |
|---|---|---|
| `on.push.branches: [main]` + `paths-ignore: ["**.md", ".gitignore"]` | main에만 트리거, 문서/gitignore 변경은 제외 | Phase 4+에서 docs/04, docs/05 같은 회고 commit이 매번 배포를 트리거하면 noise. 코드 변경에만 반응. |
| `workflow_dispatch` | 수동 트리거 허용 | hotfix 시 push 없이도 재배포 가능. |
| `permissions: id-token: write` | OIDC 토큰 발급 권한 | 이거 없으면 `azure/login@v2`가 403. GitHub Actions의 OIDC 사용 필수 권한. |
| `concurrency: deploy-function, cancel-in-progress: false` | 같은 그룹 동시 배포 큐잉, 진행 중인 배포는 안 죽임 | 부분 배포 사고 방지. 한 번에 한 배포만, 새 배포는 이전 완료 후 시작. |
| `setup-uv@v3` + `enable-cache: true` | uv 자동 설치 + lock 기반 캐싱 | 빌드 시간 단축. 첫 배포 1m04s, 캐시 후엔 더 줄어듦. |
| `uv python install` | `.python-version` 읽어 자동 설치 | 인프라 `python_version=3.11`과 정렬. 별도 `python-version` step 불필요. |
| `uv export --frozen --no-emit-project --no-hashes` | `uv.lock` → `requirements-deploy.txt` | **`--frozen`**: lock 갱신 금지(재현성). **`--no-emit-project`**: 이 repo `[tool.uv] package=false`라 어차피 no-op이지만 명시. **`--no-hashes`**: pip `--target` 설치와 hash 검증 충돌 회피. |
| `uv pip install --target ".python_packages/lib/site-packages"` | 의존성을 `.python_packages/`에 사전 설치 | Functions 런타임은 이 디렉토리에서 import. **`.funcignore`에 일부러 안 넣은 이유**. |
| `Azure/functions-action@v1` + `scm-do-build-during-deployment: false` + `enable-oryx-build: false` | 원격 Oryx 빌드 끔, 우리가 만든 zip 그대로 배포 | Oryx는 `requirements.txt`만 알고 `pyproject.toml`/`uv.lock`은 모름. 이중 빌드 충돌 방지. azure-infra/02 표준. |
| `app-name: ${{ vars.AZURE_FUNCTIONAPP_NAME }}` | Phase 2d 변수 사용 | 코드에 `func-dataplay-lab-kc` 박지 않음. 다른 환경 이전 시 변수만 갱신. |

> ⚠️ **하지 않은 것** (의도적):
> - PR 검증 job 없음 — Phase 2c federated credential을 main only로 좁혔기 때문. PR에서 plan/lint 돌리려면 `pull_request` subject credential 추가 + 별도 workflow 필요.
> - 테스트 step 없음 — 아직 unit test가 없는 단계. 추가 시 `Install dependencies` 다음에 `pytest` step 끼우면 됨.
> - `--python 3.11`을 워크플로우에 박은 게 `.python-version`과 중복으로 보이지만, `uv pip install --target` 단계에서 명시적 선언이 안전 (자동 추론 의존하지 않음).

---

## 2. 운영 smoke test 결과

### Happy path

```bash
curl -i -X POST \
  "https://func-dataplay-lab-kc.azurewebsites.net/api/notify?code=$FUNC_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "event": "jobs.on_failure",
    "job_name": "daily-etl",
    "run_id": "12345",
    "workspace_id": "ws-001",
    "task_name": "load_silver",
    "run_url": "https://example.databricks.net/#job/runs/12345",
    "event_time": "2026-05-18T03:14:15Z"
  }'
```

→ HTTP `200 ok` + Discord 채널에 빨간 embed (title `[jobs.on_failure] daily-etl`, fields 3개, run_url 하이퍼링크) 도착 ✓

### 음성 케이스 (운영에서도 Phase 1과 동일 응답)

| 입력 | 응답 | 의미 |
|---|---|---|
| function key 없이 POST | `401` (Anonymous 차단) | `http_auth_level=FUNCTION` 운영에서만 효과 — 정상 작동 |
| GET 메서드 | `404` | `methods=["POST"]` 차단 |
| `-d 'not json'` | `400 Invalid JSON body` | `req.get_json()` `ValueError` → 400 분기 |

→ Phase 1 로컬 검증과 **동일한 음성 응답** = 코드/인프라 정렬 완료.

---

## 3. Function key 다루기

```bash
export FUNC_KEY=$(az functionapp keys list \
  --name func-dataplay-lab-kc \
  -g rg-dataplay-lab-kc \
  --query functionKeys.default -o tsv)
echo "key 길이: ${#FUNC_KEY}"   # 80자 내외
```

### 주의

- **URL query (`?code=...`)** 또는 **헤더 `x-functions-key: ...`** 둘 다 가능. Lakeflow가 어느 쪽을 보낼지에 따라 선택. URL query가 일반적.
- key는 **secret**. 채팅/PR/스크린샷에 평문 노출 금지. `echo $FUNC_KEY`도 안 하고 길이만 확인.
- 회전 필요 시: `az functionapp keys set --name ... -g ... --key-type functionKeys --key-name default --key-value <new>` 또는 portal에서 "Renew".
- key는 **Function App 내부 저장** — Terraform이나 코드 변경 없이 회전 가능.

---

## 4. 만난 함정 (회귀 방지)

### 4-1. `--forzen` 한 글자 오타 → 전체 배포 fail

`uv export --frozen`을 `--forzen`으로 오타 → `unrecognized option`. push 전에 한 글자 차이 발견. 한 글자가 첫 배포 전체를 막을 수 있음.

**예방:** push 전에 `cat .github/workflows/deploy.yml | grep -E "uv (export|pip)"` 한 줄 검수, 또는 `gh act` / `actionlint` 같은 로컬 lint 도구 도입 (현재 미적용).

### 4-2. 배포 success ≠ 호출 가능

`Azure/functions-action@v1` `success` 직후 1~2분은 **Function App 인덱싱 대기 구간**. 너무 빨리 curl 하면 404. Phase 1과 같은 함정이 운영에서도 나타남.

**진단:**
```bash
az functionapp function list \
  --name func-dataplay-lab-kc \
  -g rg-dataplay-lab-kc \
  -o table
# "notify" 한 줄이 보일 때까지 대기 후 curl
```

`notify`가 끝까지 안 보이면 → `EnableWorkerIndexing` App Setting 점검 (Phase 2a 모듈 `merge`로 강제했으니 정상이라면 박혀 있어야 함). 빠지면 함수 0개.

### 4-3. Function key는 헤더가 아니라 URL query (관례)

Functions HTTP 트리거의 `FUNCTION` auth는 **`?code=<key>` URL query 또는 `x-functions-key: <key>` 헤더** 둘 다 받음. Lakeflow처럼 외부에서 호출할 때 URL query가 더 흔함 (헤더 설정 안 되는 환경 대비).

---

## 5. 다음 단계 후보 — Key Vault 이전

Phase 2/3 마무리 후 남은 보안 부채:
- `DISCORD_WEBHOOK_URL`이 **Terraform state file에 평문** (sensitive 해도 state는 평문 저장)
- **GitHub azure-infra repo의 secret**으로도 박혀 있음 (`TF_VAR_DISCORD_WEBHOOK_URL`)
- 회전 시 매번 **`terraform apply` 필요** (state ↔ App Setting 동기화)

### Key Vault 도입 시 변화

| 항목 | 현재 | Key Vault 이후 |
|---|---|---|
| 비밀 저장 위치 | Terraform var → state → App Setting (평문 3곳) | Key Vault 1곳만 |
| Terraform 입력 | `TF_VAR_discord_webhook_url` 필수 | 변수 자체 제거 |
| GitHub secret | `TF_VAR_DISCORD_WEBHOOK_URL` (azure-infra repo) | 제거 |
| App Setting 값 | 평문 URL | `@Microsoft.KeyVault(SecretUri=...)` 참조 |
| 회전 방법 | KV 값 갱신 → `terraform apply` | KV 값 갱신만 (자동 반영) |
| Function App 권한 | 불필요 | System-Assigned MI에 `Key Vault Secrets User` |

### 도입 비용 (1회성)

- `azure-infra/modules/key_vault/` 또는 root에 `azurerm_key_vault` + `azurerm_key_vault_secret`
- Function App MI에 KV access policy/RBAC (`Key Vault Secrets User`)
- `app_settings`의 `DISCORD_WEBHOOK_URL` 값을 `@Microsoft.KeyVault(SecretUri=https://....vault.azure.net/secrets/discord-webhook-url/)` 형태로 교체
- `discord_webhook_url` TF var + GitHub secret + 워크플로우 env 한 줄 — **셋 다 제거**

도입 후엔 비밀 노출 사고 자체가 안 일어남 (값을 KV에만 적고 끝). 학습 단계 over-engineering 측면도 있지만 **Phase 3에서 겪은 회전 비용을 한 번 더 겪기 전에 옮기는 게 효율적**.

---

## 6. Phase 3 종료 체크리스트

- [x] `.github/workflows/deploy.yml` commit + push
- [x] 첫 배포 success (`gh run watch`)
- [x] `az functionapp function list`에 `notify` 보임 (인덱싱 완료)
- [x] `POST /api/notify?code=<key>` → 200 + Discord 채널 embed 도착
- [x] 음성 케이스 (401/404/400) 의도대로 응답
- [ ] (Phase 4 후보) Key Vault 이전
- [ ] (Phase 4 후보) Lakeflow → 이 URL로 실제 알림 연결 + 첫 운영 알림 수신

→ **본 프로젝트의 코어 목표 (Lakeflow → Discord 중계 Functions 앱 배포) 달성.** 이후는 보안 강화 / 실제 통합 단계.
