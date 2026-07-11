연락처 차단 1:1 채팅 + OCR + 세션 튕김 수정판

핵심 수정
- 채팅방 인증을 Flask 세션 쿠키에서 방별 user_token으로 변경
- 방 입장 시 /room/방코드?token=... 형태로 접속
- 토큰을 localStorage에 보관
- 모든 메시지/OCR API 호출에 X-User-Token 헤더 전송
- 인증 실패 시 JSON 401을 반환하고 메인으로 이동
- 기존처럼 302 리다이렉트가 반복되지 않음
- 카카오 인앱 브라우저에서도 세션 쿠키 문제를 피함
- 이미지 업로드 API와 EasyOCR 검사 포함
- 기존 SQLite DB의 message_type 컬럼이 없으면 자동 추가

GitHub 교체
1. 기존 app.py 전체를 새 app.py로 교체
2. requirements.txt 교체
3. render.yaml 교체
4. Commit changes
5. Render 자동 재배포 완료까지 기다림

배포 후 테스트
1. 기존 방은 버리고 새 방 생성
2. 첫 번째 기기에서 방 생성
3. 두 번째 기기에서 방 코드로 입장
4. 텍스트 전송 확인
5. 사진 선택 후 전송
6. Render Logs에 아래 요청이 보여야 정상
   POST /api/room/방코드/image 200

Render 설정
Build Command:
pip install -r requirements.txt

Start Command:
gunicorn app:app --timeout 180 --workers 1
