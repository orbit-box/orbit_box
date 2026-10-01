# 투자자 참가신청 텔레그램 봇

텔레그램 참가신청을 받고 관리자 비공개 그룹으로 즉시 알림을 보내는 봇입니다.

## 주요 기능
- 개인정보 수집·이용 동의
- 이름 / 전화번호 수집
- 전화번호 공유 버튼 + 직접 입력
- 투자경력 구간 선택
- 투자금액 구간 선택
- 보유·관심 종목 입력
- 도움 필요 항목 선택
- 최종 확인 후 참가신청
- 관리자 그룹 즉시 알림
- 관리자 상태 버튼: 접수 / 상담중 / 완료
- SQLite 저장

## 환경변수
서버에서 BOT_TOKEN, ADMIN_CHAT_ID, PRIVACY_OPERATOR, PRIVACY_CONTACT, DB_PATH를 설정하세요.

BOT_TOKEN은 GitHub 저장소에 직접 올리지 마세요.

## 실행
```bash
pip install -r requirements.txt
python bot.py
```

## Railway Start Command
```bash
python bot.py
```

전화번호와 투자 관련 정보가 저장되므로 DB 접근 권한을 제한하고, 보유기간 정책에 맞춰 삭제 절차를 운영하세요.
