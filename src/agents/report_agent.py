"""부서 뉴스 브리핑 에이전트.

Claude web_search + fetch_article 도구를 활용한 에이전트 루프로
부서 관련 당일 뉴스를 검색, 분석, 구조화하여 반환한다.
"""

import json
import logging
from datetime import datetime, timezone, timedelta

import anthropic
from langfuse import get_client as get_langfuse

from src.config import DEPARTMENT_PROFILES
from src.tools.scraper import fetch_article_body

logger = logging.getLogger(__name__)

_KST = timezone(timedelta(hours=9))
_MAX_AGENT_TURNS = 20

TOOLS = [
    {
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": 10,
    },
    {
        "name": "fetch_article",
        "description": "URL의 기사 본문을 가져온다. 검색 결과 스니펫만으로 요약이 어려울 때 사용.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
]

_OUTPUT_SCHEMA_A = """\
[
  {
    "title": "기사 제목",
    "url": "기사 URL",
    "summary": "1~2문장 핵심 요약",
    "tags": ["태그1", "태그2"],
    "category": "follow_up" 또는 "new",
    "exclusive": true 또는 false,
    "prev_reference": "YYYY-MM-DD \\"이전 제목\\"" (follow_up만, new는 null),
    "importance": "main" 또는 "reference"
  }
]
- exclusive: 제목에 [단독] 태그가 있거나 특정 언론사만 보도한 기사이면 true
- importance "main": 중요도 기준에 해당하는 주요 기사 (5~8개)
- importance "reference": 주요 기준에는 미달하지만 참고할 만한 기사 (3~5개, 제목만 전달)"""

_OUTPUT_SCHEMA_B = """\
[
  {
    "action": "modified" 또는 "added",
    "item_id": 기존 항목 ID (modified만, added는 null),
    "title": "기사 제목",
    "url": "기사 URL",
    "summary": "갱신된 요약 또는 신규 요약",
    "tags": ["태그1", "태그2"],
    "category": "follow_up" 또는 "new",
    "exclusive": true 또는 false,
    "prev_reference": null,
    "importance": "main" 또는 "reference"
  }
]
- exclusive: 제목에 [단독] 태그가 있거나 특정 언론사만 보도한 기사이면 true
- importance "main": 주요 기사 (수정/추가 대상)
- importance "reference": 참고 기사 (제목만 전달)
변경 없는 기존 항목은 포함하지 않는다. 수정/추가 항목이 없으면 빈 배열 []을 반환."""


def _dept_label(department: str) -> str:
    """부서명에 '부'가 없으면 붙인다."""
    return department if department.endswith("부") else f"{department}부"


def _build_system_prompt(
    department: str,
    date: str,
    recent_tags: list[str],
    existing_items: list[dict] | None,
) -> str:
    """시스템 프롬프트를 조립한다."""
    # 날짜 포맷
    dt = datetime.strptime(date, "%Y-%m-%d")
    date_kr = f"{dt.year}년 {dt.month}월 {dt.day}일"
    dept_label = _dept_label(department)

    is_scenario_b = existing_items is not None and len(existing_items) > 0

    sections = [
        f"당신은 {dept_label} 기자의 뉴스 브리핑 보조입니다.\n"
        "web_search로 뉴스를 검색하고, 검색 결과 스니펫만으로 요약이 "
        "어려우면 fetch_article로 원문을 읽어 보충합니다.",

        f"[오늘 날짜]\n"
        f"오늘은 {date} ({date_kr})이다. 이 날짜는 현재 시점의 실제 날짜이며, "
        "미래가 아니다. 날짜에 대한 의심 없이 당일 뉴스를 검색하라.",

        f"[검색 범위]\n"
        f"- 당일({date_kr}) 한국 뉴스만 대상\n"
        f"- 전일 이전 기사는 포함하지 않음\n"
        f"- 검색 쿼리에 날짜를 포함하여 당일 기사를 정확히 타겟하라",
    ]

    # 이전 태그
    if recent_tags:
        tags_str = " ".join(f"#{t}" for t in recent_tags)
        sections.append(f"[이전 전달 태그 - 최근 3일]\n{tags_str}")

    # 시나리오 B: 기존 캐시
    if is_scenario_b:
        lines = ["[오늘 기존 캐시]"]
        for item in existing_items:
            tags_str = " ".join(f"#{t}" for t in item.get("tags", []))
            lines.append(
                f"- id:{item['id']} | {item['title']} | 요약: {item['summary']} | {tags_str}"
            )
        sections.append("\n".join(lines))

    # 절차
    if is_scenario_b:
        sections.append(
            "[절차 - 당일 재요청]\n"
            "1. 기존 캐시 항목의 후속 정보가 있는지 검색\n"
            "2. 부서별 신규 뉴스 검색\n"
            "3. 기존 항목에 새 정보가 있으면 [수정] (기존 요약에 새 정보 병합)\n"
            "4. 기존 캐시에 없는 새로운 기사는 [추가]\n"
            "5. 변경 없는 항목은 출력하지 않음\n"
            "6. 참고할 만한 기사(importance: reference) 3~5개 추가 선정\n"
            "7. 검색과 분석이 끝나면 아래 JSON 형식으로 최종 응답"
        )
    else:
        sections.append(
            "[절차 - 당일 첫 요청]\n"
            "1. 이전 태그 기반 후속 검색 + 부서별 신규 검색 실행\n"
            "2. 검색 쿼리에 오늘 날짜를 명시\n"
            "3. 후속/심화: 이전 캐시 항목과 내용상 연결되는 보도\n"
            "4. 신규: 연결 없는 새로운 뉴스\n"
            "5. 주요 기사(importance: main) 5~8개 선정\n"
            "6. 주요 기준에 미달하지만 참고할 만한 기사(importance: reference) 3~5개 추가 선정\n"
            "7. 검색과 분석이 끝나면 아래 JSON 형식으로 최종 응답"
        )

    # 부서별 취재 영역 + 중요도 판단 기준
    profile = DEPARTMENT_PROFILES.get(dept_label, {})
    coverage = profile.get("coverage", "")
    criteria = profile.get("criteria", [])

    if coverage:
        sections.append(f"[취재 영역 - {dept_label}]\n{coverage}")

    criteria_lines = [f"[중요 기사 판단 기준 - {dept_label}]"]
    criteria_lines.append(f"{dept_label} 소속 기자가 반드시 알아야 할 기사만 포함한다:")
    for c in criteria:
        criteria_lines.append(f"- {c}")
    criteria_lines.append("단순 일정 안내, 보도자료 요약, 사소한 사안은 제외한다.")
    sections.append("\n".join(criteria_lines))

    # 요약 작성 기준
    sections.append(
        "[요약 작성 기준]\n"
        "- 1~2문장으로 핵심만 전달. 길게 쓰지 않는다\n"
        "- 구체적 정보: \"수사가 확대됐다\" 대신 \"임원 3명을 추가 소환했다\"\n"
        "- 사실 기반, 추측/의견 배제\n"
        "- 검색 결과 스니펫만으로 부족하면 fetch_article로 원문 확인"
    )

    # URL 규칙
    sections.append(
        "[URL 규칙]\n"
        "기사 URL은 네이버 뉴스 URL(n.news.naver.com 또는 naver.com 도메인)을 우선 사용한다.\n"
        "네이버 뉴스 URL이 없는 경우에만 언론사 원본 URL을 사용한다."
    )

    # 출력 형식
    schema = _OUTPUT_SCHEMA_B if is_scenario_b else _OUTPUT_SCHEMA_A
    sections.append(
        "[출력 형식]\n"
        "모든 검색과 분석을 마친 후, 반드시 아래 JSON 배열로만 최종 응답하라. "
        "JSON 외 텍스트는 포함하지 않는다.\n"
        f"{schema}"
    )

    return "\n\n".join(sections)


def _build_user_prompt(department: str, date_kr: str, is_scenario_b: bool) -> str:
    """사용자 프롬프트를 생성한다."""
    dept_label = _dept_label(department)
    if is_scenario_b:
        return (
            f"{dept_label} 관련 뉴스 업데이트를 검색하여 "
            "기존 캐시 대비 변경/추가 사항을 보고하시오."
        )
    return (
        f"{dept_label} 관련 {date_kr} 주요 뉴스를 검색하여 브리핑을 작성하시오."
    )


def _parse_response(text: str) -> list[dict]:
    """Claude 최종 응답에서 JSON 배열을 파싱한다.

    Claude가 JSON 외 텍스트를 함께 반환할 수 있으므로,
    텍스트에서 JSON 배열 부분을 찾아 파싱한다.
    """
    text = text.strip()
    # 코드블록 마크다운 제거
    if "```" in text:
        lines = text.split("\n")
        lines = [l for l in lines if not l.startswith("```")]
        text = "\n".join(lines).strip()

    # 순수 JSON이면 바로 파싱
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # JSON 배열 패턴([...])을 텍스트에서 추출
    start = text.find("[")
    if start == -1:
        logger.error("JSON 배열을 찾을 수 없음: %s", text[:200])
        return []

    # 마지막 ]를 찾아 JSON 영역 추출
    end = text.rfind("]")
    if end == -1 or end <= start:
        logger.error("JSON 배열 닫힘을 찾을 수 없음: %s", text[:200])
        return []

    json_str = text[start:end + 1]
    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        logger.error("JSON 파싱 실패: %s, 텍스트: %s", e, json_str[:200])
        return []


async def _execute_custom_tools(response) -> list[dict]:
    """응답에서 커스텀 도구(fetch_article) 호출을 실행하고 결과를 반환한다."""
    tool_results = []
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "fetch_article":
            url = block.input.get("url", "")
            logger.info("fetch_article 호출: %s", url)
            body = await fetch_article_body(url)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": body or "기사 본문을 가져올 수 없습니다.",
            })
    return tool_results


async def run_report_agent(
    api_key: str,
    department: str,
    date: str,
    recent_tags: list[str],
    existing_items: list[dict] | None = None,
) -> list[dict]:
    """에이전트 루프를 실행하여 부서 뉴스 브리핑을 생성한다.

    Args:
        api_key: 기자의 Anthropic API 키
        department: 부서명
        date: 오늘 날짜 (YYYY-MM-DD)
        recent_tags: 최근 3일 report_items 태그
        existing_items: 시나리오 B일 때 기존 캐시 항목 (None이면 시나리오 A)

    Returns:
        브리핑 항목 리스트.
        시나리오 A: [{title, url, summary, tags, category, prev_reference}]
        시나리오 B: [{action, item_id, title, url, summary, tags, category, prev_reference}]
    """
    is_scenario_b = existing_items is not None and len(existing_items) > 0

    system_prompt = _build_system_prompt(department, date, recent_tags, existing_items)

    dt = datetime.strptime(date, "%Y-%m-%d")
    date_kr = f"{dt.year}년 {dt.month}월 {dt.day}일"
    user_prompt = _build_user_prompt(department, date_kr, is_scenario_b)

    langfuse = get_langfuse()
    scenario = "B" if is_scenario_b else "A"

    with langfuse.start_as_current_observation(
        as_type="span", name="report_agent",
        metadata={"department": department, "scenario": scenario},
    ):
        client = anthropic.AsyncAnthropic(api_key=api_key)
        messages = [{"role": "user", "content": user_prompt}]

        for turn in range(_MAX_AGENT_TURNS):
            logger.info("에이전트 루프 턴 %d", turn + 1)

            response = await client.messages.create(
                model="claude-sonnet-4-5-20250929",
                max_tokens=4096,
                system=system_prompt,
                tools=TOOLS,
                messages=messages,
            )

            logger.info(
                "턴 %d 응답: stop_reason=%s, blocks=%s",
                turn + 1, response.stop_reason,
                [getattr(b, "type", "?") for b in response.content],
            )

            if response.stop_reason == "end_turn":
                # 여러 text 블록이 있을 수 있으므로 모두 결합
                text_blocks = []
                for block in response.content:
                    if getattr(block, "type", None) == "text":
                        text_blocks.append(block.text)
                final_text = "\n".join(text_blocks)
                logger.info("에이전트 루프 종료 (턴 %d), 텍스트 블록 %d개, 총 길이=%d", turn + 1, len(text_blocks), len(final_text))
                logger.info("최종 텍스트 (마지막 2000자): %s", final_text[-2000:])
                return _parse_response(final_text)

            tool_results = await _execute_custom_tools(response)

            # web_search는 서버사이드 처리이므로 커스텀 도구 결과가 없을 수 있음
            # 이 경우 응답에 텍스트가 있으면 최종 결과, 없으면 다음 턴 진행
            if not tool_results:
                final_text = ""
                for block in response.content:
                    if getattr(block, "type", None) == "text":
                        final_text = block.text
                if final_text.strip() and response.stop_reason == "end_turn":
                    return _parse_response(final_text)
                # web_search 결과를 포함한 응답 → 다음 턴으로 계속
                messages.append({"role": "assistant", "content": response.content})
                continue

            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

    logger.error("에이전트 루프 최대 턴(%d) 초과", _MAX_AGENT_TURNS)
    raise RuntimeError("에이전트 루프가 최대 턴을 초과했습니다.")
