연락처 차단 1:1 채팅 - OCR 제거 및 오류 수정판

수정 완료:
- LOCAL_DB_PATH 누락 오류 수정
- LOCAL_UPLOAD_DIR 누락 오류 수정
- EasyOCR, torch, torchvision, OCR 모델 완전 제거
- 텍스트 연락처 차단 유지
- 이미지 업로드 및 최적화 유지
- 최대 5MB, 최대 변 1600px
- JPEG 품질 82로 재압축
- 방별 user_token 인증 유지

GitHub에서 교체:
1. app.py
2. requirements.txt
3. render.yaml
4. Commit changes
5. Render Deploy live 대기

Render Start Command:
gunicorn app:app --timeout 60 --workers 1 --threads 1

정상 /health 결과:
service = contact-guard-chat
ocr = false
