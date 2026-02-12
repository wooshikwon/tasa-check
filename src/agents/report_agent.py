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

_REPORT_TOOL = {
    "name": "submit_report",
    "description": "뉴스 브리핑 분석 결과를 제출한다. 모든 검색과 분석을 마친 후 사용.",
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "description": "브리핑 항목 배열. 첫 요청 시 최소 8개, 10개 이상 목표.",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["modified", "added"],
                            "description": "기존 캐시 대비 변경 유형 (업데이트 시나리오에서만 사용)",
                        },
                        "item_id": {
                            "type": ["integer", "null"],
                            "description": "기존 항목 ID (action이 modified일 때만, 나머지는 null)",
                        },
                        "title": {
                            "type": "string",
                            "description": "기사 제목",
                        },
                        "url": {
                            "type": "string",
                            "description": "기사 URL (네이버 뉴스 URL 우선)",
                        },
                        "summary": {
                            "type": "string",
                            "description": "2~3줄 구체적 요약 (인물명, 수치, 일시 등 팩트 포함)",
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "주제 태그 배열",
                        },
                        "category": {
                            "type": "string",
                            "enum": ["follow_up", "new"],
                            "description": "follow_up=이전 보도 후속, new=신규",
                        },
                        "exclusive": {
                            "type": "boolean",
                            "description": "[단독] 태그 또는 특정 언론사만 보도한 기사이면 true",
                        },
                        "prev_reference": {
                            "type": ["string", "null"],
                            "description": "follow_up이면 'YYYY-MM-DD \"이전 제목\"', new이면 null",
                        },
                    },
                    "required": [
                        "title", "url", "summary", "tags",
                        "category", "exclusive", "prev_reference",
                    ],
                },
            },
        },
        "required": ["items"],
    },
}

TOOLS = [
    {
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": 15,
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
    _REPORT_TOOL,
]

_OUTPUT_INSTRUCTIONS_A = """\
모든 검색과 분석을 마친 후, submit_report 도구를 사용하여 결과를 제출하라.
각 항목의 필드 규칙:
- title: 기사 제목
- url: 기사 URL (네이버 뉴스 URL 우선)
- summary: 2~3줄 구체적 요약 (인물명, 수치, 일시 등 팩트 포함)
- tags: 주제 태그 배열
- category: "follow_up" (이전 보도 후속) 또는 "new" (신규)
- exclusive: true ([단독] 태그 또는 특정 언론사만 보도) 또는 false
- prev_reference: follow_up이면 "YYYY-MM-DD \\"이전 제목\\"", new이면 null
최소 8개, 10개 이상 목표. 부족하면 추가 검색을 반드시 실행한다."""

_OUTPUT_INSTRUCTIONS_B = """\
모든 검색과 분석을 마친 후, submit_report 도구를 사용하여 결과를 제출하라.
각 항목의 필드 규칙:
- action: "modified" (기존 항목 수정) 또는 "added" (신규 추가)
- item_id: 기존 항목 ID (action이 modified일 때만, added는 null)
- title: 기사 제목
- url: 기사 URL (네이버 뉴스 URL 우선)
- summary: 2~3줄 구체적 요약 (갱신 또는 신규, 인물명·수치·일시 등 팩트 포함)
- tags: 주제 태그 배열
- category: "follow_up" (이전 보도 후속) 또는 "new" (신규)
- exclusive: true ([단독] 태그 또는 특정 언론사만 보도) 또는 false
- prev_reference: null
변경 없는 기존 항목은 포함하지 않는다. 수정/추가 항목이 없으면 빈 배열을 제출."""


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

        f"[검색 범위 - 엄격 적용]\n"
        f"- 반드시 {date} ({date_kr}) 당일 보도된 기사만 포함\n"
        f"- 기사의 발행일이 {date}이 아니면 절대 포함하지 않는다\n"
        f"- 이전 날짜 기사는 내용과 무관하게 제외\n"
        f"- 검색 쿼리에 '{date_kr}'를 반드시 포함하여 당일 기사를 타겟하라",
    ]

    # 이전 태그
    if recent_tags:
        tags_str = " ".join(f"#{t}" for t in recent_tags)
        sections.append(f"[이전 전달 태그 - 최근 3일]\n{tags_str}")
    else:
        sections.append(
            "[이전 전달 태그 - 최근 3일]\n"
            "없음. 이전 전달 이력이 없으므로 모든 항목을 category: \"new\", "
            "prev_reference: null로 설정하라."
        )

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
            "첫 요청과 동일한 수준으로 폭넓게 검색한 뒤, 기존 캐시와 비교한다.\n"
            "1. 부서 관련 당일 뉴스를 폭넓게 검색 (첫 요청과 동일 범위)\n"
            "2. 검색 결과를 기존 캐시와 대조\n"
            "3. 기존 항목에 새 팩트가 추가됐으면 [수정] (기존 요약에 새 정보 병합)\n"
            "4. 기존 캐시에 없는 새로운 기사는 [추가]\n"
            "5. 변경 없는 항목은 출력하지 않음\n"
            "6. 추가 항목은 적극적으로 찾는다. 기존 캐시가 부족했을 수 있다\n"
            "7. 각 항목마다 2~3줄의 구체적 요약 작성\n"
            "8. 검색과 분석이 끝나면 submit_report 도구로 결과 제출"
        )
    else:
        sections.append(
            "[절차 - 당일 첫 요청]\n"
            "1. 이전 태그 기반 후속 검색 + 부서별 신규 검색 실행\n"
            "2. 검색 쿼리에 오늘 날짜를 명시\n"
            "3. 후속/심화: 이전 캐시 항목과 내용상 연결되는 보도\n"
            "4. 신규: 연결 없는 새로운 뉴스\n"
            "5. 최소 8개, 10개 이상 목표. 부족하면 추가 검색 실행\n"
            "6. 각 항목마다 2~3줄의 구체적 요약 작성\n"
            "7. 검색과 분석이 끝나면 submit_report 도구로 결과 제출"
        )

    # 부서별 취재 영역 + 중요도 판단 기준
    profile = DEPARTMENT_PROFILES.get(dept_label, {})
    coverage = profile.get("coverage", "")
    criteria = profile.get("criteria", [])

    if coverage:
        sections.append(f"[취재 영역 - {dept_label}]\n{coverage}")

    criteria_lines = [f"[선정 기준 - {dept_label}]"]
    criteria_lines.append(
        f"{dept_label} 데스크가 주목할 사안만 선정한다. "
        "사회적 파장, 후속 보도 가능성, 복수 언론 보도 여부를 기준으로 판단:"
    )
    for c in criteria:
        criteria_lines.append(f"- {c}")
    criteria_lines.append(
        "\n[제외 기준]\n"
        "아래에 해당하는 기사는 포함하지 않는다:\n"
        "- 단발성 사건·사고: 후속 보도 가능성이 낮은 개별 사망, 교통사고, 화재 등\n"
        "- 정례적 발표: 정부 부처 보도자료, 정기 통계, 일상적 권고·캠페인\n"
        "- 단순 일정 안내: 행사 예고, 연휴 대비 당부 등\n"
        "- 사회적 관심도가 낮은 사안: 소규모 지역 이슈, 단일 기관 내부 사안"
    )
    sections.append("\n".join(criteria_lines))

    # 요약 작성 기준
    sections.append(
        "[요약 작성 기준]\n"
        "- 2~3줄로 핵심 정보를 구체적으로 전달\n"
        "- 인물명, 기관명, 수치, 일시 등 구체적 팩트를 반드시 포함\n"
        "- 사안의 배경과 의미를 짚는다\n"
        "- 사실 기반 작성, 추측/의견 배제\n"
        "- 검색 결과 스니펫만으로 부족하면 fetch_article로 원문 확인"
    )

    # URL 규칙
    sections.append(
        "[URL 규칙]\n"
        "기사 URL은 네이버 뉴스 URL(n.news.naver.com 또는 naver.com 도메인)을 우선 사용한다.\n"
        "네이버 뉴스 URL이 없는 경우에만 언론사 원본 URL을 사용한다."
    )

    # 출력 지시
    instructions = _OUTPUT_INSTRUCTIONS_B if is_scenario_b else _OUTPUT_INSTRUCTIONS_A
    sections.append(f"[출력]\n{instructions}")

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

    여러 text 블록이 결합되면 내러티브 텍스트에 [, ] 등이 포함될 수 있으므로,
    가능한 모든 [ 위치에서 파싱을 시도한다.
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

    end = text.rfind("]")
    if end == -1:
        logger.error("JSON 배열 닫힘을 찾을 수 없음: %s", text[:200])
        return []

    # 마지막 ]부터 역방향으로 매칭되는 [를 찾아 파싱 시도
    # 내러티브 텍스트의 [단독], [검색] 등을 건너뛰기 위함
    pos = -1
    while True:
        pos = text.find("[", pos + 1)
        if pos == -1 or pos >= end:
            break
        json_str = text[pos:end + 1]
        try:
            result = json.loads(json_str)
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            continue

    # 모든 [ 위치에서 실패 → 마지막 완전한 객체까지 잘라서 복구 시도
    last_brace = text.rfind("}")
    if last_brace > 0:
        pos = -1
        while True:
            pos = text.find("[", pos + 1)
            if pos == -1 or pos >= last_brace:
                break
            truncated = text[pos:last_brace + 1] + "]"
            try:
                result = json.loads(truncated)
                if isinstance(result, list):
                    logger.warning("잘린 JSON 복구: 원본 %d자 → %d자", len(text), len(truncated))
                    return result
            except json.JSONDecodeError:
                continue

    logger.error("JSON 파싱 실패: %s", text[:300])
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
                max_tokens=8192,
                temperature=0.0,
                system=system_prompt,
                tools=TOOLS,
                messages=messages,
            )

            logger.info(
                "턴 %d 응답: stop_reason=%s, blocks=%s",
                turn + 1, response.stop_reason,
                [getattr(b, "type", "?") for b in response.content],
            )

            # submit_report 도구 호출 감지 → 구조화된 결과 추출
            for block in response.content:
                if getattr(block, "type", None) == "tool_use" and block.name == "submit_report":
                    items = block.input.get("items", [])
                    logger.info("submit_report로 결과 수신 (턴 %d): %d건", turn + 1, len(items))
                    return items

            # 최종 응답이 text인 경우 폴백 파싱
            if response.stop_reason == "end_turn":
                text_blocks = []
                for block in response.content:
                    if getattr(block, "type", None) == "text":
                        text_blocks.append(block.text)
                final_text = "\n".join(text_blocks)
                logger.info("텍스트 폴백 파싱 (턴 %d), 블록 %d개, 길이=%d", turn + 1, len(text_blocks), len(final_text))
                return _parse_response(final_text)

            # fetch_article 도구 호출 실행
            tool_results = await _execute_custom_tools(response)

            # web_search는 서버사이드 처리이므로 커스텀 도구 결과가 없을 수 있음
            if not tool_results:
                messages.append({"role": "assistant", "content": response.content})
                continue

            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

    logger.error("에이전트 루프 최대 턴(%d) 초과", _MAX_AGENT_TURNS)
    raise RuntimeError("에이전트 루프가 최대 턴을 초과했습니다.")
