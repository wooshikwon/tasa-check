"""부서 뉴스 브리핑 에이전트.

네이버 API로 수집된 기사를 LLM 필터(Haiku) + LLM 분석(Claude) 2회 호출로
부서 관련 당일 뉴스를 선별, 분석, 구조화하여 반환한다.
"""

import json
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
        "3. 중복 사안 정리: 같은 사안의 다수 기사 중 정보가 가장 풍부한 기사(최대 3건)만 선별\n"
        "4. 애매한 경우 포함 쪽으로 판단\n\n"
        "filter_news 도구로 선별된 기사 번호를 제출하세요."
    )

    langfuse = get_langfuse()
    with langfuse.start_as_current_observation(
        as_type="span", name="report_filter",
        metadata={"department": department, "input_count": len(articles)},
    ):
        client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=3)
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

def _build_report_tool(is_scenario_b: bool) -> dict:
    """시나리오에 따라 도구 스키마를 동적으로 구성한다.

    시나리오 A: action/item_id 필드 없음
    시나리오 B: action/item_id 필드 추가 (required)
    union type 사용하지 않음 — null 대신 빈값(0/"")으로 대체.
    """
    item_props = {
        "title": {
            "type": "string",
            "description": "대표 기사의 원본 제목 (수집된 기사 목록의 제목을 그대로 사용)",
        },
        "source_indices": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "참조 기사 번호 배열 (수집된 기사 목록 기준)",
        },
        "summary": {
            "type": "string",
            "description": "2~3줄 육하원칙 스트레이트 형식 요약 (당일 팩트만)",
        },
        "reason": {
            "type": "string",
            "description": "선택 사유 1문장",
        },
        "category": {
            "type": "string",
            "enum": ["follow_up", "new"],
            "description": "follow_up=이전 보도 단독 후속, new=신규 사안",
        },
        "key_facts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "당일 새로 발생/확인된 핵심 팩트 배열",
        },
        "exclusive": {
            "type": "boolean",
            "description": "[단독] 태그 또는 특정 언론사만 보도한 기사이면 true",
        },
        "prev_reference": {
            "type": "string",
            "description": "follow_up이면 'YYYY-MM-DD \"이전 제목\"', new이면 빈 문자열",
        },
    }
    required = [
        "title", "source_indices", "summary",
        "reason", "category", "key_facts",
        "exclusive", "prev_reference",
    ]

    if is_scenario_b:
        item_props["action"] = {
            "type": "string",
            "enum": ["modified", "added"],
            "description": "modified=기존 항목에 단독 기사 발견, added=새로운 사안",
        }
        item_props["item_id"] = {
            "type": "integer",
            "description": "수정 대상 기존 항목 순번 (modified일 때 해당 순번, added일 때 0)",
        }
        required = ["action", "item_id"] + required

    return {
        "name": "submit_report",
        "description": "뉴스 브리핑 분석 결과를 제출한다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "results": {
                    "type": "array",
                    "description": "브리핑 항목 배열 (선정 기준 미달 시 빈 배열)",
                    "items": {
                        "type": "object",
                        "properties": item_props,
                        "required": required,
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

    # 단독 식별
    sections.append(
        "[단독 기사 식별 — 최우선 선정 대상]\n"
        "- 제목에 [단독] 태그가 있으면 무조건 선정\n"
        "- 제목에 없더라도 본문에 \"OO 취재에 따르면\", \"본지 취재 결과\" 등 "
        "'취재에 따르면' 패턴이 있으면 자체 취재 = 사실상 단독\n"
        "- 본문 어미로 기사 가치 판단:\n"
        "  · \"알려졌다\", \"전해졌다\" → 풍문 수준, 팩트 확인 약함\n"
        "  · \"나타났다\", \"드러났다\" → 객관적 사실·공식 발표\n"
        "  · \"취재에 따르면\", \"확인됐다\" → 자체 취재·신규 팩트, 가장 높은 뉴스 가치"
    )

    # 제외 기준
    sections.append(
        "[제외 기준]\n"
        "다음 유형의 기사는 results에 포함하지 않는다:\n"
        "- 단발성 사건·사고: 추락, 사망, 교통사고, 화재 등 후속 보도 가능성이 낮은 개별 사건\n"
        "- 정례적 발표: 정부 부처 보도자료, 정기 통계, 일상적 권고·캠페인\n"
        "- 단순 일정·예고: 행사 안내, 연휴 당부, 날씨 전망\n"
        "- 관심도 낮은 사안: 소규모 지역 이슈, 단일 기관 내부 사안\n"
        "- 인터뷰·칼럼·사설: 기자 의견, 전문가 인터뷰 단독 기사\n"
        "- 재탕·종합 보도: 이미 알려진 팩트를 재구성한 기사, 타 매체 인용 정리 기사\n"
        "- 연예·스포츠 가십: 부서 취재 영역과 무관한 연예인·선수 사생활\n\n"
        "선정 전 자기 검증: reason을 먼저 작성하라. "
        "reason이 위 제외 유형에 해당하면('후속 가능성 낮음', '단발성' 등) 해당 기사는 제외 대상이다."
    )

    # 요약 작성 기준
    sections.append(
        "[요약 작성 기준]\n"
        "- 2~3줄 이내로 작성\n"
        "- 육하원칙(누가, 언제, 어디서, 무엇을, 어떻게, 왜) 스트레이트 형식\n"
        "- 당일 발생한 사실만 요약. 기사 내 과거 경위·배경은 포함하지 않는다\n"
        "  예) \"A가 14일 B를 발표했다. 앞서 7일 C가 있었다\" → 14일 발표만 요약\n"
        "- 인물명, 기관명, 수치, 일시 등 구체적 팩트 포함\n"
        "- LLM의 해석·평가·전망 금지. 판단은 reason 필드에만 기재"
    )

    # 동일 사안 병합/분리 규칙
    sections.append(
        "[동일 사안 병합/분리 규칙]\n"
        "- 동일 사안의 복수 보도는 source_indices로 묶어 1건으로 보고\n"
        "- 단독 보도([단독] 또는 '취재에 따르면')만 별도 항목으로 분리\n"
        "  예) 'A 사건' 일반 보도 3건 → 1건으로 병합 (source_indices: [1, 2, 3])\n"
        "  예) 'A 사건' 일반 보도 3건 + [단독] 1건 → 2건으로 분리"
    )

    # 중복/후속 판단 기준
    sections.append(
        "[중복/후속 판단 기준]\n"
        "동일 뉴스 판단: 육하원칙(누가, 언제, 어디서, 무엇을)의 핵심 사실이 동일하면 동일 뉴스다.\n"
        "같은 발표·발언·사건의 새로운 앵글·관점·추가 수치·반응·배경·디테일은 새 뉴스가 아니다.\n\n"
        "- new: 이전 보고 이력에 없는 새로운 사안만 선정\n"
        "- follow_up: 이전 보고된 사안 중 [단독] 또는 \"취재에 따르면\" 패턴이 있는 단독 기사만 선정 가능\n"
        "- 위 두 조건에 해당하지 않는 이전 보고 사안은 어떤 사유로도 선정하지 않는다\n"
        "- key_facts에는 당일 새로 발생/확인된 팩트만 기록한다"
    )

    # 시나리오별 출력 규칙 (기존 항목은 유저 프롬프트에서만 제공)
    if is_scenario_b:
        sections.append(
            "[출력 규칙 - 업데이트]\n"
            "수집된 기사를 유저 프롬프트의 [오늘 기존 항목]과 비교하여 변경/추가 사항만 보고한다.\n"
            "- action: \"modified\" — 기존 항목의 사안에 단독 기사([단독] 또는 '취재에 따르면')가 새로 발견된 경우\n"
            "- action: \"added\" — 기존 항목에 없는 새로운 사안\n"
            "- 기존 항목과 육하원칙이 동일한 기사는 추가 디테일이 있어도 선정하지 않는다\n"
            "- modified 항목은 item_id(기존 항목 순번)를 반드시 기재한다\n"
            "- 수정/추가 항목이 없으면 빈 배열을 제출한다\n\n"
            "results에는 [제외 기준]과 [중복/후속 판단 기준]을 모두 통과한 항목만 포함한다.\n"
            "submit_report 도구로 제출하라."
        )
    else:
        sections.append(
            "[출력 규칙 - 첫 생성]\n"
            "수집된 기사 중 부서 데스크가 반드시 알아야 할 사안만 엄선한다.\n"
            "- 건수보다 품질 우선. [제외 기준]에 해당하면 선정하지 않는다\n"
            "- 이전 보고 이력을 참조하여 follow_up/new 분류\n"
            "- reason에 선정 사유를 명시한다 (왜 데스크가 알아야 하는지)\n"
            "- source_indices로 참조 기사 번호를 기재한다 (URL 역매핑용)\n"
            "- [단독] 태그 또는 특정 언론사만 보도한 기사는 exclusive: true\n\n"
            "results에는 [제외 기준]과 [중복/후속 판단 기준]을 모두 통과한 항목만 포함한다.\n"
            "submit_report 도구로 제출하라."
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
            facts = h.get("key_facts", [])
            facts_str = ", ".join(facts) if facts else "없음"
            created = h.get("created_at", "")[:10]
            lines.append(
                f"- \"{h['title']}\" | {h['summary']} | key_facts: [{facts_str}] | "
                f"{h['category']} | {created}"
            )
        sections.append("\n".join(lines))
    else:
        sections.append(
            "[이전 보고 이력 - 최근 2일]\n"
            "이력 없음. 모든 항목을 category: \"new\", prev_reference: \"\"로 설정하라."
        )

    # 시나리오 B: 기존 항목
    is_scenario_b = existing_items is not None and len(existing_items) > 0
    if is_scenario_b:
        lines = ["[오늘 기존 항목]"]
        for seq, item in enumerate(existing_items, 1):
            facts = item.get("key_facts", [])
            facts_str = ", ".join(facts) if facts else "없음"
            lines.append(
                f"- 항목{seq} | {item['title']} | 요약: {item['summary']} "
                f"| key_facts: [{facts_str}]"
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


def _parse_report_response(message, scenario: str) -> list[dict] | None:
    """tool_use 응답에서 브리핑 결과를 추출한다. 파싱 실패 시 None."""
    for block in message.content:
        if block.type == "tool_use" and block.name == "submit_report":
            raw_results = block.input.get("results", [])
            # LLM이 배열을 JSON 문자열로 반환하는 경우 파싱
            if isinstance(raw_results, str):
                logger.warning("results가 문자열로 반환됨 (%d chars), JSON 파싱 시도", len(raw_results))
                try:
                    raw_results = json.loads(raw_results)
                except (json.JSONDecodeError, ValueError):
                    logger.error("results JSON 파싱 실패")
                    return None
            results = [r for r in raw_results if isinstance(r, dict)]
            if len(results) != len(raw_results):
                logger.warning(
                    "타입 필터링 발생: results %d→%d",
                    len(raw_results), len(results),
                )
            # 원본 데이터가 있는데 필터링 후 전부 소실 → 파싱 실패
            if not results and raw_results:
                return None
            logger.info("브리핑 결과: %d건 (시나리오 %s)", len(results), scenario)
            return results
    # tool_use 블록 없음
    return None


async def analyze_report_articles(
    api_key: str,
    articles: list[dict],
    report_history: list[dict],
    existing_items: list[dict] | None,
    department: str,
) -> list[dict]:
    """Claude API로 기사를 분석하여 브리핑을 생성한다 (tool_use 방식, 파싱 실패 시 최대 2회 재시도).

    Args:
        api_key: Anthropic API 키
        articles: 수집된 기사 목록 (title, publisher, body, originallink, pubDate)
        report_history: 최근 2일치 report_items 이력
        existing_items: 시나리오 B일 때 당일 기존 캐시 항목 (None이면 시나리오 A)
        department: 부서명

    Returns:
        브리핑 항목 리스트. 빈 배열은 유효 (중요 기사 없음 또는 변경 없음).

    Raises:
        RuntimeError: 3회 시도 후에도 파싱 실패 시
    """
    system_prompt = _build_system_prompt(department, existing_items)
    user_prompt = _build_user_prompt(articles, report_history, existing_items)

    is_scenario_b = existing_items is not None and len(existing_items) > 0
    scenario = "B" if is_scenario_b else "A"

    langfuse = get_langfuse()

    for attempt in range(3):
        # 재시도 시 temperature를 올려 동일 실패 패턴 회피
        temperature = 0.0 if attempt == 0 else 0.2

        with langfuse.start_as_current_observation(
            as_type="span", name="report_agent",
            metadata={"department": department, "scenario": scenario, "attempt": attempt + 1},
        ):
            client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=3)
            message = await client.messages.create(
                model="claude-sonnet-4-5-20250929",
                max_tokens=16384,
                temperature=temperature,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
                tools=[_build_report_tool(is_scenario_b)],
                tool_choice={"type": "tool", "name": "submit_report"},
            )

        logger.info(
            "Claude 응답 (attempt %d): stop_reason=%s, input=%d tokens, output=%d tokens",
            attempt + 1, message.stop_reason,
            message.usage.input_tokens, message.usage.output_tokens,
        )

        parsed = _parse_report_response(message, scenario)
        if parsed is not None:
            return parsed

        if attempt < 2:
            logger.warning("파싱 실패 (attempt %d), 재시도", attempt + 1)

    raise RuntimeError("브리핑 응답 파싱 실패 (3회 시도)")
