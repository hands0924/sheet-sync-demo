import os
import json
import logging
from flask import Request, abort

from google.cloud import firestore
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account
from googleapiclient.discovery import build
import requests  # Solapi 호출

# 로깅 설정
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 전역 변수: 클라이언트는 lazy initialization 패턴으로 처리
_firestore_client = None
_sheets_service = None
_solapi_session = None

def get_firestore_client():
    global _firestore_client
    if _firestore_client is None:
        # 애플리케이션 기본 자격 증명 사용 (GOOGLE_APPLICATION_CREDENTIALS 환경 변수로 서비스 계정 키 파일 경로를 지정)
        _firestore_client = firestore.Client()
    return _firestore_client

def get_sheets_service():
    global _sheets_service
    if _sheets_service is None:
        # 서비스 계정 인증 사용. GOOGLE_APPLICATION_CREDENTIALS가 설정되어 있으면 자동으로 사용됨.
        # Drive Watch 검증 등을 위해 drive API 사용 시에도 동일한 credentials 사용.
        creds, _ = google_auth_default_with_scopes(['https://www.googleapis.com/auth/drive.readonly', 
                                                     'https://www.googleapis.com/auth/spreadsheets.readonly'])
        _sheets_service = build('sheets', 'v4', credentials=creds)
    return _sheets_service

def google_auth_default_with_scopes(scopes):
    """
    ADC(Application Default Credentials)로부터 credentials 로드 후, 지정된 scopes로 refresh하여 반환.
    """
    # Application Default Credentials 사용
    from google.auth import default
    creds, project = default()
    if not creds.valid or creds.requires_scopes:
        creds = creds.with_scopes(scopes)
    # 토큰이 없거나 만료된 경우 refresh
    if not creds.valid:
        creds.refresh(GoogleAuthRequest())
    return creds, project

def get_solapi_session():
    """
    Solapi API 호출을 위한 requests.Session 초기화 (API_KEY, API_SECRET를 환경 변수에서 읽어옴).
    """
    global _solapi_session
    if _solapi_session is None:
        api_key = os.getenv('SOLAPI_API_KEY')
        api_secret = os.getenv('SOLAPI_API_SECRET')
        if not api_key or not api_secret:
            logger.error("Solapi API 키/시크릿이 설정되지 않았습니다.")
            raise RuntimeError("Missing Solapi credentials")
        session = requests.Session()
        # Solapi는 Basic Auth 혹은 HMAC 인증 방식일 수 있으므로, 여기서는 예시로 Basic Auth 사용
        session.auth = (api_key, api_secret)
        _solapi_session = session
    return _solapi_session

def send_solapi_sms(phone, message, sender_phone):
    """
    Solapi API를 통해 SMS를 발송하는 함수
    """
    session = get_solapi_session()
    
    # 전화번호 형식 정리 (하이픈 제거)
    phone = phone.replace('-', '').strip()
    if not phone.startswith('82'):
        phone = '82' + phone.lstrip('0')
    
    # 발신자 번호 형식 정리
    sender_phone = sender_phone.replace('-', '').strip()
    if not sender_phone.startswith('82'):
        sender_phone = '82' + sender_phone.lstrip('0')
    
    payload = {
        "message": {
            "to": phone,
            "from": sender_phone,
            "text": message
        }
    }
    
    logger.info(f"Solapi SMS 발송 시도 - Payload: {json.dumps(payload, ensure_ascii=False)}")
    
    try:
        resp = session.post(
            "https://api.solapi.com/messages/v4/send",
            json=payload,
            timeout=10
        )
        resp.raise_for_status()
        logger.info(f"Solapi SMS 발송 성공 - Response: {resp.text}")
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"Solapi SMS 발송 실패 - Error: {str(e)}")
        if hasattr(e.response, 'text'):
            logger.error(f"Response: {e.response.text}")
        return False

def sheet_webhook(request: Request):
    """
    Cloud Functions 엔트리포인트: Drive Watch로부터의 POST 요청 처리.
    """
    # 1) HTTP 메서드 및 헤더 검증
    if request.method != 'POST':
        return ('Method Not Allowed', 405)
    resource_state = request.headers.get('X-Goog-Resource-State')
    # DRIVE notification validation: resource_state이 'change' 등 적절한 값인지 확인
    if not resource_state:
        logger.warning("Missing X-Goog-Resource-State header")
        abort(400, description="Invalid webhook call")
    # TODO: 추가 검증: 인증 토큰, 채널 ID 일치 여부 등

    # 2) Google Sheet 내용 읽기
    sheet_id = os.getenv('SHEET_ID')
    if not sheet_id:
        logger.error("환경 변수 SHEET_ID가 설정되지 않았습니다.")
        abort(500, description="Server configuration error")
    sheets = get_sheets_service()
    # 시트 이름과 범위를 환경 변수에서 가져옴
    sheet_name = os.getenv('SHEET_NAME', 'Sheet1')  # 기본값은 'Sheet1'
    range_name = f"{sheet_name}!A1:Z"
    try:
        sheet_resp = sheets.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=range_name
        ).execute()
    except Exception as e:
        logger.exception("Sheets API 호출 실패")
        abort(500, description="Failed to read sheet")

    values = sheet_resp.get('values', [])
    # 헤더 행과 데이터 행 구분
    if not values or len(values) < 2:
        # 응답이 없거나 헤더만 있는 경우 특별 처리
        logger.info("Sheet에 데이터가 없음")
        current_rows = []
    else:
        headers = values[0]
        data_rows = values[1:]
        # 각 행의 고유 ID로 0번 열(타임스탬프) 사용
        current_rows = []
        for row in data_rows:
            # 부족한 열은 빈 문자열로 채우기
            row_full = row + ['']*(len(headers) - len(row))
            row_dict = {headers[i]: row_full[i] for i in range(len(headers))}
            timestamp = row_full[0]
            if not timestamp:
                continue
            current_rows.append((timestamp, row_dict))

    # 3) Firestore 이전 스냅샷 불러오기
    firestore_doc = os.getenv('FIRESTORE_DOC')
    if not firestore_doc:
        logger.error("환경 변수 FIRESTORE_DOC이 설정되지 않았습니다.")
        abort(500, description="Server configuration error")
    # firestore_doc 예: 'sheet_snapshots/latest'
    parts = firestore_doc.split('/', 1)
    if len(parts) != 2:
        logger.error("FIRESTORE_DOC 형식이 'collection/document'이어야 합니다.")
        abort(500, description="Server configuration error")
    collection, document = parts
    db = get_firestore_client()
    doc_ref = db.collection(collection).document(document)
    prev_snapshot = {}
    try:
        doc = doc_ref.get()
        if doc.exists:
            prev_snapshot = doc.to_dict() or {}
    except Exception:
        logger.exception("Firestore 이전 스냅샷 로드 실패")
        # 계속 처리: prev_snapshot 빈 dict로 간주

    # 4) 신규/변경된 행 식별
    new_or_updated = []
    current_dict = {ts: row for ts, row in current_rows}
    # 신규: current에 있지만 prev에 없는 ts
    for ts, row in current_rows:
        if ts not in prev_snapshot:
            new_or_updated.append((ts, row))
        else:
            # 변경 감지: row 정보 직렬화 방식에 따라 비교
            prev_row = prev_snapshot.get(ts)
            if prev_row != row:
                new_or_updated.append((ts, row))
    # 삭제된 행은 SMS와 무관하므로 무시

    # 5) Solapi로 SMS 발송
    sender_phone = os.getenv('SENDER_PHONE')
    if not sender_phone:
        logger.error("환경 변수 SENDER_PHONE이 설정되지 않았습니다.")
        abort(500, description="Server configuration error")
    
    # 예: row_dict에서 '이름', '전화번호', '문의 종류' 키 사용
    for ts, row in new_or_updated:
        name = row.get('이름') or row.get('Name') or ''
        phone = row.get('전화번호') or row.get('Phone') or ''
        inquiry = row.get('문의 종류') or row.get('Inquiry') or ''
        
        if not phone:
            logger.warning(f"전화번호 누락: ts={ts}")
            continue
            
        # SMS 메시지 내용 구성
        message_text = f"[알림]\n{name}님, '{inquiry}' 문의가 접수되었습니다. 감사합니다."
        
        # SMS 발송
        if not send_solapi_sms(phone, message_text, sender_phone):
            logger.error(f"SMS 발송 실패: ts={ts}, phone={phone}")

    # 6) Firestore 스냅샷 업데이트
    # 전체 current_rows를 dict 형태로 저장
    new_snapshot = {ts: row for ts, row in current_rows}
    try:
        doc_ref.set(new_snapshot)
    except Exception:
        logger.exception("Firestore 스냅샷 저장 실패")

    return ('OK', 200)
