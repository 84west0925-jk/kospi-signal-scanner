단타RSI신호시스템 - KIS 최신봉 정시알림 보완본

핵심 변경
- KIS 과거분봉(FHKST03010230) 대량 부트스트랩을 사용하지 않습니다.
- 기존 RSI 워밍업 데이터는 기존 yfinance 30분봉으로 준비합니다.
- 실제 텔레그램 신호를 결정하는 최신 확정 30분봉만 KIS 당일분봉(FHKST03010200)으로 조회합니다.
- KIS 확정봉은 로컬 캐시에 누적되어 다음 신호 계산에 계속 사용됩니다.

1) 최초 캐시 준비
python kis_alert_runner.py --bootstrap-only

2) KIS 1종목 통신 테스트 (장중 09:30 이후)
python kis_alert_runner.py --kis-test 005930

3) 최신 확정봉 1회 전체 스캔 (확정 후 60초 이내)
python kis_alert_runner.py --once

4) 상시 정시 알림 실행
python kis_alert_runner.py

※ 텔레그램 봇 토큰/채팅ID는 기존 설정을 그대로 사용합니다.
※ 자동단타 프로그램과 같은 App Key를 동시에 사용할 때는 REST 호출량이 합산됩니다.
