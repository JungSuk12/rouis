연락처 차단 1:1 채팅 - OCR 제거 버전

변경 사항
- EasyOCR, torch 계열 의존성 완전 제거
- 텍스트 연락처 차단은 그대로 유지
- 이미지 업로드는 유지
- 이미지는 최대 5MB
- 최대 변 1600px로 축소
- EXIF 회전 보정
- JPEG 품질 82로 재압축
- 이미지 속 연락처는 검사하지 않음
- 방별 user_token 인증 유지

GitHub에서 교체할 파일
- app.py
- requirements.txt
- render.yaml

Render Start Command
gunicorn app:app --timeout 60 --workers 1 --threads 1
