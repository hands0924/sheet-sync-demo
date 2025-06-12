import os
import re
import logging
import time
from flask import Request, abort
from threading import Thread
from datetime import datetime, timezone

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
_drive_service = None
_polling_thread = None
_is_polling = False
_last_processed_ts = None

# ---------- Firestore 클라이언트 ----------
def get_firestore_client():
    global _firestore_client
    if _firestore_client is None:
        try:
            # sheet-sync 데이터베이스 사용
            _firestore_client = firestore.Client(database='sheet-sync')
            # Firestore 초기화 확인
            _firestore_client.collections()
            logger.info("Initialized Firestore client with database 'sheet-sync'")
        except NotFound as e:
            logger.error("Firestore 데이터베이스 'sheet-sync'를 찾을 수 없습니다.")
            raise RuntimeError("Firestore database 'sheet-sync' not found") from e
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

# ---------- Drive API 서비스 ----------
def get_drive_service():
    global _drive_service
    if _drive_service is None:
        creds, _ = google_auth_default(scopes=[
            'https://www.googleapis.com/auth/drive.readonly'
        ])
        if not creds.valid:
            creds.refresh(GoogleAuthRequest())
        _drive_service = build('drive', 'v3', credentials=creds)
        logger.info("Initialized Drive service")
    return _drive_service

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
def get_last_processed_timestamp():
    """마지막으로 처리된 타임스탬프를 가져옵니다."""
    global _last_processed_ts
    
    if _last_processed_ts is None:
        firestore_doc = os.getenv('FIRESTORE_DOC')
        if not firestore_doc or '/' not in firestore_doc:
            logger.error("FIRESTORE_DOC 형식 오류")
            return None
        
        col, doc_id = firestore_doc.split('/', 1)
        db = get_firestore_client()
        doc_ref = db.collection(col).document(doc_id)
        
        try:
            doc = doc_ref.get()
            if doc.exists:
                data = doc.to_dict()
                _last_processed_ts = data.get('last_processed_ts')
                logger.info(f"마지막 처리 타임스탬프 로드: {_last_processed_ts}")
        except Exception as e:
            logger.error(f"마지막 처리 타임스탬프 로드 실패: {e}")
    
    return _last_processed_ts

def update_last_processed_timestamp(timestamp):
    """마지막으로 처리된 타임스탬프를 업데이트합니다."""
    global _last_processed_ts
    
    firestore_doc = os.getenv('FIRESTORE_DOC')
    if not firestore_doc or '/' not in firestore_doc:
        logger.error("FIRESTORE_DOC 형식 오류")
        return False
    
    col, doc_id = firestore_doc.split('/', 1)
    db = get_firestore_client()
    doc_ref = db.collection(col).document(doc_id)
    
    try:
        doc_ref.set({
            'last_processed_ts': timestamp,
            'updated_at': datetime.now(timezone.utc).isoformat()
        })
        _last_processed_ts = timestamp
        logger.info(f"마지막 처리 타임스탬프 업데이트: {timestamp}")
        return True
    except Exception as e:
        logger.error(f"마지막 처리 타임스탬프 업데이트 실패: {e}")
        return False

# ---------- Drive Watch 설정 ----------
def setup_drive_watch():
    """Drive Watch를 설정합니다."""
    sheet_id = os.getenv('SHEET_ID')
    if not sheet_id:
        logger.error("환경변수 SHEET_ID 미설정")
        return False
    
    drive = get_drive_service()
    cloud_function_url = os.getenv('CLOUD_FUNCTION_URL')
    if not cloud_function_url:
        logger.error("환경변수 CLOUD_FUNCTION_URL 미설정")
        return False
    
    try:
        # 기존 watch 채널 제거
        try:
            drive.channels().stop(body={
                'id': 'sheet-sync-channel',
                'resourceId': f'sheet-{sheet_id}'
            }).execute()
        except Exception:
            pass  # 기존 채널이 없는 경우 무시
        
        # 새로운 watch 채널 설정
        channel = drive.files().watch(
            fileId=sheet_id,
            body={
                'id': 'sheet-sync-channel',
                'type': 'web_hook',
                'address': cloud_function_url,
                'expiration': int((time.time() + 7 * 24 * 60 * 60) * 1000),  # 7일
                'payload': True
            }
        ).execute()
        
        logger.info(f"Drive Watch 설정 성공: channel_id={channel.get('id')}")
        return True
    except Exception as e:
        logger.error(f"Drive Watch 설정 실패: {e}")
        return False

# ---------- Sheet 폴링 ----------
def poll_sheet():
    """2초마다 Sheet를 폴링하여 변경사항을 확인합니다."""
    global _is_polling, _last_processed_ts
    
    sheet_id = os.getenv('SHEET_ID')
    if not sheet_id:
        logger.error("환경변수 SHEET_ID 미설정")
        return
    
    sheet_name = os.getenv('SHEET_NAME', 'Sheet1')
    range_name = f"{sheet_name}!A1:Z"
    
    # 마지막 처리 타임스탬프 로드
    _last_processed_ts = get_last_processed_timestamp()
    
    while _is_polling:
        try:
            # Sheet 읽기
            sheets = get_sheets_service()
            sheet_resp = sheets.spreadsheets().values().get(
                spreadsheetId=sheet_id,
                range=range_name
            ).execute()
            
            values = sheet_resp.get('values', [])
            if len(values) < 2:
                time.sleep(2)
                continue
                
            # 데이터 처리
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
            
            # 새로운 행만 처리
            new_rows = []
            for ts, row in current_rows:
                if _last_processed_ts is None or ts > _last_processed_ts:
                    new_rows.append((ts, row))
            
            if new_rows:
                # 가장 최근 타임스탬프 찾기
                latest_ts = max(ts for ts, _ in new_rows)
                
                # SMS 발송
                for ts, row in new_rows:
                    phone = row.get('전화번호') or row.get('Phone') or ''
                    name = row.get('이름') or row.get('Name') or ''
                    inquiry = row.get('문의 종류') or row.get('Inquiry') or ''
                    if not phone:
                        logger.warning(f"전화번호 누락: ts={ts}")
                        continue
                    text = f"[알림]\n{name}님, '{inquiry}' 문의가 접수되었습니다. 감사합니다."
                    if not send_sms(phone, text):
                        logger.error(f"SMS 발송 실패: ts={ts}, phone={phone}")
                
                # 마지막 처리 타임스탬프 업데이트
                update_last_processed_timestamp(latest_ts)
            
        except Exception as e:
            logger.error(f"폴링 중 오류 발생: {e}")
        
        time.sleep(2)  # 2초 대기

def start_polling():
    """폴링을 시작합니다."""
    global _polling_thread, _is_polling
    
    if _polling_thread is None or not _polling_thread.is_alive():
        _is_polling = True
        _polling_thread = Thread(target=poll_sheet, daemon=True)
        _polling_thread.start()
        logger.info("Sheet 폴링 시작")

def stop_polling():
    """폴링을 중지합니다."""
    global _is_polling
    
    _is_polling = False
    if _polling_thread and _polling_thread.is_alive():
        _polling_thread.join(timeout=5)
    logger.info("Sheet 폴링 중지")

# ---------- Cloud Function 엔트리포인트 ----------
def sheet_webhook(request: Request):
    # 1) 메서드 검증
    if request.method != 'POST':
        return ('Method Not Allowed', 405)
    
    # 2) 요청 처리
    try:
        data = request.get_json()
        if data and data.get('action') == 'start_polling':
            start_polling()
            return ('Polling started', 200)
        elif data and data.get('action') == 'stop_polling':
            stop_polling()
            return ('Polling stopped', 200)
    except Exception:
        pass
    
    # 3) 기존 webhook 처리
    resource_state = request.headers.get('X-Goog-Resource-State')
    if not resource_state:
        logger.warning("Invalid webhook: Missing X-Goog-Resource-State")
        abort(400, description="Invalid webhook call")
    
    # Sheet 읽기 및 처리
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

    # 마지막 처리 타임스탬프 확인
    last_ts = get_last_processed_timestamp()
    
    # 새로운 행만 처리
    new_rows = []
    for ts, row in current_rows:
        if last_ts is None or ts > last_ts:
            new_rows.append((ts, row))
    
    if new_rows:
        # 가장 최근 타임스탬프 찾기
        latest_ts = max(ts for ts, _ in new_rows)
        
        # SMS 발송
        for ts, row in new_rows:
            phone = row.get('전화번호') or row.get('Phone') or row.get("연락처 / Phone Number") or ""
            name = row.get('이름') or row.get('Name') or row.get("이름(혹은 닉네임) /  Name or nickname") or ''
            inquiry = row.get('문의 종류') or row.get('Inquiry') or row.get("프리즘지점에서,") or ''
            if not phone:
                logger.warning(f"전화번호 누락: ts={ts}")
                continue
            text = f"""
                    [포용적 금융서비스, 프리즘지점]
                    {name}님, 만사형통 프리즘 부적 이벤트에 참여해주셔서 감사합니다! 

                    프리즘지점은 퀴어 당사자와 앨라이 보험설계사가 함께하는 보험 조직입니다. 모두를 위한 미래보장을 꿈꾸며, 금융의 경계를 넘어 연대합니다.

                    선택해주신 {inquiry} 문의에 반가운 마음을 전하며, 유용한 소식과 답변 안내드릴 수 있도록 곧 다시 연락드리겠습니다. 고맙습니다!

                    프리즘지점 드림
                    [보험상담 및 채용문의]
                    https://litt.ly/prism.fin

                    앞으로 소식은
                    [인스타그램] 팔로우해주세요!
                    www.instagram.com/prism.fin"""
            if not send_sms(phone, text):
                logger.error(f"SMS 발송 실패: ts={ts}, phone={phone}")
        
        # 마지막 처리 타임스탬프 업데이트
        update_last_processed_timestamp(latest_ts)

    return ('OK', 200)
