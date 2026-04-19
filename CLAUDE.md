# bt-kvm 프로젝트 컨텍스트

## 프로젝트 개요
Linux(Ubuntu X11)의 키보드·마우스를 Bluetooth로 Android에 공유하는 KVM 도구.
Barrier와 유사하지만 네트워크 대신 Bluetooth HID를 사용.

## 현재 구현 상태 (완성)
- [x] Linux → Android 키보드·마우스 공유 (BT HID, L2CAP PSM 17/19)
- [x] X11 화면 경계 감지로 자동 전환
- [x] 마우스 반대 방향 밀기 / Scroll Lock으로 Linux 복귀
- [x] 클립보드 양방향 동기화 (RFCOMM channel 4)
- [x] Android 앱 (Kotlin, `android/` 폴더)
- [x] Barrier 연동 지원 (capture_method: x11)
- [x] 대화형 설정 도구 (`configure.py`)

## 사용자 환경
- **물리 HW**: Windows PC (키보드·마우스)
- **Barrier**: Windows PC → Ubuntu PC (X11)
- **Ubuntu**: bt-kvm 서버 실행 환경
- **Android**: 타겟 디바이스
- **개발 환경**: WSL (테스트는 회사 Ubuntu에서)

## 핵심 아키텍처

```
Windows (물리 HW) → Barrier → Ubuntu X11
                                  │
                    XGrabKeyboard/Pointer (capture_method: x11)
                                  │
                         BT HID (L2CAP 17/19)  ← 키보드·마우스
                         BT RFCOMM (ch 4)      ← 클립보드
                                  │
                               Android
```

## 파일 구조
```
server/main.py          — 진입점, sudo 필요
server/bt_hid.py        — BT HID 주변장치 (BlueZ L2CAP)
server/input_monitor.py — X11 경계 감지 + evdev/x11 캡처
server/x11_grab.py      — XGrab 캡처 백엔드 (Barrier용)
server/hid_reports.py   — HID 디스크립터 + evdev→HID 변환
server/clipboard_sync.py— RFCOMM 클립보드 서버 (xclip)
android/                — Kotlin Android 앱
configure.py            — 대화형 설정
setup.sh                — 시스템 초기 설정 (1회)
config.json             — 설정 파일
```

## 중요한 기술적 결정 사항
- **Barrier 환경에서는 capture_method를 x11로 설정해야 함**
  - Barrier는 XTEST로 X11에 이벤트 주입 → /dev/input에 안 나타남
  - x11 모드: XGrabKeyboard + XGrabPointer로 X11 레벨 캡처
- **Android 앱 없이도 키보드·마우스 작동** (BT HID는 OS 레벨)
- **클립보드는 Android 앱 필요** (Android 10+ 보안 제약)
- **Linux 복귀**: 반대 방향 80px 밀기 OR Scroll Lock
- **HID 프로토콜**: 0xA1 prefix + Report ID (1=keyboard, 2=mouse)

## 미완성 / 향후 개선 가능한 것들
- Android 해상도 수동 입력으로 Barrier처럼 정확한 화면 끝 복귀 구현
- Wayland 지원 (현재 X11만)
- 다중 Android 디바이스 지원
- Android 앱 Accessibility Service로 백그라운드 클립보드 자동 전송

## GitHub
https://github.com/guom0625-dotcom/bt-kvm
