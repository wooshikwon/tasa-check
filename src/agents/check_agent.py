"""타사 체크 분석 에이전트.

수집된 기사 목록을 Claude API로 분석하여
[단독]/[주요]/[스킵]을 분류하고, 요약과 판단 근거를 생성한다.
"""

import json
import logging

import anthropic
from langfuse import get_client as get_langfuse

from src.config import DEPARTMENT_PROFILES

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT_TEMPLATE = """\
당신은 {dept_label} 기자의 타사 체크 보조입니다.

[취재 영역 - {dept_label}]
{coverage_section}

[기자의 취재 키워드]
{keywords_section}

[키워드 관련성 필터 - 최우선 기준]
아래 기사들은 키워드로 검색된 결과이나, 검색 API 특성상 키워드와 무관한 기사가 포함될 수 있다.
반드시 기사 내용이 위 키워드와 직접적으로 관련된 경우에만 판단 대상으로 삼는다.
- "직접 관련"이란: 기사에 해당 키워드의 기관/장소/인물이 실제로 등장하거나, 해당 관할/소관 사안을 다루는 경우
- 동일 분야라도 다른 기관/관할의 기사는 관련 없는 것으로 판단한다
  예) 키워드가 "서부지법"인데 기사가 "서울중앙지법" 사건이면 → skip
  예) 키워드가 "마포경찰서"인데 기사가 "강남경찰서" 사건이면 → skip
- 키워드와 무관한 기사는 기사 가치와 무관하게 반드시 skip 처리한다

[주요 기사 판단 기준 - {dept_label}]
키워드 관련성을 통과한 기사에 한해, 아래 기준으로 판단한다:
{criteria_section}

추가 판단 기준:
- 경쟁 관점: 사실상 단독([단독] 태그 또는 특정 언론사만 보도), 복수 보도(3개 이상 동시 보도), 새로운 앵글
- 사회적 맥락: 진행 중 주요 이슈와 직접 연결, 후속 보도 가능성 높음
- 시의성: 방금 발생/확인된 사건, 오늘/내일 중 결정 예정

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
{{
  "category": "exclusive" | "important" | "skip",
  "topic_cluster": "주제 식별자 (짧은 구문)",
  "source_indices": [대표 기사 번호],
  "merged_indices": [병합된 다른 기사 번호] 또는 빈 배열,
  "title": "기사 제목 (skip 포함 모든 항목에 반드시 기재)",
  "summary": "2~3문장 요약 (skip이면 빈 문자열)",
  "reason": "주요 판단 근거 1문장 (skip이면 스킵 사유)",
  "key_facts": ["핵심 팩트1", "핵심 팩트2"]
}}
source_indices: 해당 항목의 대표 기사 번호 (위 [새로 수집된 기사] 목록의 번호)
merged_indices: 동일 사안으로 병합된 다른 기사들의 번호 (없으면 빈 배열)
"""


def _build_user_prompt(
    articles: list[dict],
    history: list[dict],
    department: str,
) -> str:
    """사용자 프롬프트를 조립한다."""
    sections = []

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

    # 새로 수집된 기사 (번호로 참조, URL은 코드에서 관리)
    lines = ["[새로 수집된 기사]"]
    for i, a in enumerate(articles, 1):
        publisher = a.get("publisher", "")
        title = a.get("title", "")
        body = a.get("body", "")
        pub_date = a.get("pubDate", "")
        lines.append(f"{i}. [{publisher}] {title}")
        if body:
            lines.append(f"   본문(1~2문단): {body}")
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
    """Claude 응답에서 JSON 배열을 파싱한다.

    max_tokens로 잘린 경우 } 위치를 역순 탐색하여 마지막 완전한 객체까지 복구한다.
    """
    text = text.strip()
    if "```" in text:
        lines = text.split("\n")
        lines = [l for l in lines if not l.startswith("```")]
        text = "\n".join(lines).strip()

    # JSON 배열 시작 위치
    start = text.find("[")
    if start == -1:
        logger.error("JSON 배열을 찾을 수 없음: %s", text[:200])
        return []
    text = text[start:]

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 잘린 JSON 복구: } 위치를 오른쪽부터 역순으로 시도
    search_end = len(text)
    while search_end > 0:
        pos = text.rfind("}", 0, search_end)
        if pos == -1:
            break
        truncated = text[:pos + 1] + "]"
        try:
            result = json.loads(truncated)
            if isinstance(result, list):
                logger.warning("잘린 JSON 복구: 원본 %d자 → %d자, %d건 복구", len(text), len(truncated), len(result))
                return result
        except json.JSONDecodeError:
            search_end = pos
            continue

    logger.error("JSON 파싱 실패: %s", text[:300])
    return []


def _dept_label(department: str) -> str:
    """부서명에 '부'가 없으면 붙인다."""
    return department if department.endswith("부") else f"{department}부"


def _build_system_prompt(keywords: list[str], department: str) -> str:
    """키워드와 부서 프로필을 포함한 시스템 프롬프트를 생성한다."""
    dept_label = _dept_label(department)
    profile = DEPARTMENT_PROFILES.get(dept_label, {})
    keywords_section = ", ".join(keywords) if keywords else "(키워드 없음)"
    coverage_section = profile.get("coverage", "")
    criteria = profile.get("criteria", [])
    criteria_section = "\n".join(f"- {c}" for c in criteria)
    return _SYSTEM_PROMPT_TEMPLATE.format(
        dept_label=dept_label,
        keywords_section=keywords_section,
        coverage_section=coverage_section,
        criteria_section=criteria_section,
    )


async def analyze_articles(
    api_key: str,
    articles: list[dict],
    history: list[dict],
    department: str,
    keywords: list[str] | None = None,
) -> list[dict]:
    """Claude API로 기사를 분석한다.

    Args:
        api_key: 기자의 Anthropic API 키
        articles: 수집된 기사 목록 (title, publisher, body, url, pubDate)
        history: 최근 24시간 보고 이력
        department: 기자 부서
        keywords: 기자의 취재 키워드 목록

    Returns:
        분석 결과 리스트. 각 항목은 category, topic_cluster, source_indices,
        merged_indices, title, summary, reason, key_facts 포함.
    """
    system_prompt = _build_system_prompt(keywords or [], department)
    user_prompt = _build_user_prompt(articles, history, department)

    langfuse = get_langfuse()
    with langfuse.start_as_current_observation(
        as_type="span", name="check_agent", metadata={"department": department},
    ):
        client = anthropic.AsyncAnthropic(api_key=api_key)
        message = await client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=16384,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )

    response_text = message.content[0].text
    return _parse_response(response_text)
