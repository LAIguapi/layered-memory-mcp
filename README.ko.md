# Layered Memory MCP Server

> 공통 코어 기반 지식 프레임워크로 AI 에이전트의 메모리를 토큰 제한 너머로 확장합니다.

[**English**](README.md) | [**中文**](README.zh-CN.md) | [**日本語**](README.ja.md)

[![PyPI version](https://img.shields.io/pypi/v/layered-memory-mcp.svg)](https://pypi.org/project/layered-memory-mcp/)
[![MCP Compatible](https://img.shields.io/badge/MCP-Compatible-blue)](https://modelcontextprotocol.io)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-green)](https://python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## 문제점

AI 에이전트는 **제한된 메모리**를 가집니다. 일반적으로 매 턴마다 주입되는 영구 컨텍스트는 2-4KB에 불과합니다. 공간이 가득 차면 나머지는 모두 잊어버립니다. 프로젝트 설정, 사용자 기본 설정, API 규약, 도메인 지식 등을 저장하려면 끊임없이 공간 제한과 싸워야 합니다.

## 해결책

**Layered Memory**는 경계가 분명한 **공통 코어**만 소유하고, 나머지는 원래 주인에게 그대로 둡니다:

```
원본 세션 / 사용자 입력 / 도구 결과
        │
        ▼
    영속화 분류기
        │
        ├─ 프레임워크 소유 공통 코어 (프레임워크가 기록)
        │    ├─ L0 색인        2-4KB, 매 턴 주입: 어떤 지식이 어디에 있는지
        │    ├─ L1 지식        지속되는 도메인 인지: 팩트, 결정, 현재 상태
        │    ├─ TODO          미완료 상태: 대기, 차단, 검증 필요
        │    └─ 메모리 체인지로그  L0/L1 의미 변경의 출처 기록
        │
        ├─ 에이전트 소유 확장면 (분류·제안만, 기본적으로 기록하지 않음)
        │    ├─ 스킬           작업 절차, 명령 시퀀스, 런북
        │    ├─ 사용자 프로필   소수의 지속적인 사용자 선호
        │    └─ 기타           더 이상 분류하지 않는 에이전트 소유 콘텐츠
        │
        └─ 영속화하지 않음
             └─ 미결정 / 일회성 콘텐츠 (임시 출력, 원본 로그)
```

**L0은 목차, L1은 책장, TODO는 미처리 트레이입니다. 원본 세션은 일기장 — 프레임워크가 검색은 해도 결코 기록하지 않는 읽기 전용 증거입니다.**

스킬과 사용자 프로필은 끝까지 에이전트의 것입니다. 프레임워크는 그것을 인식해 구조화된 제안을 돌려줄 뿐이며, [명시적으로 활성화](#선택적-에이전트-확장-실행-계층)하지 않는 한 기록하지 않습니다.

L0 색인, 벡터 스토어, 검색 색인은 모두 L1 마크다운에서 **파생된** 데이터이며 언제든 재구축할 수 있습니다.

## 주요 기능

- **스마트 지식 주입** — 한 번 쓰면 즉시 반영: 중복 제거(dedup), 섹션 타겟팅, 자동 L0 색인 동기화 (upsert/append/merge 모드)
- **영속화 라우팅** — 결정론적 분류기(`classify_memory_item`)가 콘텐츠 하나를 어느 메모리 면에 둘지 판정합니다: L1 지식, TODO 스토어, 당신 자신의 스킬/프로필 면, 또는 아무것도 보존하지 않음. 마커 + 구조 규칙만 사용하며 모델 호출도 임베딩 추론도 없습니다. 기본값은 제안 전용이라, 쓰기 결과에 주석을 덧붙일 뿐 동작을 바꾸지 않습니다
- **역량 반영(capability reflection)** — `get_memory_surface_capabilities`가 현재 설정에서 어느 면을 프레임워크가 대신 기록할 수 있고 어느 면은 제안만 할 수 있는지 그대로 보고합니다
- **선택적 에이전트 액션 실행** — `apply_agent_action`은 분류된 스킬／프로필／에이전트 소유 콘텐츠를 대신 기록할 수 있지만, 어디까지나 "명시적으로 설정된 조정자"로서만 동작합니다: 기본은 비활성, 면별 스위치, 확인 모드, `dry_run`, 그리고 명시적인 대상 경로가 필요합니다. 모든 거부는 보고되며 결코 조용히 넘어가지 않습니다
- **하이브리드 검색(RRF)** — 키워드, 퍼지, BM25, 시맨틱 검색을 Reciprocal Rank Fusion으로 결합해 안정적으로 순위를 매깁니다. 질의 유형 자동 감지로 시맨틱／키워드 비중을 조정합니다
- **에이전트 독립적 L0 접근** — `get_l0_index` 도구로 어떤 MCP 에이전트든 메모리 색인을 가져올 수 있습니다
- **멀티 에이전트 네임스페이스** — `LAYERED_MEMORY_NAMESPACE`로 에이전트별 지식을 분리하면서 공통 지식은 공유합니다
- **세션 스캔** — 최근 에이전트 세션에서 지식 후보를 추출합니다
- **건강 검증** — L0↔L1 일관성, 파일 구조, 지식 품질을 확인하고 부패(rot) 및 거버넌스 요약을 덧붙입니다
- **라이프사이클 거버넌스** — 지속되는 지식에 섞여 들어간 과정 상태(TODO / FIXME / next step / pending / 待办 / 보류 …)를 찾아내고 구조화된 권장 액션을 제시합니다: `move_to_todo`, `rewrite_as_current_rule`, `keep_with_reason`. 마커 어휘는 설정 가능합니다
- **정리 계획** — 읽기 전용 플래너가 발견 항목을 "안전하게 자동 수정 가능", "사용자 판단 필요", "자동 수정 금지" 세 가지로 분류합니다. 별도 도구가 안전한 것만 실행하며 기본값은 dry-run입니다
- **쓰기 안전성** — 파일 전체를 다시 쓸 때의 백업은 지식 디렉터리 밖(`<LAYERED_MEMORY_HOME>/data/backups/knowledge/`)에 보관되므로, 활성 지식 디렉터리에는 활성 파일만 남습니다
- **더 깔끔한 벡터 회상** — 임베딩되는 제목에서는 날짜·버전·이슈 번호 같은 노이즈를 제거합니다. 표시용 제목과 메타데이터는 원문 그대로 유지됩니다
- **설정 가능한 잡동사니 파일 스캔** — 유지보수／감사 스캔이 glob 패턴으로 "잡동사니" 계열 파일(`misc`, `notes`, `inbox`, `scratch`, `杂项` …)을 찾으므로 `misc.md`라는 파일이 반드시 있어야 하지는 않습니다
- **공간 분석** — 메모리 사용량을 모니터링하고 최적화 제안을 받습니다
- **에이전트 독립적** — MCP 호환 에이전트(Hermes, Claude, Cursor 등)와 함께 사용할 수 있습니다
- **제로 의존성** — 핵심 엔진은 Python 표준 라이브러리만 사용하며, MCP 전송을 위한 `fastmcp`만 필요합니다
- **프라이버시 우선** — 모든 데이터는 로컬에 유지되며 외부 API 호출이 없습니다

## 빠른 시작

### 설치

```bash
pip install layered-memory-mcp
```

### Hermes Agent

`~/.hermes/config.yaml`에 추가:

```yaml
mcp_servers:
  layered-memory:
    command: layered-memory-mcp
    timeout: 30
```

### OpenClaw

MCP 서버를 설치한 후 등록합니다:

```bash
pip install layered-memory-mcp

# MCP 서버로 등록
openclaw mcp set layered-memory --command layered-memory-mcp
```

Layered Memory는 OpenClaw의 내장 벡터 기반 메모리를 보완합니다:
- **OpenClaw 메모리**: 세션 기록에 대한 시맨틱 검색 (무거움, 임베딩 필요)
- **Layered Memory**: 큐레이션된 지식 파일에 대한 구조화된 키워드 검색 (가벼움, 즉시 응답)
- 둘 다 사용하세요: "X에 대해 내가 뭐라고 했지?"는 OpenClaw로, "데이터베이스 연결 문자열이 뭐였지?"는 Layered Memory로 해결

### Claude Desktop

Claude Desktop MCP 설정에 추가:

```json
{
  "mcpServers": {
    "layered-memory": {
      "command": "layered-memory-mcp"
    }
  }
}
```

### Cursor / 기타 MCP 클라이언트

```bash
# stdio 모드 (기본값)
layered-memory-mcp

# HTTP 모드
layered-memory-mcp --transport http --port 8080

# 상세 로깅
layered-memory-mcp --verbose
```

### 환경 변수

| 변수 | 설명 | 기본값 |
|------|------|--------|
| `LAYERED_MEMORY_HOME` | 메모리 데이터의 루트 디렉토리 | `~/.layered-memory/` |
| `LAYERED_MEMORY_SESSIONS_DIR` | 에이전트 세션 디렉토리 (자동 감지) | `~/.hermes/sessions/` |
| `LAYERED_MEMORY_AUTO_SYNC_L0` | 쓰기 후 L0 색인 자동 동기화 | `true` |
| `LAYERED_MEMORY_DEDUP_THRESHOLD` | 중복 제거 유사도 임계값 (0.0-1.0) | `0.7` |
| `LAYERED_MEMORY_L0_FORMAT` | L0 색인 형식: `hermes` 또는 `generic` | `hermes` |
| `LAYERED_MEMORY_NAMESPACE` | 멀티 에이전트 격리용 네임스페이스 | `shared` |
| `LAYERED_MEMORY_PROMOTION_WATCH_PATTERNS` | 유지보수 스캔에 쓰이는 잡동사니 파일 glob 패턴 (쉼표 구분) | 내장 영어 + 중국어 목록 |
| `LAYERED_MEMORY_ROUTING_ENABLED` | 콘텐츠를 분류하고 쓰기 결과에 라우팅 정보를 주석으로 덧붙임 | `true` |
| `LAYERED_MEMORY_ROUTING_MODE` | `advisory`(제안) 또는 `enforce`(강제) — `compact_memory`에만 작용 | `advisory` |
| `LAYERED_MEMORY_ROUTING_ENFORCE_DIRECT_WRITES` | 명시적 `inject_knowledge`가 비-L1 콘텐츠를 쓰려 하면 거부 | `false` |
| `LAYERED_MEMORY_AGENT_EXTENSIONS_ENABLED` | 에이전트 소유 면에 대한 대리 기록을 허용 | `false` |
| `LAYERED_MEMORY_AGENT_EXTENSIONS_MODE` | `suggest` / `apply_with_confirmation` / `apply` | `suggest` |
| `LAYERED_MEMORY_AGENT_EXTENSIONS_ROOT` | 파일을 쓰는 확장 핸들러의 기준 디렉터리 | 미설정 (거부됨) |
| `LAYERED_MEMORY_AGENT_MEMORY_PATH` | 에이전트 자신의 메모리 파일 (명시적 쓰기 대상) | 자동 감지 |
| `LAYERED_MEMORY_AGENT_MEMORY_ALLOW_AUTO_DETECT_WRITE` | 자동 감지된 메모리 파일로의 L0 투사를 허용 | `false` |
| `LAYERED_MEMORY_CHANGELOG_ENABLED` | L0/L1의 의미 변경을 메모리 체인지로그에 기록 | `true` |
| `LAYERED_MEMORY_CHANGELOG_PATH` | 체인지로그 파일 위치 | `<home>/data/memory_changelog.jsonl` |

### config.yaml

대부분의 동작은 `<LAYERED_MEMORY_HOME>/config.yaml`에서도 설정할 수 있습니다. 우선순위는 생성자 인수 → 환경 변수 → config.yaml → 내장 기본값입니다. 지식 거버넌스와 라우팅에 관련된 섹션:

```yaml
governance:
  # 아래 목록은 모두 내장 영어·중국어 기본값에 "추가"됩니다
  open_markers: ["WIP", "follow-up"]          # 미완료 과정 상태 → move_to_todo
  resolved_markers: ["shipped"]                # 해결된 이력 → rewrite_as_current_rule
  keep_reason_labels: ["보존 이유"]            # "<라벨>: <이유>" 행이 있으면 해당 섹션의 마커를 유지
  disabled_markers: ["TBD"]                    # 당신에게 노이즈인 기본 마커 끄기

promotion:
  watch_domains: ["misc"]                      # 정확히 일치하는 잡동사니 파일명 (쓰기 시에도 검사)
  watch_patterns: ["misc", "misc-*", "notes", "inbox", "scratch", "杂项"]  # 대소문자 구분 없는 glob, 유지보수／감사 스캔 전용

routing:
  enabled: true
  mode: advisory                               # advisory | enforce — compact_memory만 통제 (아래 참조)
  enforce_direct_writes: false                 # true = inject_knowledge가 비-L1 콘텐츠를 거부
  # 아래 목록도 governance와 같은 방식으로 내장 어휘에 "추가"됩니다:
  extra_skill_markers: ["운영 절차서"]
  extra_todo_markers: ["승인 대기"]
  extra_profile_markers: ["이렇게 불러"]
  extra_knowledge_markers: ["서비스 계약"]
  disabled_markers: ["스크립트"]               # 당신에게 노이즈인 기본 단어 끄기

agent_memory:
  adapter: auto                                # 어느 에이전트 제품의 파일 배치를 가정할지 (아래 참조)
  path: null                                   # 에이전트 자신의 메모리 파일 (명시적 쓰기 대상)
  allow_auto_detect_write: false               # 또는 자동 감지된 파일로의 투사를 명시적으로 허용
  separator: null                              # 어댑터가 제시하는 항목 구분자를 덮어쓰기

memory_changelog:
  enabled: true                                # 전체 스위치. 끄면 아무것도 기록하지 않음
  path: null                                   # 기본값 <home>/data/memory_changelog.jsonl
  retention_days: null                         # 보고만 하고 **실행하지 않음** — 로그를 지우는 로직은 없음
```

`mode: advisory`가 기본값입니다: 라우팅은 평소 결과 옆에 자신의 판단을 덧붙일 뿐, 쓰기 동작은 바꾸지 않습니다. 업그레이드 후 갑자기 쓰기를 거부하기 시작하는 것은 기능이 아니라 데이터 손실 사고이므로 `enforce`는 반드시 명시적으로 켜야 합니다. 그리고 이를 근거로 쓰기를 막는 경로는 단 하나, `compact_memory`뿐입니다([압축 경로의 라우팅](#압축-경로의-라우팅) 참조). 거기가 **프레임워크 자신**이 대상을 고르는 경로이기 때문입니다. `inject_knowledge`는 두 모드 모두에서 평소처럼 L1에 쓰고, 그 사실을 `routing_warning`에 밝힙니다.

`enforce_direct_writes`는 두 번째이자 서로 독립적인 스위치이며, 명시적인 쓰기를 거부할 수 있는 유일한 스위치입니다([엄격한 직접 쓰기](#엄격한-직접-쓰기선택-활성화) 참조). `mode: enforce`로 설정해도 이것이 함께 켜지지 않고, 이것을 켜도 압축이 무엇을 이전하는지는 바뀌지 않습니다. `routing.enabled: false`라면 공회전합니다 — 분류가 없으면 강제할 것도 없습니다.

어떤 키를 쓰지 않으면 기본값이 유지됩니다. `watch_patterns`를 설정하면 당신의 목록이 내장 패턴 목록을 대체합니다. `[]`로 두면 패턴 매칭을 끌 수 있습니다.

`agent_memory:`는 프레임워크가 L0 포인터／지식 색인 항목을 어느 「에이전트 자신의」 메모리 파일(`MEMORY.md`, `CLAUDE.md` 등)에 투사할 수 있는지를 결정합니다. 경로를 설정하지 않으면 프레임워크가 자동으로 탐지합니다 — 읽기에는 문제없지만 쓰기에는 위험합니다: 모호한 설정(테스트, 임시 home 위에서 돌아가는 일회성 스크립트)이 실제 파일을 해석해 조용히 덧붙일 수 있습니다. 그래서 투사 쓰기에는 다음 중 하나가 필요합니다: 명시적 대상(`agent_memory.path`, 또는 호출 시 `memory_path` 인수), 명시적 허용(`allow_auto_detect_write: true`), 탐지된 파일이 `<LAYERED_MEMORY_HOME>` 안에 있는 경우, 또는 그 home이 임시／테스트 샌드박스가 아니라 이번 설치의 실제 설정 home인 경우. 그 외에는 모두 거부되고 `agent_memory_write: {status: "skipped", reason: "unsafe_auto_detect_write", detected_path, hint}`로 보고됩니다 — L1 쓰기 자체는 언제나 정상적으로 성공하고, 멈추는 것은 투사뿐입니다. **탐지할 수 있다는 것이 권한은 아닙니다**: 어댑터를 통해 경로를 해석했다고 해서 그것을 쓸 권리가 생기지는 않습니다.

### 공통 코어는 에이전트와 무관하다

코어가 소유하는 것은 L0, L1, TODO, `MEMORY_CHANGELOG`의 의미뿐이고 그 외에는 아무것도 소유하지 않습니다. 호스트 에이전트가 자기 메모리 파일을 어디에 두는지, 항목을 어떤 문자로 구분하는지, 실시간 세션 DB가 어디 있는지, 몇 글자까지 허용되는지 — 이것들은 모두 제품별 사실이며 `src/layered_memory_mcp/adapters/` 아래의 **환경 어댑터**가 답합니다. 과거에는 `config.py`, `memory_compactor.py`, `agent_integrator.py`에 박혀 있던 리터럴이지만 지금은 그렇지 않습니다.

```yaml
agent_memory:
  adapter: auto     # 기본값
```

| 값 | 해석 결과 |
| --- | --- |
| `auto` (기본값) | 레지스트리를 순서대로 탐색 — `hermes`, `claude_code`, `cursor`, `cline`, `generic` — 어댑터 추출 이전에 코어가 하드코딩했던 후보 체인과 완전히 동일 |
| `hermes` | `~/.hermes/memories/MEMORY.md`, `§` 구분자, `~/.hermes/state.db`, `~/.hermes/sessions`, 그리고 `~/.hermes/config.yaml`에서 읽어온 실제 상한 |
| `claude_code` | `~/.claude/CLAUDE.md` |
| `cursor` | `./.cursorrules` |
| `cline` | `~/.cline/rules` |
| `generic` | 제품별 경로를 전혀 다루지 않음 — 프레임워크 자신의 `<home>/agent-memory.md`만 |
| `none` / `off` | `generic`의 별칭 |

**하위 호환을 위해 Hermes가 기본 어댑터**이며 가장 먼저 탐색되므로, 기존 설치는 예전과 똑같은 경로를 해석합니다. 알 수 없는 id는 경고와 함께 `generic`으로 폴백합니다 — 조용히 Hermes로 떨어지는 일은 없습니다. 환경 변수로는 `LAYERED_MEMORY_AGENT_ADAPTER=hermes`.

두 번째 에이전트를 지원하려면 어댑터를 등록하면 되고, 라우터·압축기·설정 계층을 고칠 필요가 없습니다:

```python
from layered_memory_mcp.adapters.environment import EnvironmentAdapterBase, environments

class AcmeEnvironment(EnvironmentAdapterBase):
    id = "acme"
    display_name = "ACME Agent"
    memory_relpath = (".acme", "brain.md")      # 사용자 home 기준 상대 경로
    install_marker_relpath = (".acme",)
    state_db_relpath = (".acme", "sessions.sqlite")
    separator = "---"
    path_tokens = (".acme", "brain.md")         # 명시적으로 설정된 경로를 식별하는 데 사용

environments.register(AcmeEnvironment(), priority=15)
```

`get_memory_surface_capabilities()`는 결과를 `agent_environment: {configured, resolved, known, entry_separator, …}` 아래에 보고합니다. 이는 아래의 `adapter`와는 다른 것입니다 — 후자가 가리키는 것은 선택적인 확장 **액션** 핸들러입니다.

Hermes 전용 구현 두 가지는 의도적으로 어댑터 밖, 각자 독립된 지원 패키지에 남겨 두었습니다: `write_guard` 플러그인(페이로드, 설치기, `hermes` CLI 탐색)과 `dashboard_plugin`입니다. `integrate_agent`의 Hermes 한정 액션(`inject_soul`, `install_dashboard`, `install_guard`)은 다른 호스트에서는 이유를 밝히며 거부합니다.

### 선택적 에이전트 확장 실행 계층

스킬, 사용자 프로필, 그 밖의 에이전트 소유 콘텐츠는 **기본적으로 제안만** 합니다: 프레임워크는 분류를 마치면 당신의 원문과 구조 힌트(`shape_hint`)가 담긴 `AgentAction`을 돌려주고, 스스로는 아무것도 쓰지 않습니다. 프레임워크가 대신 쓰게 하려면 명시적으로 켜야 합니다:

```yaml
agent_extensions:
  enabled: false                 # 전체 스위치 (기본 비활성)
  mode: suggest                  # suggest | apply_with_confirmation | apply
  adapter: null                  # 통합 라벨, 예: generic_file / custom
  root: null                     # 선택: 상대 경로의 기준 디렉터리
  surfaces:
    skill:
      enabled: false
      handler: null              # 등록된 어댑터의 이름
      path: null                 # 파일을 쓰는 핸들러의 명시적 대상
    user_profile:
      enabled: false
      handler: null
      path: null
    agent_other:
      enabled: false
      handler: null
      path: null
```

프레임워크가 어떤 면에 대신 쓰려면 다섯 조건이 동시에 성립해야 합니다: 최상위 `enabled: true`, 해당 면의 `enabled: true`, **실제로 등록된** 어댑터를 가리키는 `handler`, `suggest`가 아닌 `mode`, 그리고 그 핸들러가 안전한 대상을 해석할 수 있을 것. 하나라도 빠지거나 설정이 잘못되면 키 단위로 "분류하고 제안만 한다"로 되돌아갑니다. **켜도 소유권은 옮겨가지 않습니다**: `get_memory_surface_capabilities`는 이 면들을 계속 `owned_by: agent`, `default_behavior: suggest`로 보고하고 `can_apply`만 바꿉니다 — 프레임워크는 거기서 조정자이고 결코 소유자가 아닙니다.

`can_apply`가 보고하는 것은 스위치 상태가 아니라 *실제* 준비 상태입니다: 이름은 설정했지만 등록되지 않은 핸들러는 `handler_registered: false`를 보고하고, `apply_blockers`에는 `apply_agent_action`이 거부할 때와 같은 이유 코드(`extensions_disabled`, `surface_disabled`, `mode_suggest`, `no_handler`, `handler_not_registered`, `no_configured_path` 등)가 담깁니다. `registered_handlers`는 설정할 수 있는 핸들러 이름을 나열합니다.

#### `apply_agent_action` — 유일하게 쓸 수 있는 도구, 기본은 비활성

```
apply_agent_action(
  surface="skill", operation="create", title="릴리스 절차",
  source_content="<당신의 원문>", rationale="재사용 가능한 작업 절차",
  confirmed=false, dry_run=false,
  action=null,            # AgentAction의 JSON 형태. 주면 위의 평면 인수를 덮어씀
)
```

반환값은 `{applied, status, surface, operation, handler, adapter, mode, dry_run, confirmed, target, blockers, detail, error, metadata}`입니다. `status`가 가질 수 있는 값:

| status | 썼는가 | 의미 |
| --- | --- | --- |
| `disabled` | 아니오 | `agent_extensions`(또는 해당 면)가 꺼져 있거나 `mode: suggest` — 제안만 |
| `unsupported` | 아니오 | handler 미설정, 미등록, 또는 그 면을 지원하지 않음 |
| `refused` | 아니오 | 확장면이 아니거나, 핸들러가 거부(명시적 경로 없음, 대상이 안전하지 않음 등) |
| `invalid_action` | 아니오 | `kind` / `surface` / `operation` / `source_content` 검증 실패 — 절대 자동 보정하지 않음 |
| `confirmation_required` | 아니오 | `mode: apply_with_confirmation`(또는 액션 자체가 확인을 요구)인데 `confirmed`가 false |
| `would_apply` | 아니오 | `dry_run: true` — 대상 경로, 바이트 수, 제목을 보고 |
| `applied` | **예** | 핸들러가 기록함 |
| `error` | 아니오 | 핸들러가 예외를 던짐. 예외 정보는 `error`에 |

세 가지 모드: `suggest`(기본)는 분류해서 `AgentAction`을 넘겨줄 뿐이고, `apply_with_confirmation`은 대상을 해석해 보고하지만 `confirmed=true`를 붙인 두 번째 호출이 필요하며, `apply`는 첫 호출에서 바로 쓰되 액션 자신의 `requires_user_confirmation`이 참이면 여전히 확인을 요구합니다.

에이전트 소유 액션을 실행해도 `MEMORY_CHANGELOG`에는 **기록되지 않습니다**. 그 로그는 프레임워크 소유 L0/L1 변경의 출처 기록이며, 여기서의 쓰기는 에이전트의 것입니다.

#### `generic_file` 참조 핸들러

패키지에는 이미 등록된 핸들러 하나가 함께 들어 있지만, "등록됨"은 "권한 있음"이 아닙니다 — 위의 사슬이 모두 갖춰지고 대상 경로가 안전하게 해석되기 전까지는 아무것도 쓰지 않습니다:

```yaml
agent_extensions:
  enabled: true
  mode: apply_with_confirmation
  adapter: generic_file
  surfaces:
    skill:
      enabled: true
      handler: generic_file
      path: /srv/agent/extensions/skills.md
```

이 핸들러는 액션마다 길이 상한이 있는 마크다운 `##` 섹션 하나를 덧붙입니다: 당신의 `source_content` 위에 짧은 메타 정보 블록(면, 작업, 핸들러, 타임스탬프, rationale, `shape_hint`, 액션 metadata)을 두고, 기계가 찾기 쉽도록 `<!-- layered-memory:agent-action -->` 마커로 시작합니다. `create`, `append`, `update`는 **모두 덧붙이기**입니다. `update`는 무엇을 `supersedes`(대체)했는지만 기록하고 모호한 제자리 편집은 하지 않습니다 — 프레임워크가 소유하지 않은 파일을 고쳐 쓰는 것은 콘텐츠를 망칠 수 있는 유일한 동작이기 때문입니다. 8000자를 넘는 콘텐츠는 명시적인 마커와 함께 잘립니다.

경로 규칙은 모두 추측이 아니라 분명한 거부입니다:

| 상황 | 결과 |
| --- | --- |
| `path`도 `root`도 없음 | 거부, `no_configured_path` — 기본 위치는 의도적으로 두지 않음 |
| `root`만 있음 | `<root>/<surface>.md` |
| 상대 `path` | `root` 기준으로 해석, 없으면 `LAYERED_MEMORY_HOME` 기준. 그 안에 머물러야 함(`path_escapes_base`) |
| 절대 `path` | 수용 — 파일을 지명하는 것 자체가 이 관문이 요구하는 "명시성" |
| 디렉터리(이미 존재하거나 `/`로 끝남) | `<dir>/<surface>.md` |
| `/etc`, `/usr`, `/sys`, `/var/lib`, `C:\Windows` 등의 아래 | 거부, `unsafe_system_target` |
| `.py` / `.yaml` / `.json` / `.db` / `.env` 또는 숨김 파일 | 거부, `unsafe_file_type` — 마크다운을 덧붙이면 파일이 깨짐 |

상위 디렉터리는 당신이 명시적으로 설정한 경로에 대해서만 생성됩니다. 이 경로에는 명령 어댑터도 없고 어떤 shell 실행도 없습니다. Hermes 네이티브 스킬／프로필 파일에 쓰는 것은 **구현되지 않았으며** 여전히 범위 밖입니다.

커스텀 핸들러는 `name`, `supports(surface)`, 선택적 `readiness(surface, config)`, `apply(action, config=None, *, dry_run=False)`를 가진 클래스이며 `layered_memory_mcp.adapters.registry.register(...)`로 등록합니다. `dry_run` 인수를 선언하지 않은 어댑터는 `dry_run=True` 상태에서 절대 호출되지 않습니다 — 프레임워크는 하지 않겠다고 약속한 쓰기를 감행하는 대신 거부합니다.

## 사용법

### 1. 지식 쓰기 (권장)

`inject_knowledge` 도구는 모든 에이전트의 **기본 쓰기 경로(primary write path)**입니다. 단일 호출로 중복 제거, 섹션 타겟팅, 자동 L0 색인 동기화를 처리합니다.

```
Agent 학습: "프로덕션 DB는 prod-db:5432의 PostgreSQL 15"
→ inject_knowledge(
    domain="infrastructure",
    section="Database",
    content="PostgreSQL 15 on prod-db:5432, connection pool: 20 max",
    mode="upsert"
  )
← infrastructure.md 생성/업데이트, L0 색인 자동 동기화
```

**쓰기 모드:**
| 모드 | 동작 |
|------|------|
| `upsert` (기본값) | 유사한 콘텐츠가 있으면 교체, 새로운 내용이면 추가 |
| `append` | 항상 추가, 중복 검사 건너뜀 |
| `merge` | 기존 내용과 새 내용의 고유한 부분을 병합 |

### 2. 지식 읽기

```
Agent: "데이터베이스 연결 문자열이 뭐야?"
→ recall_knowledge(keyword="database")
← infrastructure.md에서 관련 섹션 반환
```

### 3. 건강 검증

```
→ validate_knowledge()
← L0↔L1 일관성, 고아 파일, 오래된 항목, 파일 건강 상태를 확인하고
  rot_summary / rot_health_score와 governance_summary
  (L1에서 발견된 상태 마커를 라이프사이클 액션별로 집계)를 덧붙임
```

### 4. 정리 계획과 실행

정리 흐름은 기계적 수정과 의미적 수정을 분리해서 다룹니다:

```
1. plan_knowledge_cleanup_tool(max_items=50)
   ← 읽기 전용 JSON, 세 갈래:
     safe_auto_fixes      남아 있는 *.md.bak을 knowledge/ 밖으로 이동, L0 재동기화,
                          벡터 스토어 재색인, 미완료 마커를 TODO로 복사
     needs_user_decision  해결된 이력 다시 쓰기, 같은 주제의 제목 묶음 병합, 과대 파일 분할…
     must_not_auto_fix    보존 이유 행과 함께 의도적으로 남겨 둔 마커

2. apply_safe_knowledge_cleanup_tool(actions=<safe_auto_fixes의 항목>)
   ← 기본은 dry-run: 무엇이 바뀔지만 보고하고 아무것도 쓰지 않음

3. apply_safe_knowledge_cleanup_tool(actions=..., dry_run=False)
   ← 네 가지 안전한 액션만 실행하고 그 외는 모두 거부
```

실행 도구는 L1 텍스트를 삭제하거나 다시 쓰지 않습니다. 마커를 TODO로 바꿀 때에도 원본 행은 그대로 남습니다. 병합, 재작성, 삭제, 도메인 간 이동은 사용자나 에이전트에게 맡기고 평소의 쓰기 도구로 수행합니다. 임베딩 기반 검사(파일 간 중복, 분할 후보)에는 `reconcile_knowledge_tool`을 사용하세요.

### 5. 세션 압축 (Cron Job)

대화에서 새로운 지식을 추출하기 위해 매일 cron을 설정합니다:

```
1. scan_recent_sessions → 세션 요약 가져오기
2. AI가 요약 분석 → 안정적인 팩트 식별
3. 새로운 팩트 → inject_knowledge로 기록 (L0 자동 동기화)
4. L0 색인 → 항상 최신 상태 유지
```

### 6. 레거시 CRUD (계속 사용 가능)

직접적인 파일 조작을 위한 도구:

| 도구 | 설명 |
|------|------|
| `create_knowledge_file` | 새 .md 파일 생성 (L0 자동 동기화) |
| `update_knowledge_file` | 기존 파일 덮어쓰기 (L0 자동 동기화) |
| `delete_knowledge_file` | 파일 삭제 (L0 자동 동기화) |

## MCP 도구

### 읽기 도구

| 도구 | 설명 |
|------|------|
| `recall_knowledge` | 키워드로 L1 지식 파일 검색 (관련성 점수 포함) |
| `get_knowledge_file` | 이름으로 특정 지식 파일 읽기 |
| `list_memory_stats` | 공간 통계, 파일 크기, 최적화 제안 확인 |
| `scan_recent_sessions` | 최근 세션에서 지식 추출 후보 스캔 |
| `search_sessions_by_keyword` | 키워드로 세션 기록 검색 |
| `get_l0_index` | 전체 L0 색인 가져오기 (에이전트 독립적) |
| `classify_memory_item` | 후보 콘텐츠를 어느 메모리 면에 둘지 분류 (`l1_knowledge` / `todo` / `skill` / `user_profile` / `agent_other` / `undecided` / `ephemeral`). 읽기 전용·결정론적·모델 호출 없음 |
| `get_memory_surface_capabilities` | 현재 설정에서 어느 면을 프레임워크가 기록할 수 있고 어느 면이 제안 전용인지 보고 |
| `get_memory_changelog` | L0/L1 의미 변경의 출처 기록 읽기 (domain / operation / surface / since로 필터. 최신순, 건수 상한 있음) |
| `get_memory_changelog_stats` | 체인지로그 집계: 작업／면／도메인별 건수, 기간, 설정 상태 |

### 쓰기 도구

| 도구 | 설명 |
|------|------|
| **`inject_knowledge`** | **기본 쓰기 경로** — 중복 제거, 섹션 타겟팅, 자동 L0 동기화를 통한 스마트 주입 |
| `create_knowledge_file` | 새 .md 파일 생성 (L0 자동 동기화) |
| `update_knowledge_file` | 기존 파일 덮어쓰기 (L0 자동 동기화) |
| `delete_knowledge_file` | 파일 삭제 (L0 자동 동기화) |
| `apply_agent_action` | 설정된 핸들러를 통해 에이전트 소유 확장면 액션(`skill` / `user_profile` / `agent_other`)을 실행. **기본은 비활성** — `agent_extensions`를 명시적으로 켜기 전까지는 `status: "disabled"`를 반환. `dry_run`과 `confirmed`를 지원하며 `MEMORY_CHANGELOG`에는 기록하지 않음 |

### 관리 도구

| 도구 | 설명 |
|------|------|
| `sync_l0_index` | L1 파일에서 L0 색인 수동 재구축 (`dry_run` 지원) |
| `validate_knowledge` | 건강 검증: L0↔L1 일관성, 파일 품질, 중복 항목, 그리고 rot 점수와 거버넌스(상태 마커) 요약 |
| `manage_l0_entry` | 개별 L0 항목 추가 / 제거 / 교체 |

### 거버넌스·정리 도구

| 도구 | 설명 |
|------|------|
| `audit_rot_tool` | 읽기 전용 부패 감사: 건강 점수(0-100), 과대 파일, 깨진 제목, 오래된 섹션, 파일 간 중복, 잡동사니 파일 승격 후보 |
| `reconcile_knowledge_tool` | 임베딩 기반 파일 간 중복·분할 후보 스캔 |
| `plan_knowledge_cleanup_tool` | 읽기 전용 정리 계획: `safe_auto_fixes` / `needs_user_decision` / `must_not_auto_fix` |
| `apply_safe_knowledge_cleanup_tool` | 계획 중 안전한 기계적 액션만 실행(`migrate_legacy_backups`, `sync_l0_index`, `reindex_vector_store`, `create_todo_from_marker`). 기본값은 `dry_run=True` |

이 도구들은 모두 추가된 것입니다. 기존 도구의 인수와 반환 필드는 그대로입니다. `validate_knowledge`와 쓰기 후 거버넌스 보고서는 필드만 늘어납니다 (예를 들어 발견 항목별 `action`).

## MCP 리소스

| 리소스 | 설명 |
|--------|------|
| `memory://status` | 전체 시스템 상태 및 설정 |
| `knowledge://files` | 메타데이터가 포함된 모든 지식 파일 목록 |

## MCP 프롬프트

| 프롬프트 | 설명 |
|----------|------|
| `knowledge_compression_prompt` | 세션에서 AI 기반 지식 추출을 위한 템플릿 |
| `cognitive_decision_prompt` | 체계적인 메모리 사용을 위한 의사결정 프레임워크 |

## 아키텍처 심층 분석

### 메모리 면 모델

| 메모리 면 | 소유자 | 비용 | 담는 것 |
|-----------|--------|------|---------|
| L0 색인 | 프레임워크 | 매 턴마다 토큰 소모 | ~2KB 포인터: 어떤 지식이 어디에 있는지 |
| L1 지식 | 프레임워크 | 파일 1회 읽기 | 지속되는 팩트, 결정, 경계, 현재 이해 |
| TODO | 프레임워크 | 조회 1회 | 미완료 상태: 대기, 차단, 검증 필요, 사용자 판단 대기 |
| 메모리 체인지로그 | 프레임워크 | — | L0/L1 의미 변경의 출처 기록 |
| 스킬 | **에이전트** | 스킬 1회 로드 | 재사용 가능한 절차, 명령 시퀀스, 체크리스트 |
| 사용자 프로필 | **에이전트** | 주입 | 소수의 지속적인 사용자 선호와 페르소나 |
| 기타 | **에이전트** | — | 스킬도 프로필도 아닌 에이전트 소유 콘텐츠 |
| 원본 세션 | **제공자** | 전체 검색 | 읽기 전용 증거: 검색만, 기록 없음 |

여기에는 핵심적인 구분이 두 가지 있습니다:

- **소유 ≠ 대리 기록 가능.** 소유는 고정되어 있어 L0/L1/TODO/체인지로그는 프레임워크, 스킬·프로필·기타는 에이전트, 원본 세션은 제공자의 것입니다. 프레임워크가 에이전트 소유 면에 **대신 기록할 수 있는지**는 별개의, 명시적으로 켜야 하는 문제입니다 — [선택적 에이전트 확장 실행 계층](#선택적-에이전트-확장-실행-계층)을 참조하세요.
- **진실의 원천 ≠ 파생 데이터.** L1 마크다운이 유일한 진실의 원천입니다. L0 색인, `data/vectors.db`, 검색 색인은 모두 거기서 파생되며 언제든 재구축할 수 있습니다(`sync_l0_index`, `reindex_vector_store_tool`).

### 메모리 체인지로그란 무엇인가

`MEMORY_CHANGELOG`는 **당신의 메모리**에 대한 변경 기록이며, 이 소프트웨어의 릴리스 노트가 아닙니다. 기록하는 것은 영속면의 의미적 변경뿐입니다: L1 섹션의 생성·갱신·병합·교체·삭제, 융합(fusion)과 통합(consolidation)의 되쓰기, 그리고 의미 지도가 실제로 바뀌었을 때의 L0 항목 추가·제거·이름 변경입니다.

이것은 분명히 저장소의 `CHANGELOG.md`가 아니고, 원본 세션 로그도 아니며, 범용 이벤트 로그도 아니고, TODO의 라이프사이클 이력도 아닙니다. 파생 데이터 재구축(벡터 재색인, 검색 색인)도 기록하지 않습니다 — 의미 내용이 바뀌지 않았기 때문입니다.

**저장 방식.** `<home>/data/memory_changelog.jsonl`의 추가 전용 JSONL이며 한 줄에 한 이벤트입니다. 기록은 최선 노력 방식이고 실패하면 조용히 넘어갑니다 — 출처 기록이 자신이 설명하는 쓰기를 실패시켜서는 안 되기 때문입니다. 프레임워크가 어떤 줄을 고쳐 쓰거나 지우는 일도 없습니다. `retention_days`는 설정에서 읽어 그대로 보고하지만 **실행되지는 않습니다**: 출처 기록을 지우는 것은 파괴적인 작업이고, 이력을 지우는 것은 언제나 사람의 결정입니다.

**이벤트 하나가 담는 것.** 구조만 담고 내용은 담지 않습니다: `schema_version`, `event_id`, `at`(UTC ISO), `operation`, `surface`, `domain`, `section`, `actor` / `agent_id`, `tool`, 구조화된 `summary`와 `reason`, 선택적 `source_ref`, 대상 본문의 `before_hash` / `after_hash`, 그리고 바이트 수와 쓰기 경로 세부를 담는 `metadata`. **본문 자체는 결코 담지 않습니다.** 내용의 진실의 원천은 지식 파일이며, 본문을 출처 로그로 복사하는 것은 사용자가 메모에 적어 둔 모든 비밀에 색인조차 되지 않는 두 번째 거처를 마련해 주는 셈입니다. `metadata`는 스칼라와 짧은 문자열만 받고, 중첩된 컨테이너는 직렬화하지 않고 버립니다.

**현재 기록되는 것.** `inject_knowledge`(따라서 `compact_memory`의 L1 쓰기와 `batch_inject_knowledge_tool`까지)의 생성／섹션 생성／갱신／병합／교체／융합 되쓰기／통합 되쓰기. `create_knowledge_file`, `update_knowledge_file`, `delete_knowledge_file`의 파일 전체 변경. `sync_l0_index`와 `manage_l0_entry_tool` 중 항목이 실제로 생기거나 사라지거나 바뀐 경우.

**기록되지 않는 것.** 융합／통합의 지연 핸드셰이크, `fuse_conflict`나 `consolidate_refused` 거부, 건너뛴 중복, 검증 실패, 쓰기 실패, `dry_run=True` 시험 실행, 항목 집합이 바뀌지 않은 L0 재동기화, 그리고 콘텐츠 해시가 바뀌지 않은 쓰기. 단일 섹션 삭제는 아직 이벤트가 없습니다 — 섹션 하나만 지우는 도구가 없기 때문입니다(`ROADMAP.md` 참조).

**읽는 방법.** `get_memory_changelog(domain, operation, surface, since, limit)`는 최신순·건수 상한(기본 50, 하드 상한 500)으로 반환합니다. `get_memory_changelog_stats()`는 작업／면／도메인별 건수와 기간을 집계합니다. 둘 다 읽기 전용입니다. `get_memory_surface_capabilities`는 `memory_changelog`에 대해 백엔드가 실제로 켜져 있는지를 `can_apply`에 반영하고, 해석된 `store_path`도 함께 보고합니다.

### Write-Once, Fully-Visible 파이프라인 (v0.5.0)

v0.5.0의 핵심 혁신은 **모든 쓰기 경로가 자동으로 L0 색인을 동기화**한다는 점입니다:

```
Agent가 inject_knowledge(domain="infra", section="Proxy", content="...") 호출
  │
  ├─ 1. 중복 검사 (SequenceMatcher, 임계값=0.7)
  ├─ 2. 액션 결정: upsert / append / merge / skip
  ├─ 3. 섹션 타겟팅 (## 제목을 찾거나 생성)
  ├─ 4. 파일 쓰기 (동시성 안전을 위한 fcntl.flock)
  └─ 5. 자동 L0 색인 동기화
        │
        ↓
  L0 색인 업데이트 → Agent가 다음 턴에서 확인 가능
```

이를 통해 에이전트가 L1 파일을 작성했지만 L0 색인(매 턴마다 주입됨)이 업데이트되지 않아, 이후 세션에서 새로운 지식이 무시되는 "쓰지만 보이지 않는" 문제를 해결합니다.

### 지속되는 지식 vs. 과정 상태

L1에 두는 것은 **지속되는 지식**, 즉 지금 성립하는 규칙과 팩트입니다. 미완료 작업("TODO: X 연결하기", "판단 대기")이나 "어떻게 고쳤는지"에 대한 서술은 **과정 상태**이고 금방 낡습니다. 쓰기 직후와 `validate_knowledge` 때, 거버넌스 모듈이 L1의 상태 마커를 훑고 각 마커 행을 분류합니다:

| 액션 | 의미 |
|------|------|
| `move_to_todo` | 미완료 작업. L1이 아니라 TODO 스토어(`add_todo` / `list_todos`)에 속합니다 |
| `rewrite_as_current_rule` | 해결된 이력("수정됨", "has been fixed"). 지금 성립하는 규칙으로 다시 씁니다 |
| `keep_with_reason` | 섹션에 명시적인 `<보존 이유 라벨>: <이유>` 행이 있어 마커가 의도적임이 드러난 경우 |

감지는 결정론적이며(정규식과 문자열 매칭만, 모델 호출 없음) 결과는 제안입니다. 마커 어휘는 데이터이고 config.yaml의 `governance:` 섹션에서 설정하므로, 다른 언어나 팀의 표기 방식을 추가하는 데 코드 변경이 필요하지 않습니다.

같은 검사는 **절차 혼입**도 보고합니다: 지속되는 지식이 아니라 작업 절차처럼 읽히는 섹션입니다. 마커**와** 절차적 구조가 모두 맞을 때만 표시됩니다(번호가 붙은 단계, 체크리스트, 명령 시퀀스, 코드 블록 안의 명령) — "검증"이나 "흐름"을 언급만 하는 아키텍처 설명은 지식이므로 그대로 둡니다. 표시된 섹션은 당신 자신의 스킬 면에 속합니다. 프레임워크는 보고만 하고 옮기지 않습니다.

### 쓰기 경로의 제안형 라우팅

`inject_knowledge`가 쓰는 곳은 L1입니다 — 그것이 계약이고, 라우팅은 기본적으로 그것을 바꾸지 않습니다. 라우팅이 더하는 것은 가시성입니다: 모든 결과에 `routing` 블록이 붙고, 분류기가 다른 면으로 보냈어야 한다고 본 쓰기에는 `routing_warning`도 붙습니다. 쓰기는 평소처럼 일어나고 `action`도 예전과 같습니다. 배치 쓰기도 항목별로 같은 `routing`을 노출하며, 배치 안의 `deferred_fusion`도 전체 핸드셰이크(`needs_fusion`, `expected_hash`, `fusion`)를 그대로 전달합니다 — 배치는 편의 수단일 뿐, 더 조용한 쓰기 통로가 아닙니다.

이것이 압축과의 사이에 있는 의도적인 비대칭입니다: `inject_knowledge`를 명시적으로 호출했다는 것은 호출자가 스스로 대상을 지정했다는 뜻이며, `mode: enforce`가 그 결정을 뒤집지는 않습니다. 더 엄격한 규칙을 원하는 설치는 다음 스위치를 따로 켭니다.

### 엄격한 직접 쓰기(선택 활성화)

`routing.enforce_direct_writes: true`는 위의 경고를 거부로 바꿉니다. 분류는 쓰기 **이전**에 돌아갑니다 — 중복 제거 전, 벡터 스토어 전, 무엇이든 디스크에 닿기 전입니다:

| `routing.enabled` | `enforce_direct_writes` | `routing_override` | 분류 결과 | 동작 |
|---|---|---|---|---|
| `true` | `false` (기본) | — | 아무것이든 | 평소처럼 기록. 비-L1이면 `routing_warning` 첨부 (동작 불변) |
| `true` | `true` | `"none"` (기본) | `l1_knowledge` / `l0_index` | 정상적으로 기록하고 `MEMORY_CHANGELOG` 이벤트도 남김 |
| `true` | `true` | `"none"` | 그 외 | **거부**, 아무것도 쓰지 않음 |
| `true` | `true` | `"force_l1"` | 그 외 | 기록하고 `routing_override_applied: true` 표시 |
| `false` | `true` | — | 분류하지 않음 | 평소처럼 기록, 결과는 옛 형태(routing 필드 없음) |

`routing.mode`는 끝까지 이와 직교합니다: `compact_memory`가 무엇을 이전하는지를 정할 뿐, 그 자체로 직접 쓰기를 거부하는 일은 없습니다.

거부 결과는 구조화되어 있고, 검증 오류가 아닙니다 — 요청 자체는 문제가 없고 문제는 **대상**입니다:

```json
{
  "success": false,
  "action": "routing_refused",
  "error": "routing_enforce_direct_write_refused",
  "message": "Content classified as 'skill' (owned by agent, score 0.88); direct L1 write to 'ops' / '## 发布流程' refused by routing.enforce_direct_writes. …",
  "written": false,
  "routing": {"surface": "skill", "ownership": "agent", "score": 0.88, "signals": ["…"]},
  "routing_warning": "…re-submit with routing_override='force_l1' if it really is L1 knowledge.",
  "suggested_action": {"kind": "agent_native_write", "surface": "skill", "shape_hint": ["…"]},
  "routing_enforcement": {"gate_active": true, "override_requested": "none", "override_applied": false},
  "hint": "Nothing was written: no L1 file, no section, no MEMORY_CHANGELOG event, and no agent-owned write."
}
```

거부되었을 때 아무것도 기록되지 않습니다: 파일도 섹션도, `MEMORY_CHANGELOG` 이벤트도(그 로그가 기록하는 것은 변경이고 거부는 변경이 아닙니다), 에이전트 소유 쓰기도 없습니다 — `suggested_action`은 **당신**이 실행하도록 돌려주며, 압축 경로와 같은 규율입니다. TODO 모양의 콘텐츠는 `todo_candidate`로 돌아오고, `add_todo` 호출은 여전히 당신의 판단입니다.

`inject_knowledge_tool(..., routing_override="force_l1")`(`batch_inject_knowledge_tool`에도 같은 이름의 인수)은 그래도 기록하고 결과에 `routing_override_applied: true`를 표시합니다. 분류기가 지속되는 지식을 잘못 본 경우에만 사용하세요 — 런북이나 사용자 선호, 로그를 L1에 밀어 넣는 데 쓰지 마세요. 그것들에는 각자의 면이 있고 거부 메시지가 어느 면인지 밝혀 줍니다. 알 수 없는 값은 허락이 아니라 무시로 처리됩니다. 두 스위치 모두 `get_memory_surface_capabilities`가 보고합니다(`routing_mode`, `routing_enforce_direct_writes`, 그리고 `l1_knowledge`에 대한 설명).

### 압축 경로의 라우팅

`compact_memory`는 라우팅을 인식합니다. 그 이전 규칙은 줄곧 구조적인 것("L0 포인터가 아니다" → L1)이었지만, 그것은 **출처**에 대한 판단이지 콘텐츠에 대한 분류가 아닙니다 — 그 결과 에이전트 메모리 파일에 놓인 런북, 지속적인 사용자 선호, 원본 로그가 모두 L1 지식이 되어 버렸습니다. 이제 색인이 아닌 모든 항목이 분류되고, 그 분류를 얼마나 인정할지는 모드가 결정합니다:

| 모드 | 동작 |
|------|------|
| `advisory` (기본) | 이전 동작은 그대로: 색인이 아닌 항목은 모두 L1로 갑니다. 보고서에 항목별 `routing`, 분류기가 다른 면으로 보냈어야 한다고 본 항목의 `routing_advisory`, 면과 소유자별로 집계한 `routing_summary`가 추가되므로, 무엇이 잘못 놓였는지 보고 나중에 옮길 수 있습니다 |
| `enforce` | `l1_knowledge`만 L1에 기록됩니다. TODO, 스킬, 사용자 프로필, 에이전트 소유, 미결정, 일회성 항목은 **에이전트 메모리에 그대로 남고** 당신이 둘 곳을 정할 후보로 반환됩니다 |

config.yaml의 `routing:` 섹션에서 설정하거나, 호출 때마다 `compact_memory(routing_mode="enforce")`로 넘깁니다 — `dry_run=True`와 함께 쓰면 설정을 바꾸기 전에 분리 결과를 미리 볼 수 있습니다. 인수를 생략하면 설정된 모드를 그대로 씁니다.

enforce 모드는 L1 밖으로는 아무것도 쓰지 않습니다. 막힌 항목은 보고될 뿐 결코 삭제되지 않습니다:

```json
{
  "routing_mode": "enforce",
  "routing_summary": {
    "by_surface": {"l1_knowledge": 1, "skill": 1, "todo": 1},
    "by_ownership": {"framework": 2, "agent": 1},
    "l1_injected": 1, "l1_planned": 1, "blocked_from_l1": 2
  },
  "l1_candidates": [{"surface": "l1_knowledge", "status": "migrated",
                     "destination": "knowledge/infra.md#架构边界"}],
  "todo_candidates": [{"surface": "todo", "status": "blocked_from_l1", "reason": "…"}],
  "agent_owned_candidates": [{"surface": "skill", "status": "blocked_from_l1",
                              "suggested_action": {"kind": "agent_native_write", "…": "…"}}],
  "rejected": []
}
```

각 후보에는 항목 미리보기, 라우팅된 면과 소유자, 규칙 가중치 점수, 이유, 경고, 상태, 그리고 실제로 기록된 경우의 대상 위치가 담깁니다 — 한 번의 압축을 사후에 복기할 수 있을 만큼입니다. 의도적인 경계가 두 가지 있습니다: TODO를 자동으로 만들지 않고(압축에는 TODO 쓰기 계약이 없으며 `todo_candidates`는 보고일 뿐, `add_todo`는 당신의 결정입니다), 스킬／사용자 프로필／에이전트 소유 콘텐츠는 여전히 에이전트의 것입니다 — `suggested_action`은 당신이 실행하도록 돌려주고, `agent_extensions` 어댑터가 설정되어 있어도 압축이 그것을 호출하는 일은 결코 없습니다.

기존 필드(`migrated_count`, `migrated`, `errors`, `cleaned_memory`, `stats`)의 의미는 두 모드 모두에서 동일하므로 기존 호출자는 바꿀 것이 없습니다.

### 파생 데이터: L0 색인과 벡터

L1 마크다운 파일이 유일한 진실의 원천입니다. L0 색인과 벡터 스토어(`data/vectors.db`)는 거기서 **파생**되며 언제든 재구축할 수 있습니다(`sync_l0_index`, `reindex_vector_store_tool`). 임베딩되는 텍스트는 노이즈를 제거한 제목을 사용합니다: `## Release process 2026-10-01 (v3.2)`는 `Release process`로 임베딩되므로 주기적인 항목이 날짜가 아니라 주제로 묶입니다. 표시용 제목과 저장된 메타데이터는 원문 그대로 유지됩니다. 업그레이드 후 재색인하면 기존 벡터도 더 깔끔한 제목을 쓰게 됩니다.

### 안전한 정리의 경계

자동 정리는 기계적이고 되돌릴 수 있거나 멱등인 작업으로만 제한됩니다: 남아 있는 `*.md.bak`을 `knowledge/`에서 `data/backups/knowledge/`로 옮기기, 파생된 L0와 벡터 데이터 재구축, 그리고 미완료 마커를 TODO 스토어로 *복사*하기(결정론적 id를 쓰므로 다시 실행해도 늘어나지 않습니다). 의미에 관한 수정은 모두 안전 경계 밖이며 언제나 사용자나 에이전트에게 맡깁니다: 팩트 삭제, 충돌하는 섹션 병합, 도메인 간 콘텐츠 이동, 이력을 규칙으로 다시 쓰기, TODO를 만든 뒤 원본 행 지우기. `apply_safe_knowledge_cleanup_tool`은 이런 액션을 이름으로 거부합니다.

### 관련성 점수

`recall_knowledge`를 호출하면 파일은 다음 기준으로 점수가 매겨집니다:

1. **파일명 일치** (+10점) — 키워드가 파일명에 포함됨
2. **제목 일치** (+3점) — 키워드가 `## 제목`에 포함됨
3. **본문 빈도** (출현당 +0.5점, 최대 5점까지) — 키워드가 본문에 출현하는 빈도

결과는 점수순으로 정렬되며, 전체 파일이 아닌 일치하는 `## 섹션`만 반환됩니다.

### 네임스페이스 격리 (v0.6.0)

`LAYERED_MEMORY_NAMESPACE`를 설정하면 에이전트별로 지식을 격리할 수 있습니다:

```
knowledge/
├── shared/           ← 공통 지식, 모든 에이전트에게 보임
│   ├── infrastructure.md
│   └── coding-standards.md
├── claude/           ← Claude Desktop 전용 지식
│   └── claude-specific.md
├── cursor/           ← Cursor IDE 전용 지식
│   └── cursor-config.md
└── hermes/           ← Hermes Agent 전용 지식
    └── hermes-setup.md
```

각 에이전트는 자기 네임스페이스를 먼저 보고, 그다음 `shared/`로 폴백합니다. 파일명이 충돌하면 네임스페이스가 우선합니다. 모든 읽기／검색／주입 도구가 두 디렉터리를 자동으로 병합합니다.

```bash
# Claude Desktop 설정
LAYERED_MEMORY_NAMESPACE=claude layered-memory-mcp

# 하위 호환: 기본값 "shared"는 격리 없음
layered-memory-mcp
```

### L0 색인 형식

두 가지 형식을 지원합니다:

| 형식 | 예시 | 적합한 용도 |
|------|------|------------|
| `hermes` | `[L0索引] infra: servers, DB → knowledge/infra.md` | Hermes Agent 메모리 주입 |
| `generic` | `[infra.md] Server Configuration → proxy, db, deploy` | 독립 사용 / 기타 에이전트 |

`LAYERED_MEMORY_L0_FORMAT` 환경 변수 또는 `l0_format` 생성자 인수를 통해 구성합니다.

### 세션 압축

`scan_recent_sessions` 도구는 cron 자동화를 위해 설계되었습니다:

1. 지난 N일간의 세션 파일을 스캔합니다
2. 사용자 메시지, 어시스턴트 주제, 도구 호출을 추출합니다
3. AI가 분석할 수 있는 구조화된 JSON을 반환합니다
4. AI가 안정적인 지식을 식별하여 `inject_knowledge`를 통해 L1 파일에 기록합니다

이를 통해 **자가 개선하는 메모리 시스템**이 만들어집니다 — 더 많은 지식이 대화에서 추출될수록 에이전트는 시간이 지남에 따라 더 똑똑해집니다.

## 에이전트 호환성

Layered Memory는 MCP 서버입니다 — MCP 호환 에이전트라면 모두 작동합니다.

| 에이전트 | 설정 방법 | 비고 |
|----------|-----------|------|
| **Hermes Agent** | `config.yaml` → `mcp_servers` | 네이티브 MCP 클라이언트, 메모리를 통한 L0 자동 주입 |
| **OpenClaw** | `openclaw mcp set` | 내장 벡터 메모리를 보완 |
| **Claude Desktop** | `claude_desktop_config.json` | 완전한 MCP 지원, 도구 호출을 통한 L0 |
| **Cursor** | Settings → MCP | 완전한 MCP 지원 |
| **Codex CLI** | Codex MCP config | 완전한 MCP 지원 |
| **모든 MCP 클라이언트** | stdio 또는 HTTP 전송 | 표준 MCP 프로토콜 |

### Layered Memory vs 내장 메모리, 언제 사용하나요?

대부분의 에이전트는 **제한된 영구 메모리**(매 턴 2-4KB)를 가집니다. Layered Memory는 다음을 통해 이 문제를 해결합니다:

1. **색인과 콘텐츠의 분리** — L0은 작게 유지되고(에이전트 메모리에 적합), L1은 무제한 지식을 보관합니다
2. **온디맨드 로딩** — 에이전트는 필요할 때 필요한 것만 읽습니다
3. **자가 개선** — 세션 압축이 시간이 지남에 따라 자동으로 새로운 지식을 추출합니다

### 통합 패턴

```
Agent (2KB 메모리 제한)
  └── L0 색인 (매 턴마다 주입, ~500 바이트)
        ├── [L0] infrastructure: servers, DB → knowledge/infrastructure.md
        ├── [L0] api: REST conventions → knowledge/api-conventions.md
        └── [L0] dev: code style, testing → knowledge/development.md
              │
              ↓ (recall_knowledge를 통해 필요시 로드)
        L1 지식 파일 (무제한, 키워드로 로드)
```

## 인지 의사결정 프레임워크

메모리 면 모델은 에이전트가 체계적인 의사결정 프로세스를 따를 때만 제대로 작동합니다. 이 프레임워크는 에이전트의 시스템 프롬프트에 주입하거나 `cognitive_decision_prompt` MCP 프롬프트로 로드하여 일관된 동작을 보장해야 합니다.

### 의사결정 트리

```
에이전트가 문제에 직면하거나 요청을 수신
  │
  ├─ 단계 1: L0 색인을 스캔하여 관련 도메인 탐색
  │
  ├─ 단계 2: 일치하는 항목이 있는가?
  │   ├─ 예 → 해당 L1 지식 파일 로드 (절차라면 에이전트 자신의 스킬도)
  │   │   │
  │   │   ├─ 지식으로 해결 가능 → 그대로 사용. 추측으로 우회하지 않음.
  │   │   ├─ 지식이 부분적으로 도움됨 → 적용 후 항목 강화
  │   │   └─ 지식이 부족함 → 새로운 문제로 처리 (단계 3).
  │   │
  │   └─ 아니오 → 새로운 문제로 처리 (단계 3).
  │
  ├─ 단계 3: 새로운 문제/요구사항으로 처리
  │   표준 도구와 추론으로 해결.
  │
  └─ 단계 4: 해결 후 평가 — 보존할 가치가 있는가, 어디에 둘 것인가?
      ├─ 지속되는 팩트 / 결정 / 아키텍처 → inject_knowledge로 L1에
      ├─ 미완료 작업 (대기, 차단)        → TODO 스토어에 (L1에는 두지 않음)
      ├─ 재사용 가능한 절차 / 명령 시퀀스 → 에이전트 자신의 스킬 면에
      ├─ 지속적인 사용자 선호            → 사용자 확인 후 자신의 프로필 면에
      ├─ 일회성 출력이나 원본 로그       → 보존하지 않음
      └─ 확실하지 않다면                → classify_memory_item 호출 (읽기 전용)
```

### 왜 이것이 중요한가

이 의사결정 프레임워크가 없으면 에이전트는 다음과 같은 문제를 겪기 쉽습니다:
- **기존 지식 무시** — L0 색인을 보면서도 L1 파일 로드를 잊고 추측으로 시간 낭비
- **같은 실수 반복** — 해결된 문제가 기록되지 않아 다음 세션에서 처음부터 다시 학습
- **기존 규약 우회** — 매 세션마다 제로에서 시작하여 축적된 지식 위에 구축하지 못함

이 프레임워크는 메모리 시스템을 수동적 저장소에서 **능동적 인지 루프**로 변환합니다: 조회 → 행동 → 학습 → 개선.

### 통합 방법

에이전트의 시스템 프롬프트에 다음을 추가:

```
당신은 레이어드 메모리 시스템을 사용합니다. 문제 해결 전:
1. L0 색인에서 일치하는 도메인 확인
2. 일치하면 행동하기 전에 L1 지식을 먼저 로드
3. 일치하지 않으면 일반적인 방법으로 해결
4. 해결 후에는 분류해서 보존: 지속되는 팩트는 inject_knowledge로 L1에,
   미완료 작업은 TODO 스토어에, 절차와 지속적인 사용자 선호는 자신의 면에.
   확실하지 않으면 classify_memory_item 호출
```

또는 내장 MCP 프롬프트 `cognitive_decision_prompt`를 사용하여 런타임에 전체 의사결정 프레임워크를 가져올 수 있습니다.

## 개발

```bash
# 클론
git clone https://github.com/LAIguapi/layered-memory-mcp.git
cd layered-memory-mcp

# 개발 모드로 설치
pip install -e ".[dev]"

# 테스트 실행
pytest

# 로컬 실행
python -m layered_memory_mcp.server
```

## Changelog

전체 릴리스 이력은 [CHANGELOG.md](CHANGELOG.md)를 참조하세요. `3.4.7`의 주요 내용:

- 라이프사이클 거버넌스: 구조화된 권장 액션(`move_to_todo` / `rewrite_as_current_rule` / `keep_with_reason`), 마커 어휘 설정 가능
- `plan_knowledge_cleanup_tool`과 `apply_safe_knowledge_cleanup_tool`: 읽기 전용 계획, 안전한 액션은 dry-run 먼저 후 실행
- 지식 백업을 활성 지식 디렉터리 밖 `data/backups/knowledge/`로 이동
- 벡터에 임베딩되는 텍스트의 제목에서 날짜·버전 등 노이즈 제거
- glob 기반 잡동사니 파일 스캔(`promotion.watch_patterns`), 영어·중국어 기본값 포함

## 라이선스

MIT License — 자세한 내용은 [LICENSE](LICENSE)를 참조하세요.
