"""check_agent 단위 테스트.

Claude API 호출은 mock, 프롬프트 조립과 응답 파싱만 검증한다.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from src.agents.check_agent import (
    _build_system_prompt, _build_user_prompt,
    _parse_analysis_response, analyze_articles,
)


# --- 프롬프트 조립 ---

def test_build_user_prompt_basic():
    """기본 프롬프트에 기사 정보가 포함된다."""
    articles = [
        {"title": "테스트 기사", "publisher": "조선일보", "body": "본문 내용", "url": "https://ex.com", "pubDate": "2025-01-15 10:00"},
    ]
    prompt = _build_user_prompt(articles, [], "사회")
    assert "[새로 수집된 기사]" in prompt
    assert "테스트 기사" in prompt
    assert "조선일보" in prompt
    assert "이력 없음" in prompt


def test_build_user_prompt_with_history():
    """보고 이력이 있으면 이력 섹션에 포함된다."""
    history = [
        {
            "checked_at": "2025-01-15T14:30:00",
            "topic_cluster": "서부지검 수사",
            "key_facts": ["대표 소환", "회계장부 압수"],
            "category": "important",
        }
    ]
    prompt = _build_user_prompt([], history, "사회")
    assert "서부지검 수사" in prompt
    assert "대표 소환" in prompt


def test_build_user_prompt_with_skip_history():
    """skip 이력이 있으면 별도 섹션에 포함된다."""
    history = [
        {
            "checked_at": "2025-01-15T14:30:00",
            "topic_cluster": "소규모 사건",
            "key_facts": [],
            "category": "skip",
            "reason": "단발성 사건",
        }
    ]
    prompt = _build_user_prompt([], history, "사회")
    assert "이전 skip 이력" in prompt
    assert "소규모 사건" in prompt


# --- 시스템 프롬프트 ---

def test_build_system_prompt_includes_keywords():
    """키워드가 시스템 프롬프트에 포함된다."""
    prompt = _build_system_prompt(["서부지법", "마포경찰서"], "사회부")
    assert "서부지법" in prompt
    assert "마포경찰서" in prompt
    assert "키워드 관련성 필터" in prompt


def test_build_system_prompt_no_keywords():
    """키워드가 없으면 '키워드 없음' 표시."""
    prompt = _build_system_prompt([], "사회부")
    assert "(키워드 없음)" in prompt


def test_build_system_prompt_dept_profile():
    """부서 프로필(취재 영역, 판단 기준)이 포함된다."""
    prompt = _build_system_prompt([], "사회부")
    assert "사회부" in prompt
    assert "사건·사고" in prompt


# --- 응답 파싱 ---

def _make_tool_use_message(results, skipped):
    """submit_analysis tool_use 응답 mock 생성."""
    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.name = "submit_analysis"
    tool_block.input = {"results": results, "skipped": skipped}

    message = MagicMock()
    message.content = [tool_block]
    return message


def test_parse_analysis_response_normal():
    """정상 응답에서 results + skipped 병합."""
    results = [{"category": "important", "title": "기사1", "topic_cluster": "t"}]
    skipped = [{"title": "기사2", "topic_cluster": "s", "reason": "무관"}]
    msg = _make_tool_use_message(results, skipped)

    parsed = _parse_analysis_response(msg)
    assert len(parsed) == 2
    assert parsed[0]["category"] == "important"
    assert parsed[1]["category"] == "skip"


def test_parse_analysis_response_type_filtering():
    """문자열 등 비정상 타입은 필터링된다."""
    results = [{"category": "important", "title": "정상"}]
    skipped = ["문자열이면 필터됨", {"title": "정상 스킵", "reason": "이유"}]
    msg = _make_tool_use_message(results, skipped)

    parsed = _parse_analysis_response(msg)
    assert len(parsed) == 2  # dict만 남음


def test_parse_analysis_response_all_filtered_returns_none():
    """원본 데이터가 있지만 모두 필터링되면 None (파싱 실패)."""
    results = ["string1", "string2"]
    skipped = ["string3"]
    msg = _make_tool_use_message(results, skipped)

    parsed = _parse_analysis_response(msg)
    assert parsed is None


def test_parse_analysis_response_genuine_empty():
    """원본도 비어있으면 빈 배열 반환 (파싱 실패 아님)."""
    msg = _make_tool_use_message([], [])
    parsed = _parse_analysis_response(msg)
    assert parsed == []


def test_parse_analysis_response_no_tool_use():
    """tool_use 블록이 없으면 None."""
    text_block = MagicMock()
    text_block.type = "text"
    message = MagicMock()
    message.content = [text_block]

    parsed = _parse_analysis_response(message)
    assert parsed is None


# --- analyze_articles (mock) ---

@pytest.mark.asyncio
async def test_analyze_articles_normal():
    """정상 tool_use 응답 시 결과를 반환한다."""
    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.name = "submit_analysis"
    tool_block.input = {
        "results": [
            {
                "category": "exclusive",
                "topic_cluster": "영장 기각",
                "source_indices": [1],
                "merged_indices": [],
                "title": "영장 기각",
                "summary": "요약",
                "reason": "근거",
                "key_facts": ["기각"],
            }
        ],
        "skipped": [
            {
                "topic_cluster": "소규모 사건",
                "source_indices": [2],
                "title": "소규모",
                "reason": "단발성",
            }
        ],
    }

    response = MagicMock()
    response.stop_reason = "tool_use"
    response.content = [tool_block]
    response.usage = MagicMock(input_tokens=1000, output_tokens=500)

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=response)

    with patch("src.agents.check_agent.anthropic.AsyncAnthropic", return_value=mock_client):
        results = await analyze_articles(
            api_key="sk-test",
            articles=[
                {"title": "t1", "publisher": "p1", "body": "b1", "url": "u1", "pubDate": "d1"},
                {"title": "t2", "publisher": "p2", "body": "b2", "url": "u2", "pubDate": "d2"},
            ],
            history=[],
            department="사회",
        )

    assert len(results) == 2
    assert results[0]["category"] == "exclusive"
    assert results[1]["category"] == "skip"
    mock_client.messages.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_analyze_articles_retry_on_parse_failure():
    """파싱 실패 시 1회 재시도하여 성공하면 결과를 반환한다."""
    # 1차: 모든 항목이 문자열 (파싱 실패)
    bad_block = MagicMock()
    bad_block.type = "tool_use"
    bad_block.name = "submit_analysis"
    bad_block.input = {"results": ["string"], "skipped": ["string"]}

    bad_response = MagicMock()
    bad_response.stop_reason = "tool_use"
    bad_response.content = [bad_block]
    bad_response.usage = MagicMock(input_tokens=1000, output_tokens=500)

    # 2차: 정상 응답
    good_block = MagicMock()
    good_block.type = "tool_use"
    good_block.name = "submit_analysis"
    good_block.input = {
        "results": [{"category": "important", "topic_cluster": "t", "source_indices": [1],
                      "merged_indices": [], "title": "t", "summary": "s", "reason": "r", "key_facts": []}],
        "skipped": [],
    }

    good_response = MagicMock()
    good_response.stop_reason = "tool_use"
    good_response.content = [good_block]
    good_response.usage = MagicMock(input_tokens=1000, output_tokens=500)

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(side_effect=[bad_response, good_response])

    with patch("src.agents.check_agent.anthropic.AsyncAnthropic", return_value=mock_client):
        results = await analyze_articles(
            api_key="sk-test",
            articles=[{"title": "t", "publisher": "p", "body": "b", "url": "u", "pubDate": "d"}],
            history=[],
            department="사회",
        )

    assert len(results) == 1
    assert mock_client.messages.create.call_count == 2


@pytest.mark.asyncio
async def test_analyze_articles_raises_on_persistent_failure():
    """재시도 후에도 파싱 실패하면 RuntimeError 발생."""
    bad_block = MagicMock()
    bad_block.type = "tool_use"
    bad_block.name = "submit_analysis"
    bad_block.input = {"results": ["string"], "skipped": ["string"]}

    bad_response = MagicMock()
    bad_response.stop_reason = "tool_use"
    bad_response.content = [bad_block]
    bad_response.usage = MagicMock(input_tokens=1000, output_tokens=500)

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=bad_response)

    with patch("src.agents.check_agent.anthropic.AsyncAnthropic", return_value=mock_client):
        with pytest.raises(RuntimeError, match="파싱 실패"):
            await analyze_articles(
                api_key="sk-test",
                articles=[{"title": "t", "publisher": "p", "body": "b", "url": "u", "pubDate": "d"}],
                history=[],
                department="사회",
            )

    assert mock_client.messages.create.call_count == 3


@pytest.mark.asyncio
async def test_analyze_articles_raises_on_empty_results():
    """기사가 있는데 빈 배열 반환 시 재시도 후 RuntimeError 발생."""
    empty_block = MagicMock()
    empty_block.type = "tool_use"
    empty_block.name = "submit_analysis"
    empty_block.input = {"results": [], "skipped": []}

    empty_response = MagicMock()
    empty_response.stop_reason = "tool_use"
    empty_response.content = [empty_block]
    empty_response.usage = MagicMock(input_tokens=1000, output_tokens=500)

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=empty_response)

    with patch("src.agents.check_agent.anthropic.AsyncAnthropic", return_value=mock_client):
        with pytest.raises(RuntimeError, match="빈 배열"):
            await analyze_articles(
                api_key="sk-test",
                articles=[{"title": "t", "publisher": "p", "body": "b", "url": "u", "pubDate": "d"}],
                history=[],
                department="사회",
            )

    assert mock_client.messages.create.call_count == 3
