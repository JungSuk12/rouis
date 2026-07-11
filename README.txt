연락처 차단 1:1 채팅 + OCR MVP

기능
- 한 방 최대 2명
- 텍스트 연락처/이메일/링크/SNS ID 차단
- 이미지 첨부 지원
- EasyOCR(한국어+영어)로 이미지 안의 글자를 읽고 동일한 연락처 검사 적용
- 이미지에서 연락처가 감지되면 상대에게 전송하지 않음
- 통과된 이미지는 EXIF 등 원본 메타데이터를 제거하고 JPEG로 다시 저장
- 정상 채팅은 관리자 화면에 표시하지 않음
- 차단된 텍스트와 이미지 OCR 결과만 관리자 화면에 기록

로컬 실행
1. 압축 해제
2. run_server.bat 더블클릭
3. 설치가 끝난 뒤 http://127.0.0.1:10000 접속

첫 OCR 실행
- EasyOCR 한국어/영어 모델을 처음 한 번 다운로드하므로 첫 이미지 검사는 오래 걸릴 수 있음
- 이후에는 OCR_MODEL_DIR에 저장된 모델을 재사용함

관리자
- 주소: http://127.0.0.1:10000/admin
- 기본 비밀번호: change-me

Render 설정
Build Command:
pip install -r requirements.txt

Start Command:
gunicorn app:app --timeout 180

환경변수:
ADMIN_PASSWORD=원하는비밀번호
SECRET_KEY=아주긴랜덤문자열
CHAT_DB_PATH=/var/data/contact_guard_chat.db
UPLOAD_DIR=/var/data/contact_guard_uploads
OCR_MODEL_DIR=/var/data/easyocr_models

Persistent Disk
- /var/data 마운트 권장
- OCR 모델, DB, 통과 이미지가 유지됨

주의
- EasyOCR와 PyTorch 때문에 기존 텍스트 전용 버전보다 설치 용량과 메모리 사용량이 큼
- 이미지 최대 크기 5MB
- OCR이 실패하면 안전을 위해 이미지를 상대에게 보내지 않음
- OCR은 100% 완벽하지 않으므로 운영 전 실제 우회 사례로 테스트 필요
