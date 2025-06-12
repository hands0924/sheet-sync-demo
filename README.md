# Sheet-Sync-Demo

이 프로젝트는 Google Sheet(폼 응답)의 변경사항을 **1분 미만의 실시간으로 감지**하여 **Solapi API**를 통해 SMS를 발송하는 서버리스 시스템입니다. **Drive 푸시 알림**과 **Python Cloud Function**을 사용하며, **Firestore**에 마지막 스냅샷을 저장합니다.

## 목차
1. [사전 요구사항](#사전-요구사항)
2. [GCP 프로젝트 설정](#gcp-프로젝트-설정)
3. [GitHub 저장소 설정](#github-저장소-설정)
4. [로컬 개발 환경 설정](#로컬-개발-환경-설정)
5. [배포 및 테스트](#배포-및-테스트)
6. [문제 해결](#문제-해결)

## 사전 요구사항

1. **Google Cloud Platform 계정**
   - [Google Cloud Console](https://console.cloud.google.com) 계정
   - 결제 계정 설정 (신용카드 등록 필요)
   - Google Cloud SDK 설치

2. **GitHub 계정**
   - [GitHub](https://github.com) 계정
   - Git 설치

3. **Solapi 계정**
   - [Solapi](https://solapi.com) 계정 생성
   - API 키와 시크릿 발급
   - 발신자 전화번호 등록

## GCP 프로젝트 설정

1. **새 프로젝트 생성**
   ```bash
   # Google Cloud SDK 로그인
   gcloud auth login
   
   # 새 프로젝트 생성
   gcloud projects create [PROJECT_ID] --name="Sheet Sync Demo"
   
   # 프로젝트 설정
   gcloud config set project [PROJECT_ID]
   ```

2. **필요한 API 활성화**
   ```bash
   gcloud services enable \
     drive.googleapis.com \
     sheets.googleapis.com \
     firestore.googleapis.com \
     cloudfunctions.googleapis.com \
     cloudscheduler.googleapis.com \
     cloudbuild.googleapis.com
   ```

3. **Firestore 데이터베이스 생성**
   - Cloud Console → Firestore → 데이터베이스 만들기
   - "네이티브 모드" 선택
   - 리전: us-central1
   - "사용 설정" 클릭

4. **서비스 계정 생성**
   ```bash
   # 서비스 계정 생성
   gcloud iam service-accounts create sheet-sync-sa \
     --display-name="Sheet Sync Service Account"
   
   # 필요한 권한 부여
   gcloud projects add-iam-policy-binding [PROJECT_ID] \
     --member="serviceAccount:sheet-sync-sa@[PROJECT_ID].iam.gserviceaccount.com" \
     --role="roles/datastore.user"
   
   gcloud projects add-iam-policy-binding [PROJECT_ID] \
     --member="serviceAccount:sheet-sync-sa@[PROJECT_ID].iam.gserviceaccount.com" \
     --role="roles/cloudfunctions.invoker"
   
   # 서비스 계정 키 생성
   gcloud iam service-accounts keys create sheet-sync-sa-key.json \
     --iam-account=sheet-sync-sa@[PROJECT_ID].iam.gserviceaccount.com
   ```

## GitHub 저장소 설정

1. **저장소 생성**
   - GitHub에서 새 저장소 생성
   - 로컬에서 저장소 클론
   ```bash
   git clone https://github.com/[USERNAME]/sheet-sync-demo.git
   cd sheet-sync-demo
   ```

2. **GitHub Secrets 설정**
   - 저장소 → Settings → Secrets → Actions → New Repository Secret
   - 다음 시크릿 추가:
     ```
     GCP_PROJECT: [PROJECT_ID]
     GCP_SA_KEY: [sheet-sync-sa-key.json 내용]
     SHEET_ID: [Google Sheet ID]
     SOLAPI_API_KEY: [Solapi API Key]
     SOLAPI_API_SECRET: [Solapi API Secret]
     SENDER_PHONE: [발신자 전화번호]
     FIRESTORE_DOC: sheet_snapshots/latest
     ```

## 로컬 개발 환경 설정

1. **Python 가상환경 설정**
   ```bash
   python3 -m venv venv
   source venv/bin/activate  # Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. **Google Sheet 설정**
   - [Google Forms](https://forms.google.com)에서 새 폼 생성
   - 응답을 스프레드시트로 수집하도록 설정
   - 필요한 필드 추가:
     - 이름 (인덱스 7)
     - 전화번호 (인덱스 8)
     - 문의 종류 (인덱스 9)
   - 시트를 서비스 계정과 공유:
     - `sheet-sync-sa@[PROJECT_ID].iam.gserviceaccount.com`
     - 권한: "뷰어"

## 배포 및 테스트

1. **코드 푸시**
   ```bash
   git add .
   git commit -m "Initial commit"
   git push origin main
   ```

2. **Drive Watch 설정**
   ```bash
   # Cloud Function URL 가져오기
   FUNCTION_URL="https://us-central1-[PROJECT_ID].cloudfunctions.net/sheet_webhook"
   
   # Watch 설정
   curl -X POST \
     -H "Authorization: Bearer $(gcloud auth application-default print-access-token)" \
     -H "Content-Type: application/json" \
     -d '{ \
       "id": "sheet-watch-channel-123", \
       "type": "web_hook", \
       "address": "'"${FUNCTION_URL}"'" \
     }' \
     "https://www.googleapis.com/drive/v3/files/[SHEET_ID]/watch"
   ```

3. **테스트**
   - Google Form에 테스트 응답 제출
   - Cloud Functions 로그 확인
   - SMS 수신 확인

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