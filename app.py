import io
import os

# =========================================================
# 저메모리 CPU 설정
# =========================================================

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")
os.environ.setdefault("VECLIB_MAXIMUM_THREADS", "1")

import re
import sqlite3
import secrets
import uuid
from datetime import datetime, timezone, timedelta
from functools import wraps
from pathlib import Path
from typing import Dict, List, Tuple

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template_string,
    request,
    send_from_directory,
    session,
    url_for,
)
from PIL import Image, ImageOps

# from ocr_service import (
#     extract_text_from_image,
#     is_ocr_loaded,
# )

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

KST = timezone(timedelta(hours=9))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "change-me")

# 두 명이 모두 입장한 시점부터 유지되는 채팅 시간
ROOM_SESSION_MINUTES = 10

# 한 채팅방에서 동시에 유지할 수 있는 실제 접속 화면 수
MAX_ACTIVE_CONNECTIONS = 2

# 브라우저 신호가 이 시간 동안 없으면 끊긴 접속으로 처리
ACTIVE_CONNECTION_TIMEOUT_SECONDS = 8

PREFERRED_DB_PATH = os.environ.get(
    "CHAT_DB_PATH",
    "/var/data/contact_guard_chat.db",
).strip()

PREFERRED_UPLOAD_DIR = os.environ.get(
    "UPLOAD_DIR",
    "/var/data/contact_guard_uploads",
).strip()

LOCAL_DB_PATH = "contact_guard_chat.db"
LOCAL_UPLOAD_DIR = "contact_guard_uploads"


# 원본 업로드 최대 허용 용량
# 너무 큰 파일로 인한 메모리 문제만 방지
MAX_SOURCE_IMAGE_BYTES = 20 * 1024 * 1024

# 리사이즈·압축 후 최종 저장 파일 최대 용량
MAX_OUTPUT_IMAGE_BYTES = 2 * 1024 * 1024

# 상대방에게 실제 표시하고 저장할 이미지 크기
MAX_IMAGE_SIDE = 1280

MAX_IMAGE_PIXELS = 20_000_000
JPEG_QUALITY = 82
MIN_JPEG_QUALITY = 50

ALLOWED_IMAGE_FORMATS = {
    "JPEG",
    "PNG",
    "WEBP",
}


# =========================================================
# 저장 경로
# =========================================================

def resolve_writable_path(
    preferred: str,
    local: str,
    *,
    is_directory: bool = False,
) -> str:
    preferred_path = Path(preferred)

    try:
        target_directory = (
            preferred_path
            if is_directory
            else preferred_path.parent
        )

        target_directory.mkdir(
            parents=True,
            exist_ok=True,
        )

        test_file = target_directory / ".write_test"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink(missing_ok=True)

        return str(preferred_path)

    except OSError:
        local_path = Path(local)

        target_directory = (
            local_path
            if is_directory
            else local_path.parent
        )

        target_directory.mkdir(
            parents=True,
            exist_ok=True,
        )

        return str(local_path)


DB_PATH = resolve_writable_path(
    PREFERRED_DB_PATH,
    LOCAL_DB_PATH,
)

UPLOAD_DIR = resolve_writable_path(
    PREFERRED_UPLOAD_DIR,
    LOCAL_UPLOAD_DIR,
    is_directory=True,
)


# =========================================================
# DB
# =========================================================

def db_connect() -> sqlite3.Connection:
    connection = sqlite3.connect(
        DB_PATH,
        timeout=30,
    )

    connection.row_factory = sqlite3.Row

    return connection


def column_exists(
    connection: sqlite3.Connection,
    table_name: str,
    column_name: str,
) -> bool:
    rows = connection.execute(
        f"PRAGMA table_info({table_name})"
    ).fetchall()

    return any(
        row["name"] == column_name
        for row in rows
    )


def init_db() -> None:
    with db_connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS rooms (
                room_code TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                started_at TEXT,
                expires_at TEXT
            );

            CREATE TABLE IF NOT EXISTS members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_code TEXT NOT NULL,
                user_token TEXT NOT NULL,
                nickname TEXT NOT NULL,
                kakao_nickname TEXT NOT NULL DEFAULT '',
                joined_at TEXT NOT NULL,
                UNIQUE(room_code, user_token)
            );

            CREATE TABLE IF NOT EXISTS active_connections (
                room_code TEXT NOT NULL,
                connection_id TEXT NOT NULL,
                user_token TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                PRIMARY KEY(room_code, connection_id)
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_code TEXT NOT NULL,
                user_token TEXT NOT NULL,
                nickname TEXT NOT NULL,
                message_type TEXT NOT NULL DEFAULT 'text',
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS blocked_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_code TEXT NOT NULL,
                user_token TEXT NOT NULL,
                nickname TEXT NOT NULL,
                message_type TEXT NOT NULL DEFAULT 'text',
                content TEXT NOT NULL,
                reasons TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS room_choices (
                room_code TEXT NOT NULL,
                user_token TEXT NOT NULL,
                choice TEXT NOT NULL,
                selected_at TEXT NOT NULL,
                PRIMARY KEY(room_code, user_token)
            );
            """
        )

        # 기존 DB 컬럼이 없어도 자동 보정
        if not column_exists(
            connection,
            "members",
            "kakao_nickname",
        ):
            connection.execute(
                """
                ALTER TABLE members
                ADD COLUMN kakao_nickname TEXT
                NOT NULL DEFAULT ''
                """
            )

        if not column_exists(
            connection,
            "rooms",
            "started_at",
        ):
            connection.execute(
                """
                ALTER TABLE rooms
                ADD COLUMN started_at TEXT
                """
            )

        if not column_exists(
            connection,
            "rooms",
            "expires_at",
        ):
            connection.execute(
                """
                ALTER TABLE rooms
                ADD COLUMN expires_at TEXT
                """
            )

        if not column_exists(
            connection,
            "messages",
            "message_type",
        ):
            connection.execute(
                """
                ALTER TABLE messages
                ADD COLUMN message_type TEXT
                NOT NULL DEFAULT 'text'
                """
            )

        if not column_exists(
            connection,
            "blocked_messages",
            "message_type",
        ):
            connection.execute(
                """
                ALTER TABLE blocked_messages
                ADD COLUMN message_type TEXT
                NOT NULL DEFAULT 'text'
                """
            )


init_db()


def now_kst_iso() -> str:
    return datetime.now(KST).isoformat(
        timespec="seconds"
    )


def get_room_time_state(
    room_code: str,
) -> Tuple[bool, str]:
    with db_connect() as connection:
        room = connection.execute(
            """
            SELECT
                started_at,
                expires_at
            FROM rooms
            WHERE room_code = ?
            """,
            (room_code,),
        ).fetchone()

    if room is None:
        return True, ""

    expires_at = (
        room["expires_at"]
        or ""
    )

    if not expires_at:
        return False, ""

    try:
        expires_datetime = datetime.fromisoformat(
            expires_at
        )

    except ValueError:
        return False, ""

    is_expired = (
        datetime.now(KST)
        >= expires_datetime
    )

    return is_expired, expires_at


def start_room_session_if_ready(
    room_code: str,
) -> str:
    if room_member_count(room_code) < 2:
        return ""

    with db_connect() as connection:
        room = connection.execute(
            """
            SELECT
                started_at,
                expires_at
            FROM rooms
            WHERE room_code = ?
            """,
            (room_code,),
        ).fetchone()

        if room is None:
            return ""

        if room["expires_at"]:
            return str(
                room["expires_at"]
            )

        started_datetime = datetime.now(KST)
        expires_datetime = (
            started_datetime
            + timedelta(
                minutes=ROOM_SESSION_MINUTES
            )
        )

        started_at = started_datetime.isoformat(
            timespec="seconds"
        )
        expires_at = expires_datetime.isoformat(
            timespec="seconds"
        )

        connection.execute(
            """
            UPDATE rooms
            SET
                started_at = ?,
                expires_at = ?
            WHERE room_code = ?
              AND expires_at IS NULL
            """,
            (
                started_at,
                expires_at,
                room_code,
            ),
        )

    return expires_at


# =========================================================
# 연락처 감지
# =========================================================

KOREAN_DIGIT_WORDS: Dict[str, str] = {
    "공": "0",
    "영": "0",
    "빵": "0",
    "일": "1",
    "하나": "1",
    "이": "2",
    "둘": "2",
    "삼": "3",
    "셋": "3",
    "사": "4",
    "넷": "4",
    "오": "5",
    "다섯": "5",
    "육": "6",
    "여섯": "6",
    "칠": "7",
    "일곱": "7",
    "팔": "8",
    "여덟": "8",
    "구": "9",
    "아홉": "9",

    # 영문·발음 우회 표현
    "원": "1",
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
}

CONTACT_KEYWORDS = [
    "전화번호",
    "전번",
    "폰번",
    "연락처",
    "번호교환",
    "번호 교환",
    "카톡아이디",
    "카톡 아이디",
    "카카오아이디",
    "카카오 아이디",
    "텔레그램",
    "인스타",
    "인스타그램",
    "디엠",
    "dm",
    "오픈채팅",
    "오픈톡",
    "오픈링크",
]

URL_PATTERN = re.compile(
    r"(?i)\b(?:"
    r"https?://|"
    r"www\.|"
    r"open\.kakao\.com|"
    r"pf\.kakao\.com|"
    r"t\.me/|"
    r"instagram\.com/"
    r")\S+"
)

EMAIL_PATTERN = re.compile(
    r"(?i)\b"
    r"[a-z0-9._%+-]+"
    r"@"
    r"[a-z0-9.-]+"
    r"\.[a-z]{2,}"
    r"\b"
)

PHONE_PATTERN = re.compile(
    r"(?<!\d)"
    r"(?:\+?82[\s.\-)]*|0)"
    r"[\s.\-()]*"
    r"1[016789]"
    r"(?:[\s.\-()]*\d){7,8}"
    r"(?!\d)"
)

SNS_ID_PATTERN = re.compile(
    r"(?i)"
    r"(?:카톡|카카오|텔레그램|인스타|"
    r"instagram|telegram|line|라인)"
    r"\s*"
    r"(?:아이디|id|계정)?"
    r"\s*[:：]?\s*"
    r"[@a-z0-9_.\-]{4,}"
)


NUMERIC_OBFUSCATION_REPLACEMENTS: Dict[str, str] = {
    "o": "0",
    "ｏ": "0",
    "ㅇ": "0",
    "l": "1",
    "i": "1",
    "|": "1",
    "０": "0",
    "１": "1",
    "２": "2",
    "３": "3",
    "４": "4",
    "５": "5",
    "６": "6",
    "７": "7",
    "８": "8",
    "９": "9",
}


def has_consecutive_numeric_obfuscation(
    text: str,
    minimum_items: int = 3,
) -> bool:
    value = text.casefold()

    numeric_items = sorted(
        set(
            list(KOREAN_DIGIT_WORDS.keys())
            + list(
                NUMERIC_OBFUSCATION_REPLACEMENTS.keys()
            )
            + list("0123456789")
        ),
        key=len,
        reverse=True,
    )

    numeric_pattern = re.compile(
        "|".join(
            re.escape(item)
            for item in numeric_items
        ),
        re.IGNORECASE,
    )

    separator_pattern = re.compile(
        r"^[\s.·,，\-_/\\()（）\[\]{}:：]*$"
    )

    consecutive_count = 0
    previous_end = None

    for match in numeric_pattern.finditer(value):
        if previous_end is None:
            consecutive_count = 1

        else:
            gap = value[
                previous_end:match.start()
            ]

            if separator_pattern.fullmatch(gap):
                consecutive_count += 1

            else:
                consecutive_count = 1

        if consecutive_count >= minimum_items:
            return True

        previous_end = match.end()

    return False


def normalize_for_contact_detection(
    text: str,
) -> str:
    value = text.casefold()

    for source, target in (
        NUMERIC_OBFUSCATION_REPLACEMENTS.items()
    ):
        value = value.replace(
            source,
            target,
        )

    for word in sorted(
        KOREAN_DIGIT_WORDS,
        key=len,
        reverse=True,
    ):
        value = value.replace(
            word,
            KOREAN_DIGIT_WORDS[word],
        )

    return value


def compact_numeric_text(text: str) -> str:
    normalized = normalize_for_contact_detection(
        text
    )

    return re.sub(
        r"[^0-9]",
        "",
        normalized,
    )


def detect_contact_info(
    text: str,
) -> List[str]:
    reasons: List[str] = []

    lowered = text.casefold()

    has_numeric_obfuscation = (
        has_consecutive_numeric_obfuscation(
            text,
            minimum_items=3,
        )
    )

    normalized = normalize_for_contact_detection(
        text
    )

    compact_digits = compact_numeric_text(
        text
    )

    if any(
        keyword.casefold() in lowered
        for keyword in CONTACT_KEYWORDS
    ):
        reasons.append(
            "연락처 교환 표현"
        )

    # 숫자·한글 숫자·영문 숫자·우회 문자가
    # 연속으로 3개 이상 이어지면 전송 차단
    if has_numeric_obfuscation:
        reasons.append(
            "연속 우회 숫자 표현"
        )

    if URL_PATTERN.search(text):
        reasons.append(
            "외부 링크"
        )

    if EMAIL_PATTERN.search(text):
        reasons.append(
            "이메일 주소"
        )

    if PHONE_PATTERN.search(normalized):
        reasons.append(
            "전화번호 형식"
        )

    if re.search(
        r"01[016789]\d{7,8}",
        compact_digits,
    ):
        reasons.append(
            "우회 전화번호 형식"
        )

    if SNS_ID_PATTERN.search(text):
        reasons.append(
            "SNS·메신저 ID"
        )

    return list(
        dict.fromkeys(reasons)
    )


# =========================================================
# 이미지 저장
# =========================================================

def save_clean_image(raw_bytes: bytes) -> Tuple[str, str]:
    if not raw_bytes:
        raise ValueError("이미지 내용이 비어 있어.")

    if len(raw_bytes) > MAX_SOURCE_IMAGE_BYTES:
        max_mb = (
            MAX_SOURCE_IMAGE_BYTES
            / (1024 * 1024)
        )

        raise ValueError(
            f"원본 이미지는 최대 {max_mb:g}MB까지 가능해."
        )

    image = None
    output_buffer = None

    try:
        with Image.open(
            io.BytesIO(raw_bytes)
        ) as source_image:
            if source_image.format not in ALLOWED_IMAGE_FORMATS:
                raise ValueError(
                    "JPG, PNG, WEBP 이미지만 가능해."
                )

            width, height = source_image.size

            if width * height > MAX_IMAGE_PIXELS:
                raise ValueError(
                    "이미지 해상도가 너무 커."
                )

            source_image.load()

            image = ImageOps.exif_transpose(
                source_image
            ).convert("RGB")

            if max(image.size) > MAX_IMAGE_SIDE:
                image.thumbnail(
                    (
                        MAX_IMAGE_SIDE,
                        MAX_IMAGE_SIDE,
                    ),
                    Image.Resampling.LANCZOS,
                )

            quality = JPEG_QUALITY
            encoded_bytes = b""

            while quality >= MIN_JPEG_QUALITY:
                if output_buffer is not None:
                    output_buffer.close()

                output_buffer = io.BytesIO()

                image.save(
                    output_buffer,
                    format="JPEG",
                    quality=quality,
                    optimize=False,
                    progressive=False,
                )

                encoded_bytes = (
                    output_buffer.getvalue()
                )

                if (
                    len(encoded_bytes)
                    <= MAX_OUTPUT_IMAGE_BYTES
                ):
                    break

                quality -= 5

            if (
                not encoded_bytes
                or len(encoded_bytes)
                > MAX_OUTPUT_IMAGE_BYTES
            ):
                max_mb = (
                    MAX_OUTPUT_IMAGE_BYTES
                    / (1024 * 1024)
                )

                raise ValueError(
                    "이미지를 줄였지만 "
                    f"{max_mb:g}MB 이하로 "
                    "압축할 수 없어."
                )

            filename = f"{uuid.uuid4().hex}.jpg"
            save_path = Path(UPLOAD_DIR) / filename

            save_path.write_bytes(
                encoded_bytes
            )

    except ValueError:
        raise

    except Image.DecompressionBombError as error:
        raise ValueError(
            "이미지 해상도가 너무 커."
        ) from error

    except Exception as error:
        raise ValueError(
            "이미지 파일을 읽을 수 없어."
        ) from error

    finally:
        if output_buffer is not None:
            output_buffer.close()

        if image is not None:
            image.close()

    return filename, str(save_path)

# =========================================================
# 방/사용자 토큰
# =========================================================

def generate_room_code() -> str:
    alphabet = (
        "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    )

    while True:
        code = "".join(
            secrets.choice(alphabet)
            for _ in range(6)
        )

        with db_connect() as connection:
            exists = connection.execute(
                """
                SELECT 1
                FROM rooms
                WHERE room_code = ?
                """,
                (code,),
            ).fetchone()

        if not exists:
            return code


def generate_user_token() -> str:
    return secrets.token_urlsafe(24)


def get_request_user_token() -> str:
    token = (
        request.headers.get(
            "X-User-Token",
            "",
        ).strip()
        or request.args.get(
            "token",
            "",
        ).strip()
        or request.form.get(
            "token",
            "",
        ).strip()
    )

    return token


def get_request_connection_id() -> str:
    return request.headers.get(
        "X-Connection-ID",
        "",
    ).strip()


def get_room_member(
    room_code: str,
    user_token: str,
):
    if not user_token:
        return None

    with db_connect() as connection:
        return connection.execute(
            """
            SELECT *
            FROM members
            WHERE room_code = ?
              AND user_token = ?
            """,
            (
                room_code,
                user_token,
            ),
        ).fetchone()


def room_member_count(
    room_code: str,
) -> int:
    with db_connect() as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM members
            WHERE room_code = ?
            """,
            (room_code,),
        ).fetchone()

    return int(row["count"])


def cleanup_stale_connections(
    room_code: str,
) -> None:
    cutoff = (
        datetime.now(KST)
        - timedelta(
            seconds=ACTIVE_CONNECTION_TIMEOUT_SECONDS
        )
    ).isoformat(
        timespec="seconds"
    )

    with db_connect() as connection:
        connection.execute(
            """
            DELETE FROM active_connections
            WHERE room_code = ?
              AND last_seen_at < ?
            """,
            (
                room_code,
                cutoff,
            ),
        )


def register_active_connection(
    room_code: str,
    user_token: str,
    connection_id: str,
) -> bool:
    cleanup_stale_connections(
        room_code
    )

    now_value = now_kst_iso()

    with db_connect() as connection:
        existing = connection.execute(
            """
            SELECT 1
            FROM active_connections
            WHERE room_code = ?
              AND connection_id = ?
            """,
            (
                room_code,
                connection_id,
            ),
        ).fetchone()

        if existing:
            connection.execute(
                """
                UPDATE active_connections
                SET
                    user_token = ?,
                    last_seen_at = ?
                WHERE room_code = ?
                  AND connection_id = ?
                """,
                (
                    user_token,
                    now_value,
                    room_code,
                    connection_id,
                ),
            )

            return True

        row = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM active_connections
            WHERE room_code = ?
            """,
            (room_code,),
        ).fetchone()

        if int(row["count"]) >= MAX_ACTIVE_CONNECTIONS:
            return False

        connection.execute(
            """
            INSERT INTO active_connections(
                room_code,
                connection_id,
                user_token,
                last_seen_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                room_code,
                connection_id,
                user_token,
                now_value,
            ),
        )

    return True


def refresh_active_connection(
    room_code: str,
    user_token: str,
    connection_id: str,
) -> bool:
    if not connection_id:
        return False

    cleanup_stale_connections(
        room_code
    )

    with db_connect() as connection:
        cursor = connection.execute(
            """
            UPDATE active_connections
            SET last_seen_at = ?
            WHERE room_code = ?
              AND connection_id = ?
              AND user_token = ?
            """,
            (
                now_kst_iso(),
                room_code,
                connection_id,
                user_token,
            ),
        )

    return cursor.rowcount > 0


def remove_active_connection(
    room_code: str,
    connection_id: str,
) -> None:
    if not connection_id:
        return

    with db_connect() as connection:
        connection.execute(
            """
            DELETE FROM active_connections
            WHERE room_code = ?
              AND connection_id = ?
            """,
            (
                room_code,
                connection_id,
            ),
        )


def require_room_member_api(view):
    @wraps(view)
    def wrapped(
        room_code: str,
        *args,
        **kwargs,
    ):
        normalized_room_code = (
            room_code.upper().strip()
        )

        user_token = (
            get_request_user_token()
        )

        member = get_room_member(
            normalized_room_code,
            user_token,
        )

        if member is None:
            return jsonify(
                {
                    "ok": False,
                    "message": (
                        "채팅방 인증이 만료됐어. "
                        "메인 화면에서 다시 입장해줘."
                    ),
                }
            ), 401

        is_expired, expires_at = (
            get_room_time_state(
                normalized_room_code
            )
        )

        if is_expired:
            return jsonify(
                {
                    "ok": False,
                    "expired": True,
                    "message": (
                        "채팅 시간이 종료되어 "
                        "연결이 끊겼어."
                    ),
                    "expires_at": expires_at,
                }
            ), 410

        connection_id = (
            get_request_connection_id()
        )

        if not refresh_active_connection(
            normalized_room_code,
            user_token,
            connection_id,
        ):
            return jsonify(
                {
                    "ok": False,
                    "connection_rejected": True,
                    "message": (
                        "이 채팅방은 동시에 "
                        "두 개의 화면만 접속할 수 있어."
                    ),
                }
            ), 409

        request.room_member = member
        request.user_token = user_token
        request.connection_id = connection_id

        return view(
            normalized_room_code,
            *args,
            **kwargs,
        )

    return wrapped


# =========================================================
# HTML
# =========================================================

STYLE = """
<style>
:root {
  color-scheme: dark;
  --bg: #101114;
  --panel: #191b20;
  --text: #f5f6f8;
  --muted: #a5a9b2;
  --border: #30343d;
  --accent: #f2f4f8;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  min-height: 100vh;
  background: var(--bg);
  color: var(--text);
  font-family: system-ui, sans-serif;
}
.page {
  width: min(1000px, calc(100% - 32px));
  margin: auto;
  padding: 38px 0;
}
.narrow {
  width: min(820px, calc(100% - 32px));
}
.card {
  margin-bottom: 16px;
  padding: 22px;
  border: 1px solid var(--border);
  border-radius: 18px;
  background: var(--panel);
}
.grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}
.muted {
  color: var(--muted);
  line-height: 1.6;
}
label {
  display: block;
  margin: 14px 0 7px;
  color: var(--muted);
}
input, textarea {
  width: 100%;
  padding: 13px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: #22252c;
  color: white;
  font: inherit;
}
button {
  padding: 12px 17px;
  border: 0;
  border-radius: 12px;
  font-weight: 800;
  cursor: pointer;
}
.primary {
  background: var(--accent);
  color: #111;
}
.full {
  width: 100%;
  margin-top: 16px;
}
.alert {
  margin: 12px 0;
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: 12px;
}
.error { color: #ffb4b4; }
.success { color: #b8f7ca; }
.hidden { display: none; }
.chat {
  height: 100vh;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.header {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: flex-start;
}
.messages {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  overflow-anchor: none;
  padding: 16px;
  border: 1px solid var(--border);
  border-radius: 18px;
  background: #15171b;
}
.message {
  width: min(76%, 620px);
  margin-bottom: 13px;
}
.mine { margin-left: auto; }
.meta {
  margin: 0 6px 5px;
  color: var(--muted);
  font-size: 12px;
}
.mine .meta { text-align: right; }
.bubble {
  padding: 12px 14px;
  border-radius: 16px;
  background: #2a2e36;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.mine .bubble {
  background: #eceef2;
  color: #111;
}
.chat-image {
  display: block;
  max-width: 100%;
  max-height: 460px;
  border-radius: 14px;
}
.composer {
  display: grid;
  grid-template-columns: auto 1fr auto;
  gap: 10px;
  align-items: stretch;
}
.composer textarea { resize: none; }
.composer button { min-width: 84px; }
.file-button {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0 14px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: #22252c;
  cursor: pointer;
}
.file-button input { display: none; }
.file-name {
  margin: 0;
  color: var(--muted);
  font-size: 12px;
}
#choice-bar {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
  width: 100%;
}
.choice-button {
  min-height: 42px;
  padding: 9px 12px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: #22252c;
  color: var(--text);
  font-size: 14px;
}
.choice-button:hover:not(:disabled) {
  background: #2a2e36;
}
.choice-button.selected-like {
  border-color: #8de3aa;
  background: #21452d;
  color: #c9f7d7;
}
.choice-button.selected-pass {
  border-color: #e49a9a;
  background: #4b2929;
  color: #ffd0d0;
}
.choice-button:disabled {
  cursor: default;
  opacity: 1;
}
#matched-kakao-box {
  margin: 0;
}
table {
  width: 100%;
  min-width: 700px;
  border-collapse: collapse;
}
th, td {
  padding: 11px;
  border-bottom: 1px solid var(--border);
  text-align: left;
  vertical-align: top;
}
.table-wrap { overflow: auto; }
a { color: white; }
.small { font-size: 13px; }
.code {
  font-family: monospace;
  font-size: 1.1em;
}
@media (max-width: 700px) {
  .grid {
    grid-template-columns: 1fr;
  }
  .page {
    width: calc(100% - 16px);
    padding: 12px 0 24px;
  }
  .chat {
    height: auto;
    min-height: 100dvh;
    display: flex;
    flex-direction: column;
    gap: 10px;
    overflow-anchor: none;
  }
  .messages {
    flex: 0 0 auto;
    width: 100%;
    height: 60dvh;
    min-height: 390px;
    max-height: 620px;
    overflow-y: auto;
    overscroll-behavior: contain;
    -webkit-overflow-scrolling: touch;
  }
  .composer {
    flex: 0 0 auto;
    width: 100%;
    grid-template-columns: auto minmax(0, 1fr) auto;
    gap: 7px;
    align-items: stretch;
  }
  .composer textarea {
    min-width: 0;
    padding: 11px;
  }
  .composer button {
    grid-column: auto;
    min-width: 58px;
    padding: 10px 12px;
  }
  .file-button {
    padding: 0 11px;
  }
  .file-name {
    min-height: 0;
  }
  .message {
    width: 90%;
  }
  .header {
    flex-direction: column;
    gap: 6px;
  }
  #choice-bar {
    grid-template-columns: 1fr 1fr;
    gap: 8px;
  }
  .choice-button {
    min-width: 0;
    min-height: 38px;
    padding: 8px 6px;
    border-radius: 4px;
    font-size: 13px;
  }
  #matched-kakao-box {
    padding: 9px;
    font-size: 13px;
  }
}
</style>
"""

HOME_HTML = """
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta
    name="viewport"
    content="width=device-width,initial-scale=1"
  >
  <title>연락처 차단 채팅</title>
  """ + STYLE + """
</head>
<body>
  <main class="page narrow">
    <section class="card">
      <h1>연락처 차단 1:1 채팅</h1>
      <p class="muted">
        전화번호, 이메일, 링크,
        SNS·메신저 ID가 감지되면
        상대에게 전달하지 않아.
      </p>
    </section>

    {% if error %}
      <div class="alert error">
        {{ error }}
      </div>
    {% endif %}

    <section
      id="reconnect-card"
      class="card hidden"
    >
      <h2>최근 채팅방 재접속</h2>

      <p class="muted">
        실수로 나갔거나 새로고침한 경우,
        기존 인증으로 다시 들어갈 수 있어.
      </p>

      <button
        id="reconnect-button"
        class="primary full"
        type="button"
      >
        다시 들어가기
      </button>
    </section>

    <section class="grid">
      <form
        class="card"
        method="post"
        action="{{ url_for('create_room') }}"
      >
        <h2>새 방 만들기</h2>

        <label>채팅에서 사용할 닉네임</label>

        <input
          name="nickname"
          maxlength="20"
          required
        >

        <label>본인 카카오톡 닉네임</label>

        <input
          name="kakao_nickname"
          maxlength="30"
          required
        >

        <button class="primary full">
          방 만들기
        </button>
      </form>

      <form
        class="card"
        method="post"
        action="{{ url_for('join_room') }}"
      >
        <h2>방 참여하기</h2>

        <label>방 코드</label>

        <input
          name="room_code"
          maxlength="6"
          required
        >

        <label>채팅에서 사용할 닉네임</label>

        <input
          name="nickname"
          maxlength="20"
          required
        >

        <label>본인 카카오톡 닉네임</label>

        <input
          name="kakao_nickname"
          maxlength="30"
          required
        >

        <button class="primary full">
          입장하기
        </button>
      </form>
    </section>
  </main>

<script>
const lastRoomCode =
  localStorage.getItem(
    "contact_guard_last_room_code"
  );

const lastRoomToken =
  lastRoomCode
    ? localStorage.getItem(
        `contact_guard_token_${lastRoomCode}`
      )
    : "";

const reconnectCard =
  document.getElementById(
    "reconnect-card"
  );

const reconnectButton =
  document.getElementById(
    "reconnect-button"
  );

async function initializeReconnectCard() {
  if (
    !lastRoomCode
    || !lastRoomToken
  ) {
    return;
  }

  try {
    const response = await fetch(
      `/api/room/${lastRoomCode}/reconnect-status`,
      {
        cache: "no-store",
        headers: {
          "X-User-Token": lastRoomToken
        }
      }
    );

    const data = await response.json();

    if (
      !response.ok
      || !data.can_reconnect
    ) {
      localStorage.removeItem(
        "contact_guard_last_room_code"
      );

      localStorage.removeItem(
        `contact_guard_token_${lastRoomCode}`
      );

      reconnectCard.classList.add(
        "hidden"
      );

      return;
    }

    reconnectCard.classList.remove(
      "hidden"
    );

    reconnectButton.textContent =
      `${lastRoomCode} 방 다시 들어가기`;

    reconnectButton.addEventListener(
      "click",
      () => {
        window.location.href =
          `/room/${lastRoomCode}`
          + `?token=${encodeURIComponent(
            lastRoomToken
          )}`;
      }
    );

  } catch (error) {
    console.error(error);

    reconnectCard.classList.add(
      "hidden"
    );
  }
}

initializeReconnectCard();
</script>
</body>
</html>
"""

CHAT_HTML = """
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">

  <meta
    name="viewport"
    content="width=device-width,initial-scale=1"
  >

  <title>채팅방</title>

  """ + STYLE + """
</head>

<body>
  <main class="page chat">
    <header class="header">
      <div>
        <h1>
          방 코드

          <button
            id="copy"
            class="code"
            type="button"
          >
            {{ room_code }}
          </button>
        </h1>

        <p class="muted">
          {{ nickname }}
          ·
          <span id="member-count">1</span>/2명
          ·
          남은 시간
          <strong id="room-timer">10:00</strong>
        </p>
      </div>

      <a
        id="leave-room"
        href="{{ url_for('home') }}"
      >
        나가기
      </a>
    </header>

    <section
      id="choice-bar"
      aria-label="상대 선택"
    >
      <button
        id="choice-like"
        class="choice-button"
        type="button"
      >
        마음에 듦
      </button>

      <button
        id="choice-pass"
        class="choice-button"
        type="button"
      >
        안 맞음
      </button>
    </section>

    <div
      id="matched-kakao-box"
      class="alert success hidden"
    >
      상대 카카오톡 닉네임:
      <strong id="matched-kakao-nickname"></strong>
    </div>

    <section
      id="messages"
      class="messages"
    ></section>

    <div
      id="notice"
      class="alert hidden"
    ></div>

    <p
      id="file-name"
      class="file-name"
    ></p>

    <form
      id="form"
      class="composer"
    >
      <label class="file-button">
        사진

        <input
          id="image-input"
          type="file"
          accept="image/jpeg,image/png,image/webp"
        >
      </label>

      <textarea
        id="input"
        maxlength="500"
        rows="2"
        placeholder="메시지를 입력해. 연락처·링크·SNS ID는 차단돼."
      ></textarea>

      <button class="primary">
        전송
      </button>
    </form>
  </main>

<script>
const roomCode = "{{ room_code }}";
const initialToken = "{{ user_token }}";
const tokenStorageKey = `contact_guard_token_${roomCode}`;
const connectionStorageKey =
  `contact_guard_connection_${roomCode}`;

let connectionId =
  sessionStorage.getItem(
    connectionStorageKey
  );

if (!connectionId) {
  connectionId =
    (
      crypto.randomUUID
        ? crypto.randomUUID()
        : `${Date.now()}-${Math.random()}`
    );

  sessionStorage.setItem(
    connectionStorageKey,
    connectionId
  );
}

localStorage.setItem(
  tokenStorageKey,
  initialToken
);

localStorage.setItem(
  "contact_guard_last_room_code",
  roomCode
);

const userToken =
  localStorage.getItem(tokenStorageKey)
  || initialToken;

let lastMessageId = 0;
let isLoadingMessages = false;
let roomExpiresAt = "";
let roomExpired = false;
const renderedMessageIds = new Set();

const messagesElement =
  document.getElementById("messages");

const inputElement =
  document.getElementById("input");

const imageInputElement =
  document.getElementById("image-input");

const fileNameElement =
  document.getElementById("file-name");

const noticeElement =
  document.getElementById("notice");

const choiceLikeButton =
  document.getElementById(
    "choice-like"
  );

const choicePassButton =
  document.getElementById(
    "choice-pass"
  );

const matchedKakaoBox =
  document.getElementById(
    "matched-kakao-box"
  );

const matchedKakaoNickname =
  document.getElementById(
    "matched-kakao-nickname"
  );

function authenticatedHeaders(extra = {}) {
  return {
    "X-User-Token": userToken,
    "X-Connection-ID": connectionId,
    ...extra
  };
}

function setChoiceButtonsDisabled(
  disabled
) {
  choiceLikeButton.disabled = disabled;
  choicePassButton.disabled = disabled;
}

function applyChoiceState(
  state
) {
  const myChoice =
    state.my_choice || "";

  const bothSelected =
    Boolean(state.both_selected);

  const matched =
    Boolean(state.matched);

  choiceLikeButton.classList.remove(
    "selected-like"
  );

  choicePassButton.classList.remove(
    "selected-pass"
  );

  matchedKakaoBox.classList.add(
    "hidden"
  );

  matchedKakaoNickname.textContent = "";

  if (myChoice === "like") {
    choiceLikeButton.classList.add(
      "selected-like"
    );

    setChoiceButtonsDisabled(true);
  } else if (myChoice === "pass") {
    choicePassButton.classList.add(
      "selected-pass"
    );

    setChoiceButtonsDisabled(true);
  } else {
    setChoiceButtonsDisabled(false);
  }

  if (bothSelected && matched) {
    const partnerKakaoNickname =
      state.partner_kakao_nickname || "";

    if (partnerKakaoNickname) {
      matchedKakaoNickname.textContent =
        partnerKakaoNickname;

      matchedKakaoBox.classList.remove(
        "hidden"
      );
    }
  }
}

function showNotice(
  text,
  success = false
) {
  noticeElement.textContent = text;

  noticeElement.className =
    "alert "
    + (success ? "success" : "error");

  setTimeout(
    () => {
      noticeElement.className =
        "alert hidden";
    },
    5000
  );
}

function disconnectExpiredRoom() {
  if (roomExpired) {
    return;
  }

  roomExpired = true;

  localStorage.removeItem(
    "contact_guard_last_room_code"
  );

  localStorage.removeItem(
    tokenStorageKey
  );

  showNotice(
    "채팅 시간이 종료되어 연결이 끊겼어."
  );

  inputElement.disabled = true;
  imageInputElement.disabled = true;

  const submitButton =
    document.querySelector(
      "#form button[type='submit'], #form button"
    );

  if (submitButton) {
    submitButton.disabled = true;
  }

  setTimeout(
    () => {
      window.location.href = "/";
    },
    1500
  );
}

function updateRoomTimer() {
  const timerElement =
    document.getElementById("room-timer");

  if (!roomExpiresAt) {
    timerElement.textContent = "10:00";
    return;
  }

  const expiresAtMs =
    new Date(roomExpiresAt).getTime();

  const remainingSeconds = Math.max(
    0,
    Math.ceil(
      (expiresAtMs - Date.now()) / 1000
    )
  );

  const minutes = Math.floor(
    remainingSeconds / 60
  );

  const seconds =
    remainingSeconds % 60;

  timerElement.textContent =
    `${String(minutes).padStart(2, "0")}:`
    + `${String(seconds).padStart(2, "0")}`;

  if (remainingSeconds <= 0) {
    disconnectExpiredRoom();
  }
}

setInterval(
  updateRoomTimer,
  250
);

function redirectToHomeOnUnauthorized(
  response
) {
  if (response.status === 410) {
    disconnectExpiredRoom();
    return true;
  }

  if (response.status === 409) {
    showNotice(
      "이미 두 개의 화면이 접속 중이야."
    );

    inputElement.disabled = true;
    imageInputElement.disabled = true;

    setTimeout(
      () => {
        window.location.href = "/";
      },
      1500
    );

    return true;
  }

  if (response.status !== 401) {
    return false;
  }

  console.warn(
    "채팅방 인증 확인이 일시적으로 실패했어."
  );

  showNotice(
    "서버 연결을 다시 확인하고 있어."
  );

  return true;
}

function addMessage(message) {
  if (renderedMessageIds.has(message.id)) {
    return;
  }

  renderedMessageIds.add(message.id);

  const article =
    document.createElement("article");

  article.className =
    "message "
    + (message.is_mine ? "mine" : "");

  const meta =
    document.createElement("div");

  meta.className = "meta";
  meta.textContent = message.nickname;

  const bubble =
    document.createElement("div");

  bubble.className = "bubble";

  if (message.message_type === "image") {
    const image =
      document.createElement("img");

    image.className = "chat-image";
    image.src = message.content;
    image.alt = "채팅 이미지";
    image.loading = "lazy";

    bubble.appendChild(image);
  } else {
    bubble.textContent = message.content;
  }

  article.append(
    meta,
    bubble
  );

  messagesElement.appendChild(
    article
  );

  messagesElement.scrollTop =
    messagesElement.scrollHeight;
}

async function registerConnection() {
  const response = await fetch(
    `/api/room/${roomCode}/connect`,
    {
      method: "POST",
      headers: authenticatedHeaders()
    }
  );

  if (
    redirectToHomeOnUnauthorized(response)
  ) {
    return false;
  }

  if (!response.ok) {
    return false;
  }

  const data = await response.json();

  roomExpiresAt =
    data.expires_at || "";

  updateRoomTimer();

  return true;
}

async function loadMessages() {
  if (isLoadingMessages) {
    return;
  }

  isLoadingMessages = true;

  try {
    const response = await fetch(
      `/api/room/${roomCode}/messages?after=${lastMessageId}`,
      {
        cache: "no-store",
        headers: authenticatedHeaders()
      }
    );

    if (
      redirectToHomeOnUnauthorized(response)
    ) {
      return;
    }

    if (!response.ok) {
      return;
    }

    const data = await response.json();

    document.getElementById(
      "member-count"
    ).textContent = data.member_count;

    roomExpiresAt =
      data.expires_at || "";

    updateRoomTimer();

    for (const message of data.messages) {
      lastMessageId = Math.max(
        lastMessageId,
        message.id
      );

      addMessage(message);
    }

  } catch (error) {
    console.error(error);

  } finally {
    isLoadingMessages = false;
  }
}

async function loadChoiceState() {
  const response = await fetch(
    `/api/room/${roomCode}/choice`,
    {
      cache: "no-store",
      headers: authenticatedHeaders()
    }
  );

  if (
    redirectToHomeOnUnauthorized(response)
  ) {
    return;
  }

  if (!response.ok) {
    return;
  }

  const data = await response.json();

  applyChoiceState(data);
}

async function sendChoice(choice) {
  setChoiceButtonsDisabled(true);

  try {
    const response = await fetch(
      `/api/room/${roomCode}/choice`,
      {
        method: "POST",
        headers: authenticatedHeaders(
          {
            "Content-Type":
              "application/json"
          }
        ),
        body: JSON.stringify(
          {
            choice
          }
        )
      }
    );

    if (
      redirectToHomeOnUnauthorized(response)
    ) {
      return;
    }

    const data = await response.json();

    if (!response.ok) {
      showNotice(
        data.message
        || "선택을 저장하지 못했어."
      );

      setChoiceButtonsDisabled(false);
      return;
    }

    applyChoiceState(data);

  } catch (error) {
    console.error(error);

    showNotice(
      "선택 저장 중 오류가 발생했어."
    );

    setChoiceButtonsDisabled(false);
  }
}

choiceLikeButton.addEventListener(
  "click",
  () => {
    const confirmed = window.confirm(
      "마음에 듦을 선택할까? "
      + "선택 후에는 바꿀 수 없어."
    );

    if (confirmed) {
      sendChoice("like");
    }
  }
);

choicePassButton.addEventListener(
  "click",
  () => {
    const confirmed = window.confirm(
      "안 맞음을 선택할까? "
      + "선택 후에는 바꿀 수 없어."
    );

    if (confirmed) {
      sendChoice("pass");
    }
  }
);

async function sendText(content) {
  return fetch(
    `/api/room/${roomCode}/messages`,
    {
      method: "POST",
      headers: authenticatedHeaders(
        {
          "Content-Type":
            "application/json"
        }
      ),
      body: JSON.stringify(
        {
          content
        }
      )
    }
  );
}

async function sendImage(file) {
  const formData = new FormData();

  formData.append(
    "image",
    file
  );

  return fetch(
    `/api/room/${roomCode}/image`,
    {
      method: "POST",
      headers: authenticatedHeaders(),
      body: formData
    }
  );
}

imageInputElement.addEventListener(
  "change",
  () => {
    const file =
      imageInputElement.files[0];

    fileNameElement.textContent =
      file
        ? `선택한 사진: ${file.name}`
        : "";
  }
);

document.getElementById(
  "form"
).addEventListener(
  "submit",
  async event => {
    event.preventDefault();

    const file =
      imageInputElement.files[0];

    const content =
      inputElement.value.trim();

    if (!file && !content) {
      showNotice(
        "메시지나 사진을 선택해."
      );

      return;
    }

    const button =
      event.submitter;

    button.disabled = true;

    button.textContent =
      file
        ? "이미지 최적화 중..."
        : "전송 중...";

    try {
      const response = file
        ? await sendImage(file)
        : await sendText(content);

      if (
        redirectToHomeOnUnauthorized(response)
      ) {
        return;
      }

      const data = await response.json();

      if (!response.ok) {
        const reasons =
          data.reasons?.length
            ? ` (${data.reasons.join(", ")})`
            : "";

        showNotice(
          `${data.message}${reasons}`
        );

        return;
      }

      inputElement.value = "";
      imageInputElement.value = "";
      fileNameElement.textContent = "";

      await loadMessages();

    } catch (error) {
      console.error(error);

      showNotice(
        "전송 중 오류가 발생했어."
      );

    } finally {
      button.disabled = false;
      button.textContent = "전송";
    }
  }
);

inputElement.addEventListener(
  "keydown",
  event => {
    if (
      event.key === "Enter"
      && !event.shiftKey
    ) {
      event.preventDefault();

      document
        .getElementById("form")
        .requestSubmit();
    }
  }
);

function releaseConnection() {
  const payload = JSON.stringify(
    {
      connection_id: connectionId
    }
  );

  navigator.sendBeacon(
    `/api/room/${roomCode}/disconnect`,
    new Blob(
      [payload],
      {
        type: "application/json"
      }
    )
  );
}

window.addEventListener(
  "pagehide",
  releaseConnection
);

document.getElementById(
  "leave-room"
).addEventListener(
  "click",
  event => {
    const confirmed = window.confirm(
      "채팅방에서 나갈까? "
      + "10분이 끝나기 전에는 "
      + "메인 화면의 재접속 버튼으로 "
      + "다시 들어올 수 있어."
    );

    if (!confirmed) {
      event.preventDefault();
    }
  }
);

document.getElementById(
  "copy"
).addEventListener(
  "click",
  async () => {
    await navigator.clipboard.writeText(
      roomCode
    );

    showNotice(
      "방 코드를 복사했어.",
      true
    );
  }
);

async function pollMessages() {
  await loadMessages();
  await loadChoiceState();

  setTimeout(
    pollMessages,
    1200
  );
}

async function initializeConnection() {
  try {
    const connected =
      await registerConnection();

    if (!connected) {
      return;
    }

    pollMessages();

  } catch (error) {
    console.error(error);

    showNotice(
      "채팅방 연결 중 오류가 발생했어."
    );
  }
}

initializeConnection();
</script>
</body>
</html>
"""

ADMIN_LOGIN_HTML = """
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">

  <meta
    name="viewport"
    content="width=device-width,initial-scale=1"
  >

  <title>관리자</title>

  """ + STYLE + """
</head>

<body>
  <main class="page narrow">
    <form
      class="card"
      method="post"
    >
      <h1>관리자 로그인</h1>

      {% if error %}
        <div class="alert error">
          {{ error }}
        </div>
      {% endif %}

      <label>비밀번호</label>

      <input
        type="password"
        name="password"
        required
      >

      <button class="primary full">
        로그인
      </button>
    </form>
  </main>
</body>
</html>
"""

ADMIN_HTML = """
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">

  <meta
    name="viewport"
    content="width=device-width,initial-scale=1"
  >

  <title>관리자</title>

  """ + STYLE + """
</head>

<body>
  <main class="page">
    <header class="header">
      <div>
        <h1>관리자</h1>

        <p class="muted small">
          DB: {{ db_path }}
        </p>
      </div>

      <form
        method="post"
        action="{{ url_for('admin_logout') }}"
      >
        <button>
          로그아웃
        </button>
      </form>
    </header>

    <section class="card">
      <h2>방 현황</h2>

      <div class="table-wrap">
        <table>
          <tr>
            <th>방</th>
            <th>참여자</th>
            <th>정상 메시지</th>
            <th>생성</th>
          </tr>

          {% for room in rooms %}
            <tr>
              <td>{{ room.room_code }}</td>
              <td>{{ room.member_count }}</td>
              <td>{{ room.message_count }}</td>
              <td>{{ room.created_at }}</td>
            </tr>
          {% else %}
            <tr>
              <td colspan="4">
                방이 없어.
              </td>
            </tr>
          {% endfor %}
        </table>
      </div>
    </section>

    <section class="card">
      <h2>차단 기록</h2>

      <div class="table-wrap">
        <table>
          <tr>
            <th>시간</th>
            <th>방</th>
            <th>닉네임</th>
            <th>유형</th>
            <th>사유</th>
            <th>원문</th>
          </tr>

          {% for row in blocked %}
            <tr>
              <td>{{ row.created_at }}</td>
              <td>{{ row.room_code }}</td>
              <td>{{ row.nickname }}</td>
              <td>{{ row.message_type }}</td>
              <td>{{ row.reasons }}</td>
              <td>{{ row.content }}</td>
            </tr>
          {% else %}
            <tr>
              <td colspan="6">
                차단 기록이 없어.
              </td>
            </tr>
          {% endfor %}
        </table>
      </div>
    </section>
  </main>
</body>
</html>
"""


# =========================================================
# 화면
# =========================================================

@app.route("/")
def home():
    return render_template_string(
        HOME_HTML
    )


@app.route(
    "/create-room",
    methods=["POST"],
)
def create_room():
    nickname = request.form.get(
        "nickname",
        "",
    ).strip()[:20]

    kakao_nickname = request.form.get(
        "kakao_nickname",
        "",
    ).strip()[:30]

    if not nickname or not kakao_nickname:
        return render_template_string(
            HOME_HTML,
            error=(
                "채팅 닉네임과 "
                "카카오톡 닉네임을 "
                "모두 입력해줘."
            ),
        )

    room_code = generate_room_code()
    user_token = generate_user_token()
    created_at = now_kst_iso()

    with db_connect() as connection:
        connection.execute(
            """
            INSERT INTO rooms(
                room_code,
                created_at
            )
            VALUES (?, ?)
            """,
            (
                room_code,
                created_at,
            ),
        )

        connection.execute(
            """
            INSERT INTO members(
                room_code,
                user_token,
                nickname,
                kakao_nickname,
                joined_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                room_code,
                user_token,
                nickname,
                kakao_nickname,
                created_at,
            ),
        )

    return redirect(
        url_for(
            "chat_room",
            room_code=room_code,
            token=user_token,
        )
    )


@app.route(
    "/join-room",
    methods=["POST"],
)
def join_room():
    room_code = request.form.get(
        "room_code",
        "",
    ).upper().strip()

    nickname = request.form.get(
        "nickname",
        "",
    ).strip()[:20]

    kakao_nickname = request.form.get(
        "kakao_nickname",
        "",
    ).strip()[:30]

    if (
        not room_code
        or not nickname
        or not kakao_nickname
    ):
        return render_template_string(
            HOME_HTML,
            error=(
                "방 코드, 채팅 닉네임, "
                "카카오톡 닉네임을 "
                "모두 입력해줘."
            ),
        )

    with db_connect() as connection:
        room = connection.execute(
            """
            SELECT 1
            FROM rooms
            WHERE room_code = ?
            """,
            (room_code,),
        ).fetchone()

    if not room:
        return render_template_string(
            HOME_HTML,
            error="존재하지 않는 방 코드야.",
        )

    if room_member_count(room_code) >= 2:
        return render_template_string(
            HOME_HTML,
            error="이미 두 명이 입장한 방이야.",
        )

    user_token = generate_user_token()
    joined_at = now_kst_iso()

    with db_connect() as connection:
        connection.execute(
            """
            INSERT INTO members(
                room_code,
                user_token,
                nickname,
                kakao_nickname,
                joined_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                room_code,
                user_token,
                nickname,
                kakao_nickname,
                joined_at,
            ),
        )

    start_room_session_if_ready(
        room_code
    )

    return redirect(
        url_for(
            "chat_room",
            room_code=room_code,
            token=user_token,
        )
    )


@app.route(
    "/room/<room_code>",
    methods=["GET"],
)
def chat_room(room_code: str):
    normalized_room_code = (
        room_code.upper().strip()
    )

    user_token = request.args.get(
        "token",
        "",
    ).strip()

    member = get_room_member(
        normalized_room_code,
        user_token,
    )

    if member is None:
        return redirect(
            url_for("home")
        )

    is_expired, _ = get_room_time_state(
        normalized_room_code
    )

    if is_expired:
        return redirect(
            url_for("home")
        )

    return render_template_string(
        CHAT_HTML,
        room_code=normalized_room_code,
        nickname=member["nickname"],
        user_token=user_token,
    )


# =========================================================
# 재접속 상태 확인 API
# =========================================================

@app.route(
    "/api/room/<room_code>/reconnect-status",
    methods=["GET"],
)
def reconnect_status(room_code: str):
    normalized_room_code = (
        room_code.upper().strip()
    )

    user_token = get_request_user_token()

    member = get_room_member(
        normalized_room_code,
        user_token,
    )

    if member is None:
        return jsonify(
            {
                "ok": True,
                "can_reconnect": False,
            }
        )

    is_expired, expires_at = (
        get_room_time_state(
            normalized_room_code
        )
    )

    if is_expired:
        return jsonify(
            {
                "ok": True,
                "can_reconnect": False,
                "expired": True,
                "expires_at": expires_at,
            }
        )

    return jsonify(
        {
            "ok": True,
            "can_reconnect": True,
            "expires_at": expires_at,
        }
    )


# =========================================================
# 접속 화면 관리 API
# =========================================================

@app.route(
    "/api/room/<room_code>/connect",
    methods=["POST"],
)
def connect_room_screen(room_code: str):
    normalized_room_code = (
        room_code.upper().strip()
    )

    user_token = get_request_user_token()
    connection_id = get_request_connection_id()

    member = get_room_member(
        normalized_room_code,
        user_token,
    )

    if member is None:
        return jsonify(
            {
                "ok": False,
                "message": (
                    "채팅방 인증이 만료됐어."
                ),
            }
        ), 401

    is_expired, expires_at = (
        get_room_time_state(
            normalized_room_code
        )
    )

    if is_expired:
        return jsonify(
            {
                "ok": False,
                "expired": True,
                "message": (
                    "채팅 시간이 종료되어 "
                    "연결이 끊겼어."
                ),
                "expires_at": expires_at,
            }
        ), 410

    if not connection_id:
        return jsonify(
            {
                "ok": False,
                "message": (
                    "접속 화면 정보를 "
                    "확인할 수 없어."
                ),
            }
        ), 400

    if not register_active_connection(
        normalized_room_code,
        user_token,
        connection_id,
    ):
        return jsonify(
            {
                "ok": False,
                "connection_rejected": True,
                "message": (
                    "이미 두 개의 화면이 "
                    "접속 중이야."
                ),
            }
        ), 409

    return jsonify(
        {
            "ok": True,
            "expires_at": expires_at,
        }
    )


@app.route(
    "/api/room/<room_code>/disconnect",
    methods=["POST"],
)
def disconnect_room_screen(room_code: str):
    normalized_room_code = (
        room_code.upper().strip()
    )

    connection_id = (
        request.get_json(
            silent=True
        )
        or {}
    ).get(
        "connection_id",
        "",
    )

    remove_active_connection(
        normalized_room_code,
        str(connection_id).strip(),
    )

    return jsonify(
        {
            "ok": True,
        }
    )


# =========================================================
# 호감 선택 API
# =========================================================

@app.route(
    "/api/room/<room_code>/choice",
    methods=["GET"],
)
@require_room_member_api
def get_room_choice(room_code: str):
    user_token = request.user_token

    with db_connect() as connection:
        my_row = connection.execute(
            """
            SELECT choice
            FROM room_choices
            WHERE room_code = ?
              AND user_token = ?
            """,
            (
                room_code,
                user_token,
            ),
        ).fetchone()

        rows = connection.execute(
            """
            SELECT
                rc.user_token,
                rc.choice,
                m.kakao_nickname
            FROM room_choices rc
            JOIN members m
              ON m.room_code = rc.room_code
             AND m.user_token = rc.user_token
            WHERE rc.room_code = ?
            """,
            (room_code,),
        ).fetchall()

    my_choice = (
        my_row["choice"]
        if my_row is not None
        else ""
    )

    both_selected = (
        len(rows) >= 2
    )

    matched = (
        both_selected
        and all(
            row["choice"] == "like"
            for row in rows[:2]
        )
    )

    partner_kakao_nickname = ""

    if matched:
        for row in rows:
            if row["user_token"] != user_token:
                partner_kakao_nickname = (
                    row["kakao_nickname"]
                    or ""
                )
                break

    return jsonify(
        {
            "ok": True,
            "my_choice": my_choice,
            "both_selected": both_selected,
            "matched": matched,
            "partner_kakao_nickname": (
                partner_kakao_nickname
            ),
        }
    )


@app.route(
    "/api/room/<room_code>/choice",
    methods=["POST"],
)
@require_room_member_api
def set_room_choice(room_code: str):
    payload = (
        request.get_json(silent=True)
        or {}
    )

    choice = str(
        payload.get(
            "choice",
            "",
        )
    ).strip().lower()

    if choice not in {
        "like",
        "pass",
    }:
        return jsonify(
            {
                "ok": False,
                "message": (
                    "선택 값이 올바르지 않아."
                ),
            }
        ), 400

    user_token = request.user_token
    selected_at = now_kst_iso()

    with db_connect() as connection:
        existing = connection.execute(
            """
            SELECT 1
            FROM room_choices
            WHERE room_code = ?
              AND user_token = ?
            """,
            (
                room_code,
                user_token,
            ),
        ).fetchone()

        if existing is not None:
            return jsonify(
                {
                    "ok": False,
                    "message": (
                        "이미 선택을 완료했어."
                    ),
                }
            ), 409

        connection.execute(
            """
            INSERT INTO room_choices(
                room_code,
                user_token,
                choice,
                selected_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                room_code,
                user_token,
                choice,
                selected_at,
            ),
        )

        rows = connection.execute(
            """
            SELECT
                rc.user_token,
                rc.choice,
                m.kakao_nickname
            FROM room_choices rc
            JOIN members m
              ON m.room_code = rc.room_code
             AND m.user_token = rc.user_token
            WHERE rc.room_code = ?
            """,
            (room_code,),
        ).fetchall()

    both_selected = (
        len(rows) >= 2
    )

    matched = (
        both_selected
        and all(
            row["choice"] == "like"
            for row in rows[:2]
        )
    )

    partner_kakao_nickname = ""

    if matched:
        for row in rows:
            if row["user_token"] != user_token:
                partner_kakao_nickname = (
                    row["kakao_nickname"]
                    or ""
                )
                break

    return jsonify(
        {
            "ok": True,
            "my_choice": choice,
            "both_selected": both_selected,
            "matched": matched,
            "partner_kakao_nickname": (
                partner_kakao_nickname
            ),
        }
    )


# =========================================================
# 메시지 API
# =========================================================

@app.route(
    "/api/room/<room_code>/messages",
    methods=["GET"],
)
@require_room_member_api
def get_messages(room_code: str):
    try:
        after_id = max(
            0,
            int(
                request.args.get(
                    "after",
                    "0",
                )
            ),
        )

    except ValueError:
        after_id = 0

    with db_connect() as connection:
        rows = connection.execute(
            """
            SELECT
                id,
                user_token,
                nickname,
                message_type,
                content,
                created_at
            FROM messages
            WHERE room_code = ?
              AND id > ?
            ORDER BY id ASC
            LIMIT 200
            """,
            (
                room_code,
                after_id,
            ),
        ).fetchall()

    user_token = request.user_token

    messages = []

    for row in rows:
        content = row["content"]

        if row["message_type"] == "image":
            content = url_for(
                "uploaded_image",
                filename=content,
            )

        messages.append(
            {
                "id": row["id"],
                "nickname": row["nickname"],
                "message_type": row[
                    "message_type"
                ],
                "content": content,
                "created_at": row[
                    "created_at"
                ],
                "is_mine": (
                    row["user_token"]
                    == user_token
                ),
            }
        )

    is_expired, expires_at = (
        get_room_time_state(
            room_code
        )
    )

    if is_expired:
        return jsonify(
            {
                "ok": False,
                "expired": True,
                "message": (
                    "채팅 시간이 종료되어 "
                    "연결이 끊겼어."
                ),
                "expires_at": expires_at,
            }
        ), 410

    return jsonify(
        {
            "messages": messages,
            "member_count": room_member_count(
                room_code
            ),
            "expires_at": expires_at,
        }
    )


@app.route(
    "/api/room/<room_code>/messages",
    methods=["POST"],
)
@require_room_member_api
def send_message(room_code: str):
    payload = (
        request.get_json(silent=True)
        or {}
    )

    content = str(
        payload.get(
            "content",
            "",
        )
    ).strip()

    if not content:
        return jsonify(
            {
                "ok": False,
                "message": (
                    "메시지가 비어 있어."
                ),
            }
        ), 400

    if len(content) > 500:
        return jsonify(
            {
                "ok": False,
                "message": (
                    "메시지는 500자까지 "
                    "보낼 수 있어."
                ),
            }
        ), 400

    member = request.room_member
    user_token = request.user_token
    created_at = now_kst_iso()

    reasons = detect_contact_info(
        content
    )

    if reasons:
        with db_connect() as connection:
            connection.execute(
                """
                INSERT INTO blocked_messages(
                    room_code,
                    user_token,
                    nickname,
                    message_type,
                    content,
                    reasons,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    room_code,
                    user_token,
                    member["nickname"],
                    "text",
                    content,
                    ", ".join(reasons),
                    created_at,
                ),
            )

        return jsonify(
            {
                "ok": False,
                "blocked": True,
                "message": (
                    "연락처 또는 외부 연락 "
                    "수단이 감지되어 "
                    "전송하지 않았어."
                ),
                "reasons": reasons,
            }
        ), 400

    with db_connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO messages(
                room_code,
                user_token,
                nickname,
                message_type,
                content,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                room_code,
                user_token,
                member["nickname"],
                "text",
                content,
                created_at,
            ),
        )

    return jsonify(
        {
            "ok": True,
            "message_id": cursor.lastrowid,
        }
    )


@app.route(
    "/api/room/<room_code>/image",
    methods=["POST"],
)
@require_room_member_api
def send_image(room_code: str):
    uploaded_file = request.files.get(
        "image"
    )

    if uploaded_file is None:
        return jsonify(
            {
                "ok": False,
                "message": (
                    "이미지가 선택되지 않았어."
                ),
            }
        ), 400

    raw_bytes = uploaded_file.read()

    image_path = None
    filename = ""
    ocr_text = ""
    reasons = []

    try:
        filename, image_path = save_clean_image(
            raw_bytes
        )

        # =================================================
        # OCR 임시 비활성화
        # =================================================
        #
        # ocr_text = extract_text_from_image(
        #     image_path
        # )
        #
        # reasons = detect_contact_info(
        #     ocr_text
        # )

        ocr_text = ""
        reasons = []

    except ValueError as error:
        if image_path:
            try:
                Path(
                    image_path
                ).unlink(
                    missing_ok=True
                )

            except OSError:
                pass

        return jsonify(
            {
                "ok": False,
                "message": str(
                    error
                ),
            }
        ), 400

    except Exception:
        import traceback

        traceback.print_exc()

        if image_path:
            try:
                Path(
                    image_path
                ).unlink(
                    missing_ok=True
                )

            except OSError:
                pass

        return jsonify(
            {
                "ok": False,
                "message": (
                    "이미지 처리 중 "
                    "오류가 발생했어."
                ),
            }
        ), 500

    member = request.room_member
    user_token = request.user_token
    created_at = now_kst_iso()

    # OCR를 다시 켰을 때 사용하는 차단 처리
    # 현재 reasons는 빈 리스트이므로 실행되지 않음
    if reasons:
        if image_path:
            try:
                Path(
                    image_path
                ).unlink(
                    missing_ok=True
                )

            except OSError as error:
                print(
                    "[IMAGE] blocked image "
                    "delete failed: "
                    f"{error}",
                    flush=True,
                )

        with db_connect() as connection:
            connection.execute(
                """
                INSERT INTO blocked_messages(
                    room_code,
                    user_token,
                    nickname,
                    message_type,
                    content,
                    reasons,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    room_code,
                    user_token,
                    member["nickname"],
                    "image",
                    ocr_text[:2000],
                    ", ".join(
                        reasons
                    ),
                    created_at,
                ),
            )

        return jsonify(
            {
                "ok": False,
                "blocked": True,
                "message": (
                    "이미지에서 연락처 또는 "
                    "외부 연락 수단이 감지되어 "
                    "전송하지 않았어."
                ),
                "reasons": reasons,
            }
        ), 400

    with db_connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO messages(
                room_code,
                user_token,
                nickname,
                message_type,
                content,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                room_code,
                user_token,
                member["nickname"],
                "image",
                filename,
                created_at,
            ),
        )

    return jsonify(
        {
            "ok": True,
            "message_id": cursor.lastrowid,
        }
    )


@app.route(
    "/uploads/<path:filename>",
    methods=["GET"],
)
def uploaded_image(filename: str):
    return send_from_directory(
        UPLOAD_DIR,
        filename,
        conditional=True,
    )


# =========================================================
# 관리자
# =========================================================

def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get(
            "admin_authenticated"
        ):
            return redirect(
                url_for("admin_login")
            )

        return view(*args, **kwargs)

    return wrapped


@app.route(
    "/admin/login",
    methods=["GET", "POST"],
)
def admin_login():
    error = None

    if request.method == "POST":
        password = request.form.get(
            "password",
            "",
        )

        if secrets.compare_digest(
            password,
            ADMIN_PASSWORD,
        ):
            session[
                "admin_authenticated"
            ] = True

            return redirect(
                url_for("admin_dashboard")
            )

        error = "비밀번호가 맞지 않아."

    return render_template_string(
        ADMIN_LOGIN_HTML,
        error=error,
    )


@app.route(
    "/admin/logout",
    methods=["POST"],
)
def admin_logout():
    session.pop(
        "admin_authenticated",
        None,
    )

    return redirect(
        url_for("admin_login")
    )


@app.route(
    "/admin",
    methods=["GET"],
)
@admin_required
def admin_dashboard():
    with db_connect() as connection:
        blocked = connection.execute(
            """
            SELECT
                room_code,
                nickname,
                message_type,
                content,
                reasons,
                created_at
            FROM blocked_messages
            ORDER BY id DESC
            LIMIT 300
            """
        ).fetchall()

        rooms = connection.execute(
            """
            SELECT
                r.room_code,
                r.created_at,
                COUNT(
                    DISTINCT m.user_token
                ) AS member_count,
                COUNT(
                    DISTINCT msg.id
                ) AS message_count
            FROM rooms r
            LEFT JOIN members m
                ON m.room_code = r.room_code
            LEFT JOIN messages msg
                ON msg.room_code = r.room_code
            GROUP BY
                r.room_code,
                r.created_at
            ORDER BY r.created_at DESC
            LIMIT 100
            """
        ).fetchall()

    return render_template_string(
        ADMIN_HTML,
        blocked=blocked,
        rooms=rooms,
        db_path=DB_PATH,
    )


@app.route(
    "/health",
    methods=["GET"],
)
def health():
    return jsonify(
        {
            "status": "healthy",
            "service": (
                "contact-guard-chat"
            ),
            "auth_mode": (
                "room-token-header"
            ),
            "db_path": DB_PATH,
            "upload_dir": UPLOAD_DIR,
            "ocr_loaded": False,
            "ocr_concurrency": 0,
        }
    )
