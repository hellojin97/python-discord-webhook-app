# `sarisia/actions-status-discord` — 액션 reference

GitHub Actions에서 job 결과를 Discord webhook으로 알리는 액션. 본 repo는 Function App(`/api/notify`)으로 **Lakeflow → Discord** 중계가 핵심 흐름이라 이 액션을 직접 쓰진 않지만, **같은 Discord 통합 영역**의 인접 패턴이라 reference로 정리. (실제 적용 사례는 자매 repo `azure-infra`의 [terraform-apply 결과 알림](https://github.com/hellojin97/azure-infra/blob/main/docs/webhook/01-notify-on-apply.md))

> 기준 버전: **v1.16.0** (2026-01-09). 공식 출처: https://github.com/sarisia/actions-status-discord

---

## 본 repo의 Function App 흐름과의 차이 (한눈에)

| 차원 | 본 repo Function App (`/api/notify`) | sarisia 액션 |
|---|---|---|
| 트리거 | Lakeflow Job (외부 system) → HTTP POST | GitHub Actions job 종료 시점 |
| Payload schema | `event/job_name/run_id/...` (Lakeflow-style) | embed 자동 생성 (status 기반) |
| 채널 분리 | Function App scope 안에서 routing 가능 | webhook URL = 채널. 분리하려면 webhook 여러 개 |
| 비밀 보관 | Key Vault (Function App MI로 resolve) | GitHub secret (`${{ secrets.* }}`) |
| 코드 | Python (`function_app.py`) | YAML 4-6줄 |
| 회전 | KV에 새 값 set (apply 불필요) | `gh secret set ...` |

→ 두 흐름은 트리거/저장소/payload가 모두 다르지만 **"Discord webhook URL을 절대 평문 노출시키지 않는다"는 원칙은 동일**. 본 repo의 [Phase 2 §4-5 노출 사고](04-phase2-deployment-infra.md), [Phase 4의 Key Vault 외부화](06-phase4-keyvault-migration.md)와 같은 맥락에서 이 액션도 webhook을 secret으로만 다뤄야 함.

---

## TL;DR — 최소 형식

```yaml
- uses: sarisia/actions-status-discord@v1
  if: always()
  with:
    webhook: ${{ secrets.DISCORD_WEBHOOK_URL }}
```

이 4줄로 끝. 자동으로:
- `${{ job.status }}` 감지 → Success / Failure / Cancelled
- 색 자동 (초록 / 빨강 / 회색)
- title은 `${{ github.workflow }}` (워크플로우 이름)
- Branch / Commit / Workflow / Actor 같은 context fields 자동 첨부
- timestamp 자동

---

## 1. 입력 파라미터 (전체)

| 파라미터 | 타입 | 기본값 | 설명 |
|---|---|---|---|
| `webhook` | String | `env.DISCORD_WEBHOOK` | Discord webhook URL. **`/github` 접미사 붙이면 안 됨** (FAQ 1순위 함정) |
| `status` | String | `${{ job.status }}` | `Success` / `Failure` / `Cancelled` |
| `content` | String | (empty) | embed 밖 메시지. mention(`<@userid>`/`<@&roleid>`) 지원 |
| `title` | String | `${{ github.workflow }}` | embed title (markdown 지원) |
| `description` | String | (empty) | embed description (markdown 지원) |
| `image` | String | (empty) | 첨부 이미지 URL |
| `color` | String | (자동) | hex `0xFFFFFF`. 안 주면 status에 따라 자동 |
| `url` | String | (empty) | title 클릭 시 이동 URL |
| `username` | String | webhook default | webhook의 username override |
| `avatar_url` | String | webhook default | webhook의 avatar override |
| `nofail` | Bool | `true` | 알림 실패가 job 실패가 되는지. `false`면 알림 실패도 job fail |
| `nocontext` | Bool | `false` | context fields(Branch/Commit/Workflow/Actor) 숨김 |
| `noprefix` | Bool | `false` | title 앞에 `"Success: "` 같은 prefix 안 붙임 |
| `nodetail` | Bool | `false` | `nocontext + noprefix` 한꺼번에 true |
| `notimestamp` | Bool | `false` | timestamp 숨김 |
| `ack_no_webhook` | Bool | `false` | webhook이 비어 있어도 에러 안 냄 (조건부 사용 시 유용) |

### Output

| Output | 설명 |
|---|---|
| `payload` | 액션이 보낸 raw payload. `id: notify` 박으면 `${{ steps.notify.outputs.payload }}`로 디버깅 capture |

---

## 2. 핵심 동작 규칙

### Status 자동 감지
`status` input을 안 주면 default가 `${{ job.status }}`. 보통 명시 안 함.

### 색 자동
| status | 색 |
|---|---|
| Success | 초록 (대략 #2ECC71) |
| Failure | 빨강 (대략 #E74C3C) |
| Cancelled | 회색/노랑 |

override는 `color: 0xFF91A4` 같은 hex.

### Context fields 자동 첨부
- Repository / Ref / Workflow / Actor / Event 등
- 숨기려면 `nocontext: true` 또는 `nodetail: true`

### Title prefix
default로 status 라벨이 title 앞에 붙음 (`"Success: Terraform Apply"`). 빼려면 `noprefix: true`.

### `nodetail` 한 줄로
context + prefix 한 번에 꺼짐. release announcement처럼 "성공 알림"이 아닌 일반 메시지에 유용.

### Markdown 지원
`title`, `description`, `content`는 markdown 처리. `**bold**`, `[link](url)`, fenced code 등.

### Mention 동작
- `content`의 mention (`<@userid>`, `<@&roleid>`)은 실제 ping
- embed 안의 mention은 ping 안 됨 (Discord 정책)

---

## 3. YAML 패턴 모음

### 3-1. 최소
```yaml
- uses: sarisia/actions-status-discord@v1
  if: always()
  with:
    webhook: ${{ secrets.DISCORD_WEBHOOK_URL }}
```

### 3-2. 풀 옵션
```yaml
- uses: sarisia/actions-status-discord@v1
  if: always()
  with:
    webhook:     ${{ secrets.DISCORD_WEBHOOK_URL }}
    status:      ${{ job.status }}
    content:     "Hey <@316911818725392384>"
    title:       deploy
    description: Build and deploy to GitHub Pages
    image:       ${{ secrets.EMBED_IMAGE }}
    color:       0x0000ff
    url:         https://github.com/sarisia/actions-status-discord
    username:    GitHub Actions
    avatar_url:  ${{ secrets.AVATAR_URL }}
```

### 3-3. Failure만 알림
```yaml
- uses: sarisia/actions-status-discord@v1
  if: failure()    # always() 대신
  with:
    webhook: ${{ secrets.DISCORD_WEBHOOK_URL }}
```

### 3-4. Release announcement (status 무관, 깔끔한 embed)
```yaml
- uses: sarisia/actions-status-discord@v1
  with:
    webhook:     ${{ secrets.DISCORD_WEBHOOK_URL }}
    nodetail:    true
    title:       "New version of `myapp` is ready!"
    description: |
      Version `${{ github.event.release.tag_name }}`
      [Download here](${{ github.event.release.html_url }})
    color:       0xff91a4
```

### 3-5. webhook을 env로 (여러 step에서 공유)
```yaml
env:
  DISCORD_WEBHOOK: ${{ secrets.DISCORD_WEBHOOK_URL }}
steps:
  - uses: sarisia/actions-status-discord@v1   # webhook input 생략 — env.DISCORD_WEBHOOK fallback
    if: always()
```

### 3-6. 디버깅 — raw payload 확인
```yaml
- uses: sarisia/actions-status-discord@v1
  id: notify
  if: always()
  with:
    webhook: ${{ secrets.DISCORD_WEBHOOK_URL }}

- name: Dump payload
  if: always()
  run: echo "${{ steps.notify.outputs.payload }}"
```

---

## 4. 함정 (재발 방지)

### 4-1. `/github` 접미사 절대 금지
일부 Discord webhook 문서가 "GitHub-compatible webhook"용으로 `/github` 접미사를 안내하지만, **이 액션은 raw webhook을 호출**하므로 절대 붙이지 말 것. URL 끝이 `.../webhooks/<id>/<token>`까지여야 함. README FAQ의 1순위.

### 4-2. `if: always()` 누락 = failure 알림 안 옴
GitHub Actions의 default는 "이전 step success 시에만 실행". `if: always()` 없으면 apply가 fail한 경우 알림 step 자체가 skip됨 → failure 알림이 절대 안 옴. 거의 모든 케이스에 필수.

### 4-3. webhook URL은 항상 secret
URL 자체가 인증 토큰. 노출되면 누구나 그 채널에 메시지 보낼 수 있음. 코드/log에 평문 박지 말 것.
- `gh secret set ...` 으로 등록 (interactive prompt 또는 stdin 모드 — `--body 'URL'` 인자는 history 노출 위험. [Phase 2 §4-5 노출 사고](04-phase2-deployment-infra.md))
- 노출되면 즉시 Discord에서 webhook 재생성

### 4-4. `nofail` default = `true` (알림 실패가 job 결과를 안 바꿈)
알림이 silently 실패해도 workflow는 success. 알림이 일관성에 critical하면 `nofail: false`. 단 알림 실패로 workflow 전체가 빨갛게 되는 게 부담스러우면 default 유지.

### 4-5. 여러 webhook (newline-separated)
secret 값에 webhook URL을 줄바꿈으로 여러 개 넣으면 모두에게 보냄. 일부 실패해도 나머지는 계속. 동일 알림을 두 채널/팀에 broadcast할 때 유용.

### 4-6. embed의 mention은 ping 안 됨
"실패 시 본인 ping"이 필요하면 `description`이나 `title`이 아니라 **`content` (embed 밖)**에 mention 박아야 함:
```yaml
content: "Hey <@316911818725392384>"
```

### 4-7. SLSA provenance / supply chain
액션 자체가 SLSA provenance를 발급해서 `action.yml` / `lib/index.js`의 무결성을 GitHub CLI로 검증 가능. lab에서 `@v1` 정도면 충분, prod라면:
```yaml
uses: sarisia/actions-status-discord@<full-sha>
```
full commit SHA pin이 supply chain 가드.

---

## 5. 환경 변수

| 변수 | 용도 | 비고 |
|---|---|---|
| `DISCORD_WEBHOOK` | `webhook` input 안 줄 때 fallback | env로 박으면 같은 워크플로우의 여러 step이 공유 |

---

## 6. 호환성

| 플랫폼 | 상태 |
|---|---|
| GitHub-hosted runners (ubuntu/macOS/windows) | ✅ 공식 |
| Ubuntu ARM | ✅ |
| macOS Apple Silicon | ✅ |
| GHES | 실험적 (untested) |
| Gitea, Forgejo | 실험적 (untested) |
| Guilded | ✅ — Guilded가 Discord Webhook API 호환 |

---

## 7. 버전 pinning 전략

| 패턴 | 안정성 | 자동 보안 패치 | 용도 |
|---|---|---|---|
| `@v1` (major) | 중 | ✅ — patch/minor 자동 적용 | lab, 빠른 셋업 |
| `@v1.16.0` (semver) | 상 | ❌ | minor까지 정확 고정 |
| `@<full-sha>` (commit) | 최상 (supply chain 가드) | ❌ | prod 권장 |

prod로 옮길 때 `@v1` → `@<sha>`로 격상. 해당 SHA는 GitHub release 또는 commit 페이지에서 복사.
