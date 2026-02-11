"""타사 체크 분석 에이전트.

수집된 기사 목록을 Claude API로 분석하여
[단독]/[주요]/[스킵]을 분류하고, 요약과 판단 근거를 생성한다.
"""

import json
import logging

import anthropic

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
당신은 기자의 타사 체크 보조입니다.

[주요 기사 판단 기준]
아래 기준 중 하나 이상에 해당하면 [주요]로 판단한다:

A. 팩트 기반
  - 공식 조치: 체포, 구속, 기소, 영장 청구/기각, 판결, 정책 발표
  - 수치적 규모: 금액, 인원, 피해 규모가 유의미한 수준
  - 관계자 급: 고위 공직자, 대기업 임원, 공인 등
  - 중대한 전개: 소환→구속, 수사→기소 등 국면 전환

B. 경쟁 관점
  - 사실상 단독: [단독] 태그 없어도 특정 언론사만 보도한 기사
  - 복수 보도: 3개 이상 주요 언론사가 동시 보도
  - 새로운 앵글: 동일 사안에 대한 새로운 관점/정보

C. 사회적 맥락 (맥락이 제공된 경우)
  - 진행 중 주요 이슈와 직접 연결
  - 정책/법률 변경에 영향 가능
  - 후속 보도 가능성 높음

D. 시의성
  - 속보성: 방금 발생/확인된 사건
  - 임박 이벤트: 오늘/내일 중 결정/발표/공판 예정

[중복 제거 기준]
1. 동일 배치 내: 같은 사안의 여러 언론사 기사 → 가장 포괄적인 1건만 남김
2. 이전 보고 대비: 이력과 실질적으로 동일한 내용이면 스킵
3. 중복 예외: 이전 보고 주제라도 중요한 새 팩트(공식 조치, 수치 변경, 인물 추가 등)가 있으면 보고

[요약 작성 기준]
- 구체적 정보: "수사가 확대됐다" 대신 "임원 3명을 추가 소환했다"
- 핵심 수치/사실 포함: 인물명, 기관명, 일시 등
- 맥락 제공: 이 뉴스가 왜 중요한지 한 문장으로 짚는다
- 사실 기반 작성, 추측/의견 배제

[출력 형식]
반드시 아래 JSON 배열로만 응답하라. JSON 외 텍스트는 포함하지 않는다.
각 기사 항목:
{
  "category": "exclusive" | "important" | "skip",
  "topic_cluster": "주제 식별자 (짧은 구문)",
  "publisher": "언론사명",
  "title": "기사 제목",
  "summary": "2~3문장 요약",
  "reason": "주요 판단 근거 1문장 (skip이면 빈 문자열)",
  "key_facts": ["핵심 팩트1", "핵심 팩트2"],
  "article_urls": ["원본 URL"],
  "merged_from": ["병합된 다른 기사 URL"] 또는 빈 배열
}
"""


def _build_user_prompt(
    articles: list[dict],
    report_context: list[dict],
    history: list[dict],
    department: str,
) -> str:
    """사용자 프롬프트를 조립한다."""
    sections = []

    # 사회적 맥락 (report_items가 있을 때만)
    if report_context:
        lines = [f"[당일 사회적 맥락 - {department}부]"]
        for i, item in enumerate(report_context, 1):
            tags = " ".join(f"#{t}" for t in item.get("tags", []))
            lines.append(f"({i}) {item['title']} {tags}")
        sections.append("\n".join(lines))

    # 보고 이력
    if history:
        lines = ["[기자의 최근 보고 이력]"]
        for h in history:
            time_str = h["checked_at"][:16] if h.get("checked_at") else ""
            facts = ", ".join(f"({j}) {f}" for j, f in enumerate(h["key_facts"], 1))
            lines.append(f"- {time_str} 보고: \"{h['topic_cluster']}\"")
            lines.append(f"  확인된 팩트: {facts}")
        sections.append("\n".join(lines))
    else:
        sections.append("[기자의 최근 보고 이력]\n이력 없음")

    # 새로 수집된 기사
    lines = ["[새로 수집된 기사]"]
    for i, a in enumerate(articles, 1):
        publisher = a.get("publisher", "")
        title = a.get("title", "")
        body = a.get("body", "")
        url = a.get("url", "")
        pub_date = a.get("pubDate", "")
        lines.append(f"{i}. [{publisher}] {title}")
        if body:
            lines.append(f"   본문(1~2문단): {body}")
        lines.append(f"   URL: {url}")
        lines.append(f"   시각: {pub_date}")
    sections.append("\n".join(lines))

    sections.append(
        "각 기사에 대해:\n"
        "1. 중복 제거: 동일 배치 내 병합 + 이전 보고 대비 중복 판단\n"
        "2. [단독] 식별: 제목 태그 또는 사실상 단독 여부\n"
        "3. 중복 아닌 기사에 주요도 판단 (A~D 기준 적용)\n"
        "4. 보고 대상: 요약 + 해당되는 판단 근거 명시"
    )

    return "\n\n".join(sections)


def _parse_response(text: str) -> list[dict]:
    """Claude 응답에서 JSON 배열을 파싱한다."""
    text = text.strip()
    # 코드블록 마크다운 제거
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.startswith("```")]
        text = "\n".join(lines)
    return json.loads(text)


async def analyze_articles(
    api_key: str,
    articles: list[dict],
    report_context: list[dict],
    history: list[dict],
    department: str,
) -> list[dict]:
    """Claude API로 기사를 분석한다.

    Args:
        api_key: 기자의 Anthropic API 키
        articles: 수집된 기사 목록 (title, publisher, body, url, pubDate)
        report_context: 당일 report_items (optional 맥락)
        history: 최근 24시간 보고 이력
        department: 기자 부서

    Returns:
        분석 결과 리스트. 각 항목은 category, topic_cluster, publisher,
        title, summary, reason, key_facts, article_urls, merged_from 포함.
    """
    user_prompt = _build_user_prompt(articles, report_context, history, department)

    client = anthropic.AsyncAnthropic(api_key=api_key)
    message = await client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    response_text = message.content[0].text
    return _parse_response(response_text)
