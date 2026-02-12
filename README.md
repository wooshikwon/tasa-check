# tasa-check

기자의 타사 체크 업무를 자동화하는 텔레그램 봇.

## 배경

기자는 비정기적으로 타사 보도를 확인하고, 자사가 놓친 기사를 파악하여 후속 보도를 작성해야 한다. 이 과정은 수십 개 언론사의 기사를 반복적으로 훑어야 하므로 인지적 자원을 크게 소모하는 업무다.

tasa-check는 LLM이 기자의 키워드 및 부서와 관련된 뉴스를 수집·분석·요약하여 텔레그램으로 전달함으로써, 타사 체크에 드는 시간과 피로도를 줄이기 위한 프로젝트다.

## 사용법

텔레그램 봇: https://t.me/tasa_check_bot

### 명령어

| 명령어 | 설명 |
|---|---|
| `/start` | 초기 등록. 부서, 취재 키워드, Anthropic API 키를 입력한다 |
| `/check` | 타사 체크. 키워드 기반으로 최근 기사를 수집·분석하여 주요 기사를 전달한다 |
| `/report` | 부서 브리핑. 부서 전체의 당일 주요 뉴스를 검색·정리하여 전달한다 |
| `/setkey` | API 키 변경. `/setkey sk-ant-...` 형식으로 입력하면 메시지가 자동 삭제된다 |

### /check vs /report

- `/check`: 기자가 등록한 **키워드** 중심. 네이버 뉴스 검색 → 언론사 필터 → Claude 분석. 단독/주요/스킵을 분류하고 이전 체크와 중복을 제거한다.
- `/report`: **부서** 중심. Claude가 웹 검색으로 당일 부서 관련 뉴스를 직접 수집·정리한다. 같은 날 재요청 시 이전 브리핑과 비교하여 변경/추가분만 갱신한다.

## 기술 스택

- Python 3.12, [uv](https://github.com/astral-sh/uv)
- Anthropic Claude API (BYOK 방식 — 사용자가 자신의 API 키를 등록)
- python-telegram-bot
- aiosqlite (SQLite 비동기)
- Langfuse (LLM 호출 모니터링)
- Oracle Cloud Free Tier (서버 배포)
