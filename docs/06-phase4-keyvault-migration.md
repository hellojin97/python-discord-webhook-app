# Phase 4 — Key Vault 이전 (Discord webhook URL 외부화)

Phase 4의 목표는 **`DISCORD_WEBHOOK_URL`을 Terraform/GitHub/App Setting의 평문 흐름에서 완전히 빼고, Key Vault 한 곳에만 두는 것**.

전제 (모두 Phase 3까지 충족):
- Function App `func-dataplay-lab-kc`가 운영 중, `DISCORD_WEBHOOK_URL` App Setting에 평문 URL 박힘
- 코드는 `os.environ["DISCORD_WEBHOOK_URL"]`로 읽음 — Key Vault 참조 전환 후에도 코드 변경 0

구조·표준 패턴은 [02-apply-deploy-pipeline.md](02-apply-deploy-pipeline.md), Phase 2/3 회고는 [04](04-phase2-deployment-infra.md)/[05](05-phase3-cicd.md).

> 진행 중. 이 문서는 각 Step 완료 시점에 갱신된다. 현재 **Step 1 (Key Vault 리소스 + Function App MI role assignment) 완료** 시점.

---

## 0. 왜 Key Vault로 옮기는가?

### 현재 비밀 흐름 (Phase 3 종료 시점)

```
GitHub secret  TF_VAR_DISCORD_WEBHOOK_URL   (azure-infra repo)
        ↓
Terraform var  var.discord_webhook_url      (sensitive)
        ↓
tfstate blob   "DISCORD_WEBHOOK_URL=..."    ← 평문 저장
        ↓
App Setting    DISCORD_WEBHOOK_URL=...      ← 평문 저장
        ↓
Function 런타임 os.environ["..."]
```

**평문이 머무는 곳이 3군데** (GitHub secret · tfstate · App Setting). 회전하려면 terraform apply가 필요하고, Phase 2에서 실제로 셸 quote 오류로 webhook URL이 history에 평문 노출돼 즉시 회전한 사고도 한 번 있었음 ([04-phase2 §4-5](04-phase2-deployment-infra.md)).

### Key Vault 이후

```
                           Key Vault Secret  discord-webhook-url   ← 비밀이 사는 유일한 곳
                                  ↑                      ↓
                       사용자가 1회 az CLI로 주입       App Setting  DISCORD_WEBHOOK_URL=@Microsoft.KeyVault(...)
                                                              ↓
                                                       Function 런타임  os.environ["..."]   (Azure가 resolve해서 plain string으로 전달)
```

- Terraform var: 제거
- GitHub secret: 제거
- tfstate에 평문: 없음
- App Setting: 참조 문자열만 (`@Microsoft.KeyVault(...)`)
- 회전: `az keyvault secret set` 한 번 (terraform apply 불필요)

---

## 1. 전체 흐름 (7단계)

| 단계 | 주체 | 작업 | 상태 |
|---|---|---|---|
| **a** | 사용자 (로컬 `az`) | azure-infra SP에 subscription **Owner** 격상 (role assignment 생성 권한) | ✅ |
| **b** | GitHub Actions (azure-infra SP) | `terraform apply` — KV 리소스 + Function App MI에 `Key Vault Secrets User` | ✅ |
| **c** | 사용자 (로컬 `az`) | 사용자 본인에게 KV scope `Key Vault Secrets Officer` 부여 | ⏳ |
| **d** | 사용자 (로컬 `az`) | `az keyvault secret set`로 webhook URL **1회** 주입 | ⏳ |
| **e** | GitHub Actions (azure-infra SP) | 두 번째 `terraform apply` — `app_setting` 값을 `@Microsoft.KeyVault(...)` 참조로 전환 | ⏳ |
| **f** | 사용자 (로컬 `curl`) | 운영 smoke test (Discord 수신 확인) | ⏳ |
| **g** | 양쪽 | `discord_webhook_url` var + CI env + GitHub secret 정리 | ⏳ |

> a→b는 GitHub Actions가 권한 있는 상태에서 KV/RBAC을 만든다. c→d는 그 권한으로 사용자가 직접 secret 값을 넣는다. e는 인프라가 KV 참조로 갈아탄다. **secret 값은 한 번도 Terraform/GitHub에 들어가지 않는다** — 이게 메모리 원칙 "TF var/GitHub secret/CI env 모두 제거"의 실현 방식.

---

## 2. 핵심 의사결정

### 2-1. Secret 자체는 Terraform 바깥에서 관리 — (A)

| 안 | 내용 | 채택 |
|---|---|---|
| (A) | Terraform은 KV + RBAC만. webhook URL은 `az keyvault secret set`으로 1회 주입. | ✅ |
| (B) | `azurerm_key_vault_secret` 리소스 + `lifecycle { ignore_changes = [value] }`. 첫 apply에 placeholder, 이후 CLI로 set. |  |

**왜 (A):** (B)는 placeholder value를 어디서든 받아야 해서 결국 TF_VAR가 또 필요. "TF var 모두 제거" 원칙과 충돌. (A)는 secret 흔적이 Terraform 어디에도 안 남음 — IaC 가시성은 살짝 떨어지지만 (state에 secret 리소스 자체가 없음) 비밀 경로 단순화가 더 가치 있음.

### 2-2. Key Vault permission 모델 = RBAC (legacy access policy 아님)

```hcl
rbac_authorization_enabled = true   # provider 4.x: 이 이름이 정식
```

- 권한은 별도 `azurerm_role_assignment`로 분리 → IaC 추적 깔끔
- access policy 모델은 자원에 권한이 박혀 있어 drift 추적 어려움
- ⚠️ provider 5.0에서 access policy 관련 인자는 더 제한될 예정. 새로 만드는 KV는 RBAC 권장.

### 2-3. App Setting 참조 형식: versionless

```
@Microsoft.KeyVault(VaultName=kv-dataplay-lab-kc;SecretName=discord-webhook-url)
```

- 다른 형식 `@Microsoft.KeyVault(SecretUri=https://.../secrets/<name>/<version>/)`은 **버전을 pin** — 회전해도 안 따라감 → 회전 후 silent하게 옛 값 계속 사용.
- VaultName/SecretName 형식은 항상 최신 enabled 버전을 가져옴 → `az keyvault secret set` 한 번이면 새 값으로 자동 갱신.

### 2-4. role assignment: Function App MI는 Terraform, 사용자는 외부

| 주체 | 부여 방법 | 이유 |
|---|---|---|
| Function App System-Assigned MI | Terraform (`azurerm_role_assignment.kv_func_secrets_user`) | 런타임 동작에 필요한 영구적 권한. IaC로 추적해야 다른 환경 복제 가능. |
| 사용자 본인 (Officer) | 로컬 `az role assignment create` (Terraform 외부) | "사용자가 직접 secret 값 주입한다"는 행위와 짝. Terraform이 매번 apply 주체에 따라 principal이 바뀌면 drift 잡힘. |

### 2-5. azure-infra SP를 subscription Owner로 격상 — (1)

`terraform apply`가 `azurerm_role_assignment`를 만들려면 `Microsoft.Authorization/roleAssignments/write` 권한 필요. 기존 `Contributor`는 이게 제외돼 있어서 403.

| 안 | 내용 | 채택 |
|---|---|---|
| (1) SP를 subscription scope Owner로 격상 | 단순. lab 수준에서 위험 낮음. 향후 다른 RBAC 변경도 자동. | ✅ |
| (2) RG scope `Role Based Access Control Administrator` 추가 | scope 좁음. 다른 RG 다루면 또 부여 필요. |  |
| (3) role assignment를 Terraform 밖으로 빼고 수동 | secret과 일관. 다만 IaC 가시성 ↓. |  |

기존 `Contributor`는 그대로 둠 (Azure는 가장 강한 권한 적용 — Owner가 효과).

---

## 3. Step 1 완료 — 무엇이 만들어졌나

### 3-1. azure-infra 변경 (4 files)

| 파일 | 변경 |
|---|---|
| `terraform/modules/key_vault/main.tf` (신규) | `azurerm_key_vault` — RBAC mode + soft-delete 7일 + purge protection off |
| `terraform/modules/key_vault/variables.tf` (신규) | `name`(validation) / `resource_group_name` / `location` / `tenant_id` / `tags` |
| `terraform/modules/key_vault/outputs.tf` (신규) | `id` / `name` / `vault_uri` |
| `terraform/main.tf` | `module "key_vault"` 호출 + `azurerm_role_assignment.kv_func_secrets_user` (Function App MI → KV `Key Vault Secrets User`) |

### 3-2. Azure 측 신규 객체

| 객체 | 값 |
|---|---|
| Key Vault 이름 | `kv-dataplay-lab-kc` |
| Vault URI | `https://kv-dataplay-lab-kc.vault.azure.net/` |
| Permission 모델 | RBAC (`rbac_authorization_enabled = true`) |
| Soft-delete retention | 7일 (기본 90일 → lab 빠른 destroy/recreate용으로 축소) |
| Purge protection | `false` (lab만, prod라면 `true` 권장) |
| Function App MI principal id | `4f0350c8-7c32-46da-8b54-6415d5af6699` |
| Role Assignment | `Key Vault Secrets User` on KV scope |

### 3-3. SP 권한 변화 (azure-infra OIDC SP `d27c582a-...`)

| Scope | Role | 비고 |
|---|---|---|
| `/subscriptions/<sub>` | `Contributor` | 기존 |
| `/subscriptions/<sub>` | **`Owner`** | 이번에 추가 (`az role assignment create`) |
| `/subscriptions/<sub>/.../tfstate-storage-account` | `Storage Blob Data Contributor` | 기존 (tfstate backend용) |

### 3-4. plan 결과 검증 (apply 전)

| 항목 | 기대 | 실제 |
|---|---|---|
| KV 생성 | `+ create` | ✓ |
| Function App MI role assignment | `+ create` (principal_id = `4f0350c8-...`) | ✓ |
| 사용자 Officer role assignment | 없음 (Terraform 밖에서 부여 예정) | ✓ |
| function module | 변경 없음 또는 cosmetic drift만 | △ ([§4-5](#4-5-app_settings--sensitive-value--표시는-실제-변경이-아닐-가능성-높음)) |
| 총량 | `Plan: 2 to add, 1 to change, 0 to destroy` | ✓ |

---

## 4. 만난 함정 (회귀 방지)

### 4-1. `enable_rbac_authorization` deprecation

```
"enable_rbac_authorization" is deprecated: Reason: ""
```

azurerm provider 4.x 후반부터 deprecate, **5.0에서 완전 제거**. 새 이름은 `rbac_authorization_enabled`. (자료: hashicorp/terraform-provider-azurerm 5.0 upgrade guide)

자동으로 한쪽으로 바꿔주지는 않으니, KV 모듈 새로 짤 때 처음부터 `rbac_authorization_enabled` 쓰는 것이 안전.

### 4-2. `Contributor`만으로는 role assignment 생성 불가 → 403

| Role | `Microsoft.Authorization/*/Write` | role assignment 생성 |
|---|---|---|
| `Contributor` | ❌ 제외 | 불가 |
| `Owner` | ✅ 포함 | 가능 |
| `User Access Administrator` | ✅ 포함 | 가능 (다른 권한 없음) |
| `Role Based Access Control Administrator` | ✅ (assignment만) | 가능 |

azure-infra SP가 기본 `Contributor`로 RG/Storage/Function App 등은 만들 수 있지만, **`azurerm_role_assignment` 리소스 생성에서 403**이 뜸. 이번엔 subscription scope Owner로 격상 해결 (Step 1-a).

**검증 명령:**
```bash
az role assignment list --assignee <client-id> --all --query "[].{role:roleDefinitionName, scope:scope}" -o table
```

### 4-3. Terraform state lock 잔존 → GitHub Actions plan 실패

로컬에서 `terraform plan` 돌리다 Ctrl+C나 셸 닫힘으로 비정상 종료되면 backend blob의 lock이 남음. 이후 GitHub Actions에서:

```
Error acquiring the state lock
Lock Info:
  ID:        778ecc1e-...
  Operation: OperationTypePlan
  Who:       dawn@...
```

**해결:**
```bash
cd azure-infra/terraform
terraform force-unlock <lock-id>
```

→ confirm prompt → `yes`. 그 후 PR check rerun.

**예방:** 로컬 plan은 끝까지 돌리고 종료하거나, plan은 GitHub Actions에만 맡기기.

### 4-4. RBAC 모드에서 subscription Owner여도 KV 데이터 평면 권한은 별도

`az keyvault secret set/show/delete` 같은 데이터 평면 작업은 **subscription scope Owner로는 안 됨**. KV scope에서 `Key Vault Secrets Officer` (또는 Administrator) 별도 부여 필요. RBAC 모드에서는 관리 평면 ↔ 데이터 평면 권한이 깔끔히 분리돼 있음.

다음 Step c에서 사용자 본인에게 별도 부여 예정. SP에게는 부여하지 않음 — apply가 secret 값을 다룰 일이 없고, 최소 권한 원칙.

### 4-5. `app_settings = (sensitive value)` ~ 표시는 실제 변경이 아닐 가능성 높음

```
~ resource "azurerm_linux_function_app" "this" {
    ~ app_settings = (sensitive value)
    ~ tags         = { "hidden-link: /app-insights-resource-id" = "..." -> null }
}
```

- `app_settings`가 `sensitive = true`로 마킹돼 있어서 plan에 값 마스킹. Terraform이 sensitive 값에 대해 noop를 확신 못 하면 보수적으로 `~`로 표기 → **변경 유무 확인 불가**.
- `hidden-link` tag 제거는 [04-phase2 §3 cosmetic drift](04-phase2-deployment-infra.md#무시한-cosmetic-drift)와 동일 현상. Azure Portal이 자동으로 다시 붙임.

이번엔 어차피 Step e에서 `app_settings`를 KV 참조로 교체할 거라 무시. apply 후 Function App이 살아있고 Discord 수신이 그대로면 OK.

### 4-6. `az role assignment list -o table`의 "Principal" 컬럼은 **App ID** (Object ID 아님)

`az role assignment list --scope <kv-id> -o table` 출력에서 Service Principal/MI row에 보이는 GUID는 **App ID (= client ID)**, role assignment에 실제로 박혀 있는 `principal_id` (= Object ID)가 아님. 두 ID는 다르므로 plan 출력의 `principal_id`와 list 출력의 "Principal"이 안 맞아 보이는 게 정상 — 같은 객체의 두 식별자.

| 식별자 | Function App MI 예 | 어디서 보이는가 |
|---|---|---|
| Object ID (= principal id) | `4f0350c8-...` | `azurerm_role_assignment.principal_id`, `az functionapp identity show --query principalId`, Terraform plan |
| App ID (= client id) | `e2560800-...` | `az role assignment list -o table`의 "Principal", `az ad sp show --id <objectId> --query appId` |

User principal은 같은 컬럼에 UPN(email)로 표시. SP/MI는 displayName이 아니라 appId — 표시 규칙이 통일적이지 않으니 헷갈리기 쉬움.

**검증할 때:** GUID 비교 대신 Graph로 정체 조회:
```bash
az rest --method GET \
  --url "https://graph.microsoft.com/v1.0/servicePrincipals/<role-assignment-principal-id>" \
  --query "{displayName:displayName, type:servicePrincipalType, appId:appId}"
```

### 4-7. `azurerm_linux_function_app.app_settings`는 **authoritative map** — `WEBSITE_RUN_FROM_PACKAGE`가 wipe됨

Step e (app_setting을 KV 참조로 교체)의 apply 직후 **404 + 함수 0개 인덱싱** 사건의 진짜 원인.

**현상:**
- `az functionapp function list` → `relay_to_discord` 보임 (ARM metadata는 정상)
- `curl /admin/host/status` → `state=Running`
- `curl /admin/functions` → `[]` (런타임이 함수 0개 인식)
- App Insights logs: `"No job functions found. Try making your job classes and methods public..."`
- App Settings list에 **`WEBSITE_RUN_FROM_PACKAGE` 없음**

**원인:** `Azure/functions-action@v1`은 Linux Consumption + RBAC 환경에서 zip을 storage blob에 올린 후 `WEBSITE_RUN_FROM_PACKAGE=<SAS URL>`을 App Setting에 set한다 (deploy log의 `"Will use WEBSITE_RUN_FROM_PACKAGE to deploy"`). 그런데 Terraform의 `azurerm_linux_function_app.app_settings`는 **authoritative map** — 명시 안 한 키는 apply 시 wipe.

Phase 2/3에서 안 깨졌던 이유: terraform-apply가 먼저 돌고 그 후 deploy.yml이 마지막에 RUN_FROM_PACKAGE를 set → 다음 apply가 없으니 유지. Phase 4 Step e가 **deploy 후 처음으로 돌아간 terraform apply** → wipe.

**진단 — 한 명령:**
```bash
az functionapp config appsettings list -n func-dataplay-lab-kc -g rg-dataplay-lab-kc \
  --query "[?starts_with(name, 'WEBSITE_RUN_FROM_PACKAGE')].{name:name, value:value}"
```
빈 배열이면 wipe 확정.

**복구:** `gh workflow run deploy.yml --ref main` 으로 zip-deploy 재실행 → RUN_FROM_PACKAGE 다시 박힘 → 워커 자동 restart → 인덱싱 복구.

**재발 방지 (필수):** `modules/function/main.tf`의 `azurerm_linux_function_app.this` 에:
```hcl
lifecycle {
  ignore_changes = [
    app_settings["WEBSITE_RUN_FROM_PACKAGE"],
  ]
}
```
map의 특정 키만 ignore하는 `attr["key"]` 문법. 이 키만 외부 관리로 두고 나머지 `app_settings`(`DISCORD_WEBHOOK_URL` 등)는 Terraform이 계속 통제.

> 이 fix가 main에 들어가기 *전*에 deploy.yml을 트리거하면 §4-8 race가 다시 일어남. 순서 매우 중요.

### 4-8. terraform-apply ↔ deploy.yml race — 둘 다 같은 App Setting을 다투면 마지막에 이긴 쪽이 남는다

§4-7 복구를 시도하는 과정에서 **deploy.yml과 terraform-apply가 7초 차이로 race**가 나서 한 번 더 실패. Activity log 일부:

| 시각 (UTC) | 이벤트 | Caller |
|---|---|---|
| 15:25:34 | terraform-apply workflow_dispatch 시작 | azure-infra SP |
| 15:26:51 | deploy.yml 시작 (workflow_dispatch) | python-discord-webhook-app SP |
| 15:27:00 | `config/write` — functions-action이 `WEBSITE_RUN_FROM_PACKAGE` set | deploy SP |
| **15:27:07** | `config/write` — terraform-apply가 같은 키 wipe (**7초 후**) | azure-infra SP |
| 15:27:33 | `Sync Trigger call was successful.` (그러나 이미 wipe됨) | deploy SP |

deploy log엔 모든 step success로 보이는데 실제 결과는 또 인덱싱 0. activity log를 보기 전까진 원인 안 잡힘.

**진단:**
```bash
az monitor activity-log list \
  --resource-id "/subscriptions/.../sites/func-dataplay-lab-kc" \
  --start-time "<deploy-시작-시각-1시간전>" \
  --query "[?status.value=='Succeeded'].{time:eventTimestamp, op:operationName.value, caller:caller}" \
  -o table
```
`config/write` 가 두 caller로 연달아 보이면 race 확정.

**해결:**
1. **근본 fix (영속적):** §4-7의 `lifecycle.ignore_changes`. main에 들어간 후엔 terraform이 RUN_FROM_PACKAGE를 절대 안 건드림 → race 자체가 무해.
2. **순서 enforcement (lifecycle 박힌 후에도 안전):** terraform-apply 완료 *후*에 deploy.yml 트리거. `gh run watch --repo hellojin97/azure-infra` 로 apply 완료 확인.

> azure-infra의 `workflow_dispatch` trigger도 race source가 될 수 있음. GitHub UI에서 "Re-run all jobs"를 누르면 일부 surface에서 workflow_dispatch로 기록 — 의식적으로 dispatch하지 않았어도 history에는 그렇게 남을 수 있다.

### 4-9. KV 이름은 globally unique + soft-delete 90일 reserve

- `kv-<name>` 형식, 3-24자, 알파벳 시작, 알파벳/숫자/하이픈, 끝은 영숫자
- 한 번 destroy하면 soft-delete retention 기간 동안 이름 예약됨 — 이번엔 7일로 설정해서 lab에서 destroy/recreate 빠르게
- prod라면 `soft_delete_retention_days = 90` + `purge_protection_enabled = true` (소실 위험 ↓)

---

## 5. 다음 단계 (Step c~g)

### Step c — 사용자 본인에게 KV Secrets Officer 부여

```bash
KV_ID=$(az keyvault show \
  --name kv-dataplay-lab-kc \
  --resource-group rg-dataplay-lab-kc \
  --query id -o tsv)

az role assignment create \
  --assignee "$(az ad signed-in-user show --query id -o tsv)" \
  --role "Key Vault Secrets Officer" \
  --scope "$KV_ID"
```

→ 확인: `az role assignment list --scope "$KV_ID" -o table` 에 Officer 한 줄 추가됨.

### Step d — webhook URL 1회 주입

```bash
# 셸 history 노출 회피 위해 --value 인자 대신 stdin
az keyvault secret set \
  --vault-name kv-dataplay-lab-kc \
  --name discord-webhook-url \
  --file <(echo -n "<URL>")     # 또는 따로 파일 만들어서
```

또는 가장 안전한 방식 (값을 명령에 안 박음):
```bash
read -s WEBHOOK_URL    # silent 입력
az keyvault secret set \
  --vault-name kv-dataplay-lab-kc \
  --name discord-webhook-url \
  --value "$WEBHOOK_URL"
unset WEBHOOK_URL
```

→ 확인: `az keyvault secret show --vault-name kv-dataplay-lab-kc --name discord-webhook-url --query value -o tsv | head -c 30` (앞 일부만)

### Step e — app_setting을 KV 참조로 전환

`azure-infra/terraform/main.tf`의 `module "function"` 블록:

```hcl
app_settings = {
  DISCORD_WEBHOOK_URL = "@Microsoft.KeyVault(VaultName=${module.key_vault.name};SecretName=discord-webhook-url)"
}
```

→ PR → terraform-plan 결과 확인 → merge → terraform-apply.

**Apply 직후 검증:**
```bash
az functionapp config appsettings list \
  --name func-dataplay-lab-kc \
  --resource-group rg-dataplay-lab-kc \
  --query "[?name=='DISCORD_WEBHOOK_URL']"
```

성공이면 `value`가 `@Microsoft.KeyVault(VaultName=...;SecretName=...)` 문자열 그대로 보임. Function 런타임은 Azure가 resolve해서 평문으로 받음.

### Step f — 운영 smoke test

[05-phase3 §2 happy path](05-phase3-cicd.md#happy-path) 동일 curl → 200 + Discord 채널 embed 도착하면 끝.

만약 500이 뜨고 로그에 `"Could not resolve Key Vault reference"` → role assignment / KV 이름 / secret 이름 오타 의심.

### Step g — 기존 비밀 경로 정리

| 위치 | 작업 |
|---|---|
| `azure-infra/terraform/variables.tf` | `variable "discord_webhook_url"` 블록 삭제 |
| `azure-infra/.github/workflows/terraform-plan.yml` | env 블록의 `TF_VAR_discord_webhook_url:` 한 줄 삭제 |
| `azure-infra/.github/workflows/terraform-apply.yml` | 동일 |
| GitHub `azure-infra` repo secret | `gh secret delete TF_VAR_DISCORD_WEBHOOK_URL --repo hellojin97/azure-infra` |
| (선택) SP의 `Contributor` role | `az role assignment delete --assignee <sp> --role Contributor --scope /subscriptions/<sub>` (Owner만 남기는 게 더 깔끔, 어차피 강한 권한이 적용 중) |

---

## 6. 정책 변경 시 (참고 명령)

| 변경 | 명령 |
|---|---|
| Webhook URL 회전 | `az keyvault secret set --vault-name kv-dataplay-lab-kc --name discord-webhook-url --value <new>` (terraform apply 불필요 — versionless 참조라 다음 호출부터 새 값) |
| 옛 버전으로 롤백 | `az keyvault secret list-versions --vault-name kv-dataplay-lab-kc --name discord-webhook-url` → 옛 version `--enabled true`, 새 version `--enabled false` |
| KV destroy 후 즉시 재생성 | (soft-delete 7일 잠금) `az keyvault purge --name kv-dataplay-lab-kc` 후 `terraform apply` |
| 다른 비밀 추가 (예: Slack도 같이 보내려면) | `az keyvault secret set ... --name slack-webhook-url` + Function App `app_setting`에 `SLACK_WEBHOOK_URL = @Microsoft.KeyVault(...)` 한 줄 추가 + 코드에서 `os.environ` 추가 |

---

## 7. 진행 체크리스트

- [x] (a) azure-infra SP에 subscription Owner 격상
- [x] (b) `terraform apply` — KV + Function App MI role 생성
- [x] (c) 사용자 본인에게 KV Secrets Officer 부여
- [x] (d) `az keyvault secret set`로 webhook URL 1회 주입
- [x] (e) `app_setting`을 KV 참조로 전환 + 두 번째 apply
- [x] **(추가) `lifecycle.ignore_changes`로 `WEBSITE_RUN_FROM_PACKAGE` 보호** (§4-7) — Step e apply가 RUN_FROM_PACKAGE를 wipe해서 인덱싱이 깨진 후 추가
- [x] (f) 운영 smoke test (Discord 수신 확인) — `200 OK` + embed 도착
- [x] (g) 기존 `discord_webhook_url` var / CI env / GitHub secret 정리 (azure-infra PR #10)
