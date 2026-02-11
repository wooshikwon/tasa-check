"""report_agent 단위 테스트.

Claude API 호출은 mock, 프롬프트 조립과 응답 파싱만 검증한다.
"""

import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from src.agents.report_agent import (
    _build_system_prompt,
    _build_user_prompt,
    _parse_response,
    run_report_agent,
)


# --- 시스템 프롬프트 조립 ---

def test_build_system_prompt_scenario_a():
    """시나리오 A: 기존 캐시 없이 프롬프트가 생성된다."""
    prompt = _build_system_prompt("사회", "2026-02-11", ["서부지검", "수사"], None)
    assert "사회부" in prompt
    assert "2026-02-11" in prompt
    assert "#서부지검" in prompt
    assert "당일 첫 요청" in prompt
    assert "기존 캐시" not in prompt


def test_build_system_prompt_scenario_b():
    """시나리오 B: 기존 캐시가 프롬프트에 포함된다."""
    existing = [
        {"id": 1, "title": "수사 확대", "summary": "요약", "tags": ["서부지검"]},
    ]
    prompt = _build_system_prompt("사회", "2026-02-11", [], existing)
    assert "오늘 기존 캐시" in prompt
    assert "id:1" in prompt
    assert "수사 확대" in prompt
    assert "당일 재요청" in prompt


def test_build_system_prompt_no_tags():
    """태그가 없으면 태그 섹션이 생략된다."""
    prompt = _build_system_prompt("정치", "2026-02-11", [], None)
    assert "이전 전달 태그" not in prompt


# --- 사용자 프롬프트 ---

def test_build_user_prompt_scenario_a():
    prompt = _build_user_prompt("사회", "2026년 2월 11일", False)
    assert "사회부" in prompt
    assert "브리핑" in prompt


def test_build_user_prompt_scenario_b():
    prompt = _build_user_prompt("사회", "2026년 2월 11일", True)
    assert "업데이트" in prompt


# --- 응답 파싱 ---

def test_parse_response_plain_json():
    text = json.dumps([{"title": "테스트", "category": "new"}])
    result = _parse_response(text)
    assert len(result) == 1
    assert result[0]["title"] == "테스트"


def test_parse_response_code_block():
    text = '```json\n[{"title": "A", "action": "added"}]\n```'
    result = _parse_response(text)
    assert result[0]["action"] == "added"


def test_parse_response_empty_array():
    """시나리오 B에서 변경 없으면 빈 배열."""
    result = _parse_response("[]")
    assert result == []


# --- run_report_agent (mock) ---

def _make_end_turn_response(items: list[dict]):
    """stop_reason=end_turn인 mock 응답 생성."""
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = json.dumps(items, ensure_ascii=False)

    response = MagicMock()
    response.stop_reason = "end_turn"
    response.content = [text_block]
    return response


def _make_tool_use_response(tool_name: str, tool_input: dict, tool_id: str = "tool_1"):
    """stop_reason=tool_use인 mock 응답 생성."""
    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.name = tool_name
    tool_block.input = tool_input
    tool_block.id = tool_id

    response = MagicMock()
    response.stop_reason = "tool_use"
    response.content = [tool_block]
    return response


@pytest.mark.asyncio
async def test_run_report_agent_scenario_a():
    """시나리오 A: 에이전트 루프 1턴(end_turn)으로 결과 반환."""
    items = [
        {"title": "뉴스1", "url": "u1", "summary": "요약", "tags": ["태그"], "category": "new", "prev_reference": None},
    ]
    mock_response = _make_end_turn_response(items)

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)

    with patch("src.agents.report_agent.anthropic.AsyncAnthropic", return_value=mock_client):
        result = await run_report_agent(
            api_key="sk-test",
            department="사회",
            date="2026-02-11",
            recent_tags=["서부지검"],
            existing_items=None,
        )

    assert len(result) == 1
    assert result[0]["title"] == "뉴스1"


@pytest.mark.asyncio
async def test_run_report_agent_with_fetch_article():
    """fetch_article 도구 호출이 있으면 scraper를 실행하고 루프를 계속한다."""
    # 1턴: fetch_article 요청
    tool_response = _make_tool_use_response("fetch_article", {"url": "https://example.com/1"})
    # 2턴: 최종 응답
    final_items = [
        {"title": "기사", "url": "https://example.com/1", "summary": "요약", "tags": ["태그"], "category": "new", "prev_reference": None},
    ]
    final_response = _make_end_turn_response(final_items)

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(side_effect=[tool_response, final_response])

    with (
        patch("src.agents.report_agent.anthropic.AsyncAnthropic", return_value=mock_client),
        patch("src.agents.report_agent.fetch_article_body", new_callable=AsyncMock, return_value="기사 본문 내용"),
    ):
        result = await run_report_agent(
            api_key="sk-test",
            department="사회",
            date="2026-02-11",
            recent_tags=[],
        )

    assert len(result) == 1
    assert mock_client.messages.create.await_count == 2


@pytest.mark.asyncio
async def test_run_report_agent_scenario_b_empty():
    """시나리오 B에서 변경 없으면 빈 배열 반환."""
    mock_response = _make_end_turn_response([])

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)

    existing = [{"id": 1, "title": "기존", "summary": "요약", "tags": ["태그"]}]
    with patch("src.agents.report_agent.anthropic.AsyncAnthropic", return_value=mock_client):
        result = await run_report_agent(
            api_key="sk-test",
            department="사회",
            date="2026-02-11",
            recent_tags=[],
            existing_items=existing,
        )

    assert result == []
