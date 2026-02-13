"""타사 체크 분석 에이전트.

수집된 기사 목록을 Claude API로 분석하여
[단독]/[주요]/[스킵]을 분류하고, 요약과 판단 근거를 생성한다.
Haiku 사전 필터로 키워드 무관 기사를 제거한 뒤 Sonnet으로 분석한다.
"""

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
        client = anthropic.AsyncAnthropic(api_key=api_key)
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

[취재 영역 - {dept_label}]
{coverage_section}

[기자의 취재 키워드]
{keywords_section}

[키워드 관련성 필터 - 최우선 기준]
아래 기사들은 키워드로 검색된 결과이나, 검색 API 특성상 키워드와 무관한 기사가 포함될 수 있다.
이 필터는 모든 판단(단독, 주요도, 중복 등)보다 먼저 적용된다.
반드시 기사의 주체·대상이 위 키워드에 명시된 기업/기관/인물과 직접 일치하는 경우에만 판단 대상으로 삼는다.
- 같은 업종·분야라도 키워드에 없는 기업/기관의 기사는 관련 없는 것으로 판단한다
  예) "엔비디아" → 삼성전자 반도체, TSMC 등 다른 반도체 기업은 skip
  예) "구글" → 네이버, 카카오 등 다른 IT 기업은 skip
  예) "서울경찰청" → 충북경찰청, 경남경찰청 등 다른 지방청은 skip
  예) "서부지법" → 서울중앙지법, 수원지법 등은 skip
- 키워드 기업/기관이 기사에 부수적으로만 언급되는 경우도 skip
  예) 키워드 "엔비디아" → "삼성전자가 엔비디아向 HBM 납품" → 주체가 삼성전자이므로 skip
- 키워드와 무관한 기사는 [단독] 태그 여부나 기사 가치와 무관하게 반드시 skip 처리한다

[주요 기사 판단 기준 - {dept_label}]
키워드 관련성을 통과한 기사에 한해, 아래 기준으로 판단한다:
{criteria_section}

추가 판단 기준:
- 경쟁 관점: 사실상 단독([단독] 태그 또는 특정 언론사만 보도), 복수 보도(3개 이상 동시 보도), 새로운 앵글
- 사회적 맥락: 진행 중 주요 이슈와 직접 연결, 후속 보도 가능성 높음
- 시의성: 방금 발생/확인된 사건, 오늘/내일 중 결정 예정

[단독 기사 식별]
키워드 관련성 필터를 통과한 기사에 한해 적용한다.
- 제목에 [단독] 태그가 있으면 우선 선정
- 제목에 없더라도 본문에 "OO 취재에 따르면", "본지 취재 결과" 등 '취재에 따르면' 패턴이 있으면 자체 취재 = 사실상 단독
- 본문 어미로 기사 가치 판단:
  · "알려졌다", "전해졌다" → 풍문 수준, 팩트 확인 약함
  · "나타났다", "드러났다" → 객관적 사실·공식 발표
  · "취재에 따르면", "확인됐다" → 자체 취재·신규 팩트, 가장 높은 뉴스 가치

[중복 제거 기준]
1. 동일 배치 내: 같은 사안의 여러 언론사 기사 → 가장 포괄적인 1건을 대표로, 나머지는 merged_indices에 병합
2. 이전 보고 대비: 이력과 동일한 주제면 skip. 추가 디테일(수치, 모델명, 반응, 후속 보도 등)은 새 사안이 아니다.
3. 유일한 중복 예외: 이전 보고 주제라도 [단독] 태그 또는 "취재에 따르면" 패턴이 있는 단독 기사만 재보고

[이전 skip 기사]
이전에 skip된 주제와 동일한 기사는 무조건 skip 유지. 승격 불가.

[제외 기준]
키워드와 관련되더라도 아래에 해당하면 skip한다:
- 홍보성 사용 통계: 기업이 배포한 이용자 수·대화량·다운로드 수 등 마케팅성 수치
- 생활·문화 트렌드: 기술 제품의 일상 활용 사례, 시즌별 이용 패턴 소개
- 보도자료 단순 전재: 비즈니스 임팩트 없이 기업 발표 수치만 나열한 기사
- 단발성 사건·사고: 후속 보도 가능성 낮은 개별 사건
- 정례적 발표: 정부 보도자료, 정기 통계, 일상적 권고
- 인터뷰·칼럼·사설: 기자 의견, 전문가 인터뷰 단독 기사
- 연예·스포츠 가십: 취재 영역과 무관한 연예인·선수 사생활
자기 검증: reason에 '홍보성', '트렌드', '후속 보도 가능성 낮음', '단발성' 등을 적게 된다면 그 기사는 skip 대상이다.

[요약 작성 기준]
- 구체적 정보: "수사가 확대됐다" 대신 "임원 3명을 추가 소환했다"
- 핵심 수치/사실 포함: 인물명, 기관명, 일시 등
- 맥락 제공: 이 뉴스가 왜 중요한지 한 문장으로 짚는다
- 사실 기반 작성, 추측/의견 배제

[출력]
submit_analysis 도구를 사용하여 결과를 제출하라.
모든 기사를 빠짐없이 results 또는 skipped 중 하나에 분류해야 한다.
동일 사안 병합 시 대표 1건만 남기되, 병합된 기사 번호도 빠짐없이 기재한다.

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
    return _SYSTEM_PROMPT_TEMPLATE.format(
        dept_label=dept_label,
        keywords_section=keywords_section,
        coverage_section=coverage_section,
        criteria_section=criteria_section,
    )


def _parse_analysis_response(message) -> list[dict] | None:
    """tool_use 응답에서 분석 결과를 추출한다. 파싱 실패 시 None."""
    for block in message.content:
        if block.type == "tool_use" and block.name == "submit_analysis":
            raw_input = block.input
            raw_results = raw_input.get("results", [])
            raw_skipped = raw_input.get("skipped", [])
            results = [r for r in raw_results if isinstance(r, dict)]
            skipped = [s for s in raw_skipped if isinstance(s, dict)]
            if len(results) != len(raw_results) or len(skipped) != len(raw_skipped):
                logger.warning(
                    "타입 필터링 발생: results %d→%d, skipped %d→%d, raw_keys=%s",
                    len(raw_results), len(results), len(raw_skipped), len(skipped),
                    list(raw_input.keys()),
                )
            # 원본 데이터가 있는데 필터링 후 전부 소실 → 파싱 실패
            if not results and not skipped and (raw_results or raw_skipped):
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
    """Claude API로 기사를 분석한다 (tool_use 방식, 파싱 실패 시 최대 2회 재시도).

    Args:
        api_key: 기자의 Anthropic API 키
        articles: 수집된 기사 목록 (title, publisher, body, url, pubDate)
        history: 최근 24시간 보고 이력
        department: 기자 부서
        keywords: 기자의 취재 키워드 목록

    Returns:
        분석 결과 리스트 (주요 + 스킵 병합).

    Raises:
        RuntimeError: 3회 시도 후에도 파싱 실패 시
    """
    system_prompt = _build_system_prompt(keywords or [], department)
    user_prompt = _build_user_prompt(articles, history, department)

    langfuse = get_langfuse()

    for attempt in range(3):
        # 재시도 시 temperature를 올려 동일 실패 패턴 회피
        temperature = 0.0 if attempt == 0 else 0.2

        with langfuse.start_as_current_observation(
            as_type="span", name="check_agent",
            metadata={"department": department, "attempt": attempt + 1},
        ):
            client = anthropic.AsyncAnthropic(api_key=api_key)
            message = await client.messages.create(
                model="claude-sonnet-4-5-20250929",
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
                if attempt < 2:
                    logger.warning("빈 결과 반환 (기사 %d건, attempt %d), 재시도", len(articles), attempt + 1)
                    continue
                raise RuntimeError("분석 결과 빈 배열 (3회 시도)")
            return parsed

        if attempt < 2:
            logger.warning("파싱 실패 (attempt %d), 재시도", attempt + 1)

    raise RuntimeError("분석 응답 파싱 실패 (3회 시도)")
