"""첨부파일 텍스트 추출.

지원 형식: PDF, DOCX, TXT
메모리 보호를 위해 동시 1건만 처리.
"""

import asyncio

_file_parse_semaphore = asyncio.Semaphore(1)

_MAX_TEXT_LENGTH = 10_000  # 추출 텍스트 최대 길이


async def extract_text(file_bytes: bytes, mime_type: str) -> str:
    """바이트 데이터에서 텍스트를 추출한다.

    Args:
        file_bytes: 파일 바이너리 데이터
        mime_type: MIME 타입

    Returns:
        추출된 텍스트 (최대 10,000자)

    Raises:
        ValueError: 미지원 파일 형식
    """
    async with _file_parse_semaphore:
        # 동기 파싱을 asyncio.to_thread로 비동기 실행
        return await asyncio.to_thread(_extract_sync, file_bytes, mime_type)


def _extract_sync(file_bytes: bytes, mime_type: str) -> str:
    """동기 텍스트 추출."""
    if mime_type == "application/pdf":
        return _extract_pdf(file_bytes)
    elif mime_type in (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
    ):
        return _extract_docx(file_bytes)
    elif mime_type and mime_type.startswith("text/"):
        return file_bytes.decode("utf-8", errors="replace")[:_MAX_TEXT_LENGTH]
    else:
        raise ValueError(f"지원하지 않는 파일 형식입니다: {mime_type}")


def _extract_pdf(file_bytes: bytes) -> str:
    """PDF에서 텍스트 추출. pymupdf 사용."""
    import fitz  # pymupdf

    doc = fitz.open(stream=file_bytes, filetype="pdf")
    text_parts = []
    for page in doc:
        text_parts.append(page.get_text())
    doc.close()
    text = "\n".join(text_parts)
    return text[:_MAX_TEXT_LENGTH]


def _extract_docx(file_bytes: bytes) -> str:
    """DOCX에서 텍스트 추출. python-docx 사용."""
    import io

    from docx import Document

    doc = Document(io.BytesIO(file_bytes))
    text_parts = [para.text for para in doc.paragraphs if para.text.strip()]
    text = "\n".join(text_parts)
    return text[:_MAX_TEXT_LENGTH]
