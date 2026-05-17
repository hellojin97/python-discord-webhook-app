# Phase 2 — 배포 인프라 (azure-infra + Azure + GitHub)

Phase 2의 목표는 **Phase 3(CI/CD)가 깔릴 수 있도록 인프라/자격증명을 모두 준비**하는 것이다.
즉 `git push` 한 번이면 GitHub Actions가 Azure에 OIDC로 로그인 → 이 repo가 Function App에 배포할 수 있는 상태까지.

구조·표준 패턴은 [02-apply-deploy-pipeline.md](02-apply-deploy-pipeline.md) (azure-infra 표준 적용 가이드) 참고.

> 완료일: 2026-05-18. 이 문서는 **무엇이 만들어졌고, 왜 그렇게 만들었는지, 어디서 막힐 수 있는지**를 정리한다 (재현/회귀 방지용).

---

## 결과 요약

| 단계 | 산출물 | 상태 |
|---|---|---|
| 2a | `azure-infra` Terraform: `app_settings` 변수 + `DISCORD_WEBHOOK_URL` 주입 + outputs 2개 | ✅ apply 완료 (1 changed, 0 destroyed) |
| 2b | App Registration + Service Principal + Function App scope `Website Contributor` role | ✅ |
| 2c | Federated credential — `repo:hellojin97/python-discord-webhook-app:ref:refs/heads/main` | ✅ |
| 2d | GitHub variables: `AZURE_CLIENT_ID` / `AZURE_TENANT_ID` / `AZURE_SUBSCRIPTION_ID` / `AZURE_FUNCTIONAPP_NAME` | ✅ |

→ 다음은 Phase 3 (`deploy.yml` 작성 + main push → 운영 smoke test). [02 문서](02-apply-deploy-pipeline.md) 체크리스트 C.

---

## 1. 최종 상태

### 1-1. `azure-infra` 변경 (Terraform, 5 files)

| 파일 | 변경 |
|---|---|
| `terraform/modules/function/variables.tf` | `app_settings = map(string)` (sensitive, default `{}`) 변수 추가 |
| `terraform/modules/function/main.tf` | `azurerm_linux_function_app.this`에 `app_settings = merge({AzureWebJobsFeatureFlags = "EnableWorkerIndexing"}, var.app_settings)` |
| `terraform/variables.tf` (신규) | `discord_webhook_url` (string, sensitive) |
| `terraform/main.tf` | `module "function"`에 `app_settings = { DISCORD_WEBHOOK_URL = var.discord_webhook_url }` 전달 |
| `terraform/outputs.tf` | `function_app_id`, `function_app_name` 추가 |

### 1-2. Azure 측 신규 객체

| 객체 | 값 |
|---|---|
| App Registration | `github-actions-python-discord-webhook-app` |
| Client ID (= `AZURE_CLIENT_ID`) | `21b51eb5-099a-4b82-b64a-a7393c390051` |
| SP Object ID | `3b9e288a-534d-4fd6-b022-f4bd435981b7` |
| Tenant ID | `3026e9ae-1c6f-48bc-aa40-711484d97639` |
| Subscription ID | `d17a6b68-0254-4879-8601-3e71f5b8e06c` |
| Role Assignment | `Website Contributor` on `/subscriptions/.../sites/func-dataplay-lab-kc` |
| Federated Credential | subject = `repo:hellojin97/python-discord-webhook-app:ref:refs/heads/main` |

### 1-3. GitHub Actions Variables (이 repo)

| Name | 출처 |
|---|---|
| `AZURE_CLIENT_ID` | App Registration `appId` |
| `AZURE_TENANT_ID` | `az account show --query tenantId` |
| `AZURE_SUBSCRIPTION_ID` | `terraform output -raw subscription_id` |
| `AZURE_FUNCTIONAPP_NAME` | `terraform output -raw function_app_name` |

> 4개 모두 **variable**(not secret). 자체로는 비밀이 아니고 federated credential subject 매칭이 권한의 진짜 게이트이기 때문. `DISCORD_WEBHOOK_URL`은 GitHub에 안 박음 — Phase 2a에서 Function App **App Setting**으로 직접 주입했고, 코드는 `os.environ`으로 읽음.

---

## 2. 의사결정 정리

### 2-1. function 모듈 설계 (advisor 권장 그대로)

| 결정 | 이유 |
|---|---|
| 모듈에 **generic `app_settings = map(string)`** 변수만 (Discord-aware 아님) | 이 모듈은 azure-infra 공용. 다른 Function App 호출자에게 Discord 결합을 강요하지 않기 위해. |
| `merge({AzureWebJobsFeatureFlags="EnableWorkerIndexing"}, var.app_settings)` | Python v2 모델 필수 플래그를 **caller가 빠뜨려도 안전**하게. 다만 base가 앞이라 caller가 일부러 override는 가능 (의도된 escape hatch). |
| `app_settings`에 `sensitive = true` | caller가 webhook URL 같은 비밀을 넣을 수 있으므로 `terraform plan` 출력 마스킹. |
| root에서 `discord_webhook_url`을 `var.app_settings`로 전달 | 비밀 관리 책임은 caller(=root)에서. 모듈은 generic. |
| `discord_webhook_url`도 `sensitive = true` | plan diff 마스킹 + 무심코 echo/로그 노출 방지. |
| root `outputs.tf`에 `function_app_id` / `function_app_name` 노출 | Phase 2b의 `az role assignment --scope`, Phase 2d의 `gh variable set` 한 줄로 끝내려고. |

> ⚠️ **state file에는 비밀이 평문**으로 들어감. azurerm backend(blob)의 접근 제어가 비밀 보호의 마지막 선. RBAC(Storage Blob Data Contributor) 신중히.

### 2-2. SP scope · role assignment

| 결정 | 이유 |
|---|---|
| `--scope <function_app_id>` 한정 | 02 문서 표준. SP가 Function App 외의 다른 리소스를 건드릴 수 없음. RG/Subscription 넓히지 않음. |
| Role = **`Website Contributor`** | App Service/Functions 배포에 필요한 최소 권한. Contributor (RG 전체)는 과함. |
| `--assignee-object-id` + `--assignee-principal-type ServicePrincipal` | `--assignee <name>`은 동명 객체가 있을 때 모호. **Object ID로 박는 게 단정적**. principal type 명시도 같은 이유. |

### 2-3. Federated credential

| 결정 | 이유 |
|---|---|
| subject = `repo:hellojin97/python-discord-webhook-app:ref:refs/heads/main` (main only) | Phase 3에서 PR validate job을 안 돌릴 예정. 권한 최소화. |
| PR 검증 필요 시 별도 credential 추가 (`subject: ...:pull_request`) | 하나의 App Registration에 credential 여러 개 가능. 나중에 `az ad app federated-credential create` 한 번이면 됨. |
| `audiences = ["api://AzureADTokenExchange"]` (고정) | Azure가 발급 대상으로 자기를 확인. 변경 금지. |

### 2-4. GitHub variables vs secrets

| 결정 | 이유 |
|---|---|
| 4개 ID 값을 **variable**로 (secret 아님) | Client/Tenant/Subscription/Function App name은 그 자체로 비밀 아님. **federated credential subject가 매칭돼야만** 사용 가능 = 다른 repo에서는 무용지물. Azure 공식 가이드 + azure-infra/02-deploy-code.md 일치. |
| `DISCORD_WEBHOOK_URL`은 GitHub에 안 박음 | 진짜 비밀이고, App Setting으로 이미 Function App에 들어갔음. 코드는 Azure에서 `os.environ`으로 직접 읽음. |

---

## 3. Plan 결과 검증 (apply 전)

`terraform plan` 출력에서 반드시 확인했고, 모두 정상이었음:

| 항목 | 기대 | 실제 |
|---|---|---|
| Function App 동작 | `~ update in-place` | ✓ |
| 변경되는 속성 | `app_settings = (sensitive value)` 한 줄 | ✓ |
| Discord URL 마스킹 | `(sensitive value)` | ✓ |
| outputs 추가 | `+ function_app_id`, `+ function_app_name` | ✓ |
| 총량 | `Plan: 0 to add, 1 to change, 0 to destroy` | ✓ |

### 무시한 cosmetic drift

- **`hidden-link: /app-insights-resource-id` tag 제거** — Azure가 Function App ↔ App Insights를 연결할 때 자동으로 다는 내부 tag. Terraform 모듈 `tags` map에 없어서 "remove"로 보임. apply 후 Azure가 다시 붙여서 다음 plan에서 또 drift로 보일 수 있음 (cosmetic, 동작 영향 없음).
- **`client_object_id` output 변경** — 이전과 다른 principal(SP vs user)로 terraform 돌렸을 때 자연스러운 차이. data source 출력만 바뀜.

→ 둘 다 의도된 변경은 아니지만 무해. 향후 plan에서도 같은 두 줄이 떠도 무시.

---

## 4. 만난 함정 (회귀 방지)

### 4-1. Terraform backend partial config

`backend.tf`가 `use_oidc + use_azuread_auth`만 지정하고 storage 정보(`storage_account_name`/`container_name`/`key`/`resource_group_name`)는 비워둔 상태 → `terraform init` 때 **`-backend-config=...` 4개를 반드시 같이 줘야 함**. 셸 변수 `TFSTATE_*`가 사라지면 init 실패 (`empty container name` → 빈 값 prompt 무한).

**복구 순서:**
```bash
# 1) tfstate storage account 찾기
az storage account list --query "[?contains(name, 'tfstate')].{name:name, rg:resourceGroup}" -o table

# 2) 컨테이너 이름 확인 (default 'tfstate' 아닐 수 있음!)
az storage container list --account-name "$TFSTATE_SA" --auth-mode login -o table

# 3) 환경변수 복원 후
terraform init -reconfigure \
  -backend-config="resource_group_name=$TFSTATE_RG" \
  -backend-config="storage_account_name=$TFSTATE_SA" \
  -backend-config="container_name=$TFSTATE_CONTAINER" \
  -backend-config="key=terraform.tfstate"
```

> 이번 작업에서 처음에 container name을 `tfstate`로 가정했다가 **404 ContainerNotFound** — 실제 컨테이너 이름이 달랐음. 가정하지 말고 `az storage container list`로 확인할 것.
>
> ⚠️ **local backend로 우회 금지** — 기존 remote state와 단절. 새 빈 state로 시작하면 모든 리소스 import 다시 해야 함.

### 4-2. OIDC subject 오타 = 403

federated credential `subject`의 `<owner>/<repo>` 오타는 GitHub Actions 배포 시 **403 OIDC**로 떨어짐. 가장 흔한 함정.

**검증 순서 (Phase 3 시작 전 반드시):**
```bash
# 이 repo의 실제 remote URL이 federated credential subject와 일치하는지
git remote -v
# fetch  https://github.com/hellojin97/python-discord-webhook-app.git
az ad app federated-credential list --id "$FUNC_CLIENT_ID" -o table
# subject: repo:hellojin97/python-discord-webhook-app:ref:refs/heads/main
```

owner/repo 두 부분이 정확히 일치해야 함.

### 4-3. RBAC propagation delay

`az role assignment list --assignee ... --scope ...` 가 빈 출력이 나올 수 있음 — Azure AD propagation은 보통 30초~수 분. **`create` 명령이 success JSON을 돌려주면 그게 ground truth**. list 빈 출력 ≠ 실패.

### 4-4. `merge()` 우선순위

`merge({EnableWorkerIndexing="..."}, var.app_settings)` 에서 **뒤 인자가 override**. 즉 caller가 `app_settings = { AzureWebJobsFeatureFlags = "Something" }` 박으면 모듈의 base를 덮어씀. 의도된 escape hatch지만, "절대 못 덮음"을 강제하려면 base를 뒤로 옮겨야 함 — 현재 정책은 명시적 override 허용.

### 4-5. 인프라 → 코드 순서

여전히 절대 원칙. 이번에 잘 지켰지만, 다음 변경 시(예: App Setting 추가)에도 같은 순서:
1. `azure-infra` PR/apply → app_settings에 새 키 추가
2. 그 다음 이 repo 코드에서 `os.environ["NEW_KEY"]` 추가/배포

순서를 어기면 새 env var를 못 찾아 런타임 에러.

---

## 5. Phase 3 진입 조건 (체크)

- [x] Function App에 `AzureWebJobsFeatureFlags=EnableWorkerIndexing` + `DISCORD_WEBHOOK_URL` 박힘 (Terraform `app_settings`)
- [x] SP에 Function App scope `Website Contributor` 부여
- [x] Federated credential: `repo:hellojin97/python-discord-webhook-app:ref:refs/heads/main`
- [x] GitHub variables 4개 (`AZURE_CLIENT_ID/TENANT_ID/SUBSCRIPTION_ID/FUNCTIONAPP_NAME`)
- [x] `git remote -v` owner/repo가 federated credential subject와 일치
- [ ] (Phase 3) `.github/workflows/deploy.yml` 작성 — `uv sync` → `.python_packages/lib/site-packages` 사전 설치 → `azure/functions-action@v1` (`enable-oryx-build: false`)
- [ ] (Phase 3) main push → `gh run watch` → 운영 smoke test (function key 포함 POST → Discord 수신)

→ Phase 3 작업은 [02-apply-deploy-pipeline.md](02-apply-deploy-pipeline.md) §3-C, 그리고 azure-infra `docs/function/02-deploy-code.md`의 deploy workflow YAML을 이 repo의 owner/`--python` 버전(3.11)에 맞춰 적용.

---

## 6. 다음에 정책 바꾸려면

| 변경 | 명령 |
|---|---|
| PR에서도 OIDC 토큰 발급 (Phase 3에서 PR validate 추가 시) | `az ad app federated-credential create --id "$FUNC_CLIENT_ID" --parameters '{"name":"github-pull-request","issuer":"https://token.actions.githubusercontent.com","subject":"repo:hellojin97/python-discord-webhook-app:pull_request","audiences":["api://AzureADTokenExchange"]}'` |
| `DISCORD_WEBHOOK_URL`을 Key Vault로 이전 | Function App System-Assigned MI에 KV `Key Vault Secrets User` 부여 → `app_setting` 값을 `@Microsoft.KeyVault(SecretUri=...)` 참조로 교체 → Terraform에서 `discord_webhook_url` var 제거 |
| federated credential 삭제/재생성 | `az ad app federated-credential delete --id "$FUNC_CLIENT_ID" --federated-credential-id <id>` (id는 list 명령으로 확인) |
| App Registration 통째로 삭제 (역방향 cleanup) | `az ad app delete --id "$FUNC_CLIENT_ID"` (SP·credential·role assignment 같이 제거됨) |
