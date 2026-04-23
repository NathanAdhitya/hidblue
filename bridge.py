#!/usr/bin/python3

import argparse
import collections
import errno
import os
import random
import selectors
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

from evdev import InputDevice, ecodes, list_devices

import keymap
from server import BLEHIDServer


def ev_code(name):
    value = ecodes.ecodes.get(name)
    if not isinstance(value, int):
        raise RuntimeError(f"evdev constant {name} is unavailable")
    return value


EV_KEY = ev_code("EV_KEY")
EV_REL = ev_code("EV_REL")
EV_SYN = ev_code("EV_SYN")
SYN_REPORT = ev_code("SYN_REPORT")
REL_X = ev_code("REL_X")
REL_Y = ev_code("REL_Y")
REL_WHEEL = ev_code("REL_WHEEL")
KEY_A = ev_code("KEY_A")
KEY_ENTER = ev_code("KEY_ENTER")
KEY_SPACE = ev_code("KEY_SPACE")
KEY_F3 = ev_code("KEY_F3")
BTN_LEFT = ev_code("BTN_LEFT")
BTN_RIGHT = ev_code("BTN_RIGHT")
BTN_MIDDLE = ev_code("BTN_MIDDLE")
KEY_V = ev_code("KEY_V")

DEFAULT_MOUSE_SPEED = 1
DEFAULT_MOUSE_DEADZONE = 0
DEFAULT_MOUSE_FLUSH_HZ = 125.0
MOUSE_BUTTONS = {
    BTN_LEFT: 0,
    BTN_RIGHT: 1,
    BTN_MIDDLE: 2,
}

MOUSE_JIGGLE_EVERY_SECONDS = 45
mouse_jiggle_timeout = time.monotonic() + MOUSE_JIGGLE_EVERY_SECONDS
REMOTE_PASTE_HZ = 125.0
REMOTE_PASTE_INTERVAL = 1.0 / REMOTE_PASTE_HZ
HID_MOD_LEFT_SHIFT = 1 << 1

def report_to_hex(report):
    return " ".join(f"{byte:02x}" for byte in report)


def read_local_clipboard_string():
    clipboard_commands = [
        ["wl-paste"],
        ["xclip", "-selection", "clipboard", "-o"],
        ["xsel", "--clipboard", "--output"],
    ]

    errors = []
    for command in clipboard_commands:
        try:
            result = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            return result.stdout, None
        except FileNotFoundError:
            errors.append(f"{command[0]} is not installed")
        except subprocess.TimeoutExpired:
            errors.append(f"{command[0]} timed out")
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            if stderr:
                errors.append(f"{command[0]} failed: {stderr}")
            else:
                errors.append(f"{command[0]} failed with exit code {exc.returncode}")

    return None, "; ".join(errors)


def build_paste_char_map():
    keytable = keymap.keytable

    def hid(key_name):
        value = keytable.get(key_name)
        if value is None:
            raise RuntimeError(f"Missing HID mapping for {key_name}")
        return value

    char_map = {
        " ": (0, hid("KEY_SPACE")),
        "\n": (0, hid("KEY_ENTER")),
        "\t": (0, hid("KEY_TAB")),
        "-": (0, hid("KEY_MINUS")),
        "_": (HID_MOD_LEFT_SHIFT, hid("KEY_MINUS")),
        "=": (0, hid("KEY_EQUAL")),
        "+": (HID_MOD_LEFT_SHIFT, hid("KEY_EQUAL")),
        "[": (0, hid("KEY_LEFTBRACE")),
        "{": (HID_MOD_LEFT_SHIFT, hid("KEY_LEFTBRACE")),
        "]": (0, hid("KEY_RIGHTBRACE")),
        "}": (HID_MOD_LEFT_SHIFT, hid("KEY_RIGHTBRACE")),
        "\\": (0, hid("KEY_BACKSLASH")),
        "|": (HID_MOD_LEFT_SHIFT, hid("KEY_BACKSLASH")),
        ";": (0, hid("KEY_SEMICOLON")),
        ":": (HID_MOD_LEFT_SHIFT, hid("KEY_SEMICOLON")),
        "'": (0, hid("KEY_APOSTROPHE")),
        '"': (HID_MOD_LEFT_SHIFT, hid("KEY_APOSTROPHE")),
        "`": (0, hid("KEY_GRAVE")),
        "~": (HID_MOD_LEFT_SHIFT, hid("KEY_GRAVE")),
        ",": (0, hid("KEY_COMMA")),
        "<": (HID_MOD_LEFT_SHIFT, hid("KEY_COMMA")),
        ".": (0, hid("KEY_DOT")),
        ">": (HID_MOD_LEFT_SHIFT, hid("KEY_DOT")),
        "/": (0, hid("KEY_SLASH")),
        "?": (HID_MOD_LEFT_SHIFT, hid("KEY_SLASH")),
    }

    digits = "1234567890"
    shifted_digits = "!@#$%^&*()"
    for index, digit in enumerate(digits):
        key_name = f"KEY_{digit}"
        char_map[digit] = (0, hid(key_name))
        char_map[shifted_digits[index]] = (HID_MOD_LEFT_SHIFT, hid(key_name))

    for letter in "abcdefghijklmnopqrstuvwxyz":
        key_name = f"KEY_{letter.upper()}"
        char_map[letter] = (0, hid(key_name))
        char_map[letter.upper()] = (HID_MOD_LEFT_SHIFT, hid(key_name))

    return char_map


PASTE_CHAR_MAP = build_paste_char_map()


@dataclass
class InputSource:
    device: InputDevice
    keyboard: bool = False
    mouse: bool = False


def parse_args():
    parser = argparse.ArgumentParser(
        description="Forward evdev keyboard/mouse events through BLE HID (GATT) using BlueZ."
    )
    parser.add_argument(
        "--keyboard",
        action="append",
        default=[],
        metavar="/dev/input/eventX",
        help="Keyboard input device path. Can be repeated.",
    )
    parser.add_argument(
        "--mouse",
        action="append",
        default=[],
        metavar="/dev/input/eventX",
        help="Mouse input device path. Can be repeated.",
    )
    parser.add_argument(
        "--grab-on-start",
        action="store_true",
        help="Exclusively grab selected input devices at startup.",
    )
    parser.add_argument(
        "--list-input-devices",
        action="store_true",
        help="Print available evdev devices and detected roles, then exit.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose event/report debug logs.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not start BLE server; print translated HID reports instead.",
    )
    parser.add_argument(
        "--mouse-speed",
        type=float,
        default=DEFAULT_MOUSE_SPEED,
        help=(
            "Relative mouse speed multiplier for forwarded movement "
            f"(default: {DEFAULT_MOUSE_SPEED})."
        ),
    )
    parser.add_argument(
        "--mouse-deadzone",
        type=int,
        default=DEFAULT_MOUSE_DEADZONE,
        help=(
            "Ignore tiny relative deltas whose absolute value is <= this threshold "
            f"(default: {DEFAULT_MOUSE_DEADZONE})."
        ),
    )
    parser.add_argument(
        "--mouse-flush-hz",
        type=float,
        default=DEFAULT_MOUSE_FLUSH_HZ,
        help=(
            "Maximum BLE mouse report flush frequency in Hz "
            f"(default: {DEFAULT_MOUSE_FLUSH_HZ})."
        ),
    )
    return parser.parse_args()


def detect_roles(device):
    caps = device.capabilities()
    key_caps = set(caps.get(EV_KEY, []))
    rel_caps = set(caps.get(EV_REL, []))

    is_keyboard = KEY_A in key_caps and KEY_ENTER in key_caps and KEY_SPACE in key_caps
    is_mouse = REL_X in rel_caps and REL_Y in rel_caps and (
        BTN_LEFT in key_caps or BTN_RIGHT in key_caps or BTN_MIDDLE in key_caps
    )
    return is_keyboard, is_mouse


def list_input_device_roles():
    print("Available input devices:")
    for path in sorted(list_devices()):
        try:
            device = InputDevice(path)
        except OSError as exc:
            print(f"  {path}: <unavailable> ({exc})")
            continue
        try:
            is_keyboard, is_mouse = detect_roles(device)
            roles = []
            if is_keyboard:
                roles.append("keyboard")
            if is_mouse:
                roles.append("mouse")
            role_text = ", ".join(roles) if roles else "other"
            print(f"  {path}: {device.name} [{role_text}]")
        finally:
            device.close()


def add_source(sources, path, keyboard=False, mouse=False):
    source = sources.get(path)
    if source is not None:
        source.keyboard = source.keyboard or keyboard
        source.mouse = source.mouse or mouse
        return True

    try:
        device = InputDevice(path)
    except OSError as exc:
        print(f"Cannot open {path}: {exc}")
        return False

    sources[path] = InputSource(device=device, keyboard=keyboard, mouse=mouse)
    return True


def discover_sources(keyboard_paths, mouse_paths):
    sources = {}
    has_manual_keyboard = len(keyboard_paths) > 0
    has_manual_mouse = len(mouse_paths) > 0

    for path in keyboard_paths:
        add_source(sources, path, keyboard=True)

    for path in mouse_paths:
        add_source(sources, path, mouse=True)

    need_auto_keyboard = not has_manual_keyboard
    need_auto_mouse = not has_manual_mouse
    if need_auto_keyboard or need_auto_mouse:
        for path in sorted(list_devices()):
            try:
                probe = InputDevice(path)
            except OSError:
                continue

            try:
                is_keyboard, is_mouse = detect_roles(probe)
            finally:
                probe.close()

            want_keyboard = need_auto_keyboard and is_keyboard
            want_mouse = need_auto_mouse and is_mouse
            if want_keyboard or want_mouse:
                add_source(sources, path, keyboard=want_keyboard, mouse=want_mouse)

    return sources


def build_evdev_to_hid_map():
    evdev_to_hid = {}
    for key_name, hid_code in keymap.keytable.items():
        evdev_code = ecodes.ecodes.get(key_name)
        if isinstance(evdev_code, int):
            evdev_to_hid[evdev_code] = hid_code
    return evdev_to_hid


def hid_modifier_mask(hid_code):
    if 224 <= hid_code <= 231:
        return 1 << (hid_code - 224)
    return 0


def encode_signed_byte(value):
    return value if value >= 0 else 256 + value


def set_device_grab(sources, enable):
    action = "grab" if enable else "ungrab"
    failures = []
    for source in sources.values():
        try:
            if enable:
                source.device.grab()
            else:
                source.device.ungrab()
        except OSError as exc:
            failures.append((source.device.path, exc))

    if failures:
        for path, exc in failures:
            print(f"Failed to {action} {path}: {exc}")
    else:
        if enable:
            print("Input grab enabled")
        else:
            print("Input grab disabled")

    return not failures


def release_sources(sources):
    for source in sources.values():
        try:
            source.device.ungrab()
        except OSError:
            pass
        try:
            source.device.close()
        except OSError:
            pass


def run_event_loop(
    sources,
    send_keyboard_callback,
    send_mouse_callback,
    grab_on_start=False,
    debug=False,
    mouse_speed=DEFAULT_MOUSE_SPEED,
    mouse_deadzone=DEFAULT_MOUSE_DEADZONE,
    mouse_flush_hz=DEFAULT_MOUSE_FLUSH_HZ,
):
    global mouse_jiggle_timeout
    
    selector = selectors.DefaultSelector()
    evdev_to_hid = build_evdev_to_hid_map()

    keyboard_state = bytearray([
        0xA1,
        0x01,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
    ])

    grabbed = False

    mouse_buttons = 0
    pending_dx = 0
    pending_dy = 0
    pending_wheel = 0
    mouse_button_dirty = False
    mouse_residual_x = 0.0
    mouse_residual_y = 0.0
    last_mouse_flush = 0.0
    mouse_flush_interval = 1.0 / mouse_flush_hz
    remote_paste_queue = collections.deque()
    remote_paste_next_tick = 0.0
    f3_held = False
    f3_combo_used = False
    suppress_v_release = False

    for source in sources.values():
        selector.register(source.device, selectors.EVENT_READ, source)
        if debug:
            roles = []
            if source.keyboard:
                roles.append("keyboard")
            if source.mouse:
                roles.append("mouse")
            role_text = ",".join(roles)
            print(f"[debug] listening on {source.device.path} ({source.device.name}) roles={role_text}")

    def send_keyboard_report():
        report = bytes(keyboard_state)
        if debug:
            print(f"[kbd-report] {report_to_hex(report)}")
        modifiers = int(keyboard_state[2])
        keys = [int(key) for key in keyboard_state[4:10]]
        send_keyboard_callback(modifiers, keys)

    def send_mouse_report(delta_x, delta_y, wheel=0):
        report = bytearray([
            0xA1,
            0x02,
            mouse_buttons,
            encode_signed_byte(delta_x),
            encode_signed_byte(delta_y),
            encode_signed_byte(wheel),
        ])
        final_report = bytes(report)
        if debug:
            print(
                "[mouse-report] "
                f"buttons={mouse_buttons:03b} dx={delta_x} dy={delta_y} wheel={wheel} "
                f"raw={report_to_hex(final_report)}"
            )
        send_mouse_callback(mouse_buttons, delta_x, delta_y, wheel)

    def flush_mouse():
        nonlocal pending_dx, pending_dy, pending_wheel, mouse_button_dirty, mouse_residual_x, mouse_residual_y

        raw_dx = pending_dx
        raw_dy = pending_dy
        raw_wheel = pending_wheel
        pending_dx = 0
        pending_dy = 0
        pending_wheel = 0

        step_x = max(-127, min(127, raw_dx))
        step_y = max(-127, min(127, raw_dy))
        step_wheel = max(-127, min(127, raw_wheel))
        # if step_x >= 127 or step_x <= -127 or step_y >= 127 or step_y <= -127:
        # 	print(f"[mouse-step] clamping large step ({step_x},{step_y}) to fit signed byte")
        # print(f"[mouse-step] sending step ({step_x},{step_y}) from scaled ({scaled_dx},{scaled_dy})")
        send_mouse_report(step_x, step_y, step_wheel)
   
        if mouse_button_dirty:
            send_mouse_report(0, 0)

        mouse_button_dirty = False

    def clear_remote_paste_queue(reason):
        nonlocal remote_paste_queue
        if remote_paste_queue:
            remote_paste_queue.clear()
            print(f"[remote-paste] queue cleared: {reason}")

    def queue_remote_paste_text(text):
        nonlocal remote_paste_queue, remote_paste_next_tick

        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        remote_paste_queue.clear()

        unsupported_count = 0
        unsupported_preview = []
        for ch in normalized:
            stroke = PASTE_CHAR_MAP.get(ch)
            if stroke is None:
                unsupported_count += 1
                if len(unsupported_preview) < 8:
                    unsupported_preview.append(repr(ch))
                continue
            modifiers, key_code = stroke
            remote_paste_queue.append((modifiers, [key_code]))
            remote_paste_queue.append((0, []))

        if unsupported_count > 0:
            preview_text = ", ".join(unsupported_preview)
            print(
                "[remote-paste] skipped unsupported characters: "
                f"count={unsupported_count}, sample={preview_text}"
            )

        if remote_paste_queue:
            remote_paste_next_tick = time.monotonic()
            print(
                f"[remote-paste] queued {len(remote_paste_queue) // 2} chars "
                f"({len(remote_paste_queue)} keyframes)"
            )
        else:
            print("[remote-paste] nothing to paste (clipboard empty or unsupported chars only)")

    def start_remote_paste_from_clipboard():
        if not grabbed:
            print("[remote-paste] ignored: enable grab first (press F3)")
            return

        clipboard_text, clipboard_error = read_local_clipboard_string()
        if clipboard_error is not None:
            print(f"[remote-paste] clipboard read failed: {clipboard_error}")
            return

        if clipboard_text is None:
            print("[remote-paste] clipboard read failed: no clipboard provider output")
            return

        queue_remote_paste_text(clipboard_text)

    def send_remote_paste_frame(now):
        nonlocal remote_paste_next_tick
        if not grabbed or not remote_paste_queue:
            return
        if now < remote_paste_next_tick:
            return

        modifiers, keys = remote_paste_queue.popleft()
        keyboard_state[2] = modifiers & 0xFF
        for index in range(4, 10):
            keyboard_state[index] = 0
        for index, key_code in enumerate(keys[:6], start=4):
            keyboard_state[index] = key_code & 0xFF
        send_keyboard_report()
        remote_paste_next_tick = now + REMOTE_PASTE_INTERVAL

        if not remote_paste_queue:
            print("[remote-paste] completed")

    def clear_forwarded_input_state():
        nonlocal mouse_buttons, pending_dx, pending_dy, pending_wheel, mouse_button_dirty

        keyboard_dirty = any(keyboard_state[2:10])
        for index in range(2, 10):
            keyboard_state[index] = 0
        if keyboard_dirty:
            send_keyboard_report()

        clear_remote_paste_queue("grab released")

        if mouse_buttons != 0 or mouse_button_dirty:
            mouse_buttons = 0
            send_mouse_report(0, 0)
        pending_dx = 0
        pending_dy = 0
        pending_wheel = 0
        mouse_button_dirty = False

    def toggle_grab():
        nonlocal grabbed, last_mouse_flush
        requested = not grabbed
        changed = set_device_grab(sources, requested)
        if changed:
            grabbed = requested
            if grabbed:
                last_mouse_flush = time.monotonic()
            else:
                clear_forwarded_input_state()

    def handle_keyboard_event(source, event):
        nonlocal f3_held, f3_combo_used, suppress_v_release

        if event.code == KEY_F3:
            if event.value == 1:
                f3_held = True
                f3_combo_used = False
            elif event.value == 0:
                f3_held = False
                if f3_combo_used:
                    f3_combo_used = False
                    if debug:
                        print(f"[grab-toggle] combo used, skipping F3 toggle on {source.device.path}")
                else:
                    if debug:
                        print(f"[grab-toggle] dev={source.device.path} key=F3")
                    toggle_grab()
            return

        if event.code == KEY_V and f3_held:
            if event.value == 1:
                f3_combo_used = True
                suppress_v_release = True
                if debug:
                    print(f"[remote-paste] trigger F3+V from {source.device.path}")
                start_remote_paste_from_clipboard()
            elif event.value == 0 and suppress_v_release:
                suppress_v_release = False
            return

        if event.code == KEY_V and event.value == 0 and suppress_v_release:
            suppress_v_release = False
            return

        if remote_paste_queue:
            if debug and event.value in (0, 1):
                print(f"[remote-paste] dropping live key event during paste: code={event.code} value={event.value}")
            return

        if not grabbed:
            return

        hid_code = evdev_to_hid.get(event.code)
        if hid_code is None:
            if debug and event.value in (0, 1):
                print(f"[kbd-unmapped] dev={source.device.path} code={event.code} value={event.value}")
            return

        if event.value == 1:
            if debug:
                print(f"[kbd-down] dev={source.device.path} evdev={event.code} hid={hid_code}")
            modifier_mask = hid_modifier_mask(hid_code)
            if modifier_mask:
                keyboard_state[2] |= modifier_mask
            else:
                if hid_code not in keyboard_state[4:10]:
                    for index in range(4, 10):
                        if keyboard_state[index] == 0:
                            keyboard_state[index] = hid_code
                            break
            send_keyboard_report()

        elif event.value == 0:
            if debug:
                print(f"[kbd-up] dev={source.device.path} evdev={event.code} hid={hid_code}")
            modifier_mask = hid_modifier_mask(hid_code)
            if modifier_mask:
                keyboard_state[2] &= (~modifier_mask & 0xFF)
            else:
                for index in range(4, 10):
                    if keyboard_state[index] == hid_code:
                        keyboard_state[index] = 0
                        break
            send_keyboard_report()

        elif debug and event.value == 2:
            print(f"[kbd-repeat] dev={source.device.path} evdev={event.code} hid={hid_code}")

    def handle_mouse_event(source, event):
        nonlocal mouse_buttons, pending_dx, pending_dy, pending_wheel, mouse_button_dirty, last_mouse_flush
        global mouse_jiggle_timeout

        if not grabbed:
            return

        if event.type == EV_KEY and event.code in MOUSE_BUTTONS and event.value in (0, 1):
            bit = 1 << MOUSE_BUTTONS[event.code]
            if event.value == 1:
                mouse_buttons |= bit
            else:
                mouse_buttons &= (~bit & 0xFF)
            if debug:
                action = "down" if event.value == 1 else "up"
                print(
                    f"[mouse-button-{action}] dev={source.device.path} "
                    f"code={event.code} bits={mouse_buttons:03b}"
                )
            mouse_button_dirty = True

        elif event.type == EV_REL:
            if event.code == REL_X:
                pending_dx += event.value
                if debug and event.value != 0:
                    print(f"[mouse-rel] dev={source.device.path} axis=X value={event.value}")
            elif event.code == REL_Y:
                pending_dy += event.value
                if debug and event.value != 0:
                    print(f"[mouse-rel] dev={source.device.path} axis=Y value={event.value}")
            elif event.code == REL_WHEEL:
                pending_wheel += event.value
                if debug and event.value != 0:
                    print(f"[mouse-rel] dev={source.device.path} axis=WHEEL value={event.value}")

        elif event.type == EV_SYN and event.code == SYN_REPORT:
            mouse_jiggle_timeout = time.monotonic() + MOUSE_JIGGLE_EVERY_SECONDS
            now = time.monotonic()
            if now - last_mouse_flush >= mouse_flush_interval:
                flush_mouse()
                last_mouse_flush = now

    if grab_on_start:
        grabbed = set_device_grab(sources, True)

    while True:
        now = time.monotonic()
        time_until_paste = REMOTE_PASTE_INTERVAL
        if remote_paste_queue:
            time_until_paste = max(0.0, remote_paste_next_tick - now)
        timeout = min(0.02, time_until_paste)

        for key, _ in selector.select(timeout):
            source = key.data
            try:
                events = source.device.read()
            except BlockingIOError:
                continue
            except OSError as exc:
                if exc.errno in (errno.ENODEV, errno.EBADF):
                    print(f"Device disconnected: {source.device.path}")
                    selector.unregister(source.device)
                    source.device.close()
                    continue
                raise

            for event in events:
                if source.keyboard and event.type == EV_KEY:
                    handle_keyboard_event(source, event)
                if source.mouse and event.type in (EV_KEY, EV_REL, EV_SYN):
                    handle_mouse_event(source, event)

        send_remote_paste_frame(time.monotonic())
     
        # check for mouse jiggle timeout
        now = time.monotonic()
        if now >= mouse_jiggle_timeout:
            # if debug:
            print(f"[mouse-jiggle] sending periodic jiggle to prevent idle timeout")
                
            # random move down or up by 1-3 pixels
            dx = random.randint(-3, 3)
            dy = random.randint(-3, 3)
            dx = dx if dx != 0 else 1
            dy = dy if dy != 0 else 1
            
            pending_dx += dx
            pending_dy += dy
            
            flush_mouse()
            
            sleep_time = random.uniform(0.1, 0.3)
            time.sleep(sleep_time)
            
            pending_dx -= dx
            pending_dy -= dy
            
            flush_mouse()
            
            mouse_jiggle_timeout = now + MOUSE_JIGGLE_EVERY_SECONDS


def make_dry_run_senders(debug):
    def dry_run_send_keyboard(modifiers, keys):
        report = bytes([0xA1, 0x01, modifiers & 0xFF, 0x00] + [key & 0xFF for key in keys[:6]])
        print(f"[dry-run-kbd] {report_to_hex(report)}")

    def dry_run_send_mouse(buttons, dx, dy, wheel=0):
        report = bytes([
            0xA1,
            0x02,
            buttons & 0xFF,
            encode_signed_byte(dx),
            encode_signed_byte(dy),
            encode_signed_byte(wheel),
        ])
        print(f"[dry-run-mouse] {report_to_hex(report)}")

    if debug:
        print("Dry-run mode enabled: BLE server is skipped and reports are printed locally.")

    return dry_run_send_keyboard, dry_run_send_mouse


if __name__ == '__main__':
    args = parse_args()

    if args.list_input_devices:
        list_input_device_roles()
        sys.exit(0)

    if os.geteuid() != 0:
        print("Run this as root to access /dev/input/event* and BlueZ system D-Bus.")
        sys.exit(1)

    sources = discover_sources(args.keyboard, args.mouse)
    keyboard_count = sum(1 for source in sources.values() if source.keyboard)
    mouse_count = sum(1 for source in sources.values() if source.mouse)

    if keyboard_count == 0:
        print("No keyboard-like input device found. Use --keyboard /dev/input/eventX.")
        release_sources(sources)
        sys.exit(1)

    if mouse_count == 0:
        print("No mouse-like input device found. Use --mouse /dev/input/eventX.")
        release_sources(sources)
        sys.exit(1)

    if args.mouse_speed <= 0:
        print("--mouse-speed must be greater than 0.")
        release_sources(sources)
        sys.exit(1)

    if args.mouse_deadzone < 0:
        print("--mouse-deadzone must be >= 0.")
        release_sources(sources)
        sys.exit(1)

    if args.mouse_flush_hz <= 0:
        print("--mouse-flush-hz must be greater than 0.")
        release_sources(sources)
        sys.exit(1)

    print("Selected input devices:")
    for source in sorted(sources.values(), key=lambda src: src.device.path):
        roles = []
        if source.keyboard:
            roles.append("keyboard")
        if source.mouse:
            roles.append("mouse")
        role_text = ", ".join(roles)
        print(f"  {source.device.path}: {source.device.name} [{role_text}]")

    print("Press F3 to toggle exclusive input grab.")
    print(
        "Mouse tuning: "
        f"speed={args.mouse_speed}, deadzone={args.mouse_deadzone}, "
        f"flush_hz={args.mouse_flush_hz}"
    )

    if args.debug:
        print("Debug mode enabled: verbose input/report logs are active.")

    ble_server = None
    try:
        if args.dry_run:
            dry_kbd, dry_mouse = make_dry_run_senders(args.debug)
            run_event_loop(
                sources,
                dry_kbd,
                dry_mouse,
                grab_on_start=args.grab_on_start,
                debug=args.debug,
                mouse_speed=args.mouse_speed,
                mouse_deadzone=args.mouse_deadzone,
                mouse_flush_hz=args.mouse_flush_hz,
            )
        else:
            ble_server = BLEHIDServer()

            bridge_thread = threading.Thread(
                target=run_event_loop,
                kwargs={
                    "sources": sources,
                    "send_keyboard_callback": ble_server.send_keyboard_report,
                    "send_mouse_callback": ble_server.send_mouse_report,
                    "grab_on_start": args.grab_on_start,
                    "debug": args.debug,
                    "mouse_speed": args.mouse_speed,
                    "mouse_deadzone": args.mouse_deadzone,
                    "mouse_flush_hz": args.mouse_flush_hz,
                },
                daemon=True,
            )
            bridge_thread.start()

            print("Starting BLE HID Server. Press Ctrl+C to stop.")
            ble_server.start()
    finally:
        if ble_server is not None:
            ble_server.stop()
        release_sources(sources)
        print("Exit")
