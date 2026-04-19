#!/usr/bin/env python3
"""Interactive configuration for bt-kvm."""
import json
import os

CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.json')

EDGES = {
    '1': ('right',  'Right (→)'),
    '2': ('left',   'Left  (←)'),
    '3': ('top',    'Top   (↑)'),
    '4': ('bottom', 'Bottom(↓)'),
}

DIAGRAMS = {
    'right': """\
  ┌──────────────────┐
  │                  │
  │    Linux PC      ├──→  Android
  │                  │
  └──────────────────┘""",
    'left': """\
  ┌──────────────────┐
  │                  │
  Android  ←──┤    Linux PC      │
  │                  │
  └──────────────────┘""",
    'top': """\
           Android
              ↑
  ┌──────────────────┐
  │    Linux PC      │
  └──────────────────┘""",
    'bottom': """\
  ┌──────────────────┐
  │    Linux PC      │
  └──────────────────┘
              ↓
           Android""",
}


def load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_config(cfg: dict):
    with open(CONFIG_PATH, 'w') as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write('\n')


def prompt(label: str, default: str) -> str:
    val = input(f"{label} [{default}]: ").strip()
    return val if val else default


def main():
    cfg = load_config()
    print("\n=== bt-kvm 설정 ===\n")

    # -- edge --
    current_edge = cfg.get('edge', 'right')
    print("어느 방향으로 마우스를 넘기면 Android로 전환할까요?\n")
    for key, (_, label) in EDGES.items():
        mark = " ◀" if EDGES[key][0] == current_edge else ""
        print(f"  {key}) {label}{mark}")
    print()

    choice = input(f"선택 (현재: {current_edge}, Enter=유지): ").strip()
    if choice in EDGES:
        cfg['edge'] = EDGES[choice][0]
    elif choice == '':
        pass
    else:
        print("잘못된 입력, 변경하지 않습니다.")

    edge = cfg.get('edge', 'right')
    print(f"\n{DIAGRAMS[edge]}\n")

    # -- device name --
    cfg['device_name'] = prompt(
        "Android BT에 표시될 기기 이름",
        cfg.get('device_name', 'Linux KVM')
    )

    # -- speed --
    speed_str = prompt(
        "마우스 속도 배율 (0.5 = 절반, 2.0 = 2배)",
        str(cfg.get('mouse_speed_multiplier', 1.0))
    )
    try:
        cfg['mouse_speed_multiplier'] = float(speed_str)
    except ValueError:
        print("숫자가 아니라 변경하지 않습니다.")

    # -- edge threshold --
    thr_str = prompt(
        "경계 감지 픽셀 거리 (진입 감도)",
        str(cfg.get('edge_threshold', 3))
    )
    try:
        cfg['edge_threshold'] = int(thr_str)
    except ValueError:
        print("숫자가 아니라 변경하지 않습니다.")

    # -- return threshold --
    ret_str = prompt(
        "Linux 복귀 감지 픽셀 (반대 방향으로 얼마나 밀어야 복귀하는지)",
        str(cfg.get('return_threshold', 80))
    )
    try:
        cfg['return_threshold'] = int(ret_str)
    except ValueError:
        print("숫자가 아니라 변경하지 않습니다.")

    # -- capture method --
    print("\n입력 캡처 방식:")
    print("  1) auto   - evdev 우선, 없으면 X11 grab (기본)")
    print("  2) evdev  - /dev/input 직접 (물리 KB/마우스 연결된 경우)")
    print("  3) x11    - X11 grab (Barrier 사용 중인 경우)")
    capture_map = {'1': 'auto', '2': 'evdev', '3': 'x11'}
    current_cap = cfg.get('capture_method', 'auto')
    cap_choice = input(f"\n선택 (현재: {current_cap}, Enter=유지): ").strip()
    if cap_choice in capture_map:
        cfg['capture_method'] = capture_map[cap_choice]

    save_config(cfg)
    print(f"\n저장 완료 → {CONFIG_PATH}")
    print("\n실행:")
    print("  sudo python3 server/main.py\n")


if __name__ == '__main__':
    main()
