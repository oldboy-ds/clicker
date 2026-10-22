"""Windows timed single-click tool (Tkinter + Win32).

- Position selection through a click on a full virtual-desktop overlay.
- Schedule a single left-click at a specified local time (milliseconds supported).
- Optionally schedule on NTP server time, compensating the PC clock offset.
- No external Python packages are required.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime, timedelta
import os
import queue
import socket
import struct
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk


def enable_dpi_awareness() -> None:
    """Align Tk coordinates and Windows cursor coordinates on high-DPI monitors."""
    if sys.platform != "win32":
        return
    try:
        # Windows 10+: Per-monitor DPI awareness v2
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (OSError, AttributeError):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (OSError, AttributeError):
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except (OSError, AttributeError):
                pass


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", INPUTUNION)]


class WinMouse:
    """Low-level Windows cursor and mouse input, stdlib only."""

    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004

    def __init__(self) -> None:
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32.SetCursorPos.argtypes = (ctypes.c_int, ctypes.c_int)
        self._user32.SetCursorPos.restype = wintypes.BOOL
        self._user32.SendInput.argtypes = (
            wintypes.UINT,
            ctypes.POINTER(INPUT),
            ctypes.c_int,
        )
        self._user32.SendInput.restype = wintypes.UINT
        self._user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
        self._user32.GetSystemMetrics.restype = ctypes.c_int

    def virtual_screen(self) -> tuple[int, int, int, int]:
        get = self._user32.GetSystemMetrics
        return get(76), get(77), get(78), get(79)

    def put_overlay(self, hwnd: int, x: int, y: int, w: int, h: int) -> None:
        # Position using absolute virtual-screen coordinates, including negative x/y.
        self._user32.SetWindowPos.argtypes = (
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        )
        self._user32.SetWindowPos.restype = wintypes.BOOL
        HWND_TOPMOST = wintypes.HWND(-1)
        SWP_SHOWWINDOW = 0x0040
        if not self._user32.SetWindowPos(hwnd, HWND_TOPMOST, x, y, w, h, SWP_SHOWWINDOW):
            raise ctypes.WinError(ctypes.get_last_error())

    def click_once(self, x: int, y: int) -> tuple[int, int]:
        """Move cursor and send one left-button down+up. Return (send, return) timestamps (ns)."""
        if not self._user32.SetCursorPos(x, y):
            raise ctypes.WinError(ctypes.get_last_error())

        inputs = (INPUT * 2)()
        inputs[0].type = 0  # INPUT_MOUSE
        inputs[0].u.mi.dwFlags = self.MOUSEEVENTF_LEFTDOWN
        inputs[1].type = 0
        inputs[1].u.mi.dwFlags = self.MOUSEEVENTF_LEFTUP

        sent_at_ns = time.time_ns()
        n = self._user32.SendInput(2, inputs, ctypes.sizeof(INPUT))
        returned_at_ns = time.time_ns()
        if n != 2:
            err = ctypes.get_last_error()
            raise OSError(f"SendInput 실패: {n}/2 이벤트 전송 (Windows 오류: {err})")
        return sent_at_ns, returned_at_ns


def parse_local_datetime(text: str) -> datetime:
    """Parse a minute-precision time; seconds and milliseconds are always zero."""
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        raise ValueError("시간 형식: YYYY-MM-DD HH:MM (예: 2026-10-25 15:00)") from None


MAX_LEAD_MS = 1000


def parse_lead_ms(text: str) -> int:
    """How many milliseconds before the target time to click."""
    try:
        value = int(text.strip())
    except ValueError:
        raise ValueError(f"앞당김은 0~{MAX_LEAD_MS} 사이의 정수(ms)로 입력해 주세요.") from None
    if not 0 <= value <= MAX_LEAD_MS:
        raise ValueError(f"앞당김은 0~{MAX_LEAD_MS} 사이의 정수(ms)로 입력해 주세요.")
    return value


def to_timestamp_ns(dt: datetime) -> int:
    # Avoid converting the full floating-point UNIX timestamp to nanoseconds.
    whole = dt.replace(microsecond=0)
    return int(whole.timestamp()) * 1_000_000_000 + dt.microsecond * 1_000


def format_ns(ns: int, with_date: bool = False) -> str:
    """Local time string with milliseconds, from a UNIX timestamp in ns."""
    sec, rem = divmod(ns, 1_000_000_000)
    fmt = "%Y-%m-%d %H:%M:%S" if with_date else "%H:%M:%S"
    return f"{datetime.fromtimestamp(sec).strftime(fmt)}.{rem // 1_000_000:03d}"


# KRISS (Korea Research Institute of Standards and Science) keeps Korean standard time.
NTP_SERVERS = (
    "time.kriss.re.kr",
    "time2.kriss.re.kr",
    "time.windows.com",
    "time.bora.net",
    "time.google.com",
    "kr.pool.ntp.org",
)
NTP_EPOCH_DELTA = 2_208_988_800  # seconds from 1900-01-01 (NTP) to 1970-01-01 (UNIX)


def ns_to_ntp(ns: int) -> bytes:
    sec, rem = divmod(ns, 1_000_000_000)
    return struct.pack("!II", sec + NTP_EPOCH_DELTA, (rem << 32) // 1_000_000_000)


def ntp_to_ns(raw: bytes) -> int:
    sec, frac = struct.unpack("!II", raw)
    return (sec - NTP_EPOCH_DELTA) * 1_000_000_000 + ((frac * 1_000_000_000) >> 32)


def query_ntp(server: str, samples: int = 5, timeout: float = 2.0) -> tuple[int, int]:
    """Measure (offset_ns, rtt_ns) against an SNTP server; offset = server time - PC time.

    Several samples are taken and the one with the shortest round trip is kept,
    since its offset is least affected by asymmetric network delay.
    """
    addr = socket.getaddrinfo(server, 123, socket.AF_INET, socket.SOCK_DGRAM)[0][4]
    best: tuple[int, int] | None = None
    last_error = "응답 없음"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        for i in range(samples):
            if i:
                time.sleep(0.1)
            t1 = time.time_ns()
            # LI=0, VN=4, Mode=3 (client); our send time goes in the transmit field
            # so the matching reply can be recognized by its originate field.
            stamp = ns_to_ntp(t1)
            sock.sendto(b"\x23" + bytes(39) + stamp, addr)
            try:
                while True:
                    data, _ = sock.recvfrom(512)
                    t4 = time.time_ns()
                    if len(data) >= 48 and data[24:32] == stamp:
                        break
            except socket.timeout:
                last_error = f"{timeout:.0f}초 동안 응답 없음"
                continue
            if data[0] >> 6 == 3 or data[1] == 0:
                last_error = "서버가 동기화되지 않은 상태입니다"
                continue
            t2 = ntp_to_ns(data[32:40])  # server receive
            t3 = ntp_to_ns(data[40:48])  # server transmit
            rtt = (t4 - t1) - (t3 - t2)
            offset = ((t2 - t1) + (t3 - t4)) // 2
            if best is None or rtt < best[1]:
                best = (offset, rtt)
    if best is None:
        raise OSError(f"{server}: {last_error}")
    return best


def describe_offset(offset_ns: int) -> str:
    ms = offset_ns / 1e6
    if abs(ms) < 0.05:
        return "PC 시계가 서버와 같습니다"
    return f"PC가 서버보다 {abs(ms):.1f}ms {'느립니다' if ms > 0 else '빠릅니다'}"


# Every log entry is also appended here, next to this script.
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "click_log.txt")


class TimedClickApp:
    def __init__(self, root: tk.Tk, dry_run: bool = True) -> None:
        self.root = root
        self.root.title("정각 원클릭 · 학습용")
        self.root.resizable(True, True)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.mouse = WinMouse()
        self.coord: tuple[int, int] | None = None
        self.overlay: tk.Toplevel | None = None
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.messages: queue.Queue[tuple[str, object]] = queue.Queue()
        self.target_ns: int | None = None
        self.running = False
        # Server time - PC time, from the latest NTP measurement.
        self.offset_ns: int | None = None
        self.syncing = False

        # Initial target: 2 minutes ahead, rounded down to the minute.
        initial = datetime.now() + timedelta(minutes=2)
        self.time_var = tk.StringVar(value=initial.strftime("%Y-%m-%d %H:%M"))
        self.lead_var = tk.StringVar(value="20")
        self.preview_var = tk.StringVar(value="")
        self.position_var = tk.StringVar(value="미지정")
        self.clock_var = tk.StringVar(value="")
        self.countdown_var = tk.StringVar(value="예약 전")
        self.status_var = tk.StringVar(value="좌표 지정 → 목표 시각 입력 → 예약 시작")
        self.dry_run_var = tk.BooleanVar(value=dry_run)
        self.use_server_var = tk.BooleanVar(value=True)
        self.server_var = tk.StringVar(value=NTP_SERVERS[0])
        self.server_clock_var = tk.StringVar(value="동기화 전")
        self.offset_var = tk.StringVar(value="서버 시간을 확인하는 중...")

        self.build_widgets()
        # Size the window from its contents, so it fits at any display scaling.
        self.root.update_idletasks()
        width, height = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        self.root.minsize(width, height)
        self.root.geometry(f"{width}x{height}")
        self.root.after(50, self.refresh_ui)
        self.root.after(200, self.start_sync)

    def build_widgets(self) -> None:
        outer = ttk.Frame(self.root, padding=20)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="정각 원클릭", font=("Malgun Gothic", 19, "bold")).pack(anchor="w")
        ttk.Label(
            outer,
            text="Windows 로컬 시간 기준 · 지정 좌표 1회 클릭 · 자동 반복 없음",
            foreground="#666666",
        ).pack(anchor="w", pady=(2, 10))

        coords = ttk.LabelFrame(outer, text="1. 클릭 좌표 선택", padding=12)
        coords.pack(fill="x", pady=(0, 8))
        coord_row = ttk.Frame(coords)
        coord_row.pack(fill="x")
        self.pick_btn = ttk.Button(coord_row, text="마우스로 좌표 지정", command=self.start_pick)
        self.pick_btn.pack(side="left")
        ttk.Label(coord_row, textvariable=self.position_var, font=("Consolas", 11)).pack(
            side="left", padx=16
        )
        ttk.Label(
            coords,
            text="반투명 화면에서 목표 지점을 클릭하세요. 실제 아래 창은 클릭되지 않습니다. (ESC 취소)",
            foreground="#666666",
            wraplength=495,
        ).pack(anchor="w", pady=(8, 0))

        when = ttk.LabelFrame(outer, text="2. 실행 시각 입력", padding=12)
        when.pack(fill="x", pady=(0, 8))
        self.time_entry = ttk.Entry(when, textvariable=self.time_var, font=("Consolas", 12))
        self.time_entry.pack(fill="x")
        when_row = ttk.Frame(when)
        when_row.pack(fill="x", pady=(6, 0))
        ttk.Label(
            when_row, text="형식: 2026-10-25 15:00 (초·밀리초는 0 고정)", foreground="#666666"
        ).pack(side="left")
        ttk.Label(when_row, text="ms").pack(side="right")
        self.lead_spin = ttk.Spinbox(
            when_row,
            textvariable=self.lead_var,
            from_=0,
            to=MAX_LEAD_MS,
            increment=5,
            width=5,
            justify="right",
        )
        self.lead_spin.pack(side="right", padx=4)
        ttk.Label(when_row, text="앞당김").pack(side="right")
        ttk.Label(when, textvariable=self.preview_var, foreground="#1a5fb4").pack(
            anchor="w", pady=(4, 0)
        )

        options = ttk.Frame(outer)
        options.pack(fill="x", pady=(0, 8))
        self.server_checkbox = ttk.Checkbutton(
            options,
            text="서버 시간 기준으로 실행 (PC 시계 차이를 자동 보정)",
            variable=self.use_server_var,
        )
        self.server_checkbox.pack(anchor="w")
        self.dry_checkbox = ttk.Checkbutton(
            options,
            text="드라이런 (실제 클릭 없이 시각 측정)",
            variable=self.dry_run_var,
        )
        self.dry_checkbox.pack(anchor="w", pady=(4, 0))

        actions = ttk.Frame(outer)
        actions.pack(fill="x")
        self.start_btn = ttk.Button(actions, text="예약 시작", command=self.start_schedule)
        self.start_btn.pack(side="left", fill="x", expand=True, padx=(0, 5))
        self.test_btn = ttk.Button(actions, text="5초 후 테스트", command=self.test_5_seconds)
        self.test_btn.pack(side="left", fill="x", expand=True, padx=(5, 5))
        self.cancel_btn = ttk.Button(actions, text="예약 취소", command=self.cancel, state="disabled")
        self.cancel_btn.pack(side="left", fill="x", expand=True, padx=(5, 0))

        status = ttk.LabelFrame(outer, text="3. 상태 확인", padding=12)
        status.pack(fill="x", pady=(10, 0))
        clocks = ttk.Frame(status)
        clocks.pack(fill="x")
        ttk.Label(clocks, text="서버 시각").grid(row=0, column=0, sticky="w", padx=(0, 12))
        ttk.Label(clocks, textvariable=self.server_clock_var, font=("Consolas", 12, "bold")).grid(
            row=0, column=1, sticky="w"
        )
        ttk.Label(clocks, text="PC 시각").grid(row=1, column=0, sticky="w", padx=(0, 12))
        ttk.Label(clocks, textvariable=self.clock_var, font=("Consolas", 12)).grid(
            row=1, column=1, sticky="w"
        )
        ttk.Label(status, textvariable=self.offset_var, foreground="#1a5fb4", wraplength=495).pack(
            anchor="w", pady=(6, 0)
        )
        sync_row = ttk.Frame(status)
        sync_row.pack(fill="x", pady=(6, 0))
        ttk.Label(sync_row, text="시간 서버").pack(side="left")
        self.server_combo = ttk.Combobox(
            sync_row, textvariable=self.server_var, values=NTP_SERVERS, width=22
        )
        self.server_combo.pack(side="left", padx=8)
        self.sync_btn = ttk.Button(sync_row, text="서버 시간 동기화", command=self.start_sync)
        self.sync_btn.pack(side="left")
        ttk.Label(status, textvariable=self.countdown_var, font=("Consolas", 14, "bold")).pack(
            anchor="w", pady=(8, 4)
        )
        ttk.Label(status, textvariable=self.status_var, wraplength=495).pack(anchor="w")

        log = ttk.LabelFrame(outer, text="4. 실행 로그", padding=12)
        log.pack(fill="both", expand=True, pady=(8, 0))
        log_head = ttk.Frame(log)
        log_head.pack(fill="x", side="bottom", pady=(6, 0))
        ttk.Button(log_head, text="로그 지우기", command=self.clear_log).pack(side="right")
        log_scroll = ttk.Scrollbar(log, orient="vertical")
        log_scroll.pack(side="right", fill="y")
        self.log_text = tk.Text(
            log,
            height=4,
            font=("Consolas", 9),
            wrap="none",
            state="disabled",
            yscrollcommand=log_scroll.set,
        )
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll.configure(command=self.log_text.yview)

    def add_log(self, *lines: str) -> None:
        """Append one timestamped entry; continuation lines are indented."""
        now_ns = time.time_ns()
        body = "".join(f"    {line}\n" for line in lines[1:])
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{format_ns(now_ns)}] {lines[0]}\n{body}")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        # The file keeps the date as well, since it accumulates across runs.
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"[{format_ns(now_ns, with_date=True)}] {lines[0]}\n{body}")
        except OSError as exc:
            self.status_var.set(f"로그 파일 저장 실패: {exc}")

    def start_sync(self) -> None:
        if self.syncing or self.running:
            return
        server = self.server_var.get().strip()
        if not server:
            messagebox.showwarning("시간 서버", "시간 서버 주소를 입력해 주세요.")
            return
        self.syncing = True
        self.sync_btn.configure(state="disabled")
        self.offset_var.set(f"{server}에서 서버 시간을 확인하는 중...")
        threading.Thread(target=self.run_sync, args=(server,), name="ntp-sync", daemon=True).start()

    def run_sync(self, server: str) -> None:
        try:
            offset_ns, rtt_ns = query_ntp(server)
            self.messages.put(("synced", (server, offset_ns, rtt_ns, time.time_ns())))
        except Exception as exc:
            self.messages.put(("sync_error", (server, str(exc))))

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def start_pick(self) -> None:
        if self.running:
            messagebox.showinfo("예약 진행 중", "예약을 취소한 뒤 좌표를 변경해 주세요.")
            return
        if self.overlay is not None:
            return
        vx, vy, vw, vh = self.mouse.virtual_screen()
        overlay = tk.Toplevel(self.root)
        self.overlay = overlay
        overlay.overrideredirect(True)
        overlay.configure(bg="#153b51", cursor="crosshair")
        overlay.attributes("-alpha", 0.33)
        overlay.attributes("-topmost", True)
        overlay.geometry(f"{vw}x{vh}+0+0")
        overlay.update_idletasks()
        try:
            self.mouse.put_overlay(overlay.winfo_id(), vx, vy, vw, vh)
        except Exception as exc:
            overlay.destroy()
            self.overlay = None
            messagebox.showerror("화면 선택 오류", str(exc))
            return

        # A click on the overlay is recorded but not forwarded to the app beneath it.
        overlay.bind("<Button-1>", self.capture_position)
        overlay.bind("<Escape>", lambda _: self.stop_pick())
        overlay.focus_force()

        # Separate nontransparent instruction window, which does not obscure click area.
        hint = tk.Label(
            overlay,
            text="선택할 위치를 클릭하세요   |   ESC: 취소",
            font=("Malgun Gothic", 16, "bold"),
            bg="#153b51",
            fg="white",
            padx=20,
            pady=12,
        )
        hint.place(x=vw // 2, y=42, anchor="n")
        # Child widgets forward their clicks to the same callback.
        hint.bind("<Button-1>", self.capture_position)

    def capture_position(self, event: tk.Event) -> None:
        self.coord = (int(event.x_root), int(event.y_root))
        self.position_var.set(f"X: {self.coord[0]}    Y: {self.coord[1]}")
        self.status_var.set("좌표 저장 완료. 목표 시각을 지정하세요.")
        self.stop_pick()

    def stop_pick(self) -> None:
        if self.overlay is not None:
            self.overlay.destroy()
            self.overlay = None
            self.root.lift()

    def active_offset(self) -> int | None:
        """Offset to apply when scheduling, or None when running on the PC clock."""
        return self.offset_ns if self.use_server_var.get() else None

    def describe_plan(self) -> str:
        """Preview of when the click will actually happen, for the current inputs."""
        try:
            goal_ns = to_timestamp_ns(parse_local_datetime(self.time_var.get()))
            lead_ms = parse_lead_ms(self.lead_var.get())
        except ValueError:
            return ""
        if self.use_server_var.get() and self.offset_ns is None:
            return "서버 시간을 확인하면 실제 클릭 시각이 표시됩니다."
        offset_ns = self.active_offset()
        click_ns = goal_ns - lead_ms * 1_000_000
        text = f"→ {'서버' if offset_ns is not None else 'PC'} 시각 {format_ns(click_ns)}에 클릭"
        if offset_ns is not None:
            text += f"  (PC 시각 {format_ns(click_ns - offset_ns)})"
        return text

    def test_5_seconds(self) -> None:
        self.start_schedule(test_delay_ns=5_000_000_000)

    def start_schedule(self, test_delay_ns: int | None = None) -> None:
        """Schedule at the entered time, or test_delay_ns after confirmation for a test run."""
        if self.running:
            return
        if self.coord is None:
            messagebox.showwarning("좌표 미지정", "먼저 '마우스로 좌표 지정'을 눌러 좌표를 선택해 주세요.")
            return
        goal_ns: int | None = None
        try:
            if test_delay_ns is None:
                goal_ns = to_timestamp_ns(parse_local_datetime(self.time_var.get()))
        except ValueError as exc:
            messagebox.showerror("시간 형식 오류", str(exc))
            return
        try:
            lead_ms = parse_lead_ms(self.lead_var.get())
        except ValueError as exc:
            messagebox.showerror("앞당김 오류", str(exc))
            return
        lead_ns = lead_ms * 1_000_000

        if self.use_server_var.get() and self.offset_ns is None:
            messagebox.showwarning(
                "서버 시간 미확인",
                "서버 시간을 아직 확인하지 못했습니다.\n"
                "[서버 시간 동기화]를 누르거나 '서버 시간 기준' 체크를 해제해 주세요.",
            )
            return
        offset_ns = self.active_offset()
        shift = offset_ns or 0
        basis = "서버 시간" if offset_ns is not None else "PC 시간"
        # The entered time is on the chosen basis; the scheduler runs on the PC clock.
        if goal_ns is not None and goal_ns - lead_ns - shift <= time.time_ns():
            messagebox.showwarning("지난 시각", f"현재 {basis}보다 미래의 시각을 입력해 주세요.")
            return

        if not self.dry_run_var.get():
            when = (
                "지금부터 5초 후"
                if goal_ns is None
                else f"{basis} 기준 {format_ns(goal_ns, with_date=True)}"
            )
            lead_text = f"보다 {lead_ms}ms 먼저" if lead_ms else "에"
            confirmed = messagebox.askokcancel(
                "실제 클릭 예약",
                f"{when}{lead_text}\n"
                f"X={self.coord[0]}, Y={self.coord[1]} 위치를\n"
                "왼쪽 버튼으로 한 번 클릭합니다.\n\n실제 클릭을 예약할까요?",
            )
            if not confirmed:
                return

        # A test run counts from now, so time spent on the dialog does not matter.
        if goal_ns is None:
            goal_ns = time.time_ns() + shift + test_delay_ns
        target_ns = goal_ns - lead_ns - shift
        # A confirmation dialog may have taken time.
        if target_ns <= time.time_ns():
            messagebox.showwarning("지난 시각", "확인 중 목표 시각이 지났습니다. 다시 설정해 주세요.")
            return

        self.cancel_event.clear()
        self.target_ns = target_ns
        self.running = True
        self.start_btn.configure(state="disabled")
        self.test_btn.configure(state="disabled")
        self.pick_btn.configure(state="disabled")
        self.dry_checkbox.configure(state="disabled")
        self.server_checkbox.configure(state="disabled")
        self.sync_btn.configure(state="disabled")
        self.server_combo.configure(state="disabled")
        self.time_entry.configure(state="disabled")
        self.lead_spin.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.status_var.set("예약됨 · 실행 시각까지 대기 중...")
        mode = "드라이런" if self.dry_run_var.get() else "실제 클릭"
        lines = [
            f"예약 ({mode} · {basis} 기준) · X={self.coord[0]}, Y={self.coord[1]}",
            f"목표 {format_ns(goal_ns, with_date=True)}",
        ]
        if lead_ns:
            lines.append(f"클릭 {format_ns(goal_ns - lead_ns, with_date=True)} ({lead_ms}ms 앞당김)")
        if offset_ns is not None:
            lines.append(
                f"PC 시각 {format_ns(target_ns, with_date=True)}에 실행 ({describe_offset(offset_ns)})"
            )
        self.add_log(*lines)

        self.worker = threading.Thread(
            target=self.run_scheduler,
            args=(target_ns, self.coord, self.dry_run_var.get(), offset_ns, lead_ns),
            name="click-scheduler",
            daemon=True,
        )
        self.worker.start()

    def run_scheduler(
        self,
        target_ns: int,
        xy: tuple[int, int],
        dry_run: bool,
        offset_ns: int | None,
        lead_ns: int,
    ) -> None:
        try:
            # Correlate wall clock with a high-resolution monotonic clock.
            wall_now = time.time_ns()
            perf_now = time.perf_counter_ns()
            deadline = perf_now + target_ns - wall_now

            while not self.cancel_event.is_set():
                remaining = deadline - time.perf_counter_ns()
                if remaining <= 0:
                    break
                if remaining > 50_000_000:
                    self.cancel_event.wait(min((remaining - 25_000_000) / 1e9, 0.1))
                elif remaining > 4_000_000:
                    time.sleep(0.001)
                # Last few milliseconds: tight wait, trading brief CPU use for accuracy.

            if self.cancel_event.is_set():
                self.messages.put(("cancelled", None))
                return

            reached_at_ns = time.time_ns()
            if dry_run:
                sent_at_ns = returned_at_ns = reached_at_ns
            else:
                sent_at_ns, returned_at_ns = self.mouse.click_once(*xy)
            self.messages.put(
                (
                    "done",
                    (
                        reached_at_ns,
                        sent_at_ns,
                        returned_at_ns,
                        target_ns,
                        dry_run,
                        xy,
                        offset_ns,
                        lead_ns,
                    ),
                )
            )
        except Exception as exc:
            self.messages.put(("error", str(exc)))

    def cancel(self) -> None:
        if self.running:
            self.cancel_event.set()
            self.status_var.set("취소 요청 중...")

    def reset_controls(self) -> None:
        self.running = False
        self.target_ns = None
        self.start_btn.configure(state="normal")
        self.test_btn.configure(state="normal")
        self.pick_btn.configure(state="normal")
        self.dry_checkbox.configure(state="normal")
        self.server_checkbox.configure(state="normal")
        self.server_combo.configure(state="normal")
        if not self.syncing:
            self.sync_btn.configure(state="normal")
        self.time_entry.configure(state="normal")
        self.lead_spin.configure(state="normal")
        self.cancel_btn.configure(state="disabled")

    def refresh_ui(self) -> None:
        now_ns = time.time_ns()
        self.clock_var.set(format_ns(now_ns, with_date=True))
        if self.offset_ns is not None:
            self.server_clock_var.set(format_ns(now_ns + self.offset_ns, with_date=True))
        if not self.running:
            self.preview_var.set(self.describe_plan())

        if self.running and self.target_ns is not None:
            remain_s = max(0, (self.target_ns - time.time_ns()) / 1e9)
            self.countdown_var.set(f"남은 시간: {remain_s:.3f}초")

        while True:
            try:
                kind, data = self.messages.get_nowait()
            except queue.Empty:
                break
            if kind == "synced":
                server, offset_ns, rtt_ns, measured_ns = data
                self.offset_ns = offset_ns
                self.syncing = False
                if not self.running:
                    self.sync_btn.configure(state="normal")
                self.offset_var.set(
                    f"{describe_offset(offset_ns)}  "
                    f"(왕복 {rtt_ns / 1e6:.1f}ms · {server} · {format_ns(measured_ns)} 확인)"
                )
                self.add_log(
                    f"서버 시간 동기화 · {server}",
                    f"{describe_offset(offset_ns)} (왕복 {rtt_ns / 1e6:.1f}ms)",
                )
            elif kind == "sync_error":
                server, message = data
                self.syncing = False
                if not self.running:
                    self.sync_btn.configure(state="normal")
                prefix = "마지막 측정값 유지 · " if self.offset_ns is not None else ""
                self.offset_var.set(f"{prefix}서버 시간 확인 실패: {message}")
                self.add_log(f"서버 시간 동기화 실패 · {server}", message)
            elif kind == "done":
                reached_ns, sent_ns, returned_ns, goal_ns, dry_run, (x, y), offset_ns, lead_ns = data
                reached_err_ms = (reached_ns - goal_ns) / 1e6
                sent_err_ms = (sent_ns - goal_ns) / 1e6
                # Times are shown on the basis the user scheduled with.
                shift = offset_ns or 0
                basis = "서버 시간" if offset_ns is not None else "PC 시간"
                label = "드라이런 완료" if dry_run else "1회 클릭 명령 전송 완료"
                self.status_var.set(
                    f"{label} ({basis} 기준) | 예약 루프 종료 오차: {reached_err_ms:+.3f}ms | "
                    f"입력 전송 직전 오차: {sent_err_ms:+.3f}ms"
                )
                lines = [
                    f"{label} · {basis} 기준 · X={x}, Y={y}",
                    f"목표     {format_ns(goal_ns + shift + lead_ns)}",
                ]
                if lead_ns:
                    lines.append(
                        f"클릭예정 {format_ns(goal_ns + shift)}  ({lead_ns // 1_000_000}ms 앞당김)"
                    )
                lines.append(f"루프종료 {format_ns(reached_ns + shift)}  ({reached_err_ms:+.3f}ms)")
                if not dry_run:
                    lines.append(f"전송직전 {format_ns(sent_ns + shift)}  ({sent_err_ms:+.3f}ms)")
                    lines.append(f"SendInput 소요 {(returned_ns - sent_ns) / 1e6:.3f}ms")
                self.add_log(*lines)
                self.countdown_var.set("완료")
                self.reset_controls()
            elif kind == "cancelled":
                self.status_var.set("예약을 취소했습니다.")
                self.add_log("예약 취소")
                self.countdown_var.set("취소됨")
                self.reset_controls()
            elif kind == "error":
                self.status_var.set(f"실행 실패: {data}")
                self.add_log(f"실행 실패: {data}")
                self.countdown_var.set("오류")
                self.reset_controls()
                messagebox.showerror("클릭 오류", str(data))

        self.root.after(50, self.refresh_ui)

    def on_close(self) -> None:
        if self.running:
            if not messagebox.askyesno("종료 확인", "예약이 진행 중입니다. 취소하고 종료할까요?"):
                return
            self.cancel_event.set()
        self.root.destroy()


def main() -> None:
    if sys.platform != "win32":
        print("이 프로그램은 Windows 10/11 전용입니다.", file=sys.stderr)
        sys.exit(1)
    enable_dpi_awareness()
    root = tk.Tk()
    try:
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")
        # --live: start with dry-run unchecked (real click mode).
        TimedClickApp(root, dry_run="--live" not in sys.argv[1:])
    except Exception as exc:
        messagebox.showerror("초기화 실패", str(exc))
        raise
    root.mainloop()


if __name__ == "__main__":
    main()
