"""부서 뉴스 브리핑 에이전트.

네이버 API로 수집된 기사를 LLM 필터(Haiku) + LLM 분석(Claude) 2회 호출로
부서 관련 당일 뉴스를 선별, 분석, 구조화하여 반환한다.
"""

import json
import logging
from datetime import datetime, timezone, timedelta

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
        "key_facts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "당일 새로 발생/확인된 핵심 팩트 배열",
        },
        "exclusive": {
            "type": "boolean",
            "description": "[단독] 태그 또는 특정 언론사만 보도한 기사이면 true",
        },
    }
    required = [
        "title", "source_indices", "summary",
        "reason", "key_facts", "exclusive",
    ]

    if is_scenario_b:
        item_props["action"] = {
            "type": "string",
            "enum": ["modified", "added"],
            "description": "modified=기존 항목에 새 육하원칙 뉴스 발견, added=새로운 사안",
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


_SYSTEM_PROMPT_TEMPLATE = """\
당신은 {dept_label} 데스크의 뉴스 브리핑 보조입니다.
오늘 날짜: {today}
아래 지시사항은 절대적 규칙이다. 자의적 해석이나 예외 판단 없이 각 단계를 문자 그대로 준수하라.
아래 제공된 기사 목록을 분석하여 데스크가 주목할 사안을 선별하고 요약합니다.

[취재 영역 - {dept_label}]
{coverage_section}

[1단계: 뉴스 판단]
'육하원칙과 출처'를 갖춘 요약을 작성할 수 있는 '오늘' 기사만 뉴스다.
- 기사 본문에 사건 발생 시점(날짜, 일시)이 오늘로 명시되어야 뉴스다.
- 기사 게시 시각이 아니라 본문 속 사건 자체의 발생 시점이 '오늘'이어야 한다.
- 본문에 사건 발생 시점(오늘)과 출처가 명확하지 않은 종합·분석·해설 기사는 절대 뉴스가 아니다.

[2단계: 뉴스 가치 판단]
1단계를 통과한 기사에 대해, 데스크가 반드시 알아야 할 가치가 있는지 판단한다.
다음에 하나라도 해당하면 가치 없음으로 제외한다:
- 단발성 사건·사고: 추락, 사망, 교통사고, 화재, 소규모 지진 등 후속 보도 가능성이 낮은 개별 사건
- 정례적 발표·단속: 정부 부처 보도자료, 정기 통계, 일상적 권고·캠페인, 정기 단속 결과
- 단순 일정·예고: 행사 안내, 연휴 당부·안내, 날씨 전망
- 단순 반응·환영·규탄: 법안 통과에 대한 지자체 환영, 성명 발표 등 자체 뉴스 가치 없는 반응
- 관심도 낮은 사안: 소규모 지역 이슈, 단일 기관 내부 사안
- 인터뷰·칼럼·사설: 기자 의견, 전문가 인터뷰 단독 기사
- 재탕·종합 보도: 이미 알려진 팩트를 재구성한 기사, 타 매체 인용 정리 기사
- 연예·스포츠 가십: 부서 취재 영역과 무관한 연예인·선수 사생활

선정 기준 — 위 제외 대상이 아닌 기사 중 아래를 충족하는 것만 선정:
- 복수 언론이 보도하거나, 단독 보도라면 팩트의 무게가 충분할 것
- 후속 보도 가능성이 높거나, 사안 자체가 사회적으로 중대할 것

부서별 주요 기사 기준 — {dept_label} 데스크가 반드시 알아야 할 핵심 사안만 엄선:
{criteria_section}

제외 유형에 해당하는 기사는 reason이 그럴듯해도 반드시 제외한다. 제외 기준은 예외 없이 적용된다.
reason에는 왜 데스크가 이 사안을 알아야 하는지만 기재한다. '당일 보도', '오늘 뉴스' 등 당연한 사실은 쓰지 않는다.

[3단계: 이전 보고 대비]
이전에 보고한 기사와 육하원칙(누가, 언제, 어디서, 무엇을)의 핵심 사실이 동일하면 무조건 제외한다.
새로운 관점, 추가 수치, 업계 반응, 다른 언론사의 해석 등은 새로운 뉴스가 아니다 — 예외 없이 제외.

[4단계: 동일 사안 병합]
같은 사안의 여러 언론사 기사는 source_indices로 묶어 1건으로 보고한다.
  예) 'A 사건' 일반 보도 3건 → 1건으로 병합 (source_indices: [1, 2, 3])

[5단계: 단독 식별]
[단독] 태그가 있는 기사는 항상 선정한다.
  예) 'A 사건' 일반 보도 3건 + [단독] 1건 → 병합 1건 + [단독] 별도 1건

[요약 작성 기준]
육하원칙(누가, 언제, 어디서, 무엇을, 어떻게, 왜)과 사실 출처를 2~3문장 이내의 스트레이트 형식으로 작성.
- 당일 발생한 사실만 요약. 기사 내 과거 경위·배경은 포함하지 않는다
  예) "A가 14일 어디서 B를 발표했다. 앞서 7일 C가 있었다" → 14일 발표만 요약
- 인물명, 기관명, 장소, 수치, 일시 등 구체적 팩트 포함
- 팩트의 출처를 반드시 명기한다. 누가 발표/보도/발언했는지 주어를 생략하지 않는다
  예) "실적이 증가했다" → "A사가 14일 실적 발표에서 매출 N% 증가를 공시했다"
  예) "제재를 검토 중이다" → "미 상무부가 14일 제재 검토를 밝혔다(로이터 보도)"
- LLM의 해석·평가·전망 금지. 판단은 reason 필드에만 기재
- key_facts에는 당일 새로 발생/확인된 팩트만 기록한다

{output_rules_section}"""

_OUTPUT_RULES_A = """\
[출력 규칙 - 첫 생성]
수집된 기사 중 부서 데스크가 반드시 알아야 할 사안만 엄선한다.
- 건수보다 품질 우선. 1단계 뉴스 판단의 제외 대상이면 선정하지 않는다
- reason에 선정 사유를 명시한다 (왜 데스크가 알아야 하는지)
- source_indices로 참조 기사 번호를 기재한다 (URL 역매핑용)
- [단독] 태그 또는 특정 언론사만 보도한 기사는 exclusive: true

위 단계를 모두 통과한 항목만 results에 포함한다.
submit_report 도구로 제출하라."""

_OUTPUT_RULES_B = """\
[출력 규칙 - 업데이트]
수집된 기사를 유저 프롬프트의 [오늘 기존 항목]과 비교하여 변경/추가 사항만 보고한다.
- action: "modified" — 기존 항목의 사안에 새로운 육하원칙의 뉴스가 발견된 경우
- action: "added" — 기존 항목에 없는 새로운 사안
- 기존 항목과 육하원칙이 동일한 기사는 추가 디테일이 있어도 선정하지 않는다
- modified 항목은 item_id(기존 항목 순번)를 반드시 기재한다
- 수정/추가 항목이 없으면 빈 배열을 제출한다

위 단계를 모두 통과한 항목만 results에 포함한다.
submit_report 도구로 제출하라."""


def _build_system_prompt(
    department: str,
    existing_items: list[dict] | None,
) -> str:
    """시스템 프롬프트를 조립한다."""
    dept_label = _dept_label(department)
    profile = DEPARTMENT_PROFILES.get(dept_label, {})
    coverage_section = profile.get("coverage", "")
    criteria = profile.get("criteria", [])
    criteria_section = "\n".join(f"- {c}" for c in criteria)

    is_scenario_b = existing_items is not None and len(existing_items) > 0
    output_rules_section = _OUTPUT_RULES_B if is_scenario_b else _OUTPUT_RULES_A

    today = datetime.now(_KST).strftime("%Y-%m-%d")
    return _SYSTEM_PROMPT_TEMPLATE.format(
        dept_label=dept_label,
        today=today,
        coverage_section=coverage_section,
        criteria_section=criteria_section,
        output_rules_section=output_rules_section,
    )


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
                f"- \"{h['title']}\" | {h['summary']} | key_facts: [{facts_str}] | {created}"
            )
        sections.append("\n".join(lines))
    else:
        sections.append(
            "[이전 보고 이력 - 최근 2일]\n"
            "이력 없음."
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
            # tool schema에서 제거된 필드에 기본값 주입 (DB 호환)
            for r in results:
                r.setdefault("category", "new")
                r.setdefault("prev_reference", "")
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
    """Claude API로 기사를 분석하여 브리핑을 생성한다 (tool_use 방식, 파싱 실패 시 최대 4회 재시도).

    Args:
        api_key: Anthropic API 키
        articles: 수집된 기사 목록 (title, publisher, body, originallink, pubDate)
        report_history: 최근 2일치 report_items 이력
        existing_items: 시나리오 B일 때 당일 기존 캐시 항목 (None이면 시나리오 A)
        department: 부서명

    Returns:
        브리핑 항목 리스트. 빈 배열은 유효 (중요 기사 없음 또는 변경 없음).

    Raises:
        RuntimeError: 5회 시도 후에도 파싱 실패 시
    """
    system_prompt = _build_system_prompt(department, existing_items)
    user_prompt = _build_user_prompt(articles, report_history, existing_items)

    is_scenario_b = existing_items is not None and len(existing_items) > 0
    scenario = "B" if is_scenario_b else "A"

    langfuse = get_langfuse()

    for attempt in range(5):
        # 재시도마다 temperature를 0.1씩 올려 동일 실패 패턴 회피
        temperature = round(attempt * 0.1, 1)

        with langfuse.start_as_current_observation(
            as_type="span", name="report_agent",
            metadata={"department": department, "scenario": scenario, "attempt": attempt + 1},
        ):
            client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=3)
            message = await client.messages.create(
                model="claude-haiku-4-5-20251001",
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

        if attempt < 4:
            logger.warning("파싱 실패 (attempt %d), 재시도", attempt + 1)

    raise RuntimeError("브리핑 응답 파싱 실패 (5회 시도)")
