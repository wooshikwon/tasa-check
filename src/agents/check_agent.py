"""타사 체크 분석 에이전트.

수집된 기사 목록을 Claude API로 분석하여
[단독]/[주요]/[스킵]을 분류하고, 요약과 판단 근거를 생성한다.
Haiku 사전 필터로 부서 무관 기사를 제거한 뒤 Haiku로 분석한다.
"""

import json
import logging
from datetime import datetime, timezone, timedelta

import anthropic
from langfuse import get_client as get_langfuse

from src.config import DEPARTMENT_PROFILES
from src.filters.publisher import get_publisher_name

_KST = timezone(timedelta(hours=9))

logger = logging.getLogger(__name__)


def _dept_label(department: str) -> str:
    """부서명에 '부'가 없으면 붙인다."""
    return department if department.endswith("부") else f"{department}부"


# ── Haiku 사전 필터 ─────────────────────────────────────────

_CHECK_FILTER_TOOL = {
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


async def filter_check_articles(
    api_key: str,
    articles: list[dict],
    department: str,
) -> list[dict]:
    """Haiku LLM으로 부서 관련 기사를 사전 필터링한다.

    본문 스크래핑 전에 제목+description만으로 판단하여
    부서 무관 기사, 사진 캡션, 명백한 홍보성 기사를 제거한다.

    Args:
        api_key: Anthropic API 키
        articles: 언론사/제목 필터 후 기사 리스트 (search_news 반환 형태)
        department: 부서명

    Returns:
        필터 통과한 기사 리스트
    """
    if not articles:
        return []

    dept_label = _dept_label(department)
    profile = DEPARTMENT_PROFILES.get(dept_label, {})
    coverage = profile.get("coverage", "")
    criteria = profile.get("criteria", [])
    criteria_text = "\n".join(f"  - {c}" for c in criteria)

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
        f"취재 영역: {coverage}\n"
        f"주요 기사 기준:\n{criteria_text}\n\n"
        "아래 기사 목록에서 다음 기준으로 기사 번호를 선별하세요:\n"
        "1. 부서 관련성: 해당 부서 취재 영역에 해당하는 기사만 포함\n"
        "2. 사진 캡션 제외: 본문 없이 사진 설명만 있는 포토뉴스 제외\n"
        "3. 명백한 홍보성 제외: 보도자료를 그대로 옮긴 제품·서비스 출시 소개, 기업·기관의 자체 수상·CSR 활동 홍보, 할인·이벤트 안내 등. 단, 대규모 투자·M&A·정책 변화를 수반하는 발표는 제외하지 않는다\n"
        "4. 애매한 경우 포함 쪽으로 판단\n\n"
        "filter_news 도구로 선별된 기사 번호를 제출하세요."
    )

    langfuse = get_langfuse()
    with langfuse.start_as_current_observation(
        as_type="span", name="check_filter",
        metadata={"department": department, "input_count": len(articles)},
    ):
        client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=3)
        message = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            temperature=0.0,
            system=system_prompt,
            messages=[{"role": "user", "content": article_list_text}],
            tools=[_CHECK_FILTER_TOOL],
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


# ── Haiku 분석 ──────────────────────────────────────────────

_SYSTEM_PROMPT_TEMPLATE = """\
당신은 {dept_label} 기자의 타사 체크 보조입니다.
오늘 날짜: {today}

<analysis_rules> → <summary_rules> → <output_format> 순서로 주어진 <articles>를 처리하라.
분석 규칙은 step_1부터 순서대로 적용하며, 앞 단계에서 skip된 기사는 이후 단계를 평가하지 않는다.
아래 지시사항은 절대적 규칙이다. 자의적 해석이나 예외 판단 없이 각 단계를 문자 그대로 준수하라.

<analysis_rules>
<step_1>
키워드 관련성이 없으면 skip
이 필터는 모든 판단보다 먼저 적용된다.
기자의 취재 키워드: {keywords_section}

아래 기사들은 위 키워드로 검색된 결과이나, 검색 API 특성상 키워드와 무관한 기사가 포함될 수 있다.
기사의 주체·대상이 키워드에 명시된 기업/기관/인물과 직접 일치하는 경우에만 판단 대상으로 삼는다.
  1-1) 같은 업종·분야라도 키워드에 없는 기업/기관의 기사는 관련 없는 것으로 판단해 skip한다.
    예) "엔비디아" → 삼성전자 반도체, TSMC 등 다른 반도체 기업은 skip
    예) "구글" → 네이버, 카카오 등 다른 IT 기업은 skip
    예) "서울경찰청" → 충북경찰청, 경남경찰청 등 다른 지방청은 skip
    예) "서부지법" → 서울중앙지법, 수원지법 등은 skip
  1-2) 키워드 기업/기관이 기사에 부수적으로만 언급되는 경우도 skip
    예) 키워드 "엔비디아" → "삼성전자가 엔비디아向 HBM 납품" → 주체가 삼성전자이므로 skip
  1-3) 키워드와 무관한 기사는 [단독] 태그 여부나 기사 가치와 무관하게 반드시 skip 처리한다
</step_1>

<step_2>
본문에 오늘 발생한 팩트가 없으면 skip
기사 본문에 "N일 A가 OO했다" 등 특정 주체의 오늘({today})자 팩트가 명시된 경우만 뉴스다.
  2-1) 본문에 "N일 A가 OO했다" 형식이 없는 종합·분석·해설 기사는 오늘 뉴스가 아니므로 skip
  2-2) 본문에 '지난 00일' 팩트만 있고, 오늘 날짜의 팩트가 없는 경우에도 오늘 뉴스가 아니므로 skip
  2-3) 단, 오늘자 외신 인용("N일 로이터/NYT가 보도했다")이면, 오늘 날짜의 팩트가 없어도 당일 뉴스로 인정한다.
</step_2>

<step_3>
뉴스 가치가 부족하면 skip
다음에 하나라도 해당하면 가치 없음으로 skip한다:
  3-1) 단발성 사건·사고: 후속 보도 가능성이 낮은 단순 추락, 사망, 교통사고, 화재, 소규모 지진 등 개별 사건
  3-2) 단순 현황·통계·트렌드 발표: 정부 부처/기업 보도자료, 특정 기간 내 수치를 집계한 트렌드 통계 보도
  3-3) 인터뷰·칼럼·사설: 기자 의견, 전문가 인터뷰 기사
  3-4) 연예·스포츠 가십: 부서 취재 영역과 무관한 연예인·선수 사생활
  3-5) 생활·문화 트렌드: 기술 제품의 일상 활용 사례, 시즌별 이용 패턴 소개
</step_3>

<step_4>
보고 이력 중복 skip
이전에 보고 및 skip한 이력과 중복된 내용의 기사는 다시 보고하지 않는다:
  4-1) 이미 체크한 기사와 육하원칙(누가, 언제, 어디서, 무엇을)의 핵심 사실이 동일하면 skip한다.
  4-2) 현재 기사에 새로운 관점, 업계 반응, 추가적인 사실이 존재하더라도, 이전 보고한 기사와 핵심 육하원칙이 동일하면 skip한다.
  4-3) 이전에 이미 skip된 기사가 반복 등장한 것도 당연히 skip 유지.
</step_4>

<step_5>
results 분류 판단
위 skip 대상이 아닌 기사 중 아래를 충족하는 것만 results에 분류:
  5-1) 기사 원문에 "N일 A가 OO했다" 등 특정 주체의 오늘({today}) 행위가 명시되어 있을 것
  5-2) 복수 언론이 보도하거나, 단독 보도라면 팩트의 무게가 충분할 것
  5-3) 단발성 내용이 아니며, 뉴스 가치 판단에 비추어 사안이 중대해 '후속 보도 가능성'이 높을 것
</step_5>

</analysis_rules>

<summary_rules>
2~3문장 이내로, 날짜·주체·행위·출처를 명확히 포함한 육하원칙 스트레이트 형식으로 작성한다.
  - 첫 문장에 "누가 N일 무엇을 했다"를 포함한다.
  - 오늘({today}) 발생한 사실만 요약한다. 과거 경위는 포함하지 않는다.
  - 인물명, 기관명, 장소, 수치, 일시 등 구체적 팩트를 포함한다.
  - 외신 인용 시 출처를 명기한다: "~라고 N일 로이터가 보도했다"
  - "보도되었다/알려졌다" 등 주어 없는 피동형은 쓰지 않는다.
  - 해석·평가·전망을 넣지 않는다. 판단은 reason 필드에만 기재한다.
</summary_rules>

<output_format>
submit_analysis 도구를 사용하여 결과를 제출하라.
모든 기사를 빠짐없이 results 또는 skipped 중 하나에 분류해야 한다.

동일 사안 병합:
  - 같은 사안의 여러 언론사 기사는 가장 포괄적인 1건을 대표로, 나머지는 merged_indices에 병합한다.
  - [단독] 기사는 별도 분류한다. 예) 'A 사건' 일반 보도 3건 + [단독] 1건 → 병합 1건 + [단독] 별도 1건
  - 병합으로 흡수된 기사는 skipped에 넣지 않는다.

results 배열 (단독/주요 기사):
  - category: "exclusive" (단독) / "important" (주요)
  - topic_cluster: 주제 식별자 (짧은 구문)
  - source_indices: 대표 기사 번호 (수집된 기사 목록 번호)
  - merged_indices: 동일 사안으로 병합된 다른 기사 번호 (없으면 빈 배열)
  - title: 기사 제목
  - summary: 2~3문장 요약
  - reason: 판단 근거 1~2문장

skipped 배열 (스킵 기사):
  - topic_cluster: 주제 식별자
  - source_indices: 대표 기사 번호 (수집된 기사 목록 번호)
  - title: 기사 제목
  - reason: 스킵 사유 1~2문장
</output_format>"""

_ANALYSIS_TOOL = {
    "name": "submit_analysis",
    "description": "기사 분석 결과를 제출한다. 모든 기사를 results 또는 skipped에 빠짐없이 분류한다.",
    "input_schema": {
        "type": "object",
        "properties": {
            "thinking": {
                "type": "string",
                "description": (
                    "기사별 판단 과정. skip 시 해당 단계에서 끝, 전체 pass 시 s5까지 기록. "
                    "기사 구분은 |"
                    "\n예: 기사1: s1:skip-키워드무관 | "
                    "기사2: s1:pass, s2:skip-오늘자팩트없음 | "
                    "기사3: s1:pass, s2:pass-16일자체포, s3:pass-후속가능, "
                    "s4:pass-신규사안, s5:pass-중대"
                ),
            },
            "results": {
                "type": "array",
                "description": "s1~s5 전체 통과한 기사 배열 (동일 사안은 대표 1건만, 나머지는 merged_indices에 기재)",
                "items": {
                    "type": "object",
                    "properties": {
                        "category": {
                            "type": "string",
                            "enum": ["exclusive", "important"],
                        },
                        "topic_cluster": {
                            "type": "string",
                            "description": "주제 식별자 (짧은 구문)",
                        },
                        "source_indices": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": "대표 기사 번호",
                        },
                        "merged_indices": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": "동일 사안으로 병합된 다른 기사 번호 (없으면 빈 배열)",
                        },
                        "title": {"type": "string"},
                        "summary": {
                            "type": "string",
                            "description": "2~3문장 요약",
                        },
                        "reason": {
                            "type": "string",
                            "description": "판단 근거 1~2문장",
                        },
                    },
                    "required": [
                        "category", "topic_cluster", "source_indices",
                        "merged_indices", "title", "summary", "reason",
                    ],
                },
            },
            "skipped": {
                "type": "array",
                "description": "s1~s5 중 하나라도 skip된 기사 배열 (병합으로 흡수된 기사는 여기에 넣지 않는다)",
                "items": {
                    "type": "object",
                    "properties": {
                        "topic_cluster": {
                            "type": "string",
                            "description": "주제 식별자 (짧은 구문)",
                        },
                        "source_indices": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": "대표 기사 번호",
                        },
                        "title": {"type": "string"},
                        "reason": {
                            "type": "string",
                            "description": "스킵 사유 1~2문장",
                        },
                    },
                    "required": ["topic_cluster", "source_indices", "title", "reason"],
                },
            },
        },
        "required": ["thinking", "results", "skipped"],
    },
}


def _to_kst(iso_str: str) -> str:
    """UTC ISO 문자열을 KST 'YYYY-MM-DD HH:MM' 형식으로 변환한다."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str).replace(tzinfo=timezone.utc)
        return dt.astimezone(_KST).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return iso_str[:16]


def _build_user_prompt(
    articles: list[dict],
    history: list[dict],
    department: str,
) -> str:
    """사용자 프롬프트를 조립한다."""
    sections = []

    # 보고 이력 (보고 + skip 분리)
    reported_history = [h for h in history if h["category"] != "skip"]
    skipped_history = [h for h in history if h["category"] == "skip"]

    if reported_history:
        lines = ["<report_history>\n핵심 팩트가 동일한 기사는 재보고하지 않는다"]
        for h in reported_history:
            time_str = _to_kst(h.get("checked_at", ""))
            summary = h.get("summary", "")
            lines.append(f"- {time_str} 보고: \"{h['topic_cluster']}\"")
            lines.append(f"  요약: {summary}")
        lines.append("</report_history>")
        sections.append("\n".join(lines))
    else:
        sections.append("<report_history>\n이력 없음\n</report_history>")

    if skipped_history:
        lines = ["<skip_history>\n핵심 팩트가 동일한 기사는 재보고하지 않는다"]
        for h in skipped_history:
            reason = h.get("reason", "")
            lines.append(f"- \"{h['topic_cluster']}\" → {reason}")
        lines.append("</skip_history>")
        sections.append("\n".join(lines))

    # 수집된 기사 (번호로 참조, URL은 코드에서 관리)
    lines = ["<articles>"]
    for i, a in enumerate(articles, 1):
        publisher = a.get("publisher", "")
        title = a.get("title", "")
        body = a.get("body", "")
        pub_date = a.get("pubDate", "")
        lines.append(f"{i}. [{publisher}] {title}")
        if body:
            lines.append(f"   본문(1~3문단): {body}")
        lines.append(f"   시각: {pub_date}")
    lines.append("</articles>")
    sections.append("\n".join(lines))

    sections.append("위 기사를 분석하여 submit_analysis로 제출하라.")

    return "\n\n".join(sections)


def _build_system_prompt(keywords: list[str], department: str) -> str:
    """키워드를 포함한 시스템 프롬프트를 생성한다."""
    dept_label = _dept_label(department)
    keywords_section = ", ".join(keywords) if keywords else "(키워드 없음)"
    today = datetime.now(_KST).strftime("%Y-%m-%d")
    return _SYSTEM_PROMPT_TEMPLATE.format(
        dept_label=dept_label,
        today=today,
        keywords_section=keywords_section,
    )


def _try_parse_json_field(raw, field_name: str):
    """tool_use 응답 필드가 문자열인 경우 JSON 파싱을 시도한다."""
    if isinstance(raw, str):
        logger.warning("%s가 문자열로 반환됨 (%d chars), JSON 파싱 시도", field_name, len(raw))
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            logger.error("%s JSON 파싱 실패", field_name)
            return None
    return raw


def _parse_analysis_response(message) -> list[dict] | None:
    """tool_use 응답에서 분석 결과를 추출한다. 파싱 실패 시 None."""
    for block in message.content:
        if block.type == "tool_use" and block.name == "submit_analysis":
            raw_input = block.input
            raw_results = raw_input.get("results", [])
            raw_skipped = raw_input.get("skipped", [])
            # LLM이 배열을 JSON 문자열로 반환하는 경우 파싱
            parsed_results = _try_parse_json_field(raw_results, "results")
            parsed_skipped = _try_parse_json_field(raw_skipped, "skipped")
            if parsed_results is None or parsed_skipped is None:
                return None
            results = [r for r in parsed_results if isinstance(r, dict)]
            skipped = [s for s in parsed_skipped if isinstance(s, dict)]
            if len(results) != len(parsed_results) or len(skipped) != len(parsed_skipped):
                logger.warning(
                    "타입 필터링 발생: results %d→%d, skipped %d→%d, raw_keys=%s",
                    len(parsed_results), len(results), len(parsed_skipped), len(skipped),
                    list(raw_input.keys()),
                )
            # 원본 데이터가 있는데 필터링 후 전부 소실 → 파싱 실패
            if not results and not skipped and (parsed_results or parsed_skipped):
                return None
            if not results and not skipped:
                logger.warning("빈 결과 반환됨, tool input keys=%s", list(raw_input.keys()))
            for s in skipped:
                s["category"] = "skip"
            combined = results + skipped
            logger.info("분석 결과: 주요 %d건, 스킵 %d건", len(results), len(skipped))
            return combined
    # tool_use 블록 없음
    return None


async def analyze_articles(
    api_key: str,
    articles: list[dict],
    history: list[dict],
    department: str,
    keywords: list[str] | None = None,
) -> list[dict]:
    """Claude API로 기사를 분석한다 (tool_use 방식, 파싱 실패 시 최대 4회 재시도).

    Args:
        api_key: 기자의 Anthropic API 키
        articles: 수집된 기사 목록 (title, publisher, body, url, pubDate)
        history: 최근 72시간 보고 이력
        department: 기자 부서
        keywords: 기자의 취재 키워드 목록

    Returns:
        분석 결과 리스트 (주요 + 스킵 병합).

    Raises:
        RuntimeError: 5회 시도 후에도 파싱 실패 시
    """
    system_prompt = _build_system_prompt(keywords or [], department)
    user_prompt = _build_user_prompt(articles, history, department)

    langfuse = get_langfuse()

    for attempt in range(5):
        # 재시도마다 temperature를 0.1씩 올려 동일 실패 패턴 회피
        temperature = round(attempt * 0.1, 1)

        with langfuse.start_as_current_observation(
            as_type="span", name="check_agent",
            metadata={"department": department, "attempt": attempt + 1},
        ):
            client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=3)
            message = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=16384,
                temperature=temperature,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
                tools=[_ANALYSIS_TOOL],
                tool_choice={"type": "tool", "name": "submit_analysis"},
            )

        logger.info(
            "Claude 응답 (attempt %d): stop_reason=%s, input=%d tokens, output=%d tokens",
            attempt + 1, message.stop_reason,
            message.usage.input_tokens, message.usage.output_tokens,
        )

        parsed = _parse_analysis_response(message)
        if parsed is not None:
            # 기사가 제공됐는데 빈 결과 → 이상 응답 (모든 기사는 분류되어야 함)
            if not parsed:
                if attempt < 4:
                    logger.warning("빈 결과 반환 (기사 %d건, attempt %d), 재시도", len(articles), attempt + 1)
                    continue
                raise RuntimeError("분석 결과 빈 배열 (5회 시도)")
            return parsed

        if attempt < 4:
            logger.warning("파싱 실패 (attempt %d), 재시도", attempt + 1)

    raise RuntimeError("분석 응답 파싱 실패 (5회 시도)")
