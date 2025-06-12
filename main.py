import os
import re
import logging
import time
import json
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
            logger.info("Firestore 클라이언트 초기화 시도: database='sheet-sync'")
            # sheet-sync 데이터베이스 사용
            _firestore_client = firestore.Client(database='sheet-sync')
            # Firestore 초기화 확인
            collections = list(_firestore_client.collections())
            logger.info(f"Firestore 초기화 성공: database='sheet-sync', collections={[col.id for col in collections]}")
        except NotFound as e:
            logger.error("Firestore 데이터베이스 'sheet-sync'를 찾을 수 없습니다.")
            raise RuntimeError("Firestore database 'sheet-sync' not found") from e
        except Exception as e:
            logger.error(f"Firestore 초기화 실패: {e}", exc_info=True)
            raise
    return _firestore_client

# ---------- Sheets API 서비스 ----------
def get_sheets_service():
    global _sheets_service
    if _sheets_service is None:
        try:
            creds, _ = google_auth_default(scopes=[
                'https://www.googleapis.com/auth/drive.readonly',
                'https://www.googleapis.com/auth/spreadsheets.readonly'
            ])
            # 토큰 갱신
            if not creds.valid:
                creds.refresh(GoogleAuthRequest())
            _sheets_service = build('sheets', 'v4', credentials=creds)
            logger.info("Initialized Sheets service")
        except Exception as e:
            logger.error(f"Sheets 서비스 초기화 실패: {e}")
            raise
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
        try:
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
        except Exception as e:
            logger.error(f"Solapi 서비스 초기화 실패: {e}")
            raise
    return _message_service

# ---------- 전화번호 정규화 ----------
def normalize_korean_number(number: str) -> str:
    try:
        # 숫자만 남기기
        digits = re.sub(r"\D", "", number)
        # 국제번호 82 제거 후 0으로 시작
        if digits.startswith("82"):
            digits = '0' + digits[2:]
        # 0으로 시작하지 않으면, 0 추가
        if not digits.startswith('0'):
            digits = '0' + digits
        logger.info(f"전화번호 정규화: {number} -> {digits}")
        return digits
    except Exception as e:
        logger.error(f"전화번호 정규화 실패: {number}, 오류: {e}")
        return number

# ---------- SMS 발송 ----------
def send_sms(to_phone: str, text: str) -> bool:
    try:
        svc = get_message_service()
        from_phone = normalize_korean_number(os.getenv('SENDER_PHONE', ''))
        to_phone = normalize_korean_number(to_phone)

        logger.info(f"SMS 발송 시도: from={from_phone}, to={to_phone}")
        logger.debug(f"SMS 내용: {text}")

        msg = RequestMessage(
            from_=from_phone,
            to=to_phone,
            text=text
        )
        resp = svc.send(msg)
        info = resp.group_info.count
        logger.info(f"SMS 성공: group_id={resp.group_info.group_id}, total={info.total}, success={info.registered_success}, failed={info.registered_failed}")
        return True
    except Exception as e:
        logger.error(f"SMS 발송 실패: {e}")
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
        logger.info(f"Firestore 문서 읽기 시도: database='sheet-sync', collection='{col}', document='{doc_id}'")
        
        try:
            db = get_firestore_client()
            doc_ref = db.collection(col).document(doc_id)
            doc = doc_ref.get()
            
            if doc.exists:
                data = doc.to_dict()
                _last_processed_ts = data.get('last_processed_ts')
                logger.info(f"마지막 처리 타임스탬프 로드: {_last_processed_ts}")
            else:
                logger.info("마지막 처리 타임스탬프 없음 - 새 문서 생성")
                # 문서가 없으면 생성
                doc_ref.set({
                    'last_processed_ts': None,
                    'created_at': datetime.now(timezone.utc).isoformat(),
                    'updated_at': datetime.now(timezone.utc).isoformat()
                })
                _last_processed_ts = None
        except Exception as e:
            logger.error(f"마지막 처리 타임스탬프 로드 실패: {e}", exc_info=True)
    
    return _last_processed_ts

def update_last_processed_timestamp(timestamp):
    """마지막으로 처리된 타임스탬프를 업데이트합니다."""
    global _last_processed_ts
    
    firestore_doc = os.getenv('FIRESTORE_DOC')
    if not firestore_doc or '/' not in firestore_doc:
        logger.error("FIRESTORE_DOC 형식 오류")
        return False
    
    col, doc_id = firestore_doc.split('/', 1)
    logger.info(f"Firestore 문서 업데이트 시도: database='sheet-sync', collection='{col}', document='{doc_id}', timestamp={timestamp}")
    
    try:
        db = get_firestore_client()
        doc_ref = db.collection(col).document(doc_id)
        
        # 문서 존재 여부 확인
        doc = doc_ref.get()
        if not doc.exists:
            logger.info("문서가 존재하지 않음 - 새로 생성")
            doc_ref.set({
                'last_processed_ts': timestamp,
                'created_at': datetime.now(timezone.utc).isoformat(),
                'updated_at': datetime.now(timezone.utc).isoformat()
            })
        else:
            logger.info("기존 문서 업데이트")
            doc_ref.update({
                'last_processed_ts': timestamp,
                'updated_at': datetime.now(timezone.utc).isoformat()
            })
        
        _last_processed_ts = timestamp
        logger.info(f"마지막 처리 타임스탬프 업데이트 성공: {timestamp}")
        return True
    except Exception as e:
        logger.error(f"마지막 처리 타임스탬프 업데이트 실패: {e}", exc_info=True)
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
def parse_timestamp(ts_str):
    """한글 타임스탬프 형식을 처리합니다."""
    try:
        # 오후/오전을 24시간 형식으로 변환
        if '오후' in ts_str:
            ts_str = ts_str.replace('오후', '').strip()
            hour = int(ts_str.split(':')[0].split()[-1])
            if hour < 12:
                hour += 12
            ts_str = ts_str.replace(str(hour), str(hour))
        elif '오전' in ts_str:
            ts_str = ts_str.replace('오전', '').strip()
        
        # 날짜 형식 변환 (2025. 6. 12 -> 2025-06-12)
        date_part = ts_str.split()[0:3]
        if len(date_part) == 3:
            year = date_part[0].replace('.', '')
            month = date_part[1].replace('.', '').zfill(2)
            day = date_part[2].replace('.', '').zfill(2)
            date_str = f"{year}-{month}-{day}"
            time_str = ' '.join(ts_str.split()[3:])
            ts_str = f"{date_str} {time_str}"
        
        return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
    except Exception as e:
        logger.error(f"Error parsing timestamp '{ts_str}': {e}")
        raise

def poll_sheet():
    """2초마다 시트를 확인하고 변경사항이 있으면 처리합니다."""
    global _last_processed_ts
    logger.info("Starting sheet polling...")
    
    # Sheets API 서비스 객체 생성
    service = get_sheets_service()

    # 환경 변수에서 SHEET_ID와 SHEET_NAME 가져오기
    SHEET_ID = os.getenv('SHEET_ID')
    SHEET_NAME = os.getenv('SHEET_NAME', 'Sheet1')

    while True:
        try:
            # 시트 읽기
            result = service.spreadsheets().values().get(
                spreadsheetId=SHEET_ID,
                range=f"{SHEET_NAME}!A2:Z"
            ).execute()
            
            rows = result.get('values', [])
            if not rows:
                logger.info("No data found in sheet")
                time.sleep(2)
                continue
                
            # 마지막 처리 시간 가져오기
            last_ts = get_last_processed_timestamp()
            if last_ts is None:
                # 첫 실행인 경우, 마지막 행의 타임스탬프를 저장하고 종료
                if rows:
                    last_row = rows[-1]
                    if len(last_row) > 0:
                        try:
                            last_ts = parse_timestamp(last_row[0])
                            update_last_processed_timestamp(last_ts)
                            logger.info(f"Initial timestamp set to: {last_ts}")
                        except ValueError as e:
                            logger.error(f"Error parsing initial timestamp: {e}")
                time.sleep(2)
                continue
            
            # 새로운 행 처리
            new_rows = []
            for row in rows:
                if len(row) > 0:
                    try:
                        row_ts = parse_timestamp(row[0])
                        if row_ts > last_ts:
                            new_rows.append(row)
                    except ValueError as e:
                        logger.error(f"Error parsing timestamp: {e}")
                        continue
            
            if new_rows:
                logger.info(f"Found {len(new_rows)} new rows")
                for row in new_rows:
                    try:
                        row_ts = parse_timestamp(row[0])
                        if row_ts > last_ts:
                            # SMS 전송
                            send_sms(row)
                            # 마지막 처리 시간 업데이트
                            update_last_processed_timestamp(row_ts)
                            last_ts = row_ts
                    except ValueError as e:
                        logger.error(f"Error processing row: {e}")
                        continue
            else:
                logger.debug("No new rows found")
                
        except Exception as e:
            logger.error(f"Error in polling: {e}")
            
        time.sleep(2)

def start_polling():
    """폴링을 시작합니다."""
    global _polling_thread, _is_polling
    
    if _polling_thread is None or not _polling_thread.is_alive():
        _is_polling = True
        _polling_thread = Thread(target=poll_sheet, daemon=True)
        _polling_thread.start()
        logger.info("Sheet 폴링 시작됨")
    else:
        logger.info("이미 폴링이 실행 중입니다")

def stop_polling():
    """폴링을 중지합니다."""
    global _is_polling
    
    if _is_polling:
        _is_polling = False
        if _polling_thread and _polling_thread.is_alive():
            _polling_thread.join(timeout=5)
        logger.info("Sheet 폴링 중지됨")
    else:
        logger.info("폴링이 실행 중이지 않습니다")

# ---------- Cloud Function 엔트리포인트 ----------
def sheet_webhook(request: Request):
    # 1) 메서드 검증
    if request.method != 'POST':
        logger.warning(f"잘못된 메서드: {request.method}")
        return ('Method Not Allowed', 405)
    
    # 2) 요청 처리fina
    try:
        data = request.get_json()
        logger.info(f"요청 데이터: {data}")
        
        if data and data.get('action') == 'start_polling':
            start_polling()
            return ('Polling started', 200)
        elif data and data.get('action') == 'stop_polling':
            stop_polling()
            return ('Polling stopped', 200)
    except Exception as e:
        logger.error(f"요청 처리 중 오류: {e}", exc_info=True)
    
    # 3) 기존 webhook 처리
    resource_state = request.headers.get('X-Goog-Resource-State')
    if not resource_state:
        logger.warning("Invalid webhook: Missing X-Goog-Resource-State")
        abort(400, description="Invalid webhook call")
    
    logger.info(f"Webhook 호출: resource_state={resource_state}")
    
    # Sheet 읽기 및 처리
    sheet_id = os.getenv('SHEET_ID')
    if not sheet_id:
        logger.error("환경변수 SHEET_ID 미설정")
        abort(500, description="Server configuration error")
    
    sheets = get_sheets_service()
    sheet_name = os.getenv('SHEET_NAME', 'Sheet1')
    range_name = f"{sheet_name}!A1:Z"
    
    try:
        logger.info(f"Sheet 읽기 시도: {sheet_id}, {range_name}")
        sheet_resp = sheets.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=range_name
        ).execute()
    except Exception as e:
        logger.error(f"Sheet 읽기 실패: {e}", exc_info=True)
        abort(500, description="Failed to read sheet")

    values = sheet_resp.get('values', [])
    if len(values) < 2:
        current_rows = []
    else:
        headers = values[0]
        data_rows = values[1:]
        current_rows = []
        
        logger.info(f"헤더: {headers}")
        logger.info(f"데이터 행 수: {len(data_rows)}")
        
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
        try:
            parsed_ts = parse_timestamp(ts)
            if last_ts is None or parsed_ts > last_ts:
                new_rows.append((parsed_ts, row))
        except ValueError as e:
            logger.error(f"Error parsing timestamp: {e}")
            continue
    
    if new_rows:
        logger.info(f"새로운 행 발견: {len(new_rows)}개")
        logger.debug(f"새로운 행 데이터: {json.dumps(new_rows, ensure_ascii=False, indent=2)}")
        
        # 가장 최근 타임스탬프 찾기
        latest_ts = max(ts for ts, _ in new_rows)
        
        # SMS 발송
        for ts, row in new_rows:
            phone = row.get('전화번호') or row.get('Phone') or row.get("연락처 / Phone Number") or ""
            name = row.get('이름') or row.get('Name') or row.get("이름(혹은 닉네임) /  Name or nickname") or ''
            inquiry = row.get('문의 종류') or row.get('Inquiry') or row.get("프리즘지점에서,") or ''
            
            logger.info(f"행 처리 중: ts={ts}, phone={phone}, name={name}, inquiry={inquiry}")
            
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
        if update_last_processed_timestamp(latest_ts):
            logger.info(f"타임스탬프 업데이트 성공: {latest_ts}")
        else:
            logger.error(f"타임스탬프 업데이트 실패: {latest_ts}")

    return ('OK', 200)
