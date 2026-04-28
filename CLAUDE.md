# bt-kvm 프로젝트 컨텍스트

## 프로젝트 개요
Linux(Ubuntu X11)의 키보드·마우스를 Bluetooth로 Android에 공유하는 KVM 도구.
PC에 직접 연결된 키보드·마우스만 다룸 (Barrier 등 가상 입력 미지원).

## 현재 구현 상태 (완성)
- [x] Linux → Android 키보드·마우스 공유 (BT HID, L2CAP PSM 17/19)
- [x] X11 화면 경계 감지로 자동 전환
- [x] 마우스 반대 방향 밀기 / Scroll Lock으로 Linux 복귀
- [x] 클립보드 양방향 동기화 (RFCOMM channel 4)
- [x] Android 앱 (Kotlin, `android/` 폴더)
- [x] 대화형 설정 도구 (`configure.py`)

## 사용자 환경
- **Ubuntu**: bt-kvm 서버 실행 환경 (X11)
- **Android**: 타겟 디바이스
- **개발 환경**: WSL (테스트는 회사 Ubuntu에서)

## 핵심 아키텍처

```
PC 직접 연결 키보드·마우스 (USB / BT)
            │  /dev/input/eventX (evdev grab)
            ▼
       Ubuntu X11
            │  BT HID (L2CAP 17/19)  ← 키보드·마우스
            │  BT RFCOMM (ch 4)      ← 클립보드
            ▼
         Android
```

## 파일 구조
```
server/main.py          — 진입점, sudo 필요
server/bt_hid.py        — BT HID 주변장치 (BlueZ L2CAP)
server/input_monitor.py — X11 경계 감지 + evdev 캡처
server/hid_reports.py   — HID 디스크립터 + evdev→HID 변환
server/clipboard_sync.py— RFCOMM 클립보드 서버 (xclip)
android/                — Kotlin Android 앱
configure.py            — 대화형 설정
setup.sh                — 시스템 초기 설정 (1회)
config.json             — 설정 파일
```

## 중요한 기술적 결정 사항
- **PC 직접 연결 입력만 지원**: evdev로 `/dev/input` 직접 캡처. XTEST로 주입된 합성 이벤트(Barrier 등)는 `/dev/input`에 안 나타나므로 미지원.
- **Android 앱 없이도 키보드·마우스 작동** (BT HID는 OS 레벨)
- **클립보드는 Android 앱 필요** (Android 10+ 보안 제약)
- **Linux 복귀**: 반대 방향 80px 밀기 OR Scroll Lock(설정 가능 토글 키)
- **HID 프로토콜**: 0xA1 prefix + Report ID (1=keyboard, 2=mouse)

## 미완성 / 향후 개선 가능한 것들
- Android 해상도 수동 입력으로 정확한 화면 끝 복귀 구현
- Wayland 지원 (현재 X11만)
- 다중 Android 디바이스 지원
- Android 앱 Accessibility Service로 백그라운드 클립보드 자동 전송

## GitHub
https://github.com/guom0625-dotcom/bt-kvm
