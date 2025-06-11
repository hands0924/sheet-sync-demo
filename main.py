import os
import json
import logging
import hashlib
import hmac
import time
from google.cloud import firestore
from google.oauth2 import service_account
from googleapiclient.discovery import build
import requests
from dateutil import parser
import functions_framework
from flask import jsonify, Request

# 로깅 설정
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 환경 변수 로드
# Cloud Functions 환경에서는 load_dotenv가 필요 없으나, 로컬 테스트 시 .env를 사용할 수 있음
# from dotenv import load_dotenv
# load_dotenv()

# 필수 환경 변수
SHEET_ID = os.environ.get("SHEET_ID")
if not SHEET_ID:
    raise ValueError("SHEET_ID 환경 변수가 설정되지 않았습니다.")

FIRESTORE_DOC = os.environ.get("FIRESTORE_DOC", "sheet_snapshots/latest")
SOLAPI_API_KEY = os.environ.get("SOLAPI_API_KEY")
SOLAPI_API_SECRET = os.environ.get("SOLAPI_API_SECRET")
SENDER_PHONE = os.environ.get("SENDER_PHONE")

# SMS 기능 활성화 여부
if not all([SOLAPI_API_KEY, SOLAPI_API_SECRET, SENDER_PHONE]):
    logger.warning("SMS 발송에 필요한 환경 변수가 설정되지 않았습니다. SMS 기능이 비활성화됩니다.")
    SMS_ENABLED = False
else:
    SMS_ENABLED = True
    logger.info("SMS 발송 기능이 활성화되었습니다.")

# GCP 서비스 계정 키 (JSON 문자열 형태)
GCP_SA_KEY = os.environ.get("GCP_SA_KEY")
if not GCP_SA_KEY:
    raise ValueError("GCP_SA_KEY 환경 변수가 설정되지 않았습니다.")

# 서비스 계정 JSON 로드 및 검증
try:
    sa_key_json = json.loads(GCP_SA_KEY)
    logger.info("GCP_SA_KEY가 유효한 JSON 형식입니다.")
except json.JSONDecodeError as e:
    logger.error(f"GCP_SA_KEY JSON 파싱 실패: {e}")
    raise

# 프로젝트 ID 결정
# JSON 내부 project_id 우선, 없으면 환경변수 GOOGLE_CLOUD_PROJECT 사용
project_id = sa_key_json.get("project_id") or os.environ.get("GOOGLE_CLOUD_PROJECT")
if not project_id:
    raise ValueError("프로젝트 ID를 결정할 수 없습니다. 서비스 계정 JSON의 project_id 또는 GOOGLE_CLOUD_PROJECT 환경 변수를 설정하세요.")
logger.info(f"Firestore 및 API 호출에 사용할 프로젝트 ID: {project_id}")

# 서비스 계정 Credentials 객체 생성
try:
    # Firestore와 Sheets API에 필요한 스코프 설정
    scopes = [
        "https://www.googleapis.com/auth/datastore",
        "https://www.googleapis.com/auth/spreadsheets.readonly"
    ]
    credentials = service_account.Credentials.from_service_account_info(sa_key_json, scopes=scopes)
    logger.info("서비스 계정 Credentials 생성 성공")
except Exception as e:
    logger.error(f"서비스 계정 Credentials 생성 실패: {e}")
    raise

# Firestore 클라이언트 초기화
def get_firestore_client():
    try:
        client = firestore.Client(project=project_id, credentials=credentials)
        logger.info("Firestore 클라이언트 초기화 성공")
        return client
    except Exception as e:
        logger.error(f"Firestore 클라이언트 초기화 실패: {e}")
        raise

# Sheets API 클라이언트 초기화
def get_sheets_service():
    try:
        service = build("sheets", "v4", credentials=credentials)
        logger.info("Google Sheets API 클라이언트 초기화 성공")
        return service
    except Exception as e:
        logger.error(f"Google Sheets API 초기화 실패: {e}")
        raise

# Solapi 서명 생성
def generate_solapi_signature(api_key, api_secret, timestamp):
    message = f"{api_key}:{timestamp}"
    return hmac.new(api_secret.encode('utf-8'), message.encode('utf-8'), hashlib.sha256).hexdigest()

# SMS 발송 함수
def send_sms(phone, name, question_type):
    if not SMS_ENABLED:
        logger.warning("SMS 기능이 비활성화되어 있습니다.")
        return False
    try:
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
        payload = {"message": {"to": phone, "from": SENDER_PHONE, "text": message}}
        logger.info(f"SMS 발송 시도: {phone}, 이름: {name}, 문의 종류: {question_type}")
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        logger.info(f"Solapi 응답 상태 코드: {response.status_code}")
        logger.info(f"Solapi 응답 내용: {response.text}")
        response.raise_for_status()
        logger.info(f"SMS 발송 성공: {phone}")
        return True
    except Exception as e:
        logger.error(f"SMS 발송 실패: {e}, 전화번호: {phone}")
        return False

# Cloud Function HTTP 진입점
@functions_framework.http
def sheet_webhook(request: Request):
    try:
        logger.info("웹훅 요청 수신")
        # 헤더 검사
        resource_state = request.headers.get("X-Goog-Resource-State")
        logger.info(f"X-Goog-Resource-State: {resource_state}")
        if resource_state != "update":
            logger.warning("유효하지 않은 리소스 상태, 처리 생략")
            return jsonify({"status": "ignored", "message": "Not an update"}), 200

        # Sheets API 클라이언트
        try:
            sheets_service = get_sheets_service()
        except Exception as e:
            return jsonify({"status": "error", "message": "Sheets API 초기화 실패", "details": str(e)}), 500

        # 시트 읽기
        try:
            result = sheets_service.spreadsheets().values().get(spreadsheetId=SHEET_ID, range="Form Responses!A1:Z").execute()
            rows = result.get("values", [])
            if not rows:
                logger.warning("시트에 데이터가 없습니다.")
                return jsonify({"status": "warning", "message": "No data in sheet"}), 200
            logger.info(f"시트 데이터 읽기 성공: {len(rows)}행")
        except Exception as e:
            logger.error(f"시트 데이터 읽기 실패: {e}")
            return jsonify({"status": "error", "message": "시트 데이터 읽기 실패", "details": str(e)}), 500

        # Firestore 초기화
        try:
            db = get_firestore_client()
        except Exception as e:
            return jsonify({"status": "error", "message": "Firestore 초기화 실패", "details": str(e)}), 500

        # 이전 스냅샷 로드
        try:
            doc_ref = db.document(FIRESTORE_DOC)
            doc = doc_ref.get()
            previous_snapshot = doc.to_dict().get("snapshot", {}) if doc.exists else {}
            logger.info("이전 스냅샷 로드 성공")
        except Exception as e:
            logger.error(f"이전 스냅샷 로드 실패: {e}")
            return jsonify({"status": "error", "message": "이전 스냅샷 로드 실패", "details": str(e)}), 500

        # 스냅샷 비교
        current_snapshot = {}
        new_or_changed_rows = []
        for row in rows[1:]:
            if len(row) >= 10:
                ts = row[0]
                current_snapshot[ts] = row
                if ts not in previous_snapshot or previous_snapshot.get(ts) != row:
                    new_or_changed_rows.append(row)
                    logger.info(f"새로운/변경된 행: {ts}")
            else:
                logger.warning(f"유효하지 않은 행 형식: {row}")

        # SMS 발송
        sms_results = []
        for row in new_or_changed_rows:
            try:
                name = row[7]
                phone = row[8]
                question_type = row[9]
                success = send_sms(phone, name, question_type)
                sms_results.append({"timestamp": row[0], "phone": phone, "success": success})
            except Exception as e:
                logger.error(f"행 처리 중 오류: {e}, 데이터: {row}")
                sms_results.append({"timestamp": row[0], "error": str(e)})

        # 새 스냅샷 저장
        try:
            doc_ref.set({"snapshot": current_snapshot})
            logger.info("새 스냅샷 저장 성공")
        except Exception as e:
            logger.error(f"새 스냅샷 저장 실패: {e}")
            return jsonify({"status": "error", "message": "새 스냅샷 저장 실패", "details": str(e)}), 500

        logger.info(f"처리 완료: {len(new_or_changed_rows)}개 행, 결과: {sms_results}")
        return jsonify({"status": "success", "processed_rows": len(new_or_changed_rows), "sms_results": sms_results}), 200

    except Exception as e:
        logger.error(f"전체 처리 중 예외: {e}")
        return jsonify({"status": "error", "message": "처리 중 오류 발생", "details": str(e)}), 500
