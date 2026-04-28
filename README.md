# bt-kvm

Linux의 키보드·마우스를 Bluetooth로 Android에 공유하는 KVM 도구입니다.  
마우스가 화면 경계를 넘으면 자동으로 Android를 제어하고, 클립보드도 양방향 동기화됩니다.

---

## 동작 방식

```
PC에 직접 연결된 키보드·마우스 (USB / BT)
        │  /dev/input (evdev grab)
        ▼
   Linux Ubuntu (X11)
        │  Bluetooth HID  ← 키보드·마우스
        │  Bluetooth RFCOMM ← 클립보드
        ▼
     Android
```

- Linux가 **Bluetooth HID 주변 장치**로 등록되어 Android가 일반 BT 키보드·마우스로 인식
- 마우스가 설정한 화면 경계에 닿으면 → Android 제어로 전환
- 반대 방향으로 마우스를 밀거나 **Scroll Lock** 키 → Linux로 복귀
- 클립보드는 RFCOMM 채널로 **자동 양방향 동기화**

---

## 요구사항

### Linux (서버)

| 항목 | 내용 |
|------|------|
| OS | Ubuntu 20.04+ (X11) |
| Python | 3.10+ |
| Bluetooth | BlueZ 5.x |
| 패키지 | `bluez`, `xclip`, `python3-dbus` |
| Python 라이브러리 | `evdev`, `python-xlib` |

> **Wayland는 지원하지 않습니다.** X11 세션으로 로그인하세요.

### Android (클라이언트)

| 항목 | 내용 |
|------|------|
| Android | 8.0 (API 26) 이상 |
| 권한 | Bluetooth, 알림 |
| 별도 루팅 | 불필요 |

---

## 설치

### 1. 저장소 클론

```bash
git clone https://github.com/guom0625-dotcom/bt-kvm
cd bt-kvm
```

### 2. 시스템 설정 (1회)

```bash
sudo bash setup.sh
```

내부적으로 수행하는 작업:
- `bluez`, `xclip`, `python3-dbus`, `evdev`, `python-xlib` 설치
- BlueZ CompatibilityMode 활성화 (SDP 등록을 위해 필요)
- `bluetoothd --compat --noplugin=pnat` 옵션 적용
- RFCOMM 채널 4번 SDP 등록 (클립보드 용)

### 3. 설정

```bash
python3 configure.py
```

```
어느 방향으로 마우스를 넘기면 Android로 전환할까요?
  1) Right (→)  ◀ 현재
  2) Left  (←)
  3) Top   (↑)
  4) Bottom(↓)

  ┌──────────────────┐
  │                  │
  │    Linux PC      ├──→  Android
  │                  │
  └──────────────────┘
```

---

## Android 앱 빌드

### Android Studio 사용

1. Android Studio에서 `android/` 폴더를 프로젝트로 열기
2. **Build → Build APK**
3. `android/app/build/outputs/apk/debug/app-debug.apk` 를 Android로 복사 후 설치

### 커맨드라인 빌드

```bash
cd android
./gradlew assembleDebug
# APK 위치: app/build/outputs/apk/debug/app-debug.apk
```

> Android Studio가 없는 경우 [Android Command Line Tools](https://developer.android.com/studio#command-line-tools-only) 설치 필요

---

## 사용법

### 1. Linux 서버 실행

```bash
sudo python3 server/main.py
```

```
10:00:00 [INFO] Configuring Bluetooth adapter...
10:00:01 [INFO] Clipboard sync ready (RFCOMM channel 4)
10:00:01 [INFO] Pair 'Linux KVM' from Android BT settings, then connect.
```

### 2. Android에서 페어링

1. Android **설정 → 블루투스 → 새 기기 검색**
2. **"Linux KVM"** 선택 후 페어링
3. bt-kvm 앱 실행 → 기기 선택 → **연결**

### 3. 마우스 공유

| 동작 | 결과 |
|------|------|
| 마우스를 오른쪽 끝으로 이동 | Android 제어로 전환 |
| 마우스를 왼쪽으로 80px 밀기 | Linux로 복귀 |
| **Scroll Lock** 키 | 즉시 Linux로 복귀 |

### 4. 클립보드 공유

| 방향 | 동작 |
|------|------|
| Linux → Android | Linux에서 복사하면 자동으로 Android 클립보드에 동기화 |
| Android → Linux (포그라운드) | Android에서 복사하면 자동 동기화 |
| Android → Linux (백그라운드) | 알림의 **[클립보드 전송]** 버튼 탭 |

---

## 설정 옵션 (`config.json`)

```json
{
  "device_name":            "Linux KVM",
  "edge":                   "right",
  "edge_threshold":         3,
  "return_threshold":       80,
  "mouse_speed_multiplier": 1.0,
  "clipboard_sync":         true,
  "toggle_key":             "KEY_PAUSE",
  "mouse_return":           false
}
```

| 키 | 기본값 | 설명 |
|----|--------|------|
| `device_name` | `"Linux KVM"` | Android BT 목록에 표시될 이름 |
| `edge` | `"right"` | 전환 경계 방향 (`right` / `left` / `top` / `bottom`) |
| `edge_threshold` | `3` | 경계 감지 픽셀 거리 (작을수록 민감) |
| `return_threshold` | `80` | Linux 복귀를 위해 반대 방향으로 밀어야 하는 픽셀 |
| `mouse_speed_multiplier` | `1.0` | Android에서의 마우스 속도 배율 |
| `clipboard_sync` | `true` | 클립보드 동기화 활성화 여부 |
| `toggle_key` | `"KEY_PAUSE"` | Linux 복귀 토글 키 (evdev 키 이름) |
| `mouse_return` | `true` | 반대 방향 밀기로 복귀 활성화 여부 |

### CLI 인자 (일회성 override)

```bash
sudo python3 server/main.py --edge left --speed 1.5
```

---

## 트러블슈팅

### Android에서 기기가 보이지 않을 때

```bash
sudo hciconfig hci0 piscan    # discoverable + connectable 확인
sudo hciconfig hci0 class 0x002540
```

### sdptool 오류

```bash
sudo systemctl restart bluetooth
sudo sdptool add --channel=4 SP
```

### 권한 오류 (evdev)

```bash
sudo python3 server/main.py   # 반드시 root로 실행
```

### X11 연결 실패

```bash
echo $DISPLAY   # :0 이 출력되어야 함
xhost +local:   # 필요 시 로컬 접근 허용
```

### Android 앱 클립보드 자동 전송이 안 될 때 (Android 10+)

Android 10 이상에서는 백그라운드 앱이 클립보드를 읽을 수 없습니다.  
알림의 **[클립보드 전송]** 버튼을 탭하거나, 앱을 포그라운드로 가져온 후 복사하세요.

---

## 프로젝트 구조

```
bt-kvm/
├── config.json              # 설정 파일
├── configure.py             # 대화형 설정 도구
├── setup.sh                 # 시스템 초기 설정 (1회)
├── requirements.txt
├── server/
│   ├── main.py              # 진입점
│   ├── bt_hid.py            # Bluetooth HID 주변 장치 (L2CAP)
│   ├── input_monitor.py     # X11 경계 감지 + evdev 캡처
│   ├── hid_reports.py       # HID 리포트 디스크립터 및 변환
│   └── clipboard_sync.py    # 클립보드 RFCOMM 서버
└── android/
    └── app/src/main/
        ├── AndroidManifest.xml
        └── java/com/btkvm/
            ├── MainActivity.kt      # UI
            └── ClipboardService.kt  # Foreground Service (BT + 클립보드)
```

---

## 라이선스

MIT
