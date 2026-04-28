# BT KVM 프로젝트 디버깅 컨텍스트

## 프로젝트 개요

Linux PC를 Bluetooth HID 디바이스(키보드/마우스 콤보)로 위장시켜 스마트폰을 제어하는 KVM 프로그램. 이전에 Claude와 함께 구현했고 GitHub 저장소가 남아있음. APK 빌드까지는 완료된 상태가 아니라(이건 다른 Tesla Fleet API 프로젝트), 이쪽은 Linux 호스트 기반 BT HID 송신 프로그램.

**기본 데이터 흐름:**
```
[입력 소스] → /dev/input/eventX → user-space 프로그램
                                         ↓
                           HID Report Descriptor 변환
                                         ↓
                  BlueZ HID profile (Peripheral) → hci → [스마트폰]
```

## 발견된 문제 — 입력 소스별 동작 차이

| # | 입력 소스 | BT HID → 스마트폰 | 결과 |
|---|---|---|---|
| 1 | Linux 로컬 **유선** 마우스 | 통과 | ✅ 잘 됨 |
| 2 | Windows PC → **Barrier** → Linux PC | 통과 | ❌ 반응 매우 안 좋음 |
| 3 | Linux 로컬 **BT 키보드/마우스** (별도 동글) | 통과 | ❌ 전체적으로 느린 lag |

처음에는 Barrier가 원인이라고 생각해서 BT 동글을 추가하고 BT 키보드/마우스를 직접 연결했지만, 케이스 3에서도 lag 발생. 따라서 **Barrier 단독 문제가 아니라 BT 송신 파이프라인 자체에 문제**가 있을 가능성이 큼.

## 현재 하드웨어 셋업 (케이스 3 기준)

- **hci0** (노트북 내장 BT): BT 키보드/마우스 연결 (수신 / Central 역할)
- **hci1** (USB BT 동글): 스마트폰에 HID 송신 (Peripheral 역할)
- USB 동글은 USB 허브 경유, 노트북에서 약 1m 이격
- 스마트폰에는 다른 BT 기기 동시 연결 없음
- 증상: 끊김이나 점프가 아닌 **전체적으로 균일하게 느린 lag**

## 가설 분석

### 케이스 2 (Barrier) lag 원인 추정
- Barrier가 만드는 가상 입력 디바이스(uinput)는 합성된 이벤트라 timing jitter 큼
- Barrier는 절대좌표 기반 → BT HID 마우스 리포트(상대좌표)로 변환 시 delta 누적/큰 점프
- SYN_REPORT 타이밍이 실제 마우스와 달라 BT HID 송신 주기와 어긋남

### 케이스 3 (BT 입력 + BT 출력) lag 원인 추정
"전체적으로 균일하게 느린 lag"이 핵심 단서. 끊김/점프가 아닌 균일 지연 → 무선 구간 문제보다는 **소프트웨어 파이프라인의 처리 지연** 가능성이 높음.

#### 의심 1: BT 2단 누적 latency (구조적, 어쩔 수 없는 부분)
- 유선 마우스는 USB polling 1~8ms로 도착
- BT 마우스는 이미 7.5~30ms latency 붙은 상태로 evdev에 도착
- 이걸 다시 BT로 송신 → BT 2번 거치며 누적
- 단, 이것만으로는 30~50ms 수준이라 "lag" 체감까지는 아님

#### 의심 2: read() polling 방식 (가장 유력 — 코드 확인 필요)
프로그램이 evdev를 어떻게 읽는지가 매우 중요:
```c
// 나쁜 예: usleep polling
while (1) {
    read(fd, &ev, sizeof(ev));
    usleep(10000);  // 10ms 슬립 → 평균 5ms 추가 lag
}

// 좋은 예: blocking read 또는 epoll
while (1) {
    read(fd, &ev, sizeof(ev));  // 이벤트 올 때까지 대기
}
```
**usleep/sleep이 있으면 거의 100% 원인.**

#### 의심 3: BT connection interval (측정 필요)
BlueZ Peripheral 동작 시 폰과 협상되는 connection interval이 보통 30ms 이상으로 잡힘.
- 게이밍 HID는 7.5ms 가능
- 범용 HID로 등록 시 폰이 전력 절약 위해 30~50ms로 늘림
- 그 자체로 평균 25ms lag 깔림

확인:
```bash
sudo btmon &
# 폰 연결 시 LE Connection Update 패킷에서 Interval 값 확인
```

#### 의심 4: HID Descriptor / CoD 등록 정체성
- 안드로이드는 HID class(CoD)와 vendor/product ID로 마우스/키보드 구분
- 마우스로 인식 안 되면 폰이 입력 폴링 주기를 늘림
- 확인: `bluetoothctl` → `show <폰 MAC>` / 폰 BT 설정에서 "입력 장치" 분류 여부

#### 의심 5: USB 버스 공유
- 노트북 내장 BT(hci0)도 내부적으로 USB 버스에 붙어 있는 경우 많음 (Intel 칩셋)
- 동글과 같은 host controller면 USB 인터럽트 직렬화로 lag 가능
- 확인: `lsusb -t`로 트리 보고 두 BT 어댑터의 host controller 확인

## 다음 단계 우선순위 (Claude CLI에서 진행할 작업)

1. **GitHub 저장소 코드 분석**
   - 입력 read 루프 구조 확인 (usleep/sleep 존재 여부)
   - epoll/select 사용 여부
   - HID Report Descriptor 정의
   - BT 송신 부분 (BlueZ API 사용 패턴)

2. **`btmon`으로 connection interval 측정**
   - 폰 연결 시점 캡처
   - Min/Max Interval 값 확인

3. **`lsusb -t`로 USB 버스 분리 확인**
   - hci0와 hci1이 같은 xHCI/EHCI 밑에 있는지

4. **코드 수정 우선순위**
   - polling → epoll 전환
   - HID Descriptor 정체성 확인 (마우스/키보드 클래스로 정확히 등록되는지)
   - 필요시 connection interval 강제 협상 시도

## 참고 정보

- 사용자: Linux 커널/임베디드 드라이버 개발자 (virtio/NTB, Space4 automotive 플랫폼 경험)
- 따라서 evdev/uinput, BlueZ 내부 구조, USB 스택 등 저수준 디버깅 가능
- Git commit 메시지는 영문·한국어 병행, 간결한 형식 선호

## Claude CLI 진행 방법

작업 디렉토리에서:
```bash
claude
```
실행 후 첫 메시지로 이 문서를 읽도록 요청:
```
@bt-kvm-context.md 이 파일 읽고 현재 상황 파악해줘.
그 다음 GitHub 저장소 코드부터 분석 시작하자.
저장소 경로: <로컬 클론 경로 또는 GitHub URL>
```
