"""report_agent 단위 테스트.

Claude API 호출은 mock, 프롬프트 조립과 tool_use 응답 처리만 검증한다.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from src.agents.report_agent import (
    _build_system_prompt,
    _build_user_prompt,
    _dept_label,
    analyze_report_articles,
)


# --- _dept_label ---

def test_dept_label_without_suffix():
    assert _dept_label("사회") == "사회부"


def test_dept_label_with_suffix():
    assert _dept_label("사회부") == "사회부"


# --- 시스템 프롬프트 조립 ---

def test_build_system_prompt_scenario_a():
    """시나리오 A: 기존 캐시 없이 프롬프트가 생성된다."""
    prompt = _build_system_prompt("사회", None)
    assert "사회부" in prompt
    assert "데스크의 뉴스 브리핑 보조" in prompt
    assert "취재 영역" in prompt
    assert "첫 생성" in prompt
    assert "기존 캐시" not in prompt


def test_build_system_prompt_scenario_b():
    """시나리오 B: 기존 캐시가 프롬프트에 포함된다."""
    existing = [
        {"id": 1, "title": "수사 확대", "summary": "요약", "key_facts": ["대표 소환"]},
    ]
    prompt = _build_system_prompt("사회", existing)
    assert "오늘 기존 캐시" in prompt
    assert "id:1" in prompt
    assert "수사 확대" in prompt
    assert "업데이트" in prompt


def test_build_system_prompt_coverage():
    """부서 프로필의 취재 영역이 포함된다."""
    prompt = _build_system_prompt("사회부", None)
    assert "사건·사고" in prompt


def test_build_system_prompt_criteria():
    """부서 프로필의 판단 기준이 포함된다."""
    prompt = _build_system_prompt("사회부", None)
    assert "중요 기사 판단 기준" in prompt


def test_build_system_prompt_exclusion():
    """제외 기준이 포함된다."""
    prompt = _build_system_prompt("사회부", None)
    assert "제외 기준" in prompt
    assert "단발성 사건·사고" in prompt


def test_build_system_prompt_empty_existing():
    """빈 existing_items는 시나리오 A로 처리된다."""
    prompt = _build_system_prompt("사회", [])
    assert "첫 생성" in prompt
    assert "기존 캐시" not in prompt


# --- 사용자 프롬프트 ---

def test_build_user_prompt_articles():
    """기사 목록이 번호와 함께 포함된다."""
    articles = [
        {"title": "테스트 기사", "publisher": "조선일보", "body": "본문", "pubDate": "2026-02-12 14:00"},
    ]
    prompt = _build_user_prompt(articles, [], None)
    assert "[수집된 기사 목록]" in prompt
    assert "1. [조선일보] 테스트 기사" in prompt
    assert "본문: 본문" in prompt


def test_build_user_prompt_no_history():
    """이력이 없으면 안내 문구가 포함된다."""
    prompt = _build_user_prompt([], [], None)
    assert "이력 없음" in prompt
    assert "category: \"new\"" in prompt


def test_build_user_prompt_with_history():
    """이전 보고 이력이 key_facts와 함께 포함된다."""
    history = [
        {"title": "수사 확대", "summary": "요약", "key_facts": ["대표 소환"], "category": "new", "created_at": "2026-02-11 10:00"},
    ]
    prompt = _build_user_prompt([], history, None)
    assert "이전 보고 이력" in prompt
    assert "수사 확대" in prompt
    assert "대표 소환" in prompt


def test_build_user_prompt_scenario_b():
    """시나리오 B: 기존 캐시 항목이 key_facts와 함께 포함된다."""
    existing = [
        {"id": 1, "title": "기존 기사", "summary": "기존 요약", "key_facts": ["핵심 팩트"]},
    ]
    prompt = _build_user_prompt([], [], existing)
    assert "오늘 기존 캐시 항목" in prompt
    assert "id:1" in prompt
    assert "기존 기사" in prompt


def test_build_user_prompt_scenario_a_instruction():
    """시나리오 A: 선별 지시가 포함된다."""
    prompt = _build_user_prompt([], [], None)
    assert "선별" in prompt


def test_build_user_prompt_scenario_b_instruction():
    """시나리오 B: 비교 지시가 포함된다."""
    existing = [{"id": 1, "title": "t", "summary": "s", "key_facts": []}]
    prompt = _build_user_prompt([], [], existing)
    assert "비교" in prompt


# --- analyze_report_articles (mock) ---

def _make_tool_use_response(results: list[dict]):
    """submit_report tool_use mock 응답 생성."""
    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.name = "submit_report"
    tool_block.input = {"results": results}

    response = MagicMock()
    response.stop_reason = "tool_use"
    response.content = [tool_block]
    response.usage = MagicMock(input_tokens=1000, output_tokens=500)
    return response


@pytest.mark.asyncio
async def test_analyze_report_articles_scenario_a():
    """시나리오 A: Claude 1회 호출로 브리핑 결과를 반환한다."""
    items = [
        {
            "title": "뉴스1", "source_indices": [1],
            "summary": "요약", "reason": "사유",
            "category": "new", "key_facts": ["핵심 팩트"],
            "exclusive": False, "prev_reference": None,
        },
    ]
    mock_response = _make_tool_use_response(items)

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)

    with patch("src.agents.report_agent.anthropic.AsyncAnthropic", return_value=mock_client):
        result = await analyze_report_articles(
            api_key="sk-test",
            articles=[{"title": "t", "publisher": "p", "body": "b", "originallink": "u", "pubDate": "d"}],
            report_history=[],
            existing_items=None,
            department="사회",
        )

    assert len(result) == 1
    assert result[0]["title"] == "뉴스1"
    assert result[0]["reason"] == "사유"
    mock_client.messages.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_analyze_report_articles_scenario_b():
    """시나리오 B: modified/added 항목을 반환한다."""
    items = [
        {
            "action": "modified", "item_id": 1,
            "title": "수정 기사", "source_indices": [2],
            "summary": "갱신 요약", "reason": "새 팩트",
            "category": "follow_up", "key_facts": ["대표 소환", "추가 기소"],
            "exclusive": False, "prev_reference": '2026-02-11 "원본"',
        },
        {
            "action": "added", "item_id": None,
            "title": "새 기사", "source_indices": [3],
            "summary": "신규 요약", "reason": "사유",
            "category": "new", "key_facts": ["신규 팩트"],
            "exclusive": True, "prev_reference": None,
        },
    ]
    mock_response = _make_tool_use_response(items)

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)

    existing = [{"id": 1, "title": "기존", "summary": "요약", "key_facts": ["대표 소환"]}]
    with patch("src.agents.report_agent.anthropic.AsyncAnthropic", return_value=mock_client):
        result = await analyze_report_articles(
            api_key="sk-test",
            articles=[{"title": "t", "publisher": "p", "body": "b", "originallink": "u", "pubDate": "d"}],
            report_history=[],
            existing_items=existing,
            department="사회",
        )

    assert len(result) == 2
    assert result[0]["action"] == "modified"
    assert result[1]["exclusive"] is True


@pytest.mark.asyncio
async def test_analyze_report_articles_empty():
    """시나리오 B에서 변경 없으면 빈 배열 반환."""
    mock_response = _make_tool_use_response([])

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)

    existing = [{"id": 1, "title": "기존", "summary": "요약", "key_facts": ["팩트"]}]
    with patch("src.agents.report_agent.anthropic.AsyncAnthropic", return_value=mock_client):
        result = await analyze_report_articles(
            api_key="sk-test",
            articles=[],
            report_history=[],
            existing_items=existing,
            department="사회",
        )

    assert result == []


@pytest.mark.asyncio
async def test_analyze_report_articles_no_tool_use():
    """tool_use 응답이 없으면 빈 배열 반환."""
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "분석 불가"

    response = MagicMock()
    response.stop_reason = "end_turn"
    response.content = [text_block]
    response.usage = MagicMock(input_tokens=500, output_tokens=100)

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=response)

    with patch("src.agents.report_agent.anthropic.AsyncAnthropic", return_value=mock_client):
        result = await analyze_report_articles(
            api_key="sk-test",
            articles=[],
            report_history=[],
            existing_items=None,
            department="사회",
        )

    assert result == []
