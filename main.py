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
from flask import Flask
import threading

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

@app.route('/')
def home():
    return "Hello, World!"

def get_firestore_client():
    try:
        sa_key = os.getenv('GCP_SA_KEY')
        if not sa_key:
            raise ValueError("GCP_SA_KEY 환경 변수가 설정되지 않았습니다.")
        
        # JSON 형식으로 파싱
        credentials_dict = json.loads(sa_key)
        credentials = service_account.Credentials.from_service_account_info(
            credentials_dict,
            scopes=['https://www.googleapis.com/auth/cloud-platform']
        )
        db = firestore.Client(
            project='prism-fin',  # 하드코딩된 프로젝트 ID
            credentials=credentials,
            database='sheet-sync'  # 하드코딩된 데이터베이스 이름
        )
        logger.info("Firestore 클라이언트 초기화 성공")
        return db
    except Exception as e:
        logger.error(f"Firestore 클라이언트 초기화 실패: {e}")
        raise

def get_sheets_service():
    """Google Sheets API 서비스를 초기화하고 반환합니다."""
    try:
        credentials = service_account.Credentials.from_service_account_file(
            'sheet-sync-sa-key.json',
            scopes=['https://www.googleapis.com/auth/spreadsheets.readonly']
        )
        service = build('sheets', 'v4', credentials=credentials)
        logger.info("Google Sheets API 서비스 초기화 성공")
        return service
    except Exception as e:
        logger.error(f"Google Sheets API 서비스 초기화 실패: {e}")
        raise

def parse_timestamp(timestamp_str):
    """한국어 타임스탬프 문자열을 datetime 객체로 변환합니다."""
    try:
        if not timestamp_str:
            raise ValueError("빈 타임스탬프 문자열")
            
        # "2024년 3월 14일 오후 2:30:45" 형식 처리
        timestamp_str = timestamp_str.replace('년 ', '-').replace('월 ', '-').replace('일 ', ' ')
        
        # "2025. 6. 12 02:26:19" 형식 처리
        if '.' in timestamp_str:
            parts = timestamp_str.split('.')
            if len(parts) >= 3:
                year = parts[0].strip()
                month = parts[1].strip()
                day_time = parts[2].strip()
                timestamp_str = f"{year}-{month.zfill(2)}-{day_time}"
        
        # 오전/오후 처리
        is_pm = '오후' in timestamp_str
        timestamp_str = timestamp_str.replace('오전 ', '').replace('오후 ', '')
        
        # 시간이 한 자리 수인 경우 앞에 0을 추가
        if ':' in timestamp_str:
            time_part = timestamp_str.split(' ')[-1]
            hour, minute, second = time_part.split(':')
            if len(hour) == 1:
                hour = '0' + hour
            timestamp_str = timestamp_str.replace(time_part, f"{hour}:{minute}:{second}")
        
        # datetime 객체로 변환
        dt = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S')
        
        # 오후 시간 처리 (12시는 제외)
        if is_pm and dt.hour < 12:
            dt = dt.replace(hour=dt.hour + 12)
        
        # KST로 설정
        dt = dt.replace(tzinfo=timezone(timedelta(hours=9)))
        return dt
    except Exception as e:
        logger.error(f"타임스탬프 파싱 오류: {e}, 입력값: {timestamp_str}")
        raise ValueError(f"잘못된 타임스탬프 형식: {timestamp_str}") from e

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
    """주기적으로 시트를 확인하는 함수"""
    logger.info("폴링 시작")
    initial_load = True
    last_processed_timestamp = None
    
    # Firestore 클라이언트 초기화
    try:
        db = get_firestore_client()
        logger.info("Firestore 연결 성공")
    except Exception as e:
        logger.error(f"Firestore 연결 실패: {e}")
        return
    
    while True:
        try:
            logger.info("시트 확인 중...")
            service = get_sheets_service()
            sheet_id = os.getenv('SPREADSHEET_ID')
            sheet_name = os.getenv('SHEET_NAME')
            
            if not sheet_id:
                logger.error("SPREADSHEET_ID 환경 변수가 설정되지 않았습니다.")
                time.sleep(2)
                continue
                
            # 시트 데이터 읽기 (헤더 포함)
            result = service.spreadsheets().values().get(
                spreadsheetId=sheet_id,
                range=f'{sheet_name}!A1:Z'
            ).execute()
            
            values = result.get('values', [])
            if not values:
                logger.info("시트에 데이터가 없습니다.")
                time.sleep(2)
                continue
                
            # 헤더와 데이터 분리
            headers = values[0]
            data_rows = values[1:]
            
            # 여기서 데이터 처리 로직 추가
            logger.info(f"시트 데이터 확인: {len(data_rows)} 행")
            
            time.sleep(POLLING_INTERVAL)  # 지정된 간격만큼 대기
        except Exception as e:
            logger.error(f"시트 확인 중 오류 발생: {e}")
            time.sleep(2)

def init_google_client():
    try:
        credentials_dict = json.loads(GOOGLE_CREDENTIALS)
        credentials = service_account.Credentials.from_service_account_info(
            credentials_dict,
            scopes=['https://www.googleapis.com/auth/spreadsheets.readonly']
        )
        service = build('sheets', 'v4', credentials=credentials)
        return service
    except Exception as e:
        logger.error(f"Google API 클라이언트 초기화 실패: {str(e)}")
        raise

def init_firestore_client():
    try:
        credentials_dict = json.loads(GOOGLE_CREDENTIALS)
        credentials = service_account.Credentials.from_service_account_info(
            credentials_dict
        )
        db = firestore.Client(credentials=credentials)
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
    # 백그라운드에서 폴링 시작
    polling_thread = threading.Thread(target=poll_sheet)
    polling_thread.daemon = True
    polling_thread.start()
    
    # Flask 서버 시작
    logger.info("Flask 서버 시작 중...")
    app.run(host='0.0.0.0', port=8080, debug=True)
