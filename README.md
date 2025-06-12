# Sheet-Sync-Demo

이 프로젝트는 Google Sheet(폼 응답)의 변경사항을 **1분 미만의 실시간으로 감지**하여 **Solapi API**를 통해 SMS를 발송하는 서버리스 시스템입니다. **Cloud Run**과 **Firestore**를 사용하여 구축되었습니다.

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
     sheets.googleapis.com \
     firestore.googleapis.com \
     run.googleapis.com \
     cloudbuild.googleapis.com \
     containerregistry.googleapis.com
   ```

3. **Firestore 데이터베이스 생성**
   - Cloud Console → Firestore → 데이터베이스 만들기
   - "네이티브 모드" 선택
   - 리전: us-central1
   - 데이터베이스 ID: sheet-sync
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
     --role="roles/run.admin"
   
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
     GCP_PROJECT_ID: [PROJECT_ID]
     GCP_REGION: us-central1
     GCP_SA_KEY: [sheet-sync-sa-key.json 내용]
     SERVICE_NAME: sheet-sync
     SPREADSHEET_ID: [Google Sheet ID]
     SHEET_NAME: Sheet1
     SOLAPI_API_KEY: [Solapi API Key]
     SOLAPI_API_SECRET: [Solapi API Secret]
     SOLAPI_SENDER: [발신자 전화번호]
     POLLING_INTERVAL: 300
     ```

## 로컬 개발 환경 설정

1. **Python 가상환경 설정**
   ```bash
   python3 -m venv venv
   source venv/bin/activate  # Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. **환경 변수 설정**
   - `.env` 파일 생성 및 필요한 환경 변수 설정
   ```bash
   cp .env.example .env
   # .env 파일 편집하여 필요한 값 설정
   ```

3. **로컬 테스트**
   ```bash
   python main.py
   ```

## 배포 및 테스트

1. **GitHub Actions를 통한 배포**
   - main 브랜치에 푸시하면 자동으로 배포
   - GitHub Actions 탭에서 배포 진행 상황 확인

2. **수동 배포**
   ```bash
   # Docker 이미지 빌드
   docker build -t gcr.io/[PROJECT_ID]/sheet-sync .
   
   # 이미지 푸시
   docker push gcr.io/[PROJECT_ID]/sheet-sync
   
   # Cloud Run 배포
   gcloud run deploy sheet-sync \
     --image gcr.io/[PROJECT_ID]/sheet-sync \
     --platform managed \
     --region us-central1 \
     --allow-unauthenticated
   ```

## 문제 해결

1. **Firestore 오류**
   - Firestore 데이터베이스가 생성되어 있는지 확인
   - 서비스 계정에 올바른 권한이 부여되어 있는지 확인
   - Cloud Run 로그에서 Firestore 관련 오류 확인

2. **SMS 발송 오류**
   - Solapi API 키와 시크릿이 올바른지 확인
   - 발신자 전화번호가 등록되어 있는지 확인
   - Cloud Run 로그에서 SMS 발송 관련 오류 확인

3. **Google Sheets 오류**
   - 스프레드시트 ID가 올바른지 확인
   - 서비스 계정이 스프레드시트에 접근 권한이 있는지 확인
   - Cloud Run 로그에서 Sheets API 관련 오류 확인 