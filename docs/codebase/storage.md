# Storage Layer

저장소 계층의 구조와 동작을 설명한다. SQLite를 사용하며, 스키마 정의는 `src/storage/models.py`, CRUD 로직은 `src/storage/repository.py`, 주요 상수는 `src/config.py`에 위치한다.

---

## 1. DB 스키마

DB 파일 경로는 `src/config.py`의 `DB_PATH`로 결정된다. 환경변수 `DB_PATH`가 없으면 프로젝트 루트의 `data/tasa-check.db`가 기본값이다.

```python
DB_PATH: str = os.environ.get("DB_PATH", str(BASE_DIR / "data" / "tasa-check.db"))
```

### 1.1 journalists

사용자(기자) 프로필 테이블.

```sql
CREATE TABLE IF NOT EXISTS journalists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id TEXT UNIQUE NOT NULL,
    department TEXT NOT NULL,
    keywords TEXT NOT NULL,          -- JSON 배열
    api_key TEXT NOT NULL,           -- Fernet 암호화된 값
    last_check_at DATETIME,
    last_report_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

| 컬럼 | 타입 | 제약조건 | 설명 |
|------|------|----------|------|
| `id` | INTEGER | PK, AUTOINCREMENT | 내부 식별자 |
| `telegram_id` | TEXT | UNIQUE, NOT NULL | Telegram 사용자 ID |
| `department` | TEXT | NOT NULL | 소속 부서 (사회부, 정치부 등) |
| `keywords` | TEXT | NOT NULL | 모니터링 키워드 (JSON 배열로 직렬화) |
| `api_key` | TEXT | NOT NULL | Anthropic API 키 (Fernet 암호화 상태로 저장) |
| `last_check_at` | DATETIME | nullable | 마지막 /check 실행 시각 (UTC ISO 형식) |
| `last_report_at` | DATETIME | nullable | 마지막 /report 실행 시각 (UTC ISO 형식) |
| `created_at` | DATETIME | DEFAULT CURRENT_TIMESTAMP | 최초 등록 시각 |

- `telegram_id`에 UNIQUE 제약이 있어 한 사용자당 하나의 프로필만 존재한다.
- `keywords`와 `department`를 변경하면 `last_check_at`과 `last_report_at`이 모두 NULL로 초기화된다 (이전 체크/리포트 기준이 무의미해지므로).

### 1.2 report_cache

일일 리포트의 캐시 컨테이너. 기자 + 날짜 조합으로 하루에 하나씩 생성된다.

```sql
CREATE TABLE IF NOT EXISTS report_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    journalist_id INTEGER NOT NULL REFERENCES journalists(id),
    date DATE NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(journalist_id, date)
);
```

| 컬럼 | 타입 | 제약조건 | 설명 |
|------|------|----------|------|
| `id` | INTEGER | PK, AUTOINCREMENT | 캐시 식별자 |
| `journalist_id` | INTEGER | NOT NULL, FK -> journalists(id) | 소유 기자 |
| `date` | DATE | NOT NULL | 리포트 대상 날짜 (KST 기준) |
| `updated_at` | DATETIME | DEFAULT CURRENT_TIMESTAMP | 마지막 갱신 시각 |

- `UNIQUE(journalist_id, date)` 복합 유니크 제약으로 동일 기자 + 동일 날짜에 중복 캐시가 생성되지 않는다.
- `journalist_id`는 `journalists(id)`에 대한 외래키다.

### 1.3 report_items

리포트 브리핑의 개별 기사 아이템. `report_cache`에 종속된다.

```sql
CREATE TABLE IF NOT EXISTS report_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_cache_id INTEGER NOT NULL REFERENCES report_cache(id),
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    summary TEXT NOT NULL,
    tags TEXT NOT NULL,              -- JSON 배열
    category TEXT NOT NULL,          -- "follow_up" / "new"
    prev_reference TEXT,
    reason TEXT DEFAULT '',          -- 선택 사유
    exclusive INTEGER DEFAULT 0,    -- [단독] 여부 (0/1)
    publisher TEXT DEFAULT '',      -- 언론사명
    pub_time TEXT DEFAULT '',       -- 배포 시각 (HH:MM)
    key_facts TEXT DEFAULT '[]',   -- 핵심 팩트 JSON 배열
    source_count INTEGER DEFAULT 1, -- 통합 출처 수
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

| 컬럼 | 타입 | 제약조건 | 설명 |
|------|------|----------|------|
| `id` | INTEGER | PK, AUTOINCREMENT | 아이템 식별자 |
| `report_cache_id` | INTEGER | NOT NULL, FK -> report_cache(id) | 소속 리포트 캐시 |
| `title` | TEXT | NOT NULL | 기사 제목 |
| `url` | TEXT | NOT NULL | 기사 URL |
| `summary` | TEXT | NOT NULL | 기사 요약 |
| `tags` | TEXT | NOT NULL | 태그 목록 (JSON 배열로 직렬화) |
| `category` | TEXT | NOT NULL | 분류: `"follow_up"` (후속) 또는 `"new"` (신규) |
| `prev_reference` | TEXT | nullable | 후속 보도 시 참조한 이전 기사 정보 |
| `reason` | TEXT | DEFAULT '' | Claude가 판단한 선택 사유 |
| `exclusive` | INTEGER | DEFAULT 0 | 단독 기사 여부 (0: 아님, 1: 단독) |
| `publisher` | TEXT | DEFAULT '' | 언론사명 |
| `pub_time` | TEXT | DEFAULT '' | 기사 배포 시각 (HH:MM 형식) |
| `key_facts` | TEXT | DEFAULT '[]' | 핵심 팩트 목록 (JSON 배열로 직렬화) |
| `source_count` | INTEGER | DEFAULT 1 | 통합 출처 수 (1이면 단독 출처, 2 이상이면 병합) |
| `created_at` | DATETIME | DEFAULT CURRENT_TIMESTAMP | 최초 저장 시각 |
| `updated_at` | DATETIME | DEFAULT CURRENT_TIMESTAMP | 마지막 갱신 시각 |

- `report_cache_id`는 `report_cache(id)`에 대한 외래키다.
- `reason`, `exclusive`, `publisher`, `pub_time`, `key_facts`, `source_count` 6개 컬럼은 마이그레이션으로 추가된 컬럼이다 (1.6절 참조).

### 1.4 reported_articles

/check 명령의 분석 결과 이력. 기자별로 체크 시점에 발견된 기사 클러스터 단위로 저장된다.

```sql
CREATE TABLE IF NOT EXISTS reported_articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    journalist_id INTEGER NOT NULL REFERENCES journalists(id),
    checked_at DATETIME NOT NULL,
    topic_cluster TEXT NOT NULL,
    key_facts TEXT NOT NULL,         -- JSON 배열
    summary TEXT NOT NULL,
    article_urls TEXT NOT NULL,      -- JSON 배열
    category TEXT NOT NULL,          -- "exclusive" / "important" / "skip"
    reason TEXT DEFAULT ''           -- 판단 근거 (skip이면 스킵 사유)
);
```

| 컬럼 | 타입 | 제약조건 | 설명 |
|------|------|----------|------|
| `id` | INTEGER | PK, AUTOINCREMENT | 레코드 식별자 |
| `journalist_id` | INTEGER | NOT NULL, FK -> journalists(id) | 대상 기자 |
| `checked_at` | DATETIME | NOT NULL | 체크 실행 시각 (UTC ISO 형식) |
| `topic_cluster` | TEXT | NOT NULL | 주제 클러스터명 |
| `key_facts` | TEXT | NOT NULL | 핵심 팩트 목록 (JSON 배열로 직렬화) |
| `summary` | TEXT | NOT NULL | 요약 |
| `article_urls` | TEXT | NOT NULL | 관련 기사 URL 목록 (JSON 배열로 직렬화) |
| `category` | TEXT | NOT NULL | 분류: `"exclusive"` / `"important"` / `"skip"` |
| `reason` | TEXT | DEFAULT '' | 판단 근거 (skip인 경우 스킵 사유) |

- `reason` 컬럼은 마이그레이션으로 추가된 컬럼이다 (1.6절 참조).
- `journalist_id`는 `journalists(id)`에 대한 외래키다.

### 1.5 schedules

자동 실행 스케줄. 기자별로 check/report 명령의 실행 시각을 관리한다.

```sql
CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    journalist_id INTEGER NOT NULL REFERENCES journalists(id),
    command TEXT NOT NULL,           -- "check" / "report"
    time_kst TEXT NOT NULL,          -- "HH:MM" 형식
    UNIQUE(journalist_id, command, time_kst)
);
```

| 컬럼 | 타입 | 제약조건 | 설명 |
|------|------|----------|------|
| `id` | INTEGER | PK, AUTOINCREMENT | 스케줄 식별자 |
| `journalist_id` | INTEGER | NOT NULL, FK -> journalists(id) | 대상 기자 |
| `command` | TEXT | NOT NULL | 실행할 명령 (`"check"` 또는 `"report"`) |
| `time_kst` | TEXT | NOT NULL | 실행 시각 (KST 기준, `"HH:MM"` 형식) |

- `UNIQUE(journalist_id, command, time_kst)` 복합 유니크 제약으로 동일 기자가 같은 명령을 같은 시각에 중복 등록할 수 없다.
- `journalist_id`는 `journalists(id)`에 대한 외래키다.

### 1.6 마이그레이션 처리

`src/storage/models.py`의 `init_db()` 함수 내에서 ALTER TABLE 기반 마이그레이션을 수행한다. DDL의 `CREATE TABLE IF NOT EXISTS`로 초기 스키마를 생성한 후, 이후 추가된 컬럼들을 ALTER TABLE로 반영한다.

```python
_migrations = [
    "ALTER TABLE reported_articles ADD COLUMN reason TEXT DEFAULT ''",
    "ALTER TABLE report_items ADD COLUMN reason TEXT DEFAULT ''",
    "ALTER TABLE report_items ADD COLUMN exclusive INTEGER DEFAULT 0",
    "ALTER TABLE report_items ADD COLUMN publisher TEXT DEFAULT ''",
    "ALTER TABLE report_items ADD COLUMN pub_time TEXT DEFAULT ''",
    "ALTER TABLE report_items ADD COLUMN key_facts TEXT DEFAULT '[]'",
    "ALTER TABLE journalists ADD COLUMN last_report_at DATETIME",
    "ALTER TABLE report_items ADD COLUMN source_count INTEGER DEFAULT 1",
]
for sql in _migrations:
    try:
        await db.execute(sql)
    except Exception:
        pass  # 이미 존재하면 무시
```

- 각 ALTER TABLE을 `try/except`로 감싸서, 이미 컬럼이 존재하는 경우 SQLite가 발생시키는 오류를 무시한다.
- 새로운 컬럼 추가가 필요하면 `_migrations` 리스트에 SQL을 추가하는 방식이다.
- `init_db()`는 앱 시작 시 `main.py`의 `post_init()`에서 한 번 호출된다.

### 1.7 테이블 간 관계

```
journalists (1) --< report_cache (N)
                      |
                      +--< report_items (N)

journalists (1) --< reported_articles (N)

journalists (1) --< schedules (N)
```

- `journalists`가 중심 테이블이며, 나머지 4개 테이블이 `journalist_id` 외래키로 종속된다.
- `report_items`는 `report_cache`에 종속되며, `report_cache`는 `journalists`에 종속된다 (2단계 관계).
- 데이터 삭제 시 하위 테이블부터 삭제해야 한다 (`report_items` -> `report_cache` 순서).

---

## 2. repository.py CRUD 패턴

모든 함수는 첫 번째 인자로 `aiosqlite.Connection` 객체를 받는다. `init_db()` 시 `db.row_factory = aiosqlite.Row`가 설정되어, 쿼리 결과를 딕셔너리 스타일(`row["column_name"]`)로 접근한다.

### 2.1 Journalist CRUD

#### `upsert_journalist(db, telegram_id, department, keywords, api_key) -> int`

프로필 등록 또는 갱신.

- `api_key`를 `encrypt_api_key()`로 암호화한 후 저장한다.
- `keywords`를 `json.dumps()`로 JSON 문자열로 직렬화한다.
- `INSERT ... ON CONFLICT(telegram_id) DO UPDATE SET`으로 upsert를 수행한다.
- 갱신 시 `last_check_at`과 `last_report_at`을 NULL로 초기화한다.
- 반환값: journalist `id` (INTEGER).

#### `get_journalist(db, telegram_id) -> dict | None`

telegram_id로 기자 프로필 조회.

- `SELECT *`로 전체 컬럼을 조회한다.
- `api_key`를 `decrypt_api_key()`로 복호화하여 반환한다.
- `keywords`를 `json.loads()`로 파싱하여 리스트로 반환한다.
- 반환값: `{"id", "telegram_id", "department", "keywords", "api_key", "last_check_at", "last_report_at", "created_at"}` 딕셔너리, 또는 None.

#### `update_api_key(db, telegram_id, api_key) -> None`

API 키만 변경한다.

- `encrypt_api_key()`로 암호화 후 UPDATE 수행.
- `last_check_at`은 변경하지 않는다.

#### `update_keywords(db, telegram_id, keywords) -> None`

키워드를 변경한다.

- `json.dumps()`로 직렬화 후 UPDATE 수행.
- `last_check_at`과 `last_report_at`을 NULL로 초기화한다 (모니터링 기준이 바뀌었으므로).

#### `update_department(db, telegram_id, department) -> None`

부서를 변경한다.

- `last_check_at`과 `last_report_at`을 NULL로 초기화한다 (부서가 바뀌면 기존 체크/리포트 기준이 무의미해지므로).

#### `update_last_check_at(db, journalist_id) -> None`

마지막 /check 시각을 현재 UTC 시각으로 갱신한다.

- `datetime.now(UTC).isoformat()`으로 현재 시각을 문자열로 변환하여 저장한다.
- 인자가 `journalist_id`(INTEGER)인 점에 주의 (다른 함수들은 `telegram_id` 사용).

#### `update_last_report_at(db, journalist_id) -> None`

마지막 /report 시각을 현재 UTC 시각으로 갱신한다.

- `datetime.now(UTC).isoformat()`으로 현재 시각을 문자열로 변환하여 저장한다.
- 인자가 `journalist_id`(INTEGER)인 점에 주의 (다른 함수들은 `telegram_id` 사용).

### 2.2 Reported Articles

#### `save_reported_articles(db, journalist_id, articles) -> None`

Claude가 분석한 기사 클러스터들을 저장한다.

- `articles`는 딕셔너리 리스트. 각 항목에 `topic_cluster`, `key_facts`, `summary`, `article_urls`, `category`, `reason` 키가 포함된다.
- `checked_at`은 함수 호출 시점의 UTC ISO 문자열.
- `key_facts`와 `article_urls`는 `json.dumps()`로 직렬화한다.
- `category`가 `"skip"`인 경우에도 저장하여 중복 보고를 방지한다.

#### `get_recent_reported_articles(db, journalist_id, hours=24) -> list[dict]`

최근 N시간 이내 보고 이력을 조회한다.

- 기본값 24시간.
- `checked_at >= since` 조건으로 필터링. `since`는 현재 UTC 시각에서 `hours`만큼 뺀 값.
- `ORDER BY checked_at DESC` (최신 순).
- `key_facts`와 `article_urls`를 `json.loads()`로 파싱하여 반환한다.
- `reason` 컬럼은 마이그레이션 이전 데이터 호환을 위해 `r.keys()`에 존재 여부를 확인한 후 접근한다.
- 반환값: 딕셔너리 리스트 `[{"id", "checked_at", "topic_cluster", "key_facts", "summary", "article_urls", "category", "reason"}, ...]`.

### 2.3 Report Cache / Report Items

#### `get_or_create_report_cache(db, journalist_id, date) -> tuple[int, bool]`

당일 report_cache를 조회하거나 생성한다.

- `date`는 `"YYYY-MM-DD"` 형식 문자열 (KST 기준 날짜).
- 기존 캐시가 있으면 `(cache_id, False)` 반환 (시나리오 B: 기존 캐시에 추가/수정).
- 없으면 새로 INSERT 후 `(cache_id, True)` 반환 (시나리오 A: 신규 생성).

#### `get_report_items_by_cache(db, report_cache_id) -> list[dict]`

특정 report_cache에 속한 전체 아이템을 조회한다.

- `ORDER BY created_at` (생성 순).
- `tags`를 `json.loads()`로 파싱하여 반환한다.
- `reason`, `exclusive`, `publisher`, `pub_time`은 마이그레이션 이전 데이터 호환을 위해 `r.keys()` 존재 여부를 확인한다.
- `exclusive`는 정수(0/1)를 `bool()`로 변환하여 반환한다.
- 반환값: 딕셔너리 리스트 `[{"id", "title", "url", "summary", "category", "prev_reference", "reason", "exclusive", "publisher", "pub_time", "key_facts", "source_count"}, ...]`.

#### `save_report_items(db, report_cache_id, items) -> None`

report_items에 항목들을 저장한다.

- `items`는 딕셔너리 리스트. 각 항목에 `title`, `url`, `summary`, `tags`, `category` 등의 키가 포함된다.
- `tags`를 `json.dumps()`로 직렬화한다.
- `exclusive`는 `int()`로 변환하여 저장한다 (bool -> 0/1).
- `created_at`과 `updated_at`은 함수 호출 시점의 UTC ISO 문자열.

#### `update_report_item(db, item_id, summary, reason=None, exclusive=None, key_facts=None) -> None`

기존 report_item을 갱신한다. 시나리오 B에서 기존 아이템을 수정할 때 사용한다.

- `summary`는 항상 갱신한다.
- `reason`, `exclusive`, `key_facts`는 값이 전달된 경우에만 갱신한다 (None이면 건너뜀).
- `updated_at`을 현재 UTC 시각으로 갱신한다.
- 동적으로 UPDATE SET 절을 구성하여 필요한 필드만 갱신한다.

#### `get_recent_report_items(db, journalist_id, days=2) -> list[dict]`

최근 N일간 report_items를 조회한다. follow_up/new 판단용 이력으로 사용된다.

- 기본값 2일.
- `report_cache`와 JOIN하여 `rc.journalist_id`와 `rc.date >= cutoff` 조건으로 필터링.
- `cutoff`는 현재 UTC 시각에서 `days`만큼 뺀 값의 `"%Y-%m-%d"` 형식.
- `ORDER BY ri.created_at DESC` (최신 순).
- 반환값: 딕셔너리 리스트 `[{"title", "summary", "category", "key_facts", "created_at"}, ...]`.

#### `get_today_report_items(db, journalist_id) -> list[dict]`

당일(KST 기준) report_items를 조회한다. /check 명령 실행 시 맥락 로드용으로 사용된다.

- `datetime.now(_KST).strftime("%Y-%m-%d")`로 KST 기준 오늘 날짜를 구한다.
- `report_cache`와 JOIN하여 `rc.date = today` 조건으로 필터링.
- `ORDER BY ri.created_at` (생성 순).
- 반환값: 딕셔너리 리스트 `[{"title", "url", "summary", "tags", "category"}, ...]`.

### 2.4 Schedules

#### `save_schedules(db, journalist_id, command, times_kst) -> None`

해당 command의 기존 스케줄을 교체한다.

- 먼저 `DELETE FROM schedules WHERE journalist_id = ? AND command = ?`로 해당 명령의 기존 스케줄을 모두 삭제한다.
- 이후 `times_kst` 리스트의 각 시각을 INSERT한다.
- 교체(delete + insert) 방식으로 동작하므로, 빈 리스트를 전달하면 해당 명령의 스케줄이 모두 삭제된다.

#### `get_schedules(db, journalist_id) -> list[dict]`

사용자의 전체 스케줄을 조회한다.

- `ORDER BY command, time_kst` (명령명 -> 시각 순).
- 반환값: 딕셔너리 리스트 `[{"command", "time_kst"}, ...]`.

#### `get_all_schedules(db) -> list[dict]`

전체 사용자의 스케줄을 조회한다. 서버 시작 시 JobQueue 복원용.

- `schedules`와 `journalists`를 JOIN하여 `telegram_id`를 함께 조회한다.
- `ORDER BY s.journalist_id, s.command, s.time_kst`.
- 반환값: 딕셔너리 리스트 `[{"journalist_id", "command", "time_kst", "telegram_id"}, ...]`.

#### `delete_all_schedules(db, journalist_id) -> None`

사용자의 전체 스케줄을 삭제한다.

- `DELETE FROM schedules WHERE journalist_id = ?`로 해당 기자의 check/report 스케줄을 모두 삭제한다.

### 2.5 데이터 삭제

#### `clear_journalist_data(db, journalist_id) -> None`

기자의 report/check 데이터를 삭제한다. 스케줄은 유지한다.

- `report_items` -> `report_cache` -> `reported_articles` 순서로 삭제한다 (외래키 관계 때문에 report_items를 먼저 삭제).
- `report_items`는 서브쿼리로 `report_cache`에서 해당 기자의 캐시 ID를 조회하여 삭제한다.

#### `clear_check_data(db, journalist_id) -> None`

check 이력(`reported_articles`)만 삭제한다. report 이력은 유지한다.

- `DELETE FROM reported_articles WHERE journalist_id = ?` 한 문장으로 수행.

### 2.6 관리자 통계

#### `get_admin_stats(db) -> dict`

관리자용 전체 통계를 조회한다.

- 4개의 쿼리로 집계 데이터를 수집한다.
- 반환값:

```python
{
    "total_users": int,             # 전체 사용자 수
    "dept_stats": [(부서명, 수), ...],  # 부서별 사용자 수 (내림차순)
    "schedule_users": int,          # 스케줄 등록 사용자 수
    "schedule_stats": {"check": N, "report": N},  # 명령별 스케줄 수
    "users": [                      # 사용자별 상세
        {
            "telegram_id": str,
            "department": str,
            "keywords": list[str],  # JSON 파싱된 리스트
            "last_check_at": str | None,
            "created_at": str,
            "schedule_count": int,  # 서브쿼리로 집계
        },
        ...
    ],
}
```

---

## 3. API 키 암호화/복호화

### 3.1 Fernet 대칭 암호화

`src/storage/repository.py` 모듈 최상단에서 Fernet 인스턴스를 생성한다.

```python
from src.config import FERNET_KEY
_fernet = Fernet(FERNET_KEY.encode())
```

`FERNET_KEY`는 `src/config.py`에서 환경변수로 로드한다.

```python
FERNET_KEY: str = os.environ["FERNET_KEY"]
```

필수 환경변수이므로 설정하지 않으면 앱 시작 시 `KeyError`로 실패한다.

### 3.2 암호화/복호화 함수

```python
def encrypt_api_key(plain: str) -> str:
    return _fernet.encrypt(plain.encode()).decode()

def decrypt_api_key(encrypted: str) -> str:
    return _fernet.decrypt(encrypted.encode()).decode()
```

- `encrypt_api_key`: 평문 문자열을 바이트로 인코딩 -> Fernet 암호화 -> 결과를 문자열로 디코딩하여 반환.
- `decrypt_api_key`: 암호문 문자열을 바이트로 인코딩 -> Fernet 복호화 -> 결과를 문자열로 디코딩하여 반환.

### 3.3 암호화/복호화 시점

- 암호화 시점: `upsert_journalist()`, `update_api_key()` 호출 시, DB에 저장하기 직전에 암호화한다.
- 복호화 시점: `get_journalist()` 호출 시, DB에서 읽은 직후에 복호화하여 반환한다.
- DB에는 항상 암호화된 상태로 저장되어 있다.

### 3.4 키 형식

API 키의 형식 검증(`sk-` 접두사 등)은 storage 계층이 아닌 bot 핸들러에서 수행한다. storage 계층은 전달받은 문자열을 그대로 암호화/복호화만 담당한다.

---

## 4. 시간대 처리

### 4.1 UTC 저장 원칙

DB에 저장되는 DATETIME 값은 UTC ISO 8601 형식(`datetime.now(UTC).isoformat()`)으로 저장한다. 해당되는 컬럼:

- `journalists.last_check_at`
- `journalists.last_report_at`
- `reported_articles.checked_at`
- `report_cache.updated_at`
- `report_items.created_at`, `report_items.updated_at`

### 4.2 KST 변환

`repository.py` 모듈 상단에 KST 타임존을 정의한다.

```python
_KST = timezone(timedelta(hours=9))
```

KST 기준으로 처리하는 항목:

- `report_cache.date`: `get_or_create_report_cache()`에 전달되는 `date` 인자는 호출 측에서 KST 기준 날짜를 전달한다.
- `get_today_report_items()`: `datetime.now(_KST).strftime("%Y-%m-%d")`로 KST 기준 오늘 날짜를 구한다.
- `schedules.time_kst`: 스케줄 시각은 KST 기준 `"HH:MM"` 형식으로 저장한다.

### 4.3 last_check_at 업데이트 시점

`update_last_check_at()` 함수에서 `datetime.now(UTC).isoformat()`으로 현재 UTC 시각을 저장한다. /check 명령 실행이 완료된 후 호출된다.

`last_check_at`/`last_report_at`이 NULL로 초기화되는 경우:
- `upsert_journalist()` 갱신 시 (프로필 전체 재등록)
- `update_keywords()` 호출 시 (키워드 변경)
- `update_department()` 호출 시 (부서 변경)

---

## 5. 캐시 정리 정책

### 5.1 보관 기간

`src/config.py`에 상수로 정의되어 있다.

```python
CACHE_RETENTION_DAYS: int = 5
```

### 5.2 cleanup_old_data() 함수

```python
async def cleanup_old_data(db: aiosqlite.Connection) -> None:
    cutoff = (datetime.now(UTC) - timedelta(days=CACHE_RETENTION_DAYS)).isoformat()
    # 1. report_items 삭제 (report_cache 기준)
    await db.execute("""
        DELETE FROM report_items WHERE report_cache_id IN (
            SELECT id FROM report_cache WHERE date < ?
        )
    """, (cutoff[:10],))
    # 2. report_cache 삭제
    await db.execute("DELETE FROM report_cache WHERE date < ?", (cutoff[:10],))
    # 3. reported_articles 삭제
    await db.execute("DELETE FROM reported_articles WHERE checked_at < ?", (cutoff,))
    await db.commit()
```

삭제 대상:
- `report_items`: `report_cache.date`가 cutoff 날짜보다 오래된 캐시에 속한 아이템. `cutoff[:10]`으로 DATE 형식(`"YYYY-MM-DD"`)으로 비교한다.
- `report_cache`: `date`가 cutoff 날짜보다 오래된 캐시 컨테이너.
- `reported_articles`: `checked_at`이 cutoff 시각보다 오래된 체크 이력. 전체 ISO 문자열로 비교한다.

삭제하지 않는 대상:
- `journalists`: 사용자 프로필은 삭제하지 않는다.
- `schedules`: 스케줄은 삭제하지 않는다.

참고: 독스트링에는 "14일 이상"이라고 되어 있으나, 실제 `CACHE_RETENTION_DAYS` 값은 5일이다.

### 5.3 실행 시점

`main.py`의 `post_init()`에서 두 가지 방식으로 실행된다.

1. 앱 시작 시 즉시 실행:

```python
await cleanup_old_data(db)
```

2. 매일 04:00 KST에 자동 실행 (python-telegram-bot의 JobQueue 사용):

```python
_KST = timezone(timedelta(hours=9))
application.job_queue.run_daily(
    _daily_cleanup, time=time(hour=4, minute=0, tzinfo=_KST), name="daily_cleanup",
)
```

`_daily_cleanup` 콜백은 `context.bot_data["db"]`에서 DB 연결을 가져와 `cleanup_old_data()`를 호출한다.
