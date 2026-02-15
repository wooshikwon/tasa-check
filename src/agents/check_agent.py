"""타사 체크 분석 에이전트.

수집된 기사 목록을 Claude API로 분석하여
[단독]/[주요]/[스킵]을 분류하고, 요약과 판단 근거를 생성한다.
Haiku 사전 필터로 키워드 무관 기사를 제거한 뒤 Haiku로 분석한다.
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
    "description": "키워드 관련 기사 번호를 선별합니다",
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
    keywords: list[str],
    department: str,
) -> list[dict]:
    """Haiku LLM으로 키워드 관련 기사를 사전 필터링한다.

    본문 스크래핑 전에 제목+description만으로 판단하여
    키워드 무관 기사, 사진 캡션, 중복 사안, 명백한 홍보성을 제거한다.

    Args:
        api_key: Anthropic API 키
        articles: 언론사/제목 필터 후 기사 리스트 (search_news 반환 형태)
        keywords: 기자의 취재 키워드 목록
        department: 부서명

    Returns:
        필터 통과한 기사 리스트
    """
    if not articles:
        return []

    dept_label = _dept_label(department)
    profile = DEPARTMENT_PROFILES.get(dept_label, {})
    coverage = profile.get("coverage", "")
    keywords_joined = ", ".join(keywords) if keywords else "(키워드 없음)"

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
        f"기자의 취재 키워드: {keywords_joined}\n\n"
        "아래 기사 목록에서 다음 기준으로 기사 번호를 선별하세요:\n"
        "1. 키워드 관련성: 키워드의 기관/기업/인물(관련 인물, 기관장 등 포함)이 등장하는 기사만 포함\n"
        "2. 사진 캡션 제외: 본문 없이 사진 설명만 있는 포토뉴스 제외\n"
        "3. 중복 사안 정리: 같은 사안의 다수 기사 중 정보가 가장 풍부한 기사(최대 3건)만 선별\n"
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


# ── Sonnet 분석 ─────────────────────────────────────────────

_SYSTEM_PROMPT_TEMPLATE = """\
당신은 {dept_label} 기자의 타사 체크 보조입니다.
오늘 날짜: {today}
아래 지시사항은 절대적 규칙이다. 자의적 해석이나 예외 판단 없이 각 단계를 문자 그대로 준수하라.

[취재 영역 - {dept_label}]
{coverage_section}

[기자의 취재 키워드]
{keywords_section}

[1단계: 키워드 관련성 필터]
이 필터는 모든 판단보다 먼저 적용된다.
아래 기사들은 키워드로 검색된 결과이나, 검색 API 특성상 키워드와 무관한 기사가 포함될 수 있다.
반드시 기사의 주체·대상이 위 키워드에 명시된 기업/기관/인물과 직접 일치하는 경우에만 판단 대상으로 삼는다.
- 같은 업종·분야라도 키워드에 없는 기업/기관의 기사는 관련 없는 것으로 판단한다
  예) "엔비디아" → 삼성전자 반도체, TSMC 등 다른 반도체 기업은 skip
  예) "구글" → 네이버, 카카오 등 다른 IT 기업은 skip
  예) "서울경찰청" → 충북경찰청, 경남경찰청 등 다른 지방청은 skip
  예) "서부지법" → 서울중앙지법, 수원지법 등은 skip
- 키워드 기업/기관이 기사에 부수적으로만 언급되는 경우도 skip
  예) 키워드 "엔비디아" → "삼성전자가 엔비디아向 HBM 납품" → 주체가 삼성전자이므로 skip
- 키워드와 무관한 기사는 [단독] 태그 여부나 기사 가치와 무관하게 반드시 skip 처리한다

[2단계: 뉴스 판단]
기사 본문에 사건 발생 시점(날짜, 일시)이 오늘로 명시된 사안만 뉴스로 판단한다.
'언제'는 기사 게시 시각이 아니라 본문 속 사건 자체의 발생 시점이다.
본문에 사건 발생 시점(오늘)이 없는 종합·분석·해설 기사는 뉴스가 아니다.
육하원칙과 출처를 갖춘 요약을 작성할 수 있는 기사만 뉴스다.

[3단계: 뉴스 가치 판단]
2단계를 통과한 기사에 대해, 기자가 반드시 알아야 할 가치가 있는지 판단한다.
다음에 하나라도 해당하면 가치 없음으로 skip한다:
- 단발성 사건·사고: 추락, 사망, 교통사고, 화재, 소규모 지진 등 후속 보도 가능성이 낮은 개별 사건
- 정례적 발표·단속: 정부 부처 보도자료, 정기 통계, 일상적 권고·캠페인, 정기 단속 결과
- 단순 일정·예고: 행사 안내, 연휴 당부·안내, 날씨 전망
- 단순 반응·환영·규탄: 법안 통과에 대한 지자체 환영, 성명 발표 등 자체 뉴스 가치 없는 반응
- 관심도 낮은 사안: 소규모 지역 이슈, 단일 기관 내부 사안
- 인터뷰·칼럼·사설: 기자 의견, 전문가 인터뷰 단독 기사
- 재탕·종합 보도: 이미 알려진 팩트를 재구성한 기사, 타 매체 인용 정리 기사
- 연예·스포츠 가십: 부서 취재 영역과 무관한 연예인·선수 사생활
- 홍보성 사용 통계: 기업이 배포한 이용자 수·대화량·다운로드 수 등 마케팅성 수치
- 생활·문화 트렌드: 기술 제품의 일상 활용 사례, 시즌별 이용 패턴 소개
- 보도자료 단순 전재: 비즈니스 임팩트 없이 기업 발표 수치만 나열한 기사

선정 기준 — 위 제외 대상이 아닌 기사 중 아래를 충족하는 것만 선정:
- 복수 언론이 보도하거나, 단독 보도라면 팩트의 무게가 충분할 것
- 후속 보도 가능성이 높거나, 사안 자체가 사회적으로 중대할 것

부서별 주요 기사 기준:
{criteria_section}

제외 유형에 해당하는 기사는 reason이 그럴듯해도 반드시 skip한다. 제외 기준은 예외 없이 적용된다.
reason에는 왜 이 기사가 주요한지만 기재한다. '당일 보도', '오늘 뉴스' 등 당연한 사실은 쓰지 않는다.

[4단계: 이전 보고 대비]
이전에 보고한 기사와 육하원칙(누가, 언제, 어디서, 무엇을)의 핵심 사실이 동일하면 무조건 skip한다.
새로운 관점, 추가 수치, 업계 반응, 다른 언론사의 해석 등은 새로운 뉴스가 아니다 — 예외 없이 skip.
이전에 이미 skip된 기사가 반복된 것도 당연히 skip 유지.

[5단계: 동일 사안 병합]
같은 사안의 여러 언론사 기사는 가장 포괄적인 1건을 대표로, 나머지는 merged_indices에 병합한다.

[6단계: 단독 식별]
[단독] 태그가 있는 기사는 항상 선정한다.
  예) 'A 사건' 일반 보도 3건 + [단독] 1건 → 병합 1건 + [단독] 별도 1건

[요약 작성 기준]
- 2~3문장 이내로 작성
- 육하원칙(누가, 언제, 어디서, 무엇을, 어떻게, 왜) 스트레이트 형식으로 작성.
- 당일 발생한 사실만 요약. 기사 내 과거 경위·배경은 포함하지 않는다
  예) "A가 14일 어디서 B를 발표했다. 앞서 7일 C가 있었다" → 14일 발표만 요약
- 인물명, 기관명, 장소, 수치, 일시 등 구체적 팩트 포함
- 팩트의 출처를 반드시 명기한다. 누가 발표/보도/발언했는지 주어를 생략하지 않는다
  예) "실적이 증가했다" → "A사가 14일 실적 발표에서 매출 N% 증가를 공시했다"
  예) "제재를 검토 중이다" → "미 상무부가 14일 제재 검토를 밝혔다(로이터 보도)"
- LLM의 해석·평가·전망 금지. 판단은 reason 필드에만 기재

[출력]
submit_analysis 도구를 사용하여 결과를 제출하라.
모든 기사를 빠짐없이 results 또는 skipped 중 하나에 분류해야 한다.
동일 사안 병합 시 대표 1건만 남기되, 병합된 기사 번호도 빠짐없이 기재한다.
key_facts에는 당일 새로 발생/확인된 팩트만 기록. 기사 내 과거 경위는 넣지 않는다.

results 배열 (단독/주요 기사):
- category: "exclusive" (단독) / "important" (주요)
- topic_cluster: 주제 식별자 (짧은 구문)
- source_indices: 대표 기사 번호 ([새로 수집된 기사] 목록 번호)
- merged_indices: 동일 사안으로 병합된 다른 기사 번호 (없으면 빈 배열)
- title: 기사 제목
- summary: 2~3문장 요약
- reason: 주요 판단 근거 1문장
- key_facts: 핵심 팩트 배열

skipped 배열 (스킵 기사):
- topic_cluster: 주제 식별자
- source_indices: 대표 기사 번호 ([새로 수집된 기사] 목록 번호)
- title: 기사 제목
- reason: 스킵 사유
"""

_ANALYSIS_TOOL = {
    "name": "submit_analysis",
    "description": "기사 분석 결과를 제출한다. 모든 기사를 results 또는 skipped에 빠짐없이 분류한다.",
    "input_schema": {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "description": "단독/주요 기사 항목 배열",
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
                            "description": "주요 판단 근거 1문장",
                        },
                        "key_facts": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "핵심 팩트 배열",
                        },
                    },
                    "required": [
                        "category", "topic_cluster", "source_indices",
                        "merged_indices", "title", "summary", "reason", "key_facts",
                    ],
                },
            },
            "skipped": {
                "type": "array",
                "description": "스킵 기사 항목 배열",
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
                            "description": "스킵 사유",
                        },
                    },
                    "required": ["topic_cluster", "source_indices", "title", "reason"],
                },
            },
        },
        "required": ["results", "skipped"],
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
        lines = ["[기자의 최근 보고 이력]"]
        for h in reported_history:
            time_str = _to_kst(h.get("checked_at", ""))
            facts = ", ".join(f"({j}) {f}" for j, f in enumerate(h["key_facts"], 1))
            lines.append(f"- {time_str} 보고: \"{h['topic_cluster']}\"")
            lines.append(f"  확인된 팩트: {facts}")
        sections.append("\n".join(lines))
    else:
        sections.append("[기자의 최근 보고 이력]\n이력 없음")

    if skipped_history:
        lines = ["[이전 skip 이력 - 동일 주제는 새 정보 없이 승격 금지]"]
        for h in skipped_history:
            reason = h.get("reason", "")
            lines.append(f"- \"{h['topic_cluster']}\" → {reason}")
        sections.append("\n".join(lines))

    # 새로 수집된 기사 (번호로 참조, URL은 코드에서 관리)
    lines = ["[새로 수집된 기사]"]
    for i, a in enumerate(articles, 1):
        publisher = a.get("publisher", "")
        title = a.get("title", "")
        body = a.get("body", "")
        pub_date = a.get("pubDate", "")
        lines.append(f"{i}. [{publisher}] {title}")
        if body:
            lines.append(f"   본문(1~3문단): {body}")
        lines.append(f"   시각: {pub_date}")
    sections.append("\n".join(lines))

    sections.append(
        "각 기사에 대해:\n"
        "0. 키워드 관련성 필터: 기사가 위 키워드의 기관/관할과 직접 관련 없으면 즉시 skip\n"
        "1. 중복 제거: 동일 배치 내 병합 + 이전 보고 대비 중복 판단\n"
        "2. [단독] 식별: 제목 태그 또는 사실상 단독 여부\n"
        "3. 중복 아닌 기사에 주요도 판단 (A~D 기준 적용)\n"
        "4. 보고 대상: 요약 + 해당되는 판단 근거 명시"
    )

    return "\n\n".join(sections)


def _build_system_prompt(keywords: list[str], department: str) -> str:
    """키워드와 부서 프로필을 포함한 시스템 프롬프트를 생성한다."""
    dept_label = _dept_label(department)
    profile = DEPARTMENT_PROFILES.get(dept_label, {})
    keywords_section = ", ".join(keywords) if keywords else "(키워드 없음)"
    coverage_section = profile.get("coverage", "")
    criteria = profile.get("criteria", [])
    criteria_section = "\n".join(f"- {c}" for c in criteria)
    today = datetime.now(_KST).strftime("%Y-%m-%d")
    return _SYSTEM_PROMPT_TEMPLATE.format(
        dept_label=dept_label,
        today=today,
        keywords_section=keywords_section,
        coverage_section=coverage_section,
        criteria_section=criteria_section,
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
        history: 최근 24시간 보고 이력
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
