# azure-infra 배포 파이프라인을 이 repo에 적용하기

`azure-infra` repo의 `docs/function/02-deploy-code.md`는 **uv 기반 Azure Functions를 GitHub Actions(OIDC)로 배포**하는 완성된 플레이북이다.
이 문서는 그 패턴을 **이 repo(`python-discord-webhook-app`)에 적용하면 어떻게 되는지**, 격차와 체크리스트를 정리한다.

기본 구조·uv 개발은 [01-azure-functions-structure.md](01-azure-functions-structure.md) 참고.

> 참조 원본:
> - `azure-infra/docs/function/01-app.md` — Terraform으로 Function App 인프라 생성
> - `azure-infra/docs/function/02-deploy-code.md` — uv + GitHub Actions 배포 (이 repo가 따를 표준)
> - `azure-infra/terraform/modules/function/` — 실제 인프라 모듈

---

## 결론 먼저

**`deploy.yml`을 그대로 가져다 적용하면 배포 job은 성공하지만 함수가 안 뜬다 (404 / "no functions found").**

이유: `02-deploy-code.md`는 깨끗한 `azure-func-hello` Functions 앱을 전제로 작성됐고, 현재 repo는 `uv init`이 만든 **CLI 패키지 스켈레톤**(`src/` 레이아웃 + `[project.scripts]` + `uv_build`)이다. 구조를 doc 표준으로 재구성해야 동작한다.

> 폐기 사항: 초기 검토 때 거론된 `uv export → requirements.txt → Oryx 원격 빌드` 방식은 쓰지 않는다. 이 repo 표준은 `enable-oryx-build: false` + `.python_packages/`에 사전 설치다. 두 방식을 섞으면 이중 빌드로 충돌.

---

## 1. 코드 repo 격차 (재구성 필요)

| 항목 | 현재 상태 | doc/인프라 요구 | 안 맞으면 |
|---|---|---|---|
| 진입점 | `src/.../__init__.py` + `[project.scripts]` CLI | root `function_app.py` | 함수 인덱싱 0개 → **404** |
| 빌드 백엔드 | `[build-system] uv_build` | `[tool.uv] package=false`, build-system 없음 | `--no-emit-project`와 충돌, 불필요 빌드 |
| **Python 버전** | `.python-version`=3.12, `requires-python>=3.12` | 인프라 모듈 default **3.11**, doc `>=3.11,<3.12`, deploy.yml `--python 3.11` | **런타임 3.11에 3.12 패키지** → import 에러 (가장 흔한 침묵 버그) |
| `uv.lock` | 없음 | 반드시 commit | deploy.yml `--frozen` / `uv lock --check` 즉시 실패 |
| 의존성 | `dependencies = []` | `azure-functions` + Discord 전송용(`httpx`) | `ModuleNotFoundError: azure.functions` |
| `host.json` | 없음 | 필요 (extensionBundle) | 런타임 시작 실패 |
| `.funcignore` | 없음 | `pyproject.toml`/`uv.lock`/`.venv` 제외 | 동작은 하나 zip 비대 |
| `.gitignore` | `local.settings.json` ✓ | `.python_packages/` 도 무시 권장 | CI 산출물 커밋 위험 |

> 가장 위험한 함정은 **Python 버전 불일치(3.12 vs 인프라 3.11)**. `.python-version` · `requires-python` · Terraform `python_version` · `deploy.yml`의 `--python`을 한 값으로 정렬한다. 인프라가 이미 3.11로 apply됐다면 **3.11로 통일**.

---

## 2. 인프라 / 파이프라인 선행·조율 작업

`02-deploy-code.md`의 "코드만" 부분 외에 이 repo 적용 시 반드시 같이 해야 하는 것:

### 2-1. Function App이 먼저 존재해야 함 (선행)

`azure-infra/docs/function/01-app.md`대로 Terraform apply가 끝나 있어야, 배포용 SP를 그 Function App scope로 만들 수 있다 (`Website Contributor`, Function App 리소스 한정).

### 2-2. OIDC federated credential / GH 변수의 repo 이름 교체

`02-deploy-code.md`는 전부 `hellojin97/azure-func-hello`로 하드코딩돼 있다. 이 repo의 실제 `<owner>/python-discord-webhook-app`으로 바꿔야 OIDC 토큰 subject가 매칭된다. 안 바꾸면 배포 시 **403**.

교체 대상:
- App Registration display name (예: `github-actions-python-discord-webhook-app`)
- federated credential `subject`: `repo:<owner>/python-discord-webhook-app:ref:refs/heads/main` (및 PR용)
- `gh variable set` 의 repo 인자 + `AZURE_FUNCTIONAPP_NAME`

### 2-3. `EnableWorkerIndexing` app setting (필수)

Python v2 모델은 이 flag 없으면 함수 인덱싱이 안 된다. Terraform `modules/function/main.tf`의 `azurerm_linux_function_app` 블록에 `app_settings`로 추가하는 게 가장 깔끔(인프라 PR 필요):

```hcl
app_settings = {
  AzureWebJobsFeatureFlags = "EnableWorkerIndexing"
}
```

임시로는 `az functionapp config appsettings set ... --settings AzureWebJobsFeatureFlags=EnableWorkerIndexing`.

### 2-4. Discord 웹훅 URL은 App Setting으로

`local.settings.json`은 로컬·gitignore 전용. 운영은 Terraform `app_settings`에 `DISCORD_WEBHOOK_URL` 추가(인프라 PR) 후 코드에서 `os.environ["DISCORD_WEBHOOK_URL"]`.

```hcl
app_settings = {
  AzureWebJobsFeatureFlags = "EnableWorkerIndexing"
  DISCORD_WEBHOOK_URL      = "https://discord.com/api/webhooks/..."   # 또는 Key Vault 참조
}
```

> 더 안전하게는 Key Vault + Function MI(`@Microsoft.KeyVault(...)` 참조)로. 학습 단계에서는 app setting 직접도 허용.

### 2-5. 변경 순서 (조율 패턴)

`02-deploy-code.md`의 조율 원칙 그대로:

1. **인프라 먼저** — `azure-infra`에서 PR → 머지 → apply (app_settings 추가)
2. **그 다음 코드** — 이 repo에서 PR → 머지 → deploy

순서를 어기면(코드 먼저) 새 env var를 못 찾아 런타임 에러.

---

## 3. 적용 체크리스트

> 실제 작업은 아직 진행하지 않음. 승인 시 아래 순서로.

### A. 코드 repo 재구성 (이 repo)

- [x] `src/python_discord_webhook_app/` 제거 → root `function_app.py` 생성 (HTTP 트리거 `notify`)
- [x] 로직 패키지 `discord_relay/` 생성 (Lakeflow 페이로드 → Discord 임베드 변환)
- [x] `pyproject.toml`: `[build-system]`·`[project.scripts]` 제거, `[tool.uv] package=false`, `requires-python` 3.11로
- [x] `.python-version` → `3.11`
- [x] `uv add azure-functions httpx` → `uv.lock` 생성, commit
- [x] `host.json` 생성 (extensionBundle v4)
- [x] `.funcignore` 생성
- [x] `.gitignore`에 `.python_packages/` 추가
- [x] (로컬) `local.settings.json` 생성 — gitignore 확인됨 ✓
- [x] (로컬) `uv run func start` 로 `/api/notify` 동작 확인

### B. Azure / GitHub 사전 설정 (1회성)

- [ ] `azure-infra` Terraform apply 완료 — Function App 존재 확인
- [ ] 인프라 모듈 `app_settings`에 `EnableWorkerIndexing` + `DISCORD_WEBHOOK_URL` 추가 → 인프라 PR/apply
- [ ] 배포 전용 App Registration + SP 생성, `Website Contributor`를 Function App scope로 부여
- [ ] federated credential 생성 (subject = 이 repo, main + PR)
- [ ] `gh variable set` — `AZURE_CLIENT_ID/TENANT_ID/SUBSCRIPTION_ID/FUNCTIONAPP_NAME`

### C. 파이프라인

- [ ] `.github/workflows/deploy.yml` 추가 (`02-deploy-code.md`의 워크플로우, repo 이름·`--python` 버전만 이 repo에 맞게)
- [ ] (권장) `uv lock --check` step 추가 — lock 불일치 조기 검출
- [ ] main push → `gh run watch` → 함수 호출 smoke test

---

## 4. 적용 후 자주 만나는 에러 (요약)

`02-deploy-code.md`의 에러 섹션과 동일. 이 repo에서 특히 주의:

| 증상 | 원인 | 해결 |
|---|---|---|
| 배포 성공, 호출 시 404 | 인덱싱 대기 / `EnableWorkerIndexing` 미설정 | 1~2분 대기, app setting 확인 (2-3) |
| `ModuleNotFoundError: azure.functions` | 의존성이 zip에 미포함 | CI `uv pip install --target` step·`.funcignore`에 `.python_packages` 없는지 |
| import/실행 에러 (버전 관련) | **Python 3.12 코드 ↔ 3.11 런타임** | 모든 버전 지정 지점 3.11 정렬 (1번 표) |
| `--frozen` / `uv lock --check` 실패 | `uv.lock` 없음·불일치 | 로컬 `uv lock` 후 commit |
| `403` 배포 거부 | SP 권한 / federated subject repo 이름 불일치 | 2-2 확인 |

---

## 5. 요약

- `azure-infra/docs/function/02-deploy-code.md`는 그대로 따를 수 있는 좋은 표준이다.
- 핵심 작업은 **이 repo를 그 표준의 Functions 앱 형태로 재구성**하는 것 (CLI 패키지 → Functions 앱).
- 단일 최대 리스크는 **Python 버전 정렬(→ 3.11)**.
- 인프라 측 변경(`app_settings`)이 코드 배포보다 **먼저** 가야 한다.
