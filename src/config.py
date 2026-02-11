import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

TELEGRAM_BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]
NAVER_CLIENT_ID: str = os.environ["NAVER_CLIENT_ID"]
NAVER_CLIENT_SECRET: str = os.environ["NAVER_CLIENT_SECRET"]
FERNET_KEY: str = os.environ["FERNET_KEY"]
DB_PATH: str = os.environ.get("DB_PATH", str(BASE_DIR / "data" / "tasa-check.db"))

# /check 시간 윈도우 최대값 (초)
CHECK_MAX_WINDOW_SECONDS: int = 3 * 60 * 60

# 캐시 보관 기간 (일)
CACHE_RETENTION_DAYS: int = 14

# 부서 목록
DEPARTMENTS: list[str] = ["사회부", "정치부", "경제부", "문화부", "국제부"]
