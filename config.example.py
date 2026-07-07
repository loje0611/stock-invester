# -*- coding: utf-8 -*-
"""
키움증권 차세대 REST API 설정 파일 (템플릿)
이 파일을 config.py로 복사한 뒤 본인의 실제 키움 OpenAPI 정보를 입력하세요.

  cp config.example.py config.py
"""

# ── 모의투자 / 실전투자 전환 스위치 ──
# True = 모의투자(mockapi), False = 실전투자(api)
IS_DRY_RUN = True

# ── 실전투자용 인증 정보 ──
REAL_APP_KEY = "YOUR_REAL_APP_KEY"
REAL_SECRET_KEY = "YOUR_REAL_SECRET_KEY"
REAL_ACCOUNT_NO = "YOUR_REAL_ACCOUNT_NO"

# ── 모의투자용 인증 정보 ──
MOCK_APP_KEY = "YOUR_MOCK_APP_KEY"
MOCK_SECRET_KEY = "YOUR_MOCK_SECRET_KEY"
MOCK_ACCOUNT_NO = "YOUR_MOCK_ACCOUNT_NO"

# ── IS_DRY_RUN에 따라 자동 선택되는 활성 설정 ──
BASE_URL = "https://mockapi.kiwoom.com" if IS_DRY_RUN else "https://api.kiwoom.com"
APP_KEY = MOCK_APP_KEY if IS_DRY_RUN else REAL_APP_KEY
SECRET_KEY = MOCK_SECRET_KEY if IS_DRY_RUN else REAL_SECRET_KEY
ACCOUNT_NO = MOCK_ACCOUNT_NO if IS_DRY_RUN else REAL_ACCOUNT_NO

# 투자 금액 (원)
INVESTMENT_AMOUNT = 50_000
