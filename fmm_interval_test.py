"""
Find My Mouse - double Ctrl tests (PowerToys issue #19806)

Runs 4 tests (+1 bonus) automatically and saves everything to a results file
next to this script: fmm_results_<date-time>.txt

  The limit comes from the Windows double-click time (DCT), so gaps adapt to it:
  Test A  two Left Ctrl taps, gap = DCT/2               -> spotlight expected
  Test B  two Left Ctrl taps, gap = max(1000, 2*DCT)    -> NO spotlight expected
  Test C  lone Ctrl tap, then Ctrl+C DCT/2 later        -> accidental flash (the bug)
  Test E  Ctrl+C, then Ctrl+C again DCT/2 later         -> NO spotlight expected
          (no lone Ctrl tap; checks the "Ctrl+C / Ctrl+V" case from #51113)
  Test D  (bonus) gap sweep 60..800 ms + around DCT     -> shows the exact time limit

  Run only some tests:  python fmm_interval_test.py --only E

Before running
  - PowerToys running, Find My Mouse ON,
    Activation method = "Press Left Control twice".
  - If PowerToys runs as administrator, run this terminal as administrator too.

Run
  python fmm_interval_test.py
  The script opens Notepad itself. During the countdown take your hands off
  the mouse and keyboard and wait until it says DONE (~1.5 minutes).
  Moving the mouse resets Find My Mouse's double-press detection.

Keys pressed: only Left Ctrl, Shift, and C (inside Notepad). Mouse is never moved.
"""

import argparse
import ctypes
import json
import os
import platform
import subprocess
import sys
import time
from ctypes import wintypes
from datetime import datetime

if sys.platform != "win32":
    sys.exit("This script only works on Windows.")

import winreg  # noqa: E402  (Windows only)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
version = ctypes.WinDLL("version")
winmm = ctypes.WinDLL("winmm")

ULONG_PTR = ctypes.c_size_t
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002

VK_CONTROL, SC_LCONTROL = 0x11, 0x1D  # Left Ctrl (no extended flag = left)
VK_SHIFT, SC_LSHIFT = 0x10, 0x2A
VK_C, SC_C = 0x43, 0x2E

ACTIVATION_NAMES = {0: "Press Left Control twice", 1: "Press Right Control twice",
                    2: "Shake mouse", 3: "Custom shortcut"}


# ---------------------------------------------------------------- Win32 setup
class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


class VS_FIXEDFILEINFO(ctypes.Structure):
    _fields_ = [(n, wintypes.DWORD) for n in (
        "dwSignature", "dwStrucVersion", "dwFileVersionMS", "dwFileVersionLS",
        "dwProductVersionMS", "dwProductVersionLS", "dwFileFlagsMask", "dwFileFlags",
        "dwFileOS", "dwFileType", "dwFileSubtype", "dwFileDateMS", "dwFileDateLS")]


user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = (WNDENUMPROC, wintypes.LPARAM)
user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
user32.IsWindowVisible.argtypes = (wintypes.HWND,)
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
user32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD))
version.GetFileVersionInfoSizeW.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD))
version.GetFileVersionInfoW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p)
version.VerQueryValueW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
                                   ctypes.POINTER(wintypes.UINT))


# ---------------------------------------------------------------- output log
class Log:
    def __init__(self):
        self.lines = []

    def __call__(self, text=""):
        print(text)
        self.lines.append(text)

    def save(self, path):
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(self.lines) + "\n")


log = Log()


# ---------------------------------------------------------------- helpers
def key(vk, scan, up=False):
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.ki = KEYBDINPUT(vk, scan, KEYEVENTF_KEYUP if up else 0, 0, 0)
    if user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)) != 1:
        raise OSError(f"SendInput failed, error {ctypes.get_last_error()}")


def tap(vk, scan, hold=0.03):
    key(vk, scan)
    time.sleep(hold)
    key(vk, scan, up=True)


def wait_until(t):
    while True:
        rem = t - time.perf_counter()
        if rem <= 0:
            return
        if rem > 0.004:
            time.sleep(rem - 0.003)


def spotlight_windows():
    found = []

    def cb(hwnd, _):
        buf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, buf, 64)
        if buf.value == "FindMyMouse":
            found.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return found


def spotlight_visible(hwnds):
    return any(user32.IsWindowVisible(h) for h in hwnds)


def wait_for_visible(hwnds, seconds):
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        if spotlight_visible(hwnds):
            return True
        time.sleep(0.003)
    return False


def wait_hidden(hwnds, seconds=3.0):
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        if not spotlight_visible(hwnds):
            return True
        time.sleep(0.02)
    return False


def process_path(hwnd):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    buf = ctypes.create_unicode_buffer(1024)
    size = wintypes.DWORD(1024)
    ok = kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
    kernel32.CloseHandle(h)
    return buf.value if ok else ""


def foreground_exe():
    return process_path(user32.GetForegroundWindow()).replace("/", "\\").split("\\")[-1].lower()


def file_version(path):
    try:
        size = version.GetFileVersionInfoSizeW(path, None)
        if not size:
            return "unknown"
        data = ctypes.create_string_buffer(size)
        version.GetFileVersionInfoW(path, 0, size, data)
        ptr, length = ctypes.c_void_p(), wintypes.UINT()
        if not version.VerQueryValueW(data, "\\", ctypes.byref(ptr), ctypes.byref(length)):
            return "unknown"
        fi = ctypes.cast(ptr, ctypes.POINTER(VS_FIXEDFILEINFO)).contents
        return (f"{fi.dwProductVersionMS >> 16}.{fi.dwProductVersionMS & 0xFFFF}."
                f"{fi.dwProductVersionLS >> 16}.{fi.dwProductVersionLS & 0xFFFF}")
    except OSError:
        return "unknown"


def windows_version():
    try:
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion")
        get = lambda n: winreg.QueryValueEx(k, n)[0]  # noqa: E731
        return f"{get('DisplayVersion')} build {get('CurrentBuild')}.{get('UBR')}"
    except OSError:
        return platform.version()


def find_my_mouse_settings():
    path = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "PowerToys",
                        "FindMyMouse", "settings.json")
    try:
        with open(path, encoding="utf-8") as f:
            props = json.load(f).get("properties", {})
        val = lambda n: props.get(n, {}).get("value")  # noqa: E731
        return {"activation_method": val("activation_method"),
                "include_win_key": val("include_win_key"),
                "do_not_activate_on_game_mode": val("do_not_activate_on_game_mode"),
                "excluded_apps": val("excluded_apps")}
    except (OSError, ValueError):
        return None


def cursor():
    p = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return (p.x, p.y)


def reset_state(hwnds):
    # Any non-Ctrl key puts the double-press detector back to Idle and hides the spotlight.
    tap(VK_SHIFT, SC_LSHIFT)
    wait_hidden(hwnds)
    time.sleep(0.6)


# ---------------------------------------------------------------- trials
def double_ctrl(hwnds, gap_ms):
    """Ctrl down/up, then a second Ctrl down gap_ms after the first down."""
    reset_state(hwnds)
    hold = min(0.04, gap_ms / 2000)
    t1 = time.perf_counter()
    key(VK_CONTROL, SC_LCONTROL)
    wait_until(t1 + hold)
    key(VK_CONTROL, SC_LCONTROL, up=True)
    wait_until(t1 + gap_ms / 1000)
    t2 = time.perf_counter()
    key(VK_CONTROL, SC_LCONTROL)
    shown = wait_for_visible(hwnds, 0.25)
    key(VK_CONTROL, SC_LCONTROL, up=True)
    return shown, (t2 - t1) * 1000


def ctrl_then_ctrl_c(hwnds, gap_ms):
    """Lone Ctrl tap, then a real Ctrl+C whose Ctrl goes down gap_ms later."""
    reset_state(hwnds)
    t1 = time.perf_counter()
    key(VK_CONTROL, SC_LCONTROL)
    wait_until(t1 + 0.04)
    key(VK_CONTROL, SC_LCONTROL, up=True)
    wait_until(t1 + gap_ms / 1000)
    t2 = time.perf_counter()
    key(VK_CONTROL, SC_LCONTROL)
    shown = wait_for_visible(hwnds, 0.08)  # typical delay before pressing C
    tap(VK_C, SC_C)                        # C stops the spotlight -> a short "flash"
    key(VK_CONTROL, SC_LCONTROL, up=True)
    return shown, (t2 - t1) * 1000


def ctrl_c_twice(hwnds, gap_ms):
    """Ctrl+C, then Ctrl+C again (2nd Ctrl down gap_ms after the 1st). No lone Ctrl tap."""
    reset_state(hwnds)
    t1 = time.perf_counter()
    key(VK_CONTROL, SC_LCONTROL)
    time.sleep(0.04)
    tap(VK_C, SC_C)
    key(VK_CONTROL, SC_LCONTROL, up=True)
    wait_until(t1 + gap_ms / 1000)
    t2 = time.perf_counter()
    key(VK_CONTROL, SC_LCONTROL)
    shown = wait_for_visible(hwnds, 0.08)
    tap(VK_C, SC_C)
    key(VK_CONTROL, SC_LCONTROL, up=True)
    return shown, (t2 - t1) * 1000


def run_simple_test(name, desc, trial, hwnds, gap, tries, expect):
    log(f"\n{name}: {desc}")
    hits = 0
    for i in range(1, tries + 1):
        shown, actual = trial(hwnds, gap)
        hits += shown
        log(f"  try {i}: gap {actual:4.0f} ms -> spotlight {'YES' if shown else 'no'}")
    exp = tries if expect else 0
    verdict = "PASS (as expected)" if hits == exp else "DIFFERENT from expected"
    log(f"  RESULT: spotlight in {hits}/{tries} tries, expected {exp}/{tries} -> {verdict}")
    return f"{name}: {hits}/{tries} (expected {exp}/{tries}) {verdict}"


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tries", type=int, default=5, help="tries for tests A, B, C, E (default 5)")
    ap.add_argument("--no-sweep", action="store_true", help="skip bonus Test D")
    ap.add_argument("--only", default="ABCED",
                    help="which tests to run, e.g. --only E or --only ACE (default: all)")
    args = ap.parse_args()
    only = args.only.upper()

    here = os.path.dirname(os.path.abspath(__file__))
    out_path = os.path.join(here, f"fmm_results_{datetime.now():%Y%m%d-%H%M%S}.txt")
    summary = []

    try:
        log("=== Find My Mouse double-Ctrl test (PowerToys #19806) ===")
        log(f"Date:              {datetime.now():%Y-%m-%d %H:%M:%S}")
        log(f"Windows:           {windows_version()}")
        log(f"Python:            {platform.python_version()}")

        hwnds = spotlight_windows()
        if not hwnds:
            log("ERROR: Find My Mouse window not found. Is PowerToys running with Find My Mouse ON?")
            return
        host = process_path(hwnds[0])
        log(f"Find My Mouse in:  {host or 'unknown'}")
        log(f"Host version:      {file_version(host) if host else 'unknown'}")

        s = find_my_mouse_settings()
        if s:
            am = s["activation_method"]
            log(f"Activation method: {am} = {ACTIVATION_NAMES.get(am, 'unknown')}")
            log(f"Include Win key:   {s['include_win_key']}")
            log(f"No activate in game mode: {s['do_not_activate_on_game_mode']}")
            log(f"Excluded apps:     {s['excluded_apps']!r}")
            if am not in (0, None):
                log("WARNING: set Activation method to 'Press Left Control twice' and run again.")
            if s["include_win_key"]:
                log("WARNING: 'Include Win key' is ON; these tests press Ctrl only, expect no spotlight.")
        else:
            log("Settings file:     not found/readable (defaults assumed)")

        dct = user32.GetDoubleClickTime()
        lo, hi = min(100, dct // 5), dct
        log(f"Windows double-click time: {dct} ms")
        log(f"Expected from PowerToys code: spotlight only if the 2nd Ctrl comes {lo}-{hi} ms after the 1st")

        print("\nOpening Notepad...")
        subprocess.Popen(["notepad.exe"])
        for i in range(8, 0, -1):
            print(f"  Hands off mouse & keyboard! Starting in {i} ", end="\r")
            time.sleep(1)
        print(" " * 50)
        fg = foreground_exe()
        log(f"Active window during test: {fg or 'unknown'}")
        start_pos = cursor()

        # Gaps are chosen relative to the user's double-click time, safely away from the limits.
        gap_in = max(lo + 30, hi // 2)       # well inside the window
        gap_out = max(1000, hi * 2)          # well outside the window
        log(f"Gaps used: A and C = {gap_in} ms (inside), B = {gap_out} ms (outside)")

        winmm.timeBeginPeriod(1)
        try:
            if "A" in only:
                summary.append(run_simple_test(
                    "Test A", f"two Left Ctrl taps {gap_in} ms apart (normal use)",
                    double_ctrl, hwnds, gap_in, args.tries, expect=True))
            if "B" in only:
                summary.append(run_simple_test(
                    "Test B", f"two Left Ctrl taps {gap_out} ms apart (too slow)",
                    double_ctrl, hwnds, gap_out, args.tries, expect=False))

            for letter, desc, trial, expect in (
                    ("C", f"lone Ctrl tap, then Ctrl+C {gap_in} ms later (accidental trigger)",
                     ctrl_then_ctrl_c, True),
                    ("E", f"Ctrl+C, then Ctrl+C again {gap_in} ms later, NO lone tap (like copy then paste)",
                     ctrl_c_twice, False)):
                if letter not in only:
                    continue
                if foreground_exe() == "notepad.exe":
                    summary.append(run_simple_test(f"Test {letter}", desc, trial, hwnds, gap_in,
                                                   args.tries, expect=expect))
                else:
                    log(f"\nTest {letter}: SKIPPED - Notepad was not the active window "
                        "(Ctrl+C in the terminal would stop this script). Click Notepad and run again.")
                    summary.append(f"Test {letter}: skipped (Notepad not active)")

            if "D" in only and not args.no_sweep:
                log("\nTest D (bonus): gap sweep - where exactly is the limit?")
                log(f"  {'target':>7} {'actual':>7}  spotlight  expected")
                yes = []
                sweep = {60, 80, 130, 200, 300, 400, 470, 530, 600, 800, hi - 30, hi + 30}
                for gap in sorted(g for g in sweep if g > 0):
                    shown, actual = double_ctrl(hwnds, gap)
                    expected = lo <= actual <= hi
                    near = min(abs(actual - lo), abs(actual - hi)) < 20
                    note = "near limit (+-16 ms timer)" if near else ("OK" if shown == expected else "DIFFERENT")
                    log(f"  {gap:>5}ms {actual:>5.0f}ms  {'YES' if shown else 'no':<9}  "
                        f"{'YES' if expected else 'no':<4} {note}")
                    if shown:
                        yes.append(actual)
                rng = f"~{min(yes):.0f} to ~{max(yes):.0f} ms" if yes else "never"
                log(f"  RESULT: spotlight appeared for gaps {rng} (double-click time {dct} ms)")
                summary.append(f"Test D: spotlight for gaps {rng}, Windows double-click time {dct} ms")
            reset_state(hwnds)
        finally:
            winmm.timeEndPeriod(1)

        if cursor() != start_pos:
            log("\nNOTE: the mouse moved during the test - results may be wrong, please run again.")

        log("\n=== SUMMARY ===")
        for line in summary:
            log(line)
    except KeyboardInterrupt:
        log("\nStopped by user (Ctrl+C) - results are incomplete.")
    finally:
        log("\nDONE")
        log.save(out_path)
        print(f"\nResults saved to: {out_path}")
        print("Open it in Notepad and paste the whole text back in chat.")


if __name__ == "__main__":
    main()
