# Layered Memory MCP Server

> 4계층 지식 아키텍처로 AI 에이전트의 메모리를 토큰 제한 너머로 확장합니다.

[**English**](README.md) | [**中文**](README.zh-CN.md) | [**日本語**](README.ja.md)

[![PyPI version](https://img.shields.io/pypi/v/layered-memory-mcp.svg)](https://pypi.org/project/layered-memory-mcp/)
[![MCP Compatible](https://img.shields.io/badge/MCP-Compatible-blue)](https://modelcontextprotocol.io)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-green)](https://python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## 문제점

AI 에이전트는 **제한된 메모리**를 가집니다. 일반적으로 매 턴마다 주입되는 영구 컨텍스트는 2-4KB에 불과합니다. 공간이 가득 차면 나머지는 모두 잊어버립니다. 프로젝트 설정, 사용자 기본 설정, API 규약, 도메인 지식 등을 저장하려면 끊임없이 공간 제한과 싸워야 합니다.

## 해결책

**Layered Memory**는 지식을 4개 계층으로 구성하여, 즉시성을 용량과 교환합니다:

```
┌─────────────────────────────────────────────────────┐
│  L0 — 색인 계층 (2-4KB, 매 턴마다 주입)              │
│  순수 포인터: "어떤 지식이 어디에 있는지"              │
├─────────────────────────────────────────────────────┤
│  L1 — 지식 파일 (무제한, 필요시 로드)                  │
│  구조화된 마크다운: 설정, 규약, 팩트                    │
├─────────────────────────────────────────────────────┤
│  L2 — 스킬 계층 (필요시 로드)                          │
│  절차, 워크플로우, 도구별 지식                         │
├─────────────────────────────────────────────────────┤
│  L3 — 원본 세션 (드물게 검색)                          │
│  전체 대화 기록, 키워드로 검색 가능                    │
└─────────────────────────────────────────────────────┘
```

**L0은 목차입니다. L1은 책장입니다. L2는 요리책입니다. L3은 일기장입니다.**

## 주요 기능

- **스마트 지식 주입(Smart Knowledge Injection)** — 한 번 쓰면 즉시 반영(Write-once, fully-visible): 중복 제거(dedup), 섹션 타겟팅, 자동 L0 색인 동기화를 통한 지식 주입
- **키워드 검색** — 관련성 점수와 함께 모든 L1 파일에서 관련 지식을 검색합니다
- **세션 스캔** — 최근 에이전트 세션에서 지식 후보를 추출합니다
- **건강 검증** — L0↔L1 일관성 확인, 고아 항목 및 오래된 엔트리 감지
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
| `LAYERED_MEMORY_DEDUP_THRESHOLD` | 중복 제거 유사도 임계값 (0.3-1.0) | `0.7` |
| `LAYERED_MEMORY_L0_FORMAT` | L0 색인 형식: `hermes` 또는 `generic` | `hermes` |

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
← L0↔L1 일관성, 고아 파일, 오래된 항목, 파일 건강 상태 확인
```

### 4. 세션 압축 (Cron Job)

대화에서 새로운 지식을 추출하기 위해 매일 cron을 설정합니다:

```
1. scan_recent_sessions → 세션 요약 가져오기
2. AI가 요약 분석 → 안정적인 팩트 식별
3. 새로운 팩트 → inject_knowledge로 기록 (L0 자동 동기화)
4. L0 색인 → 항상 최신 상태 유지
```

### 5. 레거시 CRUD (계속 사용 가능)

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

### 쓰기 도구

| 도구 | 설명 |
|------|------|
| **`inject_knowledge`** | **기본 쓰기 경로** — 중복 제거, 섹션 타겟팅, 자동 L0 동기화를 통한 스마트 주입 |
| `create_knowledge_file` | 새 .md 파일 생성 (L0 자동 동기화) |
| `update_knowledge_file` | 기존 파일 덮어쓰기 (L0 자동 동기화) |
| `delete_knowledge_file` | 파일 삭제 (L0 자동 동기화) |

### 관리 도구

| 도구 | 설명 |
|------|------|
| `sync_l0_index` | L1 파일에서 L0 색인 수동 재구축 (`dry_run` 지원) |
| `validate_knowledge` | 건강 검증: L0↔L1 일관성, 파일 품질, 중복 항목 |
| `manage_l0_entry` | 개별 L0 항목 추가 / 제거 / 교체 |

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

### 왜 4계층인가?

| 계층 | 비용 | 용량 | 사용 사례 |
|------|------|------|-----------|
| L0 (색인) | 매 턴마다 토큰 소모 | ~2KB | 빠른 조회 테이블 |
| L1 (지식) | 파일 1회 읽기 | 무제한 | 구조화된 팩트 |
| L2 (스킬) | 스킬 1회 로드 | 무제한 | 절차 |
| L3 (세션) | 전체 검색 | 무제한 | 과거 기록 조회 |

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

### 관련성 점수

`recall_knowledge`를 호출하면 파일은 다음 기준으로 점수가 매겨집니다:

1. **파일명 일치** (+10점) — 키워드가 파일명에 포함됨
2. **제목 일치** (+3점) — 키워드가 `## 제목`에 포함됨
3. **본문 빈도** (출현당 +0.5점, 최대 5점까지) — 키워드가 본문에 출현하는 빈도

결과는 점수순으로 정렬되며, 전체 파일이 아닌 일치하는 `## 섹션`만 반환됩니다.

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

4계층 아키텍처는 에이전트가 체계적인 의사결정 프로세스를 따를 때만 제대로 작동합니다. 이 프레임워크는 에이전트의 시스템 프롬프트에 주입하거나 `cognitive_decision_prompt` MCP 프롬프트로 로드하여 일관된 동작을 보장해야 합니다.

### 의사결정 트리

```
에이전트가 문제에 직면하거나 요청을 수신
  │
  ├─ 단계 1: L0 색인을 스캔하여 관련 도메인 탐색
  │
  ├─ 단계 2: 일치하는 항목이 있는가?
  │   ├─ 예 → 해당 L1 지식 파일 / L2 스킬 로드
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
  └─ 단계 4: 해결 후 평가
      보존할 가치가 있는가?
      ├─ 예 → inject_knowledge를 통해 L1에 기록하거나 L2(스킬)에 기록하여 향후 재사용.
      └─ 아니오 → 완료.
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
당신은 4계층 레이어드 메모리 시스템을 사용합니다. 문제 해결 전:
1. L0 색인에서 일치하는 도메인 확인
2. 일치하면 행동하기 전에 L1/L2를 먼저 로드
3. 일치하지 않으면 일반적인 방법으로 해결
4. 해결 후 inject_knowledge를 사용하여 새로운 지식 보존
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

### v0.5.0 — Write-Once, Fully-Visible

- **`inject_knowledge` 도구** — 중복 제거, 섹션 타겟팅, 자동 L0 동기화를 갖춘 기본 쓰기 경로
- **`sync_l0_index` 도구** — dry_run 미리보기를 지원하는 수동 L0 색인 재구축
- **`validate_knowledge` 도구** — L0↔L1 일관성 검사, 건강 진단
- **`manage_l0_entry` 도구** — 세분화된 L0 항목 추가/제거/교체
- **자동 L0 동기화** — 모든 쓰기 도구(create/update/delete/inject)가 L0 색인을 자동으로 동기화
- **중복 제거 엔진** — SequenceMatcher 기반 유사도 감지, 구성 가능한 임계값
- **파일 잠금** — 동시 쓰기 안전을 위한 fcntl.flock
- **지식 감시자** — 파일 변경 시 디바운스된 L0 동기화 트리거 (HTTP 모드)
- **`cognitive_decision_prompt`** — 내장 의사결정 프레임워크 프롬프트

### v0.4.0 — 초기 릴리스

- 4계층 지식 아키텍처 (L0/L1/L2/L3)
- 관련성 점수를 갖춘 키워드 검색
- 세션 스캔 및 압축
- MCP 프로토콜 지원 (stdio + HTTP)
- 외부 의존성 없음 (핵심 엔진)

## 라이선스

MIT License — 자세한 내용은 [LICENSE](LICENSE)를 참조하세요.
