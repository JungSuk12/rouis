import io
import os
import re
import sqlite3
import secrets
import uuid
from datetime import datetime, timezone, timedelta
from functools import wraps
from pathlib import Path
from typing import Dict, List

from flask import Flask, jsonify, redirect, render_template_string, request, send_file, session, url_for
from PIL import Image, UnidentifiedImageError

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

KST = timezone(timedelta(hours=9))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "change-me")
PREFERRED_DB_PATH = os.environ.get("CHAT_DB_PATH", "/var/data/contact_guard_chat.db").strip()
LOCAL_DB_PATH = "contact_guard_chat.db"
MAX_IMAGE_BYTES = 5 * 1024 * 1024
PREFERRED_UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "/var/data/contact_guard_uploads").strip()
LOCAL_UPLOAD_DIR = "contact_guard_uploads"
OCR_MODEL_DIR = os.environ.get("OCR_MODEL_DIR", "/var/data/easyocr_models").strip()
_ocr_reader = None


def get_db_path() -> str:
    preferred = Path(PREFERRED_DB_PATH)
    try:
        preferred.parent.mkdir(parents=True, exist_ok=True)
        test_file = preferred.parent / ".write_test"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink(missing_ok=True)
        return str(preferred)
    except OSError:
        return LOCAL_DB_PATH


DB_PATH = get_db_path()


def get_upload_dir() -> Path:
    preferred = Path(PREFERRED_UPLOAD_DIR)
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        test_file = preferred / ".write_test"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink(missing_ok=True)
        return preferred
    except OSError:
        local = Path(LOCAL_UPLOAD_DIR)
        local.mkdir(parents=True, exist_ok=True)
        return local


UPLOAD_DIR = get_upload_dir()


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db_connect() as conn:
        conn.executescript(
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
                content TEXT NOT NULL,
                message_type TEXT NOT NULL DEFAULT 'text',
                file_name TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS blocked_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_code TEXT NOT NULL,
                user_token TEXT NOT NULL,
                nickname TEXT NOT NULL,
                content TEXT NOT NULL,
                reasons TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )

        columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
        if "message_type" not in columns:
            conn.execute("ALTER TABLE messages ADD COLUMN message_type TEXT NOT NULL DEFAULT 'text'")
        if "file_name" not in columns:
            conn.execute("ALTER TABLE messages ADD COLUMN file_name TEXT")


init_db()


def now_kst_iso() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


KOREAN_DIGIT_WORDS: Dict[str, str] = {
    "공": "0", "영": "0", "빵": "0",
    "일": "1", "하나": "1",
    "이": "2", "둘": "2",
    "삼": "3", "셋": "3",
    "사": "4", "넷": "4",
    "오": "5", "다섯": "5",
    "육": "6", "여섯": "6",
    "칠": "7", "일곱": "7",
    "팔": "8", "여덟": "8",
    "구": "9", "아홉": "9",
}

CONTACT_KEYWORDS = [
    "전화번호", "전번", "폰번", "연락처", "번호교환", "번호 교환",
    "카톡아이디", "카톡 아이디", "카카오아이디", "카카오 아이디",
    "텔레그램", "인스타", "인스타그램", "디엠", "dm",
    "오픈채팅", "오픈톡", "오픈링크",
]

URL_PATTERN = re.compile(
    r"(?i)\b(?:https?://|www\.|open\.kakao\.com|pf\.kakao\.com|t\.me/|instagram\.com/)\S+"
)
EMAIL_PATTERN = re.compile(r"(?i)\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b")
PHONE_PATTERN = re.compile(
    r"(?<!\d)(?:\+?82[\s.\-)]*|0)[\s.\-()]*1[016789](?:[\s.\-()]*\d){7,8}(?!\d)"
)
SNS_ID_PATTERN = re.compile(
    r"(?i)(?:카톡|카카오|텔레그램|인스타|instagram|telegram|line|라인)"
    r"\s*(?:아이디|id|계정)?\s*[:：]?\s*[@a-z0-9_.\-]{4,}"
)


def normalize_for_contact_detection(text: str) -> str:
    value = text.casefold()
    replacements = {
        "o": "0", "ｏ": "0", "l": "1", "i": "1", "|": "1",
        "０": "0", "１": "1", "２": "2", "３": "3", "４": "4",
        "５": "5", "６": "6", "７": "7", "８": "8", "９": "9",
    }
    for source, target in replacements.items():
        value = value.replace(source, target)

    for word in sorted(KOREAN_DIGIT_WORDS, key=len, reverse=True):
        value = value.replace(word, KOREAN_DIGIT_WORDS[word])

    return value


def compact_numeric_text(text: str) -> str:
    return re.sub(r"[^0-9]", "", normalize_for_contact_detection(text))


def detect_contact_info(text: str) -> List[str]:
    reasons: List[str] = []
    lowered = text.casefold()
    normalized = normalize_for_contact_detection(text)
    compact_digits = compact_numeric_text(text)

    if any(keyword.casefold() in lowered for keyword in CONTACT_KEYWORDS):
        reasons.append("연락처 교환 표현")
    if URL_PATTERN.search(text):
        reasons.append("외부 링크")
    if EMAIL_PATTERN.search(text):
        reasons.append("이메일 주소")
    if PHONE_PATTERN.search(normalized):
        reasons.append("전화번호 형식")
    if re.search(r"01[016789]\d{7,8}", compact_digits):
        reasons.append("우회 전화번호 형식")
    elif re.search(r"(?<!\d)\d{8,12}(?!\d)", compact_digits):
        reasons.append("긴 숫자열")
    if SNS_ID_PATTERN.search(text):
        reasons.append("SNS·메신저 ID")

    return list(dict.fromkeys(reasons))



def get_ocr_reader():
    global _ocr_reader
    if _ocr_reader is None:
        import easyocr

        model_dir = Path(OCR_MODEL_DIR)
        try:
            model_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            model_dir = Path("easyocr_models")
            model_dir.mkdir(parents=True, exist_ok=True)

        _ocr_reader = easyocr.Reader(
            ["ko", "en"],
            gpu=False,
            model_storage_directory=str(model_dir),
            user_network_directory=str(model_dir),
            download_enabled=True,
            verbose=False,
        )
    return _ocr_reader


def read_image_and_ocr(raw: bytes) -> tuple[Image.Image, str]:
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("이미지는 최대 5MB까지 올릴 수 있어.")

    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except (UnidentifiedImageError, OSError) as error:
        raise ValueError("정상적인 이미지 파일이 아니야.") from error

    if image.width < 20 or image.height < 20:
        raise ValueError("이미지가 너무 작아.")
    if image.width * image.height > 25_000_000:
        raise ValueError("이미지 해상도가 너무 커.")

    image = image.convert("RGB")

    import numpy as np
    reader = get_ocr_reader()
    detected = reader.readtext(np.array(image), detail=0, paragraph=False)
    ocr_text = "\n".join(str(item).strip() for item in detected if str(item).strip())
    return image, ocr_text


def save_clean_image(image: Image.Image) -> str:
    file_name = f"{uuid.uuid4().hex}.jpg"
    file_path = UPLOAD_DIR / file_name
    image.save(file_path, format="JPEG", quality=88, optimize=True)
    return file_name


def generate_room_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(6))
        with db_connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM rooms WHERE room_code = ?",
                (code,),
            ).fetchone()
        if not exists:
            return code


def current_user_token() -> str:
    token = session.get("user_token")
    if not token:
        token = secrets.token_urlsafe(18)
        session["user_token"] = token
    return token


def get_room_member(room_code: str, user_token: str):
    with db_connect() as conn:
        return conn.execute(
            "SELECT * FROM members WHERE room_code = ? AND user_token = ?",
            (room_code, user_token),
        ).fetchone()


def room_member_count(room_code: str) -> int:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM members WHERE room_code = ?",
            (room_code,),
        ).fetchone()
    return int(row["count"])


def require_room_member(view):
    @wraps(view)
    def wrapped(room_code: str, *args, **kwargs):
        room_code = room_code.upper().strip()
        if not get_room_member(room_code, current_user_token()):
            return redirect(url_for("home"))
        return view(room_code, *args, **kwargs)
    return wrapped


STYLE = """
<style>
:root{color-scheme:dark;--bg:#101114;--panel:#191b20;--text:#f5f6f8;--muted:#a5a9b2;--border:#30343d;--accent:#f2f4f8}
*{box-sizing:border-box}body{margin:0;font-family:system-ui,sans-serif;background:#101114;color:var(--text);min-height:100vh}
.page{width:min(1000px,calc(100% - 32px));margin:auto;padding:38px 0}.narrow{width:min(820px,calc(100% - 32px))}
.card{background:var(--panel);border:1px solid var(--border);border-radius:18px;padding:22px;margin-bottom:16px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.muted{color:var(--muted);line-height:1.6}
label{display:block;margin:14px 0 7px;color:var(--muted)}input,textarea{width:100%;padding:13px;border-radius:12px;border:1px solid var(--border);background:#22252c;color:white;font:inherit}
button{border:0;border-radius:12px;padding:12px 17px;font-weight:800;cursor:pointer}.primary{background:var(--accent);color:#111}.full{width:100%;margin-top:16px}
.alert{padding:12px;border:1px solid var(--border);border-radius:12px;margin:12px 0}.error{color:#ffb4b4}.success{color:#b8f7ca}.hidden{display:none}
.chat{height:100vh;display:grid;grid-template-rows:auto 1fr auto auto;gap:12px}.header{display:flex;justify-content:space-between;gap:16px;align-items:flex-start}
.messages{overflow-y:auto;border:1px solid var(--border);border-radius:18px;padding:16px;background:#15171b}.message{width:min(76%,620px);margin-bottom:13px}.mine{margin-left:auto}.meta{font-size:12px;color:var(--muted);margin:0 6px 5px}.mine .meta{text-align:right}
.bubble{padding:12px 14px;border-radius:16px;background:#2a2e36;white-space:pre-wrap;overflow-wrap:anywhere}.mine .bubble{background:#eceef2;color:#111}.chat-image{display:block;max-width:100%;max-height:420px;border-radius:12px}
.composer{display:grid;grid-template-columns:auto 1fr auto;gap:10px;align-items:stretch}.composer textarea{resize:none}.composer button{min-width:90px}.file-button{display:flex;align-items:center;justify-content:center;border:1px solid var(--border);border-radius:12px;padding:0 14px;cursor:pointer;background:#22252c}.file-button input{display:none}
table{width:100%;border-collapse:collapse;min-width:700px}th,td{padding:11px;border-bottom:1px solid var(--border);text-align:left;vertical-align:top}.table-wrap{overflow:auto}
a{color:white}.small{font-size:13px}.code{font-family:monospace;font-size:1.1em}
@media(max-width:700px){.grid{grid-template-columns:1fr}.composer{grid-template-columns:1fr}.message{width:90%}.header{flex-direction:column}}
</style>
"""


HOME_HTML = """
<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>연락처 차단 채팅</title>""" + STYLE + """</head>
<body><main class="page narrow">
<section class="card"><h1>연락처 차단 1:1 채팅</h1><p class="muted">전화번호, 이메일, 링크, SNS·메신저 ID가 감지되면 상대에게 전달하지 않아.</p></section>
{% if error %}<div class="alert error">{{ error }}</div>{% endif %}
<section class="grid">
<form class="card" method="post" action="{{ url_for('create_room') }}"><h2>새 방 만들기</h2><label>닉네임</label><input name="nickname" maxlength="20" required><button class="primary full">방 만들기</button></form>
<form class="card" method="post" action="{{ url_for('join_room') }}"><h2>방 참여하기</h2><label>방 코드</label><input name="room_code" maxlength="6" required><label>닉네임</label><input name="nickname" maxlength="20" required><button class="primary full">입장하기</button></form>
</section></main></body></html>
"""


CHAT_HTML = """
<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>채팅방</title>""" + STYLE + """</head>
<body><main class="page chat">
<header class="header"><div><h1>방 코드 <button id="copy" class="code">{{ room_code }}</button></h1><p class="muted">{{ nickname }} · <span id="member-count">1</span>/2명</p></div><a href="{{ url_for('home') }}">나가기</a></header>
<section id="messages" class="messages"></section><div id="notice" class="alert hidden"></div>
<form id="form" class="composer"><label class="file-button" title="이미지 첨부">이미지<input id="image-input" type="file" accept="image/jpeg,image/png,image/webp"></label><textarea id="input" maxlength="500" rows="2" placeholder="메시지를 입력해. 연락처·링크·SNS ID는 차단돼."></textarea><button class="primary">전송</button></form>
</main>
<script>
const room="{{ room_code }}";let last=0;const box=document.getElementById("messages"),input=document.getElementById("input"),notice=document.getElementById("notice");
function show(text,ok=false){notice.textContent=text;notice.className="alert "+(ok?"success":"error");setTimeout(()=>notice.className="alert hidden",4000)}
function add(m){const a=document.createElement("article");a.className="message "+(m.is_mine?"mine":"");const meta=document.createElement("div");meta.className="meta";meta.textContent=m.nickname;const b=document.createElement("div");b.className="bubble";if(m.message_type==="image"){const img=document.createElement("img");img.className="chat-image";img.src=m.image_url;img.alt="채팅 이미지";b.appendChild(img)}else{b.textContent=m.content}a.append(meta,b);box.appendChild(a);box.scrollTop=box.scrollHeight}
async function load(){try{const r=await fetch(`/api/room/${room}/messages?after=${last}`,{cache:"no-store"});if(!r.ok)return;const d=await r.json();document.getElementById("member-count").textContent=d.member_count;for(const m of d.messages){add(m);last=Math.max(last,m.id)}}catch(e){console.error(e)}}
const imageInput=document.getElementById("image-input");
document.getElementById("form").addEventListener("submit",async e=>{e.preventDefault();const content=input.value.trim();const file=imageInput.files[0];if(!content&&!file)return;let r;if(file){show("이미지에서 연락처를 검사하고 있어.",true);const form=new FormData();form.append("image",file);r=await fetch(`/api/room/${room}/images`,{method:"POST",body:form})}else{r=await fetch(`/api/room/${room}/messages`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({content})})}const d=await r.json();if(!r.ok){show(d.message+(d.reasons?.length?" ("+d.reasons.join(", ")+")":""));return}input.value="";imageInput.value="";await load()});
input.addEventListener("keydown",e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();document.getElementById("form").requestSubmit()}});
document.getElementById("copy").addEventListener("click",async()=>{await navigator.clipboard.writeText(room);show("방 코드를 복사했어.",true)});
load();setInterval(load,1200);
</script></body></html>
"""


ADMIN_LOGIN_HTML = """
<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>관리자</title>""" + STYLE + """</head>
<body><main class="page narrow"><form class="card" method="post"><h1>관리자 로그인</h1>{% if error %}<div class="alert error">{{ error }}</div>{% endif %}<label>비밀번호</label><input type="password" name="password" required><button class="primary full">로그인</button></form></main></body></html>
"""


ADMIN_HTML = """
<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>관리자</title>""" + STYLE + """</head>
<body><main class="page"><header class="header"><div><h1>관리자</h1><p class="muted small">DB: {{ db_path }}</p></div><form method="post" action="{{ url_for('admin_logout') }}"><button>로그아웃</button></form></header>
<section class="card"><h2>방 현황</h2><div class="table-wrap"><table><tr><th>방</th><th>참여자</th><th>정상 메시지</th><th>생성</th></tr>{% for r in rooms %}<tr><td>{{ r.room_code }}</td><td>{{ r.member_count }}</td><td>{{ r.message_count }}</td><td>{{ r.created_at }}</td></tr>{% else %}<tr><td colspan="4">방이 없어.</td></tr>{% endfor %}</table></div></section>
<section class="card"><h2>차단 기록</h2><div class="table-wrap"><table><tr><th>시간</th><th>방</th><th>닉네임</th><th>사유</th><th>원문</th></tr>{% for r in blocked %}<tr><td>{{ r.created_at }}</td><td>{{ r.room_code }}</td><td>{{ r.nickname }}</td><td>{{ r.reasons }}</td><td>{{ r.content }}</td></tr>{% else %}<tr><td colspan="5">차단 기록이 없어.</td></tr>{% endfor %}</table></div></section>
</main></body></html>
"""


@app.route("/")
def home():
    return render_template_string(HOME_HTML)


@app.route("/create-room", methods=["POST"])
def create_room():
    nickname = request.form.get("nickname", "").strip()[:20]
    if not nickname:
        return render_template_string(HOME_HTML, error="닉네임을 입력해줘.")

    room_code = generate_room_code()
    token = current_user_token()
    created_at = now_kst_iso()

    with db_connect() as conn:
        conn.execute("INSERT INTO rooms(room_code, created_at) VALUES (?, ?)", (room_code, created_at))
        conn.execute(
            "INSERT INTO members(room_code, user_token, nickname, joined_at) VALUES (?, ?, ?, ?)",
            (room_code, token, nickname, created_at),
        )
    return redirect(url_for("chat_room", room_code=room_code))


@app.route("/join-room", methods=["POST"])
def join_room():
    room_code = request.form.get("room_code", "").upper().strip()
    nickname = request.form.get("nickname", "").strip()[:20]
    token = current_user_token()

    if not room_code or not nickname:
        return render_template_string(HOME_HTML, error="방 코드와 닉네임을 모두 입력해줘.")

    with db_connect() as conn:
        room = conn.execute("SELECT 1 FROM rooms WHERE room_code = ?", (room_code,)).fetchone()

    if not room:
        return render_template_string(HOME_HTML, error="존재하지 않는 방 코드야.")

    existing = get_room_member(room_code, token)
    if not existing and room_member_count(room_code) >= 2:
        return render_template_string(HOME_HTML, error="이미 두 명이 입장한 방이야.")

    if not existing:
        with db_connect() as conn:
            conn.execute(
                "INSERT INTO members(room_code, user_token, nickname, joined_at) VALUES (?, ?, ?, ?)",
                (room_code, token, nickname, now_kst_iso()),
            )

    return redirect(url_for("chat_room", room_code=room_code))


@app.route("/room/<room_code>")
@require_room_member
def chat_room(room_code: str):
    member = get_room_member(room_code, current_user_token())
    return render_template_string(CHAT_HTML, room_code=room_code, nickname=member["nickname"])


@app.route("/api/room/<room_code>/messages", methods=["GET"])
@require_room_member
def get_messages(room_code: str):
    try:
        after_id = max(0, int(request.args.get("after", "0")))
    except ValueError:
        after_id = 0

    with db_connect() as conn:
        rows = conn.execute(
            "SELECT id, user_token, nickname, content, message_type, file_name, created_at FROM messages WHERE room_code = ? AND id > ? ORDER BY id ASC LIMIT 200",
            (room_code, after_id),
        ).fetchall()

    token = current_user_token()
    return jsonify({
        "messages": [{
            "id": row["id"],
            "nickname": row["nickname"],
            "content": row["content"],
            "created_at": row["created_at"],
            "message_type": row["message_type"],
            "image_url": (url_for("get_room_image", room_code=room_code, file_name=row["file_name"]) if row["message_type"] == "image" and row["file_name"] else None),
            "is_mine": row["user_token"] == token,
        } for row in rows],
        "member_count": room_member_count(room_code),
    })


@app.route("/api/room/<room_code>/messages", methods=["POST"])
@require_room_member
def send_message(room_code: str):
    payload = request.get_json(silent=True) or {}
    content = str(payload.get("content", "")).strip()

    if not content:
        return jsonify({"ok": False, "message": "메시지가 비어 있어."}), 400
    if len(content) > 500:
        return jsonify({"ok": False, "message": "메시지는 500자까지 보낼 수 있어."}), 400

    token = current_user_token()
    member = get_room_member(room_code, token)
    reasons = detect_contact_info(content)
    created_at = now_kst_iso()

    if reasons:
        with db_connect() as conn:
            conn.execute(
                "INSERT INTO blocked_messages(room_code,user_token,nickname,content,reasons,created_at) VALUES (?,?,?,?,?,?)",
                (room_code, token, member["nickname"], content, ", ".join(reasons), created_at),
            )
        return jsonify({
            "ok": False,
            "blocked": True,
            "message": "연락처 또는 외부 연락 수단이 감지되어 전송하지 않았어.",
            "reasons": reasons,
        }), 400

    with db_connect() as conn:
        cursor = conn.execute(
            "INSERT INTO messages(room_code,user_token,nickname,content,created_at) VALUES (?,?,?,?,?)",
            (room_code, token, member["nickname"], content, created_at),
        )
    return jsonify({"ok": True, "message_id": cursor.lastrowid})



@app.route("/api/room/<room_code>/images", methods=["POST"])
@require_room_member
def send_image(room_code: str):
    uploaded = request.files.get("image")
    if uploaded is None:
        return jsonify({"ok": False, "message": "이미지를 선택해줘."}), 400

    raw = uploaded.read(MAX_IMAGE_BYTES + 1)
    token = current_user_token()
    member = get_room_member(room_code, token)
    created_at = now_kst_iso()

    try:
        image, ocr_text = read_image_and_ocr(raw)
    except ValueError as error:
        return jsonify({"ok": False, "message": str(error)}), 400
    except Exception as error:
        print(f"[ERROR] OCR 처리 실패: {error}")
        return jsonify({"ok": False, "message": "OCR 처리에 실패해서 이미지를 전송하지 않았어."}), 503

    reasons = detect_contact_info(ocr_text) if ocr_text else []
    if reasons:
        log_content = "[이미지 OCR 결과]\n" + (ocr_text[:2000] or "텍스트 없음")
        with db_connect() as conn:
            conn.execute(
                "INSERT INTO blocked_messages(room_code,user_token,nickname,content,reasons,created_at) VALUES (?,?,?,?,?,?)",
                (room_code, token, member["nickname"], log_content, ", ".join(reasons), created_at),
            )
        return jsonify({
            "ok": False,
            "blocked": True,
            "message": "이미지 안에서 연락처 또는 외부 연락 수단이 감지되어 전송하지 않았어.",
            "reasons": reasons,
        }), 400

    file_name = save_clean_image(image)
    with db_connect() as conn:
        cursor = conn.execute(
            "INSERT INTO messages(room_code,user_token,nickname,content,message_type,file_name,created_at) VALUES (?,?,?,?,?,?,?)",
            (room_code, token, member["nickname"], "[이미지]", "image", file_name, created_at),
        )
    return jsonify({"ok": True, "message_id": cursor.lastrowid})


@app.route("/room/<room_code>/image/<file_name>")
@require_room_member
def get_room_image(room_code: str, file_name: str):
    safe_name = Path(file_name).name
    with db_connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM messages WHERE room_code = ? AND message_type = 'image' AND file_name = ?",
            (room_code, safe_name),
        ).fetchone()
    if not exists:
        return "Not found", 404

    file_path = UPLOAD_DIR / safe_name
    if not file_path.exists():
        return "Not found", 404
    return send_file(file_path, mimetype="image/jpeg", max_age=3600)

def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_authenticated"):
            return redirect(url_for("admin_login"))
        return view(*args, **kwargs)
    return wrapped


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        if secrets.compare_digest(password, ADMIN_PASSWORD):
            session["admin_authenticated"] = True
            return redirect(url_for("admin_dashboard"))
        error = "비밀번호가 맞지 않아."
    return render_template_string(ADMIN_LOGIN_HTML, error=error)


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("admin_authenticated", None)
    return redirect(url_for("admin_login"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    with db_connect() as conn:
        blocked = conn.execute(
            "SELECT room_code,nickname,content,reasons,created_at FROM blocked_messages ORDER BY id DESC LIMIT 300"
        ).fetchall()
        rooms = conn.execute(
            """
            SELECT r.room_code, r.created_at,
                   COUNT(DISTINCT m.user_token) AS member_count,
                   COUNT(DISTINCT msg.id) AS message_count
            FROM rooms r
            LEFT JOIN members m ON m.room_code = r.room_code
            LEFT JOIN messages msg ON msg.room_code = r.room_code
            GROUP BY r.room_code, r.created_at
            ORDER BY r.created_at DESC
            LIMIT 100
            """
        ).fetchall()
    return render_template_string(ADMIN_HTML, blocked=blocked, rooms=rooms, db_path=DB_PATH)


@app.route("/health")
def health():
    return jsonify({"status": "healthy", "service": "contact-guard-chat-ocr", "db_path": DB_PATH, "upload_dir": str(UPLOAD_DIR), "ocr": "easyocr-ko-en"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False)
