"""부서 뉴스 브리핑 에이전트.

네이버 API로 수집된 기사를 LLM 필터(Haiku) + LLM 분석(Claude) 2회 호출로
부서 관련 당일 뉴스를 선별, 분석, 구조화하여 반환한다.
"""

import logging
from datetime import timezone, timedelta

import anthropic
from langfuse import get_client as get_langfuse

from src.config import DEPARTMENT_PROFILES
from src.filters.publisher import get_publisher_name

logger = logging.getLogger(__name__)

_KST = timezone(timedelta(hours=9))


def _dept_label(department: str) -> str:
    """부서명에 '부'가 없으면 붙인다."""
    return department if department.endswith("부") else f"{department}부"


# ── Haiku LLM 필터 ──────────────────────────────────────────

_FILTER_TOOL = {
    "name": "filter_news",
    "description": "부서 관련 기사 번호를 선별합니다",
    "input_schema": {
        "type": "object",
        "properties": {
            "selected_indices": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "선별된 기사 번호 배열",
            },
        },
        "required": ["selected_indices"],
    },
}


async def filter_articles(
    api_key: str,
    articles: list[dict],
    department: str,
) -> list[dict]:
    """Haiku LLM으로 부서 관련 기사를 필터링한다.

    본문 스크래핑 전에 제목+description만으로 판단하여
    부서 무관 기사, 사진 캡션, 중복 사안을 제거한다.

    Args:
        api_key: Anthropic API 키
        articles: 언론사 필터 후 기사 리스트 (search_news 반환 형태)
        department: 부서명

    Returns:
        필터 통과한 기사 리스트
    """
    if not articles:
        return []

    dept_label = _dept_label(department)
    profile = DEPARTMENT_PROFILES.get(dept_label, {})
    coverage = profile.get("coverage", "")

    # 기사 목록 텍스트 조립 (번호, 언론사, 제목, description)
    lines = []
    for i, a in enumerate(articles, 1):
        pub = get_publisher_name(a.get("originallink", "")) or "?"
        title = a.get("title", "")
        desc = a.get("description", "")
        lines.append(f"[{i}] {pub} | {title} | {desc}")
    article_list_text = "\n".join(lines)

    system_prompt = (
        f"당신은 {dept_label} 뉴스 필터입니다.\n"
        f"취재 영역: {coverage}\n\n"
        "아래 기사 목록에서 다음 기준으로 기사 번호를 선별하세요:\n"
        "1. 부서 관련성: 해당 부서 취재 영역에 해당하는 기사만 포함\n"
        "2. 사진 캡션 제외: 본문 없이 사진 설명만 있는 포토뉴스 제외\n"
        "3. 중복 사안 정리: 같은 사안의 다수 기사 중 대표 기사(최대 3건)만 선별\n"
        "4. 애매한 경우 포함 쪽으로 판단\n\n"
        "filter_news 도구로 선별된 기사 번호를 제출하세요."
    )

    langfuse = get_langfuse()
    with langfuse.start_as_current_observation(
        as_type="span", name="report_filter",
        metadata={"department": department, "input_count": len(articles)},
    ):
        client = anthropic.AsyncAnthropic(api_key=api_key)
        message = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            temperature=0.0,
            system=system_prompt,
            messages=[{"role": "user", "content": article_list_text}],
            tools=[_FILTER_TOOL],
            tool_choice={"type": "tool", "name": "filter_news"},
        )

    # tool_use 응답에서 인덱스 추출
    for block in message.content:
        if block.type == "tool_use" and block.name == "filter_news":
            indices = block.input.get("selected_indices", [])
            # 1-based → 0-based 변환, 범위 검증
            filtered = []
            for idx in indices:
                if isinstance(idx, int) and 1 <= idx <= len(articles):
                    filtered.append(articles[idx - 1])
            logger.info(
                "LLM 필터 결과: %d건 → %d건 (부서: %s)",
                len(articles), len(filtered), department,
            )
            return filtered

    logger.warning("LLM 필터 tool_use 응답 없음, 전체 기사 반환")
    return articles


# ── 메인 분석 ────────────────────────────────────────────────

_REPORT_TOOL = {
    "name": "submit_report",
    "description": "뉴스 브리핑 분석 결과를 제출한다.",
    "input_schema": {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "description": "브리핑 항목 배열",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["modified", "added"],
                            "description": "기존 캐시 대비 변경 유형 (업데이트 시나리오 전용)",
                        },
                        "item_id": {
                            "type": ["integer", "null"],
                            "description": "기존 항목 ID (action=modified일 때만)",
                        },
                        "title": {
                            "type": "string",
                            "description": "기사 제목",
                        },
                        "url": {
                            "type": "string",
                            "description": "대표 기사 URL (수집된 기사의 originallink)",
                        },
                        "source_indices": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": "참조 기사 번호 배열 (수집된 기사 목록 기준)",
                        },
                        "summary": {
                            "type": "string",
                            "description": "2~3줄 구체적 요약 (인물명, 수치, 일시 등 팩트 포함)",
                        },
                        "reason": {
                            "type": "string",
                            "description": "선택 사유 1문장",
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
                        "title", "url", "source_indices", "summary",
                        "reason", "tags", "category", "exclusive",
                        "prev_reference",
                    ],
                },
            },
        },
        "required": ["results"],
    },
}


def _build_system_prompt(
    department: str,
    existing_items: list[dict] | None,
) -> str:
    """시스템 프롬프트를 조립한다.

    Args:
        department: 부서명
        existing_items: 시나리오 B일 때 당일 기존 캐시 항목 (None이면 시나리오 A)
    """
    dept_label = _dept_label(department)
    profile = DEPARTMENT_PROFILES.get(dept_label, {})
    coverage = profile.get("coverage", "")
    criteria = profile.get("criteria", [])

    is_scenario_b = existing_items is not None and len(existing_items) > 0

    sections = [
        f"당신은 {dept_label} 데스크의 뉴스 브리핑 보조입니다.\n"
        "아래 제공된 기사 목록을 분석하여 데스크가 주목할 사안을 선별하고 요약합니다.",
    ]

    # 취재 영역
    if coverage:
        sections.append(f"[취재 영역 - {dept_label}]\n{coverage}")

    # 판단 기준
    criteria_lines = [f"[중요 기사 판단 기준 - {dept_label}]"]
    criteria_lines.append(
        f"{dept_label} 데스크가 반드시 알아야 할 핵심 사안만 엄선한다. "
        "아래 기준을 모두 충족하는 기사만 선정:"
    )
    criteria_lines.append(
        "1) 복수 언론이 보도하거나, 단독 보도라면 팩트의 무게가 충분할 것"
    )
    criteria_lines.append(
        "2) 후속 보도 가능성이 높거나, 사안 자체가 사회적으로 중대할 것"
    )
    criteria_lines.append(
        "3) 아래 부서별 세부 기준에 해당할 것:"
    )
    for c in criteria:
        criteria_lines.append(f"- {c}")
    sections.append("\n".join(criteria_lines))

    # 제외 기준
    sections.append(
        "[제외 기준]\n"
        "아래에 해당하면 중요도와 무관하게 제외한다:\n"
        "- 단발성 사건·사고: 후속 보도 가능성이 낮은 개별 사망, 교통사고, 화재\n"
        "- 정례적 발표: 정부 부처 보도자료, 정기 통계, 일상적 권고·캠페인\n"
        "- 단순 일정·예고: 행사 안내, 연휴 당부, 날씨 전망\n"
        "- 관심도 낮은 사안: 소규모 지역 이슈, 단일 기관 내부 사안\n"
        "- 인터뷰·칼럼·사설: 기자 의견, 전문가 인터뷰 단독 기사\n"
        "- 재탕·종합 보도: 이미 알려진 팩트를 재구성한 기사, 타 매체 인용 정리 기사\n"
        "- 연예·스포츠 가십: 부서 취재 영역과 무관한 연예인·선수 사생활"
    )

    # 요약 작성 기준
    sections.append(
        "[요약 작성 기준]\n"
        "- 2~3줄로 핵심 정보를 구체적으로 전달\n"
        "- 인물명, 기관명, 수치, 일시 등 구체적 팩트를 반드시 포함\n"
        "- 사안의 배경과 의미를 짚는다\n"
        "- 사실 기반 작성, 추측/의견 배제"
    )

    # 시나리오별 출력 규칙
    if is_scenario_b:
        lines = ["[오늘 기존 캐시]"]
        for item in existing_items:
            tags_str = " ".join(f"#{t}" for t in item.get("tags", []))
            lines.append(
                f"- id:{item['id']} | {item['title']} | 요약: {item['summary']} | {tags_str}"
            )
        sections.append("\n".join(lines))

        sections.append(
            "[출력 규칙 - 업데이트]\n"
            "수집된 기사를 기존 캐시와 비교하여 변경/추가 사항만 보고한다.\n"
            "- 기존 항목에 새 팩트가 추가됐으면 action: \"modified\" (기존 요약에 새 정보 병합)\n"
            "- 기존 캐시에 없는 새로운 기사는 action: \"added\"\n"
            "- 변경 없는 기존 항목은 출력하지 않음\n"
            "- modified 항목은 item_id를 반드시 기재\n"
            "- 추가 항목은 적극적으로 찾는다. 기존 캐시가 부족했을 수 있다\n"
            "- 수정/추가 항목이 없으면 빈 배열을 제출\n"
            "submit_report 도구의 results 배열로 제출하라."
        )
    else:
        sections.append(
            "[출력 규칙 - 첫 생성]\n"
            "수집된 기사 중 부서 데스크가 반드시 알아야 할 사안만 엄선한다.\n"
            "- 건수보다 품질이 우선. 기준에 미달하면 적게 선정해도 된다\n"
            "- 이전 보고 이력을 참조하여 follow_up/new 분류\n"
            "- 선택 사유(reason)를 명시 (왜 데스크가 알아야 하는지)\n"
            "- source_indices로 참조 기사 번호를 기재 (URL 역매핑용)\n"
            "- [단독] 태그 또는 특정 언론사만 보도한 기사는 exclusive: true\n"
            "submit_report 도구의 results 배열로 제출하라."
        )

    return "\n\n".join(sections)


def _build_user_prompt(
    articles: list[dict],
    report_history: list[dict],
    existing_items: list[dict] | None,
) -> str:
    """사용자 프롬프트를 조립한다.

    Args:
        articles: 수집된 기사 목록 (title, publisher, body, originallink, pubDate)
        report_history: 최근 2일치 report_items 이력
        existing_items: 시나리오 B일 때 당일 기존 캐시 항목 (None이면 시나리오 A)
    """
    sections = []

    # 이전 보고 이력
    if report_history:
        lines = ["[이전 보고 이력 - 최근 2일]"]
        for h in report_history:
            tags_str = " ".join(f"#{t}" for t in h.get("tags", []))
            created = h.get("created_at", "")[:10]
            lines.append(
                f"- \"{h['title']}\" | {h['summary']} | {tags_str} | "
                f"{h['category']} | {created}"
            )
        sections.append("\n".join(lines))
    else:
        sections.append(
            "[이전 보고 이력 - 최근 2일]\n"
            "이력 없음. 모든 항목을 category: \"new\", prev_reference: null로 설정하라."
        )

    # 시나리오 B: 기존 캐시
    is_scenario_b = existing_items is not None and len(existing_items) > 0
    if is_scenario_b:
        lines = ["[오늘 기존 캐시 항목]"]
        for item in existing_items:
            tags_str = " ".join(f"#{t}" for t in item.get("tags", []))
            lines.append(
                f"- id:{item['id']} | {item['title']} | 요약: {item['summary']} | {tags_str}"
            )
        sections.append("\n".join(lines))

    # 수집된 기사 목록
    lines = ["[수집된 기사 목록]"]
    for i, a in enumerate(articles, 1):
        publisher = a.get("publisher", "")
        title = a.get("title", "")
        body = a.get("body", "")
        pub_date = a.get("pubDate", "")
        lines.append(f"{i}. [{publisher}] {title}")
        if body:
            lines.append(f"   본문: {body}")
        lines.append(f"   시각: {pub_date}")
    sections.append("\n".join(lines))

    # 분석 지시
    if is_scenario_b:
        sections.append(
            "위 기사를 기존 캐시와 비교하여 변경/추가 항목을 submit_report로 제출하시오."
        )
    else:
        sections.append(
            "위 기사를 분석하여 데스크가 주목할 사안을 선별하고 submit_report로 제출하시오."
        )

    return "\n\n".join(sections)


async def analyze_report_articles(
    api_key: str,
    articles: list[dict],
    report_history: list[dict],
    existing_items: list[dict] | None,
    department: str,
) -> list[dict]:
    """Claude API로 기사를 분석하여 브리핑을 생성한다 (tool_use 방식).

    Args:
        api_key: Anthropic API 키
        articles: 수집된 기사 목록 (title, publisher, body, originallink, pubDate)
        report_history: 최근 2일치 report_items 이력
        existing_items: 시나리오 B일 때 당일 기존 캐시 항목 (None이면 시나리오 A)
        department: 부서명

    Returns:
        브리핑 항목 리스트.
        시나리오 A: [{title, url, source_indices, summary, reason, tags, category, exclusive, prev_reference}]
        시나리오 B: [{action, item_id, ...} + 위와 동일]
    """
    system_prompt = _build_system_prompt(department, existing_items)
    user_prompt = _build_user_prompt(articles, report_history, existing_items)

    is_scenario_b = existing_items is not None and len(existing_items) > 0
    scenario = "B" if is_scenario_b else "A"

    langfuse = get_langfuse()
    with langfuse.start_as_current_observation(
        as_type="span", name="report_agent",
        metadata={"department": department, "scenario": scenario},
    ):
        client = anthropic.AsyncAnthropic(api_key=api_key)
        message = await client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=16384,
            temperature=0.0,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            tools=[_REPORT_TOOL],
            tool_choice={"type": "tool", "name": "submit_report"},
        )

    input_tokens = message.usage.input_tokens
    output_tokens = message.usage.output_tokens
    logger.info(
        "Claude 응답: stop_reason=%s, input=%d tokens, output=%d tokens",
        message.stop_reason, input_tokens, output_tokens,
    )

    # tool_use 블록에서 results 추출
    for block in message.content:
        if block.type == "tool_use" and block.name == "submit_report":
            results = block.input.get("results", [])
            results = [r for r in results if isinstance(r, dict)]
            logger.info("브리핑 결과: %d건 (시나리오 %s)", len(results), scenario)
            return results

    logger.error("tool_use 응답을 찾을 수 없음: stop_reason=%s", message.stop_reason)
    return []
