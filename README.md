# Sheet-Sync-Demo

이 프로젝트는 Google Sheet(폼 응답)의 변경사항을 **1분 미만의 실시간으로 감지**하여 **Solapi API**를 통해 SMS를 발송하는 서버리스 시스템입니다. **Drive 푸시 알림**과 **Python Cloud Function**을 사용하며, **Firestore**에 마지막 스냅샷을 저장합니다.

---

## 저장소 구조

sheet-sync-demo/
├── .github/
│ └── workflows/
│ └── deploy.yml # GitHub Actions CI/CD 파이프라인
├── main.py # Python Cloud Function 코드
├── requirements.txt # Python 의존성
└── README.md # 설정 및 사용 방법

---

## 작동 방식

1.  **Drive Watch**  
    - Google Sheet 파일에 "watch"를 등록합니다. 시트가 변경될 때마다(예: 새 폼 응답) Drive가 Cloud Function으로 HTTP POST(웹훅)를 보냅니다.

2.  **Cloud Function (`main.py`)**  
    - 알림 유효성을 검사합니다 (`X-Goog-Resource-State` 헤더 확인).  
    - 시트의 **모든 행**을 읽습니다 (`Form Responses!A1:Z`).  
    - Firestore에서 **이전 스냅샷**을 로드합니다 (`FIRESTORE_DOC`).  
    - 현재 행과 이전 행을 비교하여 새로 추가되거나 변경된 행만 식별합니다.  
    - Solapi API를 통해 새/변경된 행에 대해 SMS를 발송합니다.  
    - Firestore 스냅샷을 최신 전체 시트로 덮어씁니다.

3.  **Firestore**  
    - `FIRESTORE_DOC`(예: `sheet_snapshots/latest`)에 단일 문서를 보관하며, 각 행의 "고유 ID"(타임스탬프)를 키로 사용합니다.  
    - 각 함수 실행 시 이 문서를 읽고, 비교한 후 새로운 상태를 다시 씁니다.

4.  **CI/CD (GitHub Actions)**  
    - `main` 브랜치에 푸시할 때마다 `.github/workflows/deploy.yml`이 자동으로 Cloud Function을 재배포합니다.

---

## 사전 요구사항

1.  **GitHub 저장소** (이 파일들을 포크하거나 새로 생성)  
2.  **Google Cloud Project** (결제 활성화 필요)
    - [Google Cloud Console](https://console.cloud.google.com)에 접속
    - 새 프로젝트 생성 또는 기존 프로젝트 선택
    - 결제 계정 연결 (신용카드 등록 필요)
3.  **서비스 계정 (`sheet-sync-sa`)**
    - Cloud Console → IAM 및 관리자 → 서비스 계정으로 이동
    - "서비스 계정 만들기" 클릭
    - 이름을 "sheet-sync-sa"로 입력
    - 다음 역할 부여:
        - Firestore → Cloud Datastore 사용자
        - Sheets API → 뷰어
        - Cloud Functions → Cloud Functions 호출자
        - Cloud Scheduler → Cloud Scheduler 서비스 에이전트
    - JSON 키 생성 및 다운로드
4.  **Firestore** (네이티브 모드)
    - Cloud Console → Firestore로 이동
    - "데이터베이스 만들기" 클릭
    - "네이티브 모드" 선택
    - 리전 선택 (예: us-central1)
    - "사용 설정" 클릭
5.  **Google Sheet** (폼에 연결된)
    - [Google Forms](https://forms.google.com)에서 새 폼 생성
    - 응답을 스프레드시트로 수집하도록 설정
    - 필요한 필드 추가 (이름, 전화번호, 문의 종류 등)
6.  **Solapi 계정**
    - [Solapi](https://solapi.com)에서 계정 생성
    - API 키와 시크릿 발급
    - 발신자 전화번호 등록
7.  **GitHub Secrets**:
    - GitHub 저장소 → Settings → Secrets → Actions → New Repository Secret
    - 다음 시크릿 추가:
        | 이름                | 값                                                |
        |---------------------|--------------------------------------------------|
        | `GCP_PROJECT`       | GCP 프로젝트 ID (Cloud Console에서 확인)         |
        | `GCP_SA_KEY`        | sheet-sync-sa-key.json의 전체 내용 붙여넣기     |
        | `SHEET_ID`          | Google Sheet ID (URL에서 복사)                   |
        | `SOLAPI_API_KEY`    | Solapi API 키                                    |
        | `SOLAPI_API_SECRET` | Solapi API 시크릿                                |
        | `SENDER_PHONE`      | 등록된 발신자 전화번호                          |
        | `FIRESTORE_DOC`     | `sheet_snapshots/latest`                        |

---

## 단계별 설정 방법

### 1) 저장소 클론

```bash
git clone https://github.com/your-username/sheet-sync-demo.git
cd sheet-sync-demo
```

2) 필요한 GCP API 활성화
```bash
# Google Cloud SDK 설치 (아직 설치하지 않은 경우)
# https://cloud.google.com/sdk/docs/install

# 프로젝트 설정
gcloud config set project YOUR_PROJECT_ID

# 필요한 API 활성화
gcloud services enable \
  drive.googleapis.com \
  sheets.googleapis.com \
  firestore.googleapis.com \
  cloudfunctions.googleapis.com \
  cloudscheduler.googleapis.com \
  cloudbuild.googleapis.com
```

3) Firestore 데이터베이스 생성
- Cloud Console → Firestore → 데이터베이스 만들기
- "네이티브 모드" 선택
- 리전 선택 (예: us-central1)
- "사용 설정" 클릭

4) 서비스 계정 생성 및 키 발급
- Cloud Console → IAM 및 관리자 → 서비스 계정
- "서비스 계정 만들기" 클릭
- 이름: "sheet-sync-sa"
- 역할 부여:
    - Firestore → Cloud Datastore 사용자
    - Sheets API → 뷰어
    - Cloud Functions → Cloud Functions 호출자
    - Cloud Scheduler → Cloud Scheduler 서비스 에이전트
- "키 만들기" → JSON 선택 → 다운로드
- 다운로드된 파일을 `sheet-sync-sa-key.json`으로 저장

5) 시트를 서비스 계정과 공유
- Google Sheet 열기
- 우측 상단 "공유" 버튼 클릭
- `sheet-sync-sa@YOUR_PROJECT_ID.iam.gserviceaccount.com` 추가
- 권한: "뷰어" 선택
- "완료" 클릭

6) GitHub Secrets 추가
- GitHub 저장소 → Settings → Secrets → Actions
- "New Repository Secret" 클릭
- 위의 표에 있는 모든 시크릿 추가
- `GCP_SA_KEY`의 경우 `sheet-sync-sa-key.json` 파일의 전체 내용을 복사하여 붙여넣기

7) 커밋 및 main 브랜치에 푸시
```bash
git add .
git commit -m "Initial commit"
git push origin main
```
이렇게 하면 GitHub Actions가 트리거됩니다. Secrets가 올바르게 설정되었다면 "Deploy Cloud Function" 작업이 성공하고 `sheet_webhook` 함수가 배포됩니다.

8) Drive "Watch" 수동 설정
- Cloud Shell 또는 로컬 터미널에서:
```bash
# Google Cloud SDK 인증
gcloud auth application-default login

# Watch 설정
PROJECT_ID="YOUR_PROJECT_ID"
SHEET_ID="YOUR_SHEET_ID"
FUNCTION_URL="https://us-central1-${PROJECT_ID}.cloudfunctions.net/sheet_webhook"

curl -X POST \
  -H "Authorization: Bearer $(gcloud auth application-default print-access-token)" \
  -H "Content-Type: application/json" \
  -d '{ \
    "id": "sheet-watch-channel-123", \
    "type": "web_hook", \
    "address": "'"${FUNCTION_URL}"'" \
  }' \
  "https://www.googleapis.com/drive/v3/files/${SHEET_ID}/watch"
```

성공적인 호출 후, Drive는 다음과 같은 응답을 반환합니다:
```json
{
  "kind": "api#channel",
  "id": "sheet-watch-channel-123",
  "resourceId": "...",
  "resourceUri": "...",
  "token": "...",
  "expiration": "YYYY-MM-DDThh:mm:ss.000Z"
}
```

나중에 watch를 명시적으로 중지하려면 반환된 "resourceId"와 "expiration"을 보관하세요. watch는 약 7일 후에 만료되므로 주기적으로 갱신해야 합니다.

이제 시트가 변경될 때마다(폼 제출 또는 수동 편집) Drive가 즉시 함수의 URL로 HTTP POST를 보내고, 함수는 Solapi API를 통해 새로 추가되거나 변경된 행에 대해 SMS를 발송합니다.

## 폼 응답 형식

Google 폼 응답은 다음 형식(열 인덱스)이어야 합니다:

*   인덱스 0: 타임스탬프 (Google Forms가 자동으로 생성하는 고유 식별자로 사용)
*   인덱스 7: 이름
*   인덱스 8: 전화번호
*   인덱스 9: 문의 종류

함수는 자동으로 이러한 값들을 추출하여 제공된 전화번호로 SMS를 발송합니다.

## 타임스탬프 활용

*   각 폼 응답의 첫 번째 열(인덱스 0)은 Google Forms가 자동으로 생성하는 타임스탬프입니다.
*   이 타임스탬프는 각 응답을 고유하게 식별하는 데 사용됩니다.
*   Firestore에서 이 타임스탬프를 키로 사용하여 중복 SMS 발송을 방지합니다.
*   타임스탬프는 자동으로 생성되므로 사용자가 입력할 필요가 없습니다.

## 문제 해결

1. **Cloud Function 배포 실패**
   - GitHub Actions 로그 확인
   - 모든 Secrets가 올바르게 설정되었는지 확인
   - GCP 프로젝트의 결제가 활성화되어 있는지 확인

2. **SMS 발송 실패**
   - Cloud Functions 로그 확인
   - Solapi API 키와 시크릿이 올바른지 확인
   - 발신자 전화번호가 Solapi에 등록되어 있는지 확인

3. **시트 변경 감지 안됨**
   - Drive Watch가 만료되었는지 확인
   - Watch를 다시 설정
   - Cloud Functions 로그에서 웹훅 수신 여부 확인

4. **Firestore 오류**
   - Firestore 데이터베이스가 생성되어 있는지 확인
   - 서비스 계정에 올바른 권한이 부여되어 있는지 확인
   - Cloud Functions 로그에서 Firestore 관련 오류 확인 