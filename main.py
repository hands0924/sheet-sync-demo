import os
import json
import logging
from google.cloud import firestore
from google.oauth2 import service_account
from googleapiclient.discovery import build
import hashlib
import hmac
import time
import datetime
import requests
from dateutil import parser
from dotenv import load_dotenv
#
# .env 파일 로드 (로컬 개발 환경에서만 사용)
load_dotenv()

# 로깅 설정
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 환경 변수
SHEET_ID = os.environ.get("SHEET_ID")
if not SHEET_ID:
    raise ValueError("SHEET_ID 환경 변수가 설정되지 않았습니다.")

FIRESTORE_DOC = os.environ.get("FIRESTORE_DOC", "sheet_snapshots/latest")
SOLAPI_API_KEY = os.environ.get("SOLAPI_API_KEY")
SOLAPI_API_SECRET = os.environ.get("SOLAPI_API_SECRET")
SENDER_PHONE = os.environ.get("SENDER_PHONE")

# 필수 환경 변수 검증
if not all([SOLAPI_API_KEY, SOLAPI_API_SECRET, SENDER_PHONE]):
    logger.warning("SMS 발송에 필요한 환경 변수가 설정되지 않았습니다. SMS 기능이 비활성화됩니다.")
    SMS_ENABLED = False
else:
    SMS_ENABLED = True
    logger.info("SMS 발송 기능이 활성화되었습니다.")

# GCP 서비스 계정 키 검증
GCP_SA_KEY = os.environ.get("GCP_SA_KEY")
if not GCP_SA_KEY:
    raise ValueError("GCP_SA_KEY 환경 변수가 설정되지 않았습니다.")

try:
    # 서비스 계정 키가 유효한 JSON인지 미리 검증
    sa_key_json = json.loads(GCP_SA_KEY)
    logger.info("GCP_SA_KEY가 유효한 JSON 형식입니다.")
except json.JSONDecodeError as e:
    logger.error(f"GCP_SA_KEY JSON 파싱 실패: {str(e)}")
    logger.error(f"GCP_SA_KEY 값: {GCP_SA_KEY[:100]}...")  # 처음 100자만 로깅
    raise ValueError(f"GCP_SA_KEY가 유효한 JSON 형식이 아닙니다: {str(e)}")

# Solapi API 서명 생성 함수
def generate_solapi_signature(api_key, api_secret, timestamp):
    try:
        message = f"{api_key}:{timestamp}"
        signature = hmac.new(
            api_secret.encode('utf-8'),
            message.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return signature
    except Exception as e:
        logger.error(f"Solapi 서명 생성 실패: {str(e)}")
        raise

# SMS 발송 함수
def send_sms(phone, name, question_type):
    if not SMS_ENABLED:
        logger.warning("SMS 기능이 비활성화되어 있습니다.")
        return False
        
    try:
        # 전화번호 형식 검증
        phone = phone.strip().replace("-", "")
        if not phone.startswith("0"):
            phone = "0" + phone
        if not phone.isdigit() or len(phone) < 10:
            raise ValueError(f"유효하지 않은 전화번호 형식: {phone}")
            
        timestamp = str(int(time.time() * 1000))
        signature = generate_solapi_signature(SOLAPI_API_KEY, SOLAPI_API_SECRET, timestamp)
        
        url = "https://api.solapi.com/messages/v4/send"
        headers = {
            "Authorization": f"HMAC-SHA256 apiKey={SOLAPI_API_KEY}, date={timestamp}, salt={timestamp}, signature={signature}",
            "Content-Type": "application/json"
        }
        
        message = f"[{name}님의 문의]\n문의 종류: {question_type}\n문의가 접수되었습니다. 빠른 시일 내에 답변 드리겠습니다."
        
        payload = {
            "message": {
                "to": phone,
                "from": SENDER_PHONE,
                "text": message
            }
        }
        
        logger.info(f"SMS 발송 시도: {phone}, 이름: {name}, 문의 종류: {question_type}")
        response = requests.post(url, headers=headers, json=payload)
        
        # 응답 상세 로깅
        logger.info(f"Solapi 응답 상태 코드: {response.status_code}")
        logger.info(f"Solapi 응답 내용: {response.text}")
        
        response.raise_for_status()
        
        logger.info(f"SMS 발송 성공: {phone}")
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"SMS 발송 실패 (네트워크 오류): {str(e)}, 전화번호: {phone}")
        return False
    except ValueError as e:
        logger.error(f"SMS 발송 실패 (유효성 검증 오류): {str(e)}")
        return False
    except Exception as e:
        logger.error(f"SMS 발송 실패 (기타 오류): {str(e)}, 전화번호: {phone}")
        return False

# Cloud Function의 진입점
# 이 함수는 Google Cloud Functions에서 자동으로 호출됩니다.
# 함수 이름은 Cloud Functions 배포 시 지정한 이름과 일치해야 합니다.
def sheet_webhook(request):
    """
    Google Cloud Function의 진입점입니다.
    
    Args:
        request: HTTP 요청 객체. Google Drive의 웹훅 알림을 포함합니다.
        
    Returns:
        tuple: (응답 딕셔너리, HTTP 상태 코드)
    """
    try:
        # 요청 로깅
        logger.info("웹훅 요청 수신")
        logger.info(f"요청 헤더: {dict(request.headers)}")
        logger.info(f"요청 메서드: {request.method}")
        logger.info(f"요청 URL: {request.url}")
        
        # 환경 변수 로깅 (민감 정보 제외)
        logger.info(f"SHEET_ID 설정됨: {bool(SHEET_ID)}")
        logger.info(f"FIRESTORE_DOC 설정됨: {bool(FIRESTORE_DOC)}")
        logger.info(f"SMS 기능 활성화: {SMS_ENABLED}")
        
        # 알림 유효성 검사
        if request.headers.get("X-Goog-Resource-State") != "update":
            logger.warning("유효하지 않은 리소스 상태")
            return ({"status": "ignored", "message": "Not an update"}, 200)

        # 서비스 계정 인증
        try:
            credentials = service_account.Credentials.from_service_account_info(
                sa_key_json,  # 미리 검증된 JSON 사용
                scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
            )
            service = build("sheets", "v4", credentials=credentials)
            logger.info("Google Sheets API 인증 성공")
        except Exception as e:
            logger.error(f"Google Sheets API 인증 실패: {str(e)}")
            raise

        # 시트 데이터 읽기
        try:
            result = service.spreadsheets().values().get(
                spreadsheetId=SHEET_ID,
                range="Form Responses!A1:Z"
            ).execute()
            rows = result.get("values", [])
            logger.info(f"시트 데이터 읽기 성공: {len(rows)}행")
        except Exception as e:
            logger.error(f"시트 데이터 읽기 실패: {str(e)}")
            raise

        # Firestore 클라이언트 초기화
        try:
            db = firestore.Client()
            logger.info("Firestore 클라이언트 초기화 성공")
        except Exception as e:
            logger.error(f"Firestore 클라이언트 초기화 실패: {str(e)}")
            raise

        # 이전 스냅샷 로드
        try:
            doc_ref = db.document(FIRESTORE_DOC)
            doc = doc_ref.get()
            previous_snapshot = doc.to_dict().get("snapshot", {}) if doc.exists else {}
            logger.info("이전 스냅샷 로드 성공")
        except Exception as e:
            logger.error(f"이전 스냅샷 로드 실패: {str(e)}")
            raise

        # 현재 스냅샷 생성 및 비교
        current_snapshot = {}
        new_or_changed_rows = []
        
        for row in rows[1:]:  # 헤더 제외
            if len(row) >= 10:  # 최소 10개 열 필요
                timestamp = row[0]
                current_snapshot[timestamp] = row
                
                if timestamp not in previous_snapshot or previous_snapshot[timestamp] != row:
                    new_or_changed_rows.append(row)
                    logger.info(f"새로운/변경된 행 발견: 타임스탬프 {timestamp}")

        # SMS 발송
        sms_results = []
        for row in new_or_changed_rows:
            try:
                name = row[7]
                phone = row[8]
                question_type = row[9]
                
                success = send_sms(phone, name, question_type)
                sms_results.append({
                    "timestamp": row[0],
                    "phone": phone,
                    "success": success
                })
            except Exception as e:
                logger.error(f"행 처리 중 오류 발생: {str(e)}, 행 데이터: {row}")
                sms_results.append({
                    "timestamp": row[0],
                    "error": str(e)
                })

        # 새 스냅샷 저장
        try:
            doc_ref.set({"snapshot": current_snapshot})
            logger.info("새 스냅샷 저장 성공")
        except Exception as e:
            logger.error(f"새 스냅샷 저장 실패: {str(e)}")
            raise

        # 결과 로깅
        logger.info(f"처리 완료: {len(new_or_changed_rows)}개 행 처리, SMS 결과: {sms_results}")
        
        return ({
            "status": "success",
            "processed_rows": len(new_or_changed_rows),
            "sms_results": sms_results
        }, 200)

    except Exception as e:
        logger.error(f"전체 처리 중 오류 발생: {str(e)}")
        return ({
            "status": "error",
            "message": str(e)
        }, 500)

# 로컬 테스트를 위한 코드
# Cloud Functions에서는 이 부분이 실행되지 않습니다.
if __name__ == "__main__":
    # 테스트용 요청 객체 생성
    class TestRequest:
        def __init__(self):
            self.headers = {"X-Goog-Resource-State": "update"}
    
    # 테스트 실행
    response, status_code = sheet_webhook(TestRequest())
    print(f"Status Code: {status_code}")
    print(f"Response: {response}") 