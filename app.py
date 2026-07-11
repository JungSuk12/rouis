import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional 

from flask import Flask, jsonify, request


# =========================================================
# Flask 설정
# =========================================================

app = Flask(__name__)


# =========================================================
# 특정 키워드 감지 설정
# =========================================================

# 감지하고 싶은 키워드를 이 목록에 추가하면 된다.
# 각 항목 뒤에는 반드시 쉼표(,)를 넣어야 한다.
ALERT_KEYWORDS = [
    "전화번호",
    "전번",
    "번호",
    "010",
    "0l0",
    "교환",
    "폰번",
]


# =========================================================
# 감지 로그 설정
# =========================================================

# Render Persistent Disk를 사용하는 경우 기본 경로
ALERT_LOG_FILE = os.environ.get(
    "ALERT_LOG_FILE",
    "/var/data/alert_logs.json",
).strip()

# 로컬 테스트 환경에서 /var/data를 사용할 수 없을 때의 대체 경로
LOCAL_ALERT_LOG_FILE = "alert_logs.json"


# =========================================================
# 공통 JSON 파일 함수
# =========================================================


def load_json_file(file_path: str, default: Any) -> Any:
    """JSON 파일을 읽는다."""
    if not os.path.exists(file_path):
        return default

    try:
        with open(file_path, "r", encoding="utf-8") as file:
            content = file.read().strip()

        if not content:
            return default

        return json.loads(content)

    except (OSError, json.JSONDecodeError) as error:
        print(f"[ERROR] JSON 파일 읽기 실패: {file_path} / {error}")
        return default


def save_json_file(file_path: str, data: Any) -> bool:
    """JSON 데이터를 UTF-8 형식으로 안전하게 저장한다."""
    directory = os.path.dirname(file_path)

    if directory:
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as error:
            print(f"[ERROR] 저장 폴더 생성 실패: {directory} / {error}")
            return False

    temp_file = f"{file_path}.tmp"

    try:
        with open(temp_file, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)

        os.replace(temp_file, file_path)
        return True

    except OSError as error:
        print(f"[ERROR] JSON 파일 저장 실패: {file_path} / {error}")

        if os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except OSError:
                pass

        return False


# =========================================================
# 키워드 감지 및 로그 함수
# =========================================================


def detect_alert_keywords(utterance: str) -> List[str]:
    """
    사용자 입력에서 ALERT_KEYWORDS에 등록된 단어를 찾는다.

    영문 키워드는 대소문자를 구분하지 않는다.
    같은 키워드는 한 번만 반환한다.
    """
    detected_keywords: List[str] = []
    normalized_utterance = utterance.casefold()

    for keyword in ALERT_KEYWORDS:
        cleaned_keyword = str(keyword).strip()

        if not cleaned_keyword:
            continue

        normalized_keyword = cleaned_keyword.casefold()

        if normalized_keyword in normalized_utterance:
            if cleaned_keyword not in detected_keywords:
                detected_keywords.append(cleaned_keyword)

    return detected_keywords


def make_alert_message(detected_keywords: List[str]) -> str:
    """감지된 키워드 알림 문구를 만든다."""
    keyword_text = ", ".join(detected_keywords)

    return (
        "특정 키워드가 감지되었습니다.\n"
        f"감지된 키워드: {keyword_text}"
    )


def get_log_file_path() -> str:
    """
    Render Persistent Disk 경로를 우선 사용한다.
    해당 경로를 만들 수 없으면 로컬 파일을 사용한다.
    """
    preferred_directory = os.path.dirname(ALERT_LOG_FILE)

    if not preferred_directory:
        return ALERT_LOG_FILE

    try:
        os.makedirs(preferred_directory, exist_ok=True)
        return ALERT_LOG_FILE
    except OSError:
        return LOCAL_ALERT_LOG_FILE


def save_alert_log(
    user_id: str,
    detected_keywords: List[str],
    utterance: str,
) -> bool:
    """감지 시간, 사용자 ID, 키워드, 원본 메시지를 저장한다."""
    log_file_path = get_log_file_path()
    logs = load_json_file(log_file_path, [])

    if not isinstance(logs, list):
        logs = []

    logs.append(
        {
            "detected_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "user_id": user_id,
            "detected_keywords": detected_keywords,
            "original_message": utterance,
        }
    )

    return save_json_file(log_file_path, logs)


# =========================================================
# 카카오 응답 함수
# =========================================================


def make_kakao_response(text: str):
    """카카오 오픈빌더 단순 텍스트 응답 형식을 만든다."""
    return jsonify(
        {
            "version": "2.0",
            "template": {
                "outputs": [
                    {
                        "simpleText": {
                            "text": text,
                        }
                    }
                ]
            },
        }
    )


def make_kakao_empty_response():
    """
    키워드가 감지되지 않았을 때 사용자 화면에 아무 답변도 표시하지 않는다.
    카카오 스킬 응답 형식은 유지한다.
    """
    return jsonify(
        {
            "version": "2.0",
            "template": {
                "outputs": [],
            },
        }
    )


# =========================================================
# 서버 상태 확인
# =========================================================


@app.route("/", methods=["GET"])
def home():
    return jsonify(
        {
            "status": "ok",
            "service": "kakao-keyword-alert-bot",
            "mode": "keyword-only",
        }
    )


@app.route("/health", methods=["GET"])
def health():
    return jsonify(
        {
            "status": "healthy",
            "mode": "keyword-only",
            "alert_keywords": ALERT_KEYWORDS,
            "alert_log_file": get_log_file_path(),
        }
    )


# =========================================================
# 챗봇 API
# =========================================================


@app.route("/chatbot", methods=["POST"])
def chat():
    try:
        body = request.get_json(silent=True)

        # 잘못된 요청도 사용자에게 별도 문구를 출력하지 않는다.
        if not isinstance(body, dict):
            return make_kakao_empty_response()

        user_request = body.get("userRequest", {})

        if not isinstance(user_request, dict):
            return make_kakao_empty_response()

        user = user_request.get("user", {})

        if not isinstance(user, dict):
            user = {}

        utterance = str(user_request.get("utterance", "")).strip()
        user_id = str(user.get("id", "unknown")).strip() or "unknown"

        if not utterance:
            return make_kakao_empty_response()

        detected_keywords = detect_alert_keywords(utterance)

        # 핵심 동작:
        # 등록된 키워드가 하나도 없으면 아무 답변도 하지 않는다.
        if not detected_keywords:
            return make_kakao_empty_response()

        # 키워드가 감지된 경우에만 로그를 저장하고 알림을 출력한다.
        log_saved = save_alert_log(
            user_id=user_id,
            detected_keywords=detected_keywords,
            utterance=utterance,
        )

        if not log_saved:
            print("[WARNING] 키워드는 감지했지만 로그 저장에 실패했습니다.")

        return make_kakao_response(
            make_alert_message(detected_keywords)
        )

    except Exception as error:
        print(f"[ERROR] /chatbot 처리 실패: {error}")

        # 오류가 발생해도 일반 챗봇 답변은 하지 않는다.
        return make_kakao_empty_response()


# =========================================================
# 서버 실행
# =========================================================


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))

    app.run(
        host="0.0.0.0",
        port=port,
    )
