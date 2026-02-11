"""check_agent 단위 테스트.

Claude API 호출은 mock, 프롬프트 조립과 응답 파싱만 검증한다.
"""

import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from src.agents.check_agent import _build_system_prompt, _build_user_prompt, _parse_response, analyze_articles


# --- 프롬프트 조립 ---

def test_build_user_prompt_basic():
    """기본 프롬프트에 기사 정보가 포함된다."""
    articles = [
        {"title": "테스트 기사", "publisher": "조선일보", "body": "본문 내용", "url": "https://ex.com", "pubDate": "2025-01-15 10:00"},
    ]
    prompt = _build_user_prompt(articles, [], [], "사회")
    assert "[새로 수집된 기사]" in prompt
    assert "테스트 기사" in prompt
    assert "조선일보" in prompt
    assert "이력 없음" in prompt


def test_build_user_prompt_with_context():
    """report_items가 있으면 사회적 맥락 섹션이 포함된다."""
    context = [{"title": "수사 확대", "tags": ["서부지검", "수사"]}]
    prompt = _build_user_prompt([], context, [], "사회")
    assert "[당일 사회적 맥락 - 사회부]" in prompt
    assert "#서부지검" in prompt


def test_build_user_prompt_with_history():
    """보고 이력이 있으면 이력 섹션에 포함된다."""
    history = [
        {
            "checked_at": "2025-01-15T14:30:00",
            "topic_cluster": "서부지검 수사",
            "key_facts": ["대표 소환", "회계장부 압수"],
        }
    ]
    prompt = _build_user_prompt([], [], history, "사회")
    assert "서부지검 수사" in prompt
    assert "대표 소환" in prompt


# --- 시스템 프롬프트 ---

def test_build_system_prompt_includes_keywords():
    """키워드가 시스템 프롬프트에 포함된다."""
    prompt = _build_system_prompt(["서부지법", "마포경찰서"])
    assert "서부지법" in prompt
    assert "마포경찰서" in prompt
    assert "키워드 관련성 필터" in prompt


def test_build_system_prompt_no_keywords():
    """키워드가 없으면 '키워드 없음' 표시."""
    prompt = _build_system_prompt([])
    assert "(키워드 없음)" in prompt


# --- 응답 파싱 ---

def test_parse_response_plain_json():
    text = json.dumps([{"category": "important", "title": "테스트"}])
    result = _parse_response(text)
    assert len(result) == 1
    assert result[0]["category"] == "important"


def test_parse_response_code_block():
    """코드블록으로 감싸진 JSON도 파싱한다."""
    text = '```json\n[{"category": "skip"}]\n```'
    result = _parse_response(text)
    assert result[0]["category"] == "skip"


# --- analyze_articles (mock) ---

@pytest.mark.asyncio
async def test_analyze_articles_mock():
    """Claude API를 mock하여 전체 흐름을 검증한다."""
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=json.dumps([
        {
            "category": "exclusive",
            "topic_cluster": "영장 기각",
            "publisher": "연합뉴스",
            "title": "영장 기각",
            "summary": "요약",
            "reason": "근거",
            "key_facts": ["기각"],
            "article_urls": ["https://ex.com"],
            "merged_from": [],
        }
    ]))]

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)

    with patch("src.agents.check_agent.anthropic.AsyncAnthropic", return_value=mock_client):
        results = await analyze_articles(
            api_key="sk-test",
            articles=[{"title": "t", "publisher": "p", "body": "b", "url": "u", "pubDate": "d"}],
            report_context=[],
            history=[],
            department="사회",
        )

    assert len(results) == 1
    assert results[0]["category"] == "exclusive"
    mock_client.messages.create.assert_awaited_once()
