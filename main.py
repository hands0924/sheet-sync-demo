import os
import re
import logging
from flask import Request, abort

from google.cloud import firestore
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.auth import default as google_auth_default
from googleapiclient.discovery import build
from google.api_core.exceptions import NotFound

from solapi import SolapiMessageService
from solapi.model import RequestMessage

# 로깅 설정
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 전역 변수: lazy initialization
_firestore_client = None
_sheets_service = None
_message_service = None

# ---------- Firestore 클라이언트 ----------
def get_firestore_client():
    global _firestore_client
    if _firestore_client is None:
        try:
            _firestore_client = firestore.Client()
            # Firestore 초기화 확인
            _firestore_client.collections()
            logger.info("Initialized Firestore client")
        except NotFound as e:
            logger.error("Firestore 데이터베이스가 생성되지 않았습니다. GCP 콘솔에서 Firestore를 생성해주세요.")
            raise RuntimeError("Firestore database not initialized") from e
        except Exception as e:
            logger.error(f"Firestore 초기화 실패: {e}")
            raise
    return _firestore_client

# ---------- Sheets API 서비스 ----------
def get_sheets_service():
    global _sheets_service
    if _sheets_service is None:
        creds, _ = google_auth_default(scopes=[
            'https://www.googleapis.com/auth/drive.readonly',
            'https://www.googleapis.com/auth/spreadsheets.readonly'
        ])
        # 토큰 갱신
        if not creds.valid:
            creds.refresh(GoogleAuthRequest())
        _sheets_service = build('sheets', 'v4', credentials=creds)
        logger.info("Initialized Sheets service")
    return _sheets_service

# ---------- Solapi SDK 메시지 서비스 ----------
def get_message_service():
    global _message_service
    if _message_service is None:
        api_key = os.getenv('SOLAPI_API_KEY')
        api_secret = os.getenv('SOLAPI_API_SECRET')
        if not api_key or not api_secret:
            logger.error("Solapi API 키/시크릿 미설정")
            raise RuntimeError("Missing Solapi credentials")
        _message_service = SolapiMessageService(
            api_key=api_key,
            api_secret=api_secret
        )
        logger.info("Initialized SolapiMessageService")
    return _message_service

# ---------- 전화번호 정규화 ----------
def normalize_korean_number(number: str) -> str:
    # 숫자만 남기기
    digits = re.sub(r"\D", "", number)
    # 국제번호 82 제거 후 0으로 시작
    if digits.startswith("82"):
        digits = '0' + digits[2:]
    # 0으로 시작하지 않으면, 0 추가
    if not digits.startswith('0'):
        digits = '0' + digits
    return digits

# ---------- SMS 발송 ----------
def send_sms(to_phone: str, text: str) -> bool:
    svc = get_message_service()
    from_phone = normalize_korean_number(os.getenv('SENDER_PHONE', ''))
    to_phone = normalize_korean_number(to_phone)

    msg = RequestMessage(
        from_=from_phone,
        to=to_phone,
        text=text
    )
    try:
        resp = svc.send(msg)
        info = resp.group_info.count
        logger.info(f"SMS 성공: group_id={resp.group_info.group_id}, total={info.total}, success={info.registered_success}, failed={info.registered_failed}")
        return True
    except Exception as e:
        logger.error(f"SMS 실패: {e}")
        return False

# ---------- Firestore 스냅샷 관리 ----------
def get_snapshot():
    """Firestore에서 스냅샷을 가져옵니다."""
    firestore_doc = os.getenv('FIRESTORE_DOC')
    if not firestore_doc or '/' not in firestore_doc:
        logger.error("FIRESTORE_DOC 형식 오류")
        return {}
    
    col, doc_id = firestore_doc.split('/', 1)
    db = get_firestore_client()
    doc_ref = db.collection(col).document(doc_id)
    
    try:
        doc = doc_ref.get()
        return doc.to_dict() if doc.exists else {}
    except Exception as e:
        logger.error(f"스냅샷 로드 실패: {e}")
        return {}

def save_snapshot(snapshot):
    """Firestore에 스냅샷을 저장합니다."""
    firestore_doc = os.getenv('ççFIRESTORE_DOC')
    if not firestore_doc or '/' not in firestore_doc:
        logger.error("FIRESTORE_DOC 형식 오류")
        return False
    
    col, doc_id = firestore_doc.split('/', 1)
    db = get_firestore_client()
    doc_ref = db.collection(col).document(doc_id)
    
    try:
        doc_ref.set(snapshot)
        logger.info("스냅샷 저장 성공")
        return True
    except Exception as e:
        logger.error(f"스냅샷 저장 실패: {e}")
        return False

# ---------- Cloud Function 엔트리포인트 ----------
def sheet_webhook(request: Request):
    # 1) 메서드 검증
    if request.method != 'POST':
        return ('Method Not Allowed', 405)
    if not request.headers.get('X-Goog-Resource-State'):
        logger.warning("Invalid webhook: Missing X-Goog-Resource-State")
        abort(400, description="Invalid webhook call")

    # 2) Sheet 읽기
    sheet_id = os.getenv('SHEET_ID')
    if not sheet_id:
        logger.error("환경변수 SHEET_ID 미설정")
        abort(500, description="Server configuration error")
    sheets = get_sheets_service()
    sheet_name = os.getenv('SHEET_NAME', 'Sheet1')
    range_name = f"{sheet_name}!A1:Z"
    try:
        sheet_resp = sheets.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=range_name
        ).execute()
    except Exception as e:
        logger.error(f"Sheet 읽기 실패: {e}")
        abort(500, description="Failed to read sheet")

    values = sheet_resp.get('values', [])
    if len(values) < 2:
        current_rows = []
    else:
        headers = values[0]
        data_rows = values[1:]
        current_rows = []
        for row in data_rows:
            row_full = row + ['']*(len(headers) - len(row))
            ts, *rest = row_full
            if not ts:
                continue
            row_dict = dict(zip(headers, row_full))
            current_rows.append((ts, row_dict))

    # 3) 이전 스냅샷 로드
    prev = get_snapshot()

    # 4) 변경 감지
    new_or_updated = []
    for ts, row in current_rows:
        if ts not in prev or prev.get(ts) != row:
            new_or_updated.append((ts, row))

    # 5) SMS 발송
    for ts, row in new_or_updated:
        phone = row.get('전화번호') or row.get('Phone') or ''
        name = row.get('이름') or row.get('Name') or ''
        inquiry = row.get('문의 종류') or row.get('Inquiry') or ''
        if not phone:
            logger.warning(f"전화번호 누락: ts={ts}")
            continue
        text = f"[알림]\n{name}님, '{inquiry}' 문의가 접수되었습니다. 감사합니다."
        if not send_sms(phone, text):
            logger.error(f"SMS 발송 실패: ts={ts}, phone={phone}")

    # 6) 스냅샷 저장
    snapshot = {ts: row for ts, row in current_rows}
    if not save_snapshot(snapshot):
        logger.error("스냅샷 저장 실패")

    return ('OK', 200)
