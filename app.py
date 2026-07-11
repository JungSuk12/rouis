import io
import os
import re
import sqlite3
import secrets
import uuid
from datetime import datetime, timezone, timedelta
from functools import wraps
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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
from PIL import Image

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

KST = timezone(timedelta(hours=9))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", 1234)

PREFERRED_DB_PATH = os.environ.get(
    "CHAT_DB_PATH",
    "/var/data/contact_guard_chat.db",
).strip()

PREFERRED_UPLOAD_DIR = os.environ.get(
    "UPLOAD_DIR",
    "/var/data/contact_guard_uploads",
).strip()

PREFERRED_OCR_MODEL_DIR = os.environ.get(
    "OCR_MODEL_DIR",
    "/var/data/easyocr_models",
).strip()

LOCAL_DB_PATH = "contact_guard_chat.db"
LOCAL_UPLOAD_DIR = "contact_guard_uploads"
LOCAL_OCR_MODEL_DIR = "easyocr_models"

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_SIDE = 1280
JPEG_QUALITY = 82
ALLOWED_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP"}


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

OCR_MODEL_DIR = resolve_writable_path(
    PREFERRED_OCR_MODEL_DIR,
    LOCAL_OCR_MODEL_DIR,
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
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_code TEXT NOT NULL,
                user_token TEXT NOT NULL,
                nickname TEXT NOT NULL,
                joined_at TEXT NOT NULL,
                UNIQUE(room_code, user_token)
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
            """
        )

        # 기존 DB가 OCR 이전 버전이어도 자동 보정
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


def normalize_for_contact_detection(
    text: str,
) -> str:
    value = text.casefold()

    replacements = {
        "o": "0",
        "ｏ": "0",
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

    for source, target in replacements.items():
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
# OCR
# =========================================================

_ocr_reader = None


def get_ocr_reader():
    global _ocr_reader

    if _ocr_reader is None:
        import easyocr

        _ocr_reader = easyocr.Reader(
            ["ko", "en"],
            gpu=False,
            model_storage_directory=OCR_MODEL_DIR,
            download_enabled=True,
        )

    return _ocr_reader


def extract_text_from_image(
    image_path: str,
) -> str:
    reader = get_ocr_reader()

    results = reader.readtext(
        image_path,
        detail=0,
        paragraph=False,
    )

    return "\n".join(
        str(item).strip()
        for item in results
        if str(item).strip()
    )


# =========================================================
# 이미지 저장
# =========================================================

def save_clean_image(
    raw_bytes: bytes,
) -> Tuple[str, str]:
    if not raw_bytes:
        raise ValueError(
            "이미지 내용이 비어 있어."
        )

    if len(raw_bytes) > MAX_IMAGE_BYTES:
        raise ValueError(
            "이미지는 최대 8MB까지 가능해."
        )

    try:
        with Image.open(
            io.BytesIO(raw_bytes)
        ) as source_image:
            original_format = source_image.format

            if original_format not in ALLOWED_IMAGE_FORMATS:
                raise ValueError(
                    "JPG, PNG, WEBP 이미지만 가능해."
                )

            # 비율은 유지하고 최대 변만 줄인다.
            # 원본 해상도 이미지는 서버에 저장하지 않는다.
            source_image.thumbnail(
                (MAX_IMAGE_SIDE, MAX_IMAGE_SIDE),
                Image.Resampling.LANCZOS,
                reducing_gap=3.0,
            )

            # EXIF·알파 채널 등 불필요한 정보를 제거한다.
            image = source_image.convert("RGB")

    except ValueError:
        raise

    except Exception as error:
        raise ValueError(
            "이미지 파일을 읽을 수 없어."
        ) from error

    filename = (
        f"{uuid.uuid4().hex}.jpg"
    )

    save_path = (
        Path(UPLOAD_DIR)
        / filename
    )

    try:
        image.save(
            save_path,
            format="JPEG",
            quality=JPEG_QUALITY,
            optimize=True,
            progressive=True,
        )

    finally:
        image.close()

    return (
        filename,
        str(save_path),
    )


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

        request.room_member = member
        request.user_token = user_token

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
  display: grid;
  grid-template-rows: auto 1fr auto auto;
  gap: 12px;
}
.header {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: flex-start;
}
.messages {
  overflow-y: auto;
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
  .grid { grid-template-columns: 1fr; }
  .composer {
    grid-template-columns: auto 1fr;
  }
  .composer button {
    grid-column: 1 / -1;
  }
  .message { width: 90%; }
  .header { flex-direction: column; }
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

    <section class="grid">
      <form
        class="card"
        method="post"
        action="{{ url_for('create_room') }}"
      >
        <h2>새 방 만들기</h2>

        <label>닉네임</label>

        <input
          name="nickname"
          maxlength="20"
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

        <label>닉네임</label>

        <input
          name="nickname"
          maxlength="20"
          required
        >

        <button class="primary full">
          입장하기
        </button>
      </form>
    </section>
  </main>
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
        </p>
      </div>

      <a href="{{ url_for('home') }}">
        나가기
      </a>
    </header>

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

      <button
        id="send-button"
        class="primary"
        type="submit"
      >
        전송
      </button>
    </form>
  </main>

<script>
const roomCode = "{{ room_code }}";
const initialToken = "{{ user_token }}";
const tokenStorageKey = `contact_guard_token_${roomCode}`;

localStorage.setItem(
  tokenStorageKey,
  initialToken
);

const userToken =
  localStorage.getItem(tokenStorageKey)
  || initialToken;

let lastMessageId = 0;

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

function authenticatedHeaders(extra = {}) {
  return {
    "X-User-Token": userToken,
    ...extra
  };
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

function redirectToHomeOnUnauthorized(
  response
) {
  if (response.status === 401) {
    localStorage.removeItem(
      tokenStorageKey
    );

    window.location.href = "/";

    return true;
  }

  return false;
}

function addMessage(message) {
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

async function loadMessages() {
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

    for (const message of data.messages) {
      addMessage(message);

      lastMessageId = Math.max(
        lastMessageId,
        message.id
      );
    }

  } catch (error) {
    console.error(error);
  }
}

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

function resizeImageFile(
  file,
  maxSide = 1280,
  quality = 0.82
) {
  return new Promise(
    (resolve, reject) => {
      const imageUrl =
        URL.createObjectURL(file);

      const image =
        new Image();

      image.onload = () => {
        try {
          let width =
            image.naturalWidth;

          let height =
            image.naturalHeight;

          if (
            width > maxSide
            || height > maxSide
          ) {
            const scale =
              Math.min(
                maxSide / width,
                maxSide / height
              );

            width =
              Math.max(
                1,
                Math.round(
                  width * scale
                )
              );

            height =
              Math.max(
                1,
                Math.round(
                  height * scale
                )
              );
          }

          const canvas =
            document.createElement(
              "canvas"
            );

          canvas.width = width;
          canvas.height = height;

          const context =
            canvas.getContext("2d");

          if (!context) {
            throw new Error(
              "이미지 변환을 시작할 수 없어."
            );
          }

          context.fillStyle =
            "#ffffff";

          context.fillRect(
            0,
            0,
            width,
            height
          );

          context.drawImage(
            image,
            0,
            0,
            width,
            height
          );

          canvas.toBlob(
            blob => {
              URL.revokeObjectURL(
                imageUrl
              );

              if (!blob) {
                reject(
                  new Error(
                    "이미지 압축에 실패했어."
                  )
                );

                return;
              }

              resolve(blob);
            },
            "image/jpeg",
            quality
          );

        } catch (error) {
          URL.revokeObjectURL(
            imageUrl
          );

          reject(error);
        }
      };

      image.onerror = () => {
        URL.revokeObjectURL(
          imageUrl
        );

        reject(
          new Error(
            "선택한 이미지를 읽을 수 없어."
          )
        );
      };

      image.src = imageUrl;
    }
  );
}


async function sendImage(file) {
  const resizedBlob =
    await resizeImageFile(
      file,
      1280,
      0.82
    );

  const formData =
    new FormData();

  formData.append(
    "image",
    resizedBlob,
    "upload.jpg"
  );

  return fetch(
    `/api/room/${roomCode}/image`,
    {
      method: "POST",
      headers:
        authenticatedHeaders(),
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
      document.getElementById(
        "send-button"
      );

    button.disabled = true;

    button.textContent =
      file
        ? "OCR 검사 중..."
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

loadMessages();

setInterval(
  loadMessages,
  1200
);
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
            <th>원문/OCR</th>
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

    if not nickname:
        return render_template_string(
            HOME_HTML,
            error="닉네임을 입력해줘.",
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
                joined_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                room_code,
                user_token,
                nickname,
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

    if not room_code or not nickname:
        return render_template_string(
            HOME_HTML,
            error=(
                "방 코드와 닉네임을 "
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
                joined_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                room_code,
                user_token,
                nickname,
                joined_at,
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

    return render_template_string(
        CHAT_HTML,
        room_code=normalized_room_code,
        nickname=member["nickname"],
        user_token=user_token,
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

    return jsonify(
        {
            "messages": messages,
            "member_count": room_member_count(
                room_code
            ),
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

    try:
        filename, image_path = (
            save_clean_image(raw_bytes)
        )

    except ValueError as error:
        return jsonify(
            {
                "ok": False,
                "message": str(error),
            }
        ), 400

    member = request.room_member
    user_token = request.user_token
    created_at = now_kst_iso()

    try:
        ocr_text = extract_text_from_image(
            image_path
        )

    except Exception as error:
        print(
            "[ERROR] OCR 처리 실패: "
            f"{error}"
        )

        try:
            os.remove(image_path)
        except OSError:
            pass

        return jsonify(
            {
                "ok": False,
                "message": (
                    "OCR 검사에 실패해서 "
                    "이미지를 전송하지 않았어."
                ),
            }
        ), 500

    reasons = detect_contact_info(
        ocr_text
    )

    if reasons:
        try:
            os.remove(image_path)
        except OSError:
            pass

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
                    (
                        ocr_text
                        or "(OCR 텍스트 없음)"
                    ),
                    ", ".join(reasons),
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
            "ocr_text": ocr_text,
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
                "contact-guard-chat-ocr"
            ),
            "auth_mode": (
                "room-token-header"
            ),
            "db_path": DB_PATH,
            "upload_dir": UPLOAD_DIR,
        }
    )


if __name__ == "__main__":
    port = int(
        os.environ.get(
            "PORT",
            "10000",
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
    )
