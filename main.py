import logging
import os
from datetime import datetime, timedelta, timezone
from google.oauth2 import service_account
from googleapiclient.discovery import build
from google.cloud import firestore
import requests
import json
from dotenv import load_dotenv
import time
from solapi import SolapiMessageService
from solapi.model import RequestMessage
import base64
from flask import Flask, jsonify
import threading
import pytz

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),  # 콘솔에 출력
        logging.FileHandler('app.log')  # 파일에도 저장
    ]
)
logger = logging.getLogger(__name__)

# 환경 변수 로드
load_dotenv()

# 환경 변수에서 설정 가져오기
SPREADSHEET_ID = os.getenv('SPREADSHEET_ID')
SHEET_NAME = os.getenv('SHEET_NAME', 'Sheet1')  # 기본값 Sheet1
RANGE = os.getenv('RANGE', 'A2:Z')  # 기본값 A2:Z
SOLAPI_API_KEY = os.getenv('SOLAPI_API_KEY')
SOLAPI_API_SECRET = os.getenv('SOLAPI_API_SECRET')
SOLAPI_SENDER = os.getenv('SOLAPI_SENDER')
RECIPIENT_PHONE_NUMBER = os.getenv('RECIPIENT_PHONE_NUMBER')
POLLING_INTERVAL = int(os.getenv('POLLING_INTERVAL', '2'))  

app = Flask(__name__)

# 전역 변수로 클라이언트 초기화
sheets_service = None
firestore_client = None
polling_thread = None
stop_polling = False

@app.route('/')
def home():
    return "Hello, World!"

def get_firestore_client():
    """Firestore 클라이언트를 초기화하고 반환합니다."""
    global firestore_client
    if firestore_client is None:
        try:
            firestore_client = firestore.Client(
                project='prism-fin',  # 하드코딩된 프로젝트 ID
                database='sheet-sync'  # 하드코딩된 데이터베이스 이름
            )
            logger.info("Firestore 클라이언트 초기화 성공")
        except Exception as e:
            logger.error(f"Firestore 클라이언트 초기화 실패: {e}")
            raise
    return firestore_client

def get_sheets_service():
    """Google Sheets API 서비스를 초기화하고 반환합니다."""
    global sheets_service
    if sheets_service is None:
        try:
            sheets_service = build('sheets', 'v4')
            logger.info("Google Sheets API 서비스 초기화 성공")
        except Exception as e:
            logger.error(f"Google Sheets API 서비스 초기화 실패: {e}")
            raise
    return sheets_service

def parse_korean_datetime(datetime_str):
    """다양한 형식의 날짜 문자열을 datetime 객체로 변환합니다."""
    formats = [
        ('%Y. %m. %d 오전 %H:%M:%S', False),  # 2025. 6. 7 오전 10:41:17
        ('%Y. %m. %d 오후 %H:%M:%S', True),   # 2025. 6. 7 오후 10:41:17
        ('%Y-%m-%d %H:%M:%S', False),         # 2025-6-7 14:26:43
        ('%Y-%m-%d %H:%M', False),            # 2025-6-7 14:26
    ]
    
    for fmt, is_pm in formats:
        try:
            dt = datetime.strptime(datetime_str, fmt)
            if is_pm and dt.hour < 12:
                dt = dt.replace(hour=dt.hour + 12)
            # 한국 시간대 적용
            korea_tz = pytz.timezone('Asia/Seoul')
            dt = korea_tz.localize(dt)
            return dt
        except ValueError:
            continue
    
    raise ValueError(f"날짜 형식을 파싱할 수 없습니다: {datetime_str}")

def send_sms(phone, name, inquiry):
    """SMS를 발송합니다."""
    try:
        message_service = SolapiMessageService(
            SOLAPI_API_KEY,
            SOLAPI_API_SECRET
        )
        
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
        
        # RequestMessage 모델 사용
        message = RequestMessage(
            from_=SOLAPI_SENDER,
            to=phone,
            text=text
        )
        
        response = message_service.send(message)
        logger.info(f"SMS 발송 결과: {response}")
        logger.info(f"Group ID: {response.group_info.group_id}")
        logger.info(f"요청한 메시지 개수: {response.group_info.count.total}")
        logger.info(f"성공한 메시지 개수: {response.group_info.count.registered_success}")
        logger.info(f"실패한 메시지 개수: {response.group_info.count.registered_failed}")
        return response
    except Exception as e:
        logger.error(f"SMS 발송 실패: {e}")
        return None

def poll_sheet():
    """Google Sheets 데이터를 폴링하고 Firestore에 저장합니다."""
    try:
        logger.info("Google Sheets 데이터 확인 시작")
        sheets = get_sheets_service()
        db = get_firestore_client()
        
        # 스프레드시트 ID와 시트 이름을 환경 변수에서 가져옵니다.
        spreadsheet_id = os.getenv('SPREADSHEET_ID')
        sheet_name = os.getenv('SHEET_NAME')
        
        if not spreadsheet_id or not sheet_name:
            raise ValueError("SPREADSHEET_ID 또는 SHEET_NAME 환경 변수가 설정되지 않았습니다.")
        
        logger.info(f"스프레드시트 확인 중: ID={spreadsheet_id}, 시트={sheet_name}")
        
        # 마지막으로 처리된 정보를 Firestore에서 가져옵니다
        last_processed_ref = db.collection('metadata').document('last_processed')
        last_processed_doc = last_processed_ref.get()
        last_processed = {
            'timestamp': last_processed_doc.get('timestamp') if last_processed_doc.exists else None,
            'row_number': last_processed_doc.get('row_number') if last_processed_doc.exists and 'row_number' in last_processed_doc.to_dict() else 0
        }
        
        logger.info(f"마지막으로 처리된 정보: 타임스탬프={last_processed['timestamp']}, 행 번호={last_processed['row_number']}")
        
        # Google Sheets API를 사용하여 데이터를 가져옵니다.
        logger.info("Google Sheets API 호출 시작")
        result = sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=sheet_name
        ).execute()
        logger.info("Google Sheets API 호출 완료")
        
        values = result.get('values', [])
        
        if not values:
            logger.warning("데이터가 없습니다.")
            return
        
        logger.info(f"시트에서 총 {len(values)}행의 데이터를 가져왔습니다.")
        
        # 헤더 행을 가져와서 컬럼 인덱스 매핑
        headers = values[0]
        phone_idx = headers.index('연락처 / Phone Number') if '연락처 / Phone Number' in headers else -1
        name_idx = headers.index('이름(혹은 닉네임) /  Name or nickname') if '이름(혹은 닉네임) /  Name or nickname' in headers else -1
        
        if phone_idx == -1 or name_idx == -1:
            logger.error("필수 컬럼을 찾을 수 없습니다.")
            return
            
        logger.info(f"컬럼 매핑 - 전화번호: {phone_idx}, 이름: {name_idx}")
        
        # 헤더 행을 제외하고 데이터 처리
        data_rows = values[1:] if len(values) > 1 else []
        logger.info(f"헤더를 제외한 데이터 행 수: {len(data_rows)}")
        
        # 새로운 데이터만 처리
        new_rows = []
        for i, row in enumerate(data_rows, start=2):  # 2부터 시작 (헤더가 1행)
            if len(row) > max(phone_idx, name_idx):  # 필요한 컬럼이 모두 있는지 확인
                try:
                    row_timestamp = parse_korean_datetime(row[0])
                    # 타임스탬프가 같거나 더 크고, 행 번호가 더 큰 경우에만 처리
                    if (last_processed['timestamp'] is None or 
                        row_timestamp > last_processed['timestamp'] or 
                        (row_timestamp == last_processed['timestamp'] and i > last_processed['row_number'])):
                        new_rows.append((row, row_timestamp, i))
                except ValueError as e:
                    logger.warning(f"타임스탬프 파싱 실패: {row[0]}, {e}")
                    continue
        
        if not new_rows:
            logger.info("새로운 데이터가 없습니다.")
            return
            
        logger.info(f"새로운 데이터 {len(new_rows)}행 발견")
        
        # 데이터를 Firestore에 저장하고 SMS 전송
        latest_processed = {'timestamp': None, 'row_number': 0}
        for row, row_timestamp, row_number in new_rows:
            try:
                # Firestore에 저장
                doc_ref = db.collection('sheet_data').document()
                doc_ref.set({
                    'timestamp': row[0],
                    'phone': row[phone_idx],
                    'name': row[name_idx],
                    'row_number': row_number,
                    'processed_at': firestore.SERVER_TIMESTAMP
                })
                logger.info(f"행 처리 완료: {row[0]}, {row[name_idx]}, 행 번호: {row_number}")
                
                # SMS 전송 시도
                logger.info(f"SMS 전송 시도: {row[name_idx]}")
                send_sms(row[phone_idx], row[name_idx], '프리즘지점에서,')
                logger.info(f"SMS 전송 성공: {row[name_idx]}")
                
                # 마지막 처리 정보 업데이트
                if (latest_processed['timestamp'] is None or 
                    row_timestamp > latest_processed['timestamp'] or 
                    (row_timestamp == latest_processed['timestamp'] and row_number > latest_processed['row_number'])):
                    latest_processed = {'timestamp': row_timestamp, 'row_number': row_number}
                
            except Exception as e:
                logger.error(f"행 처리 중 오류 발생: {row}, 오류: {e}")
                continue
        
        # 마지막으로 처리된 정보 업데이트
        if latest_processed['timestamp']:
            last_processed_ref.set({
                'timestamp': latest_processed['timestamp'],
                'row_number': latest_processed['row_number'],
                'last_updated': firestore.SERVER_TIMESTAMP
            })
            logger.info(f"마지막 처리 정보 업데이트: 타임스탬프={latest_processed['timestamp']}, 행 번호={latest_processed['row_number']}")
        
        logger.info(f"데이터 처리 완료. 총 {len(new_rows)}개의 새로운 행이 처리되었습니다.")
        
    except Exception as e:
        logger.error(f"데이터 폴링 중 오류 발생: {e}")
        raise

def polling_worker():
    """백그라운드에서 지속적으로 시트 데이터를 확인하는 워커 함수"""
    global stop_polling
    while not stop_polling:
        try:
            poll_sheet()
            time.sleep(2)  # 2초 대기
        except Exception as e:
            logger.error(f"폴링 워커에서 오류 발생: {e}")
            time.sleep(2)  # 오류 발생 시에도 2초 대기

@app.route('/health', methods=['GET'])
def health_check():
    """헬스 체크 엔드포인트"""
    return jsonify({"status": "healthy"}), 200

@app.route('/poll', methods=['POST'])
def trigger_poll():
    """수동으로 폴링을 트리거하는 엔드포인트"""
    try:
        poll_sheet()
        return jsonify({"status": "success", "message": "폴링이 성공적으로 완료되었습니다."}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

def start_polling():
    """폴링 스레드를 시작합니다."""
    global polling_thread, stop_polling
    stop_polling = False
    polling_thread = threading.Thread(target=polling_worker)
    polling_thread.daemon = True  # 메인 스레드가 종료되면 함께 종료되도록 설정
    polling_thread.start()
    logger.info("폴링 스레드가 시작되었습니다.")

def stop_polling_thread():
    """폴링 스레드를 중지합니다."""
    global stop_polling, polling_thread
    stop_polling = True
    if polling_thread and polling_thread.is_alive():
        polling_thread.join()
        logger.info("폴링 스레드가 중지되었습니다.")

# Flask 2.3.0 이상 버전에서 before_first_request 대체
with app.app_context():
    start_polling()

@app.teardown_appcontext
def cleanup(exception=None):
    """애플리케이션 컨텍스트가 종료될 때 정리 작업을 수행합니다."""
    stop_polling_thread()

def init_google_client():
    try:
#        credentials_dict = json.loads(GOOGLE_CREDENTIALS)
#        credentials = service_account.Credentials.from_service_account_info(
#            credentials_dict,
#            scopes=['https://www.googleapis.com/auth/spreadsheets.readonly']
#        )
        service = build('sheets', 'v4')#, credentials=credentials)
        return service
    except Exception as e:
        logger.error(f"Google API 클라이언트 초기화 실패: {str(e)}")
        raise

def init_firestore_client():
    try:
#        credentials_dict = json.loads(GOOGLE_CREDENTIALS)
#        credentials = service_account.Credentials.from_service_account_info(
#            credentials_dict
#        )
        db = firestore.Client()#credentials=credentials)
        return db
    except Exception as e:
        logger.error(f"Firestore 클라이언트 초기화 실패: {str(e)}")
        raise

def init_solapi_client():
    try:
        return SolapiMessageService(SOLAPI_API_KEY, SOLAPI_API_SECRET)
    except Exception as e:
        logger.error(f"Solapi 클라이언트 초기화 실패: {str(e)}")
        raise

def get_sheet_data(service):
    try:
        if not SPREADSHEET_ID:
            raise ValueError("SPREADSHEET_ID가 설정되지 않았습니다.")
            
        range_name = f"{SHEET_NAME}!{RANGE}"
        result = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=range_name
        ).execute()
        return result.get('values', [])
    except Exception as e:
        logger.error(f"Google Sheet 데이터 가져오기 실패: {str(e)}")
        raise

def save_to_firestore(db, data):
    try:
        batch = db.batch()
        collection_ref = db.collection('sheet_data')
        
        for row in data:
            if len(row) >= 2:  # 최소한 timestamp와 value가 있는지 확인
                timestamp_str = row[0]
                value = row[1]
                
                try:
                    # 타임스탬프 파싱
                    timestamp = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S')
                    
                    # 문서 ID 생성 (타임스탬프 기반)
                    doc_id = timestamp.strftime('%Y%m%d%H%M%S')
                    
                    # 문서 데이터 준비
                    doc_data = {
                        'timestamp': timestamp,
                        'value': value,
                        'created_at': firestore.SERVER_TIMESTAMP
                    }
                    
                    # 배치에 추가
                    doc_ref = collection_ref.document(doc_id)
                    batch.set(doc_ref, doc_data)
                    
                except ValueError as e:
                    logger.warning(f"잘못된 타임스탬프 형식: {timestamp_str}, 오류: {str(e)}")
                    continue
        
        # 배치 커밋
        batch.commit()
        logger.info(f"Firestore에 {len(data)}개의 데이터 저장 완료")
        
    except Exception as e:
        logger.error(f"Firestore 데이터 저장 실패: {str(e)}")
        raise

def check_and_send_sms(db, solapi_client, last_check_time):
    try:
        # 마지막 체크 이후의 데이터 조회
        query = db.collection('sheet_data').where('created_at', '>', last_check_time)
        new_docs = query.stream()
        
        for doc in new_docs:
            data = doc.to_dict()
            message = f"새로운 데이터가 추가되었습니다:\n시간: {data['timestamp'].strftime('%Y-%m-%d %H:%M:%S')}\n값: {data['value']}"
            
            try:
                solapi_client.send_message({
                    'to': RECIPIENT_PHONE_NUMBER,
                    'from': SOLAPI_SENDER,
                    'text': message
                })
                logger.info(f"SMS 전송 성공: {message}")
            except Exception as e:
                logger.error(f"SMS 전송 실패: {str(e)}")
                
    except Exception as e:
        logger.error(f"새 데이터 확인 및 SMS 전송 실패: {str(e)}")
        raise

def main():
    """메인 함수"""
    try:
        logger.info("프로그램 시작")
        poll_sheet()
    except Exception as e:
        logger.error(f"프로그램 실행 중 오류 발생: {e}")
        raise

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
