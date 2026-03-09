import os
# Force X11 on Pi (avoids wayland plugin errors)
os.environ["QT_QPA_PLATFORM"] = "xcb"
os.environ["QT_QPA_PLATFORMTHEME"] = ""
os.environ["QT_LOGGING_RULES"] = "*.debug=false;qt.qpa.*=false"

# prevent IM/virtual keyboard from shifting the window on focus
os.environ["QT_IM_MODULE"] = "none"

# Stable scaling
os.environ["QT_AUTO_SCREEN_SCALE_FACTOR"] = "0"
os.environ["QT_SCALE_FACTOR"] = "1"
os.environ["QT_FONT_DPI"] = "96"

import sys
import time
import uuid
import json
import math
import random
import threading

import RPi.GPIO as GPIO
import pigpio
import qrcode
import requests

# Optional local ONNX bottle verification
try:
    import cv2
    import numpy as np
except Exception:
    cv2 = None
    np = None

from PyQt6.QtCore import (
    Qt, QTimer, QThread, pyqtSignal, pyqtProperty, QPropertyAnimation,
    QEasingCurve, QSize, QObject, QEvent
)
from PyQt6.QtGui import (
    QFont, QPainter, QLinearGradient, QColor, QImage, QPixmap,
    QPainterPath, QPen, QFontDatabase
)
from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton,
    QVBoxLayout, QHBoxLayout, QStackedWidget,
    QGraphicsOpacityEffect, QLineEdit, QDialog
)

# -----------------------------
# pygame audio (NO QtMultimedia)
# -----------------------------
import pygame

# -----------------------------
# WS2812B / NeoPixel (optional)
# -----------------------------
try:
    import board
    import neopixel
except Exception:
    board = None
    neopixel = None


# -----------------------------
# Audio environment helpers
# -----------------------------
def prepare_audio_env():
    """Try to preserve audio access when running under sudo on Raspberry Pi."""
    sudo_uid = os.environ.get("SUDO_UID")
    if sudo_uid:
        runtime_dir = f"/run/user/{sudo_uid}"
        pulse_native = f"{runtime_dir}/pulse/native"
        if os.path.isdir(runtime_dir):
            os.environ.setdefault("XDG_RUNTIME_DIR", runtime_dir)
        if os.path.exists(pulse_native):
            os.environ.setdefault("PULSE_SERVER", f"unix:{pulse_native}")

    os.environ.setdefault("SDL_AUDIODRIVER", "pulseaudio")

prepare_audio_env()

# ============================================================
# CONFIG
# ============================================================

IDLE_TIMEOUT_MS = 30000

# Hardware pins (BCM)
GPIO_TRIG = 23
GPIO_ECHO = 24
GPIO_SERVO = 16

# Ultrasonic-only fallback mode
# A bottle is considered present when it is close to the chute sensor.
ULTRA_MIN_CM = 2.0
ULTRA_MAX_CM = 18.0
ULTRA_TIMEOUT_S = 0.03
VERIFY_SECONDS = 1.0
POLL_MS = 45
CLEAR_BOTH_TIMEOUT_S = 2.0

SERVO_CLOSED_US = 500
SERVO_OPEN_US = 1200
GATE_OPEN_MS = 900

POINTS_PER_BOTTLE = 5

# WS2812B LED strip (GPIO18 / Pin 12)
LED_COUNT = 60          # change if your strip has a different LED count
LED_BRIGHTNESS = 0.35   # 0.0 - 1.0
LED_IDLE_COLOR = (0, 255, 0)      # Green (ready / idle)
LED_VERIFY_COLOR = (255, 0, 0)    # Red (verifying / busy)

# UI sizing
BTN_W = 600
BTN_H = 124
SMALL_BTN_W = 320
SMALL_BTN_H = 98

# Background animation
WATER_FPS_MS = 33
WAVE_SPEED = 0.092
WAVE_OPACITY = 0.18

# Falling bottles
BOTTLE_COUNT = 12
BOTTLE_ALPHA = 44
BOTTLE_SPEED_MIN = 0.75
BOTTLE_SPEED_MAX = 1.9

# Firebase
FIREBASE_URL = "https://ecobyte-firebase-default-rtdb.asia-southeast1.firebasedatabase.app"
FIREBASE_TIMEOUT_S = 3.5

# Optional ONNX bottle verifier (disabled until camera is connected)
USE_ONNX_VERIFIER = True
ONNX_MODEL_PATH = "best.onnx"
CAMERA_INDEX = 0
ONNX_INPUT_SIZE = 640
ONNX_CONF_THRESHOLD = 0.45
ONNX_OPEN_TIMEOUT_S = 2.0
ONNX_NMS_THRESHOLD = 0.40
ONNX_VERIFY_SECONDS = 1.0
ONNX_VERIFY_RATIO = 0.35


# ============================================================
# Theme / Font
# ============================================================

FONT_FAMILY = "TT Hoves"

def try_load_tt_hoves(base_dir: str):
    fonts_dir = os.path.join(base_dir, "fonts")
    candidates = [
        "TT-Hoves-Regular.ttf",
        "TT-Hoves-Bold.ttf",
        "TTHoves-Regular.ttf",
        "TTHoves-Bold.ttf",
        "TT Hoves Regular.ttf",
        "TT Hoves Bold.ttf",
    ]
    for fn in candidates:
        path = os.path.join(fonts_dir, fn)
        if os.path.exists(path):
            QFontDatabase.addApplicationFont(path)

# More teal/light-blue gradient
BG_TOP = QColor(35, 175, 215)
BG_BOTTOM = QColor(35, 215, 190)


# ============================================================
# Helpers
# ============================================================

def clamp(x, lo, hi):
    return max(lo, min(hi, x))

def angle_to_duty(angle_deg: float) -> float:
    a = clamp(angle_deg, 0, 180)
    return 2.5 + (a / 180.0) * 10.0

def qr_pixmap_from_text(text: str, size_px: int = 560) -> QPixmap:
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")

    img = img.resize((size_px, size_px))
    w, h = img.size
    data = img.tobytes("raw", "RGB")
    qimg = QImage(data, w, h, 3 * w, QImage.Format.Format_RGB888)
    return QPixmap.fromImage(qimg)


# ============================================================
# Sound Manager
# ============================================================

class SoundManager:
    def __init__(self, base_dir: str):
        self.ok = False
        self.base_dir = base_dir
        self.sounds_dir = os.path.join(base_dir, "sounds")

        self.ch_ui = None
        self.ch_fx = None
        self.ch_success = None

        try:
            pygame.mixer.pre_init(44100, -16, 2, 2048)
            if pygame.mixer.get_init() is not None:
                pygame.mixer.quit()

            last_err = None
            for driver in [os.environ.get("SDL_AUDIODRIVER", "pulseaudio"), "alsa", None]:
                try:
                    if driver:
                        os.environ["SDL_AUDIODRIVER"] = driver
                    elif "SDL_AUDIODRIVER" in os.environ:
                        del os.environ["SDL_AUDIODRIVER"]

                    pygame.mixer.init()
                    print(f"[SOUND] initialized with SDL_AUDIODRIVER={os.environ.get('SDL_AUDIODRIVER', 'default')}")
                    pygame.mixer.set_num_channels(16)
                    self.ch_ui = pygame.mixer.Channel(1)
                    self.ch_fx = pygame.mixer.Channel(2)
                    self.ch_success = pygame.mixer.Channel(3)
                    self.ok = True
                    break
                except Exception as e:
                    last_err = e
                    try:
                        pygame.mixer.quit()
                    except Exception:
                        pass

            if not self.ok:
                raise last_err if last_err is not None else RuntimeError("Unknown pygame mixer init failure")
        except Exception as e:
            print("Sound init failed:", e)
            self.ok = False
            return

        def p(name): return os.path.join(self.sounds_dir, name)

        self.path_idle = p("idle.wav")
        self.path_tap = p("tap.wav")
        self.path_bottle = p("bottle.wav")
        self.path_success = p("success.wav")
        self.path_qr_show = p("qr_show.wav")

        self._snd_tap = self._load(self.path_tap, "tap.wav")
        self._snd_bottle = self._load(self.path_bottle, "bottle.wav")
        self._snd_success = self._load(self.path_success, "success.wav")
        self._snd_qr_show = self._load(self.path_qr_show, "qr_show.wav")

        self._idle_playing = False
        self._lock = threading.Lock()

    def _load(self, path, label):
        if not self.ok:
            return None
        if not os.path.exists(path):
            print(f"[SOUND] missing {label}: {path}")
            return None
        try:
            return pygame.mixer.Sound(path)
        except Exception as e:
            print(f"[SOUND] load error {label}:", e)
            return None

    def play_idle(self, volume=0.55):
        if not self.ok:
            return
        with self._lock:
            if self._idle_playing:
                return
            try:
                if not os.path.exists(self.path_idle):
                    print("[SOUND] missing idle.wav:", self.path_idle)
                    return
                pygame.mixer.music.load(self.path_idle)
                pygame.mixer.music.set_volume(volume)
                pygame.mixer.music.play(-1)
                self._idle_playing = True
            except Exception as e:
                print("Idle play error:", e)

    def stop_idle(self):
        if not self.ok:
            return
        with self._lock:
            pygame.mixer.music.stop()
            self._idle_playing = False

    def tap(self):
        if self.ok and self._snd_tap and self.ch_ui:
            self._snd_tap.set_volume(0.85)
            self.ch_ui.play(self._snd_tap)

    def bottle(self):
        if self.ok and self._snd_bottle and self.ch_fx:
            self._snd_bottle.set_volume(0.9)
            self.ch_fx.play(self._snd_bottle)

    def qr_show(self):
        if not self.ok:
            return
        if self._snd_qr_show is None:
            self._snd_qr_show = self._load(self.path_qr_show, "qr_show.wav")
        if self._snd_qr_show and self.ch_ui:
            self._snd_qr_show.set_volume(1.0)
            self.ch_ui.stop()
            self.ch_ui.play(self._snd_qr_show)

    def success(self):
        if not self.ok:
            return
        if self._snd_success is None:
            self._snd_success = self._load(self.path_success, "success.wav")
        if self._snd_success and self.ch_success:
            self._snd_success.set_volume(1.0)
            self.ch_success.stop()
            self.ch_success.play(self._snd_success)


# ============================================================
# Firebase helper
# ============================================================

class FirebaseClient(QObject):
    log = pyqtSignal(str)
    ok = pyqtSignal(str)

    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url.rstrip("/")

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path}.json"

    def create_deposit_token_async(self, token: str, payload: dict):
        def worker():
            try:
                r = requests.put(self._url(f"tokens/{token}"), json=payload, timeout=FIREBASE_TIMEOUT_S)
                if r.ok:
                    self.ok.emit("deposit_saved")
                else:
                    self.log.emit(f"Firebase PUT failed: {r.status_code}")
            except Exception as e:
                self.log.emit(f"Firebase error: {e}")
        threading.Thread(target=worker, daemon=True).start()

    def consume_redeem_token_async(self, token: str):
        def worker():
            try:
                r = requests.get(self._url(f"tokens/{token}"), timeout=FIREBASE_TIMEOUT_S)
                if not r.ok:
                    self.ok.emit("redeem_invalid")
                    return
                data = r.json()
                if not data or data.get("used") is True:
                    self.ok.emit("redeem_invalid")
                    return

                patch = {"used": True, "used_ts": int(time.time())}
                r2 = requests.patch(self._url(f"tokens/{token}"), json=patch, timeout=FIREBASE_TIMEOUT_S)
                if r2.ok:
                    self.ok.emit("redeem_ok")
                else:
                    self.ok.emit("redeem_invalid")
            except Exception as e:
                self.log.emit(f"Firebase error: {e}")
                self.ok.emit("redeem_invalid")
        threading.Thread(target=worker, daemon=True).start()


# ============================================================
# Animated Background: Waves + Falling Bottles
# ============================================================

class _BottleParticle:
    def __init__(self):
        self.reset(1080, 1920)

    def reset(self, width, height):
        self.x = random.uniform(0.08, 0.92) * width
        self.y = random.uniform(-1.0, 0.2) * height
        self.scale = random.uniform(0.55, 1.15)
        self.speed = random.uniform(BOTTLE_SPEED_MIN, BOTTLE_SPEED_MAX) * (1.0 / self.scale)
        self.sway_phase = random.uniform(0, math.tau)
        self.sway_amp = random.uniform(4, 16)

class WaterBackground(QWidget):
    def __init__(self, top: QColor, bottom: QColor, asset_bottle_png: str = "", parent=None):
        super().__init__(parent)
        self._top = top
        self._bottom = bottom
        self._phase = 0.0

        self._bottles = [_BottleParticle() for _ in range(BOTTLE_COUNT)]

        self._bottle_pm = None
        if asset_bottle_png and os.path.exists(asset_bottle_png):
            pm = QPixmap(asset_bottle_png)
            if not pm.isNull():
                self._bottle_pm = pm

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(WATER_FPS_MS)

    def _tick(self):
        self._phase += WAVE_SPEED
        w = max(1, self.width())
        h = max(1, self.height())

        for b in self._bottles:
            b.y += b.speed * 2.0
            if b.y > h + 170:
                b.reset(w, h)
                b.y = -170

        if self._phase > 1e9:
            self._phase = 0.0
        self.update()

    def _draw_ridged_vector_bottle(self, p: QPainter, cx: float, cy: float, s: float, alpha: int):
        body_w = 46 * s
        body_h = 90 * s
        neck_w = 22 * s
        neck_h = 14 * s
        cap_h  = 6  * s

        x0 = cx - body_w / 2
        y0 = cy - body_h / 2

        path = QPainterPath()
        path.addRoundedRect(float(x0), float(y0 + neck_h), float(body_w), float(body_h - neck_h), float(14*s), float(14*s))
        path.addRoundedRect(float(cx - neck_w/2), float(y0), float(neck_w), float(neck_h), float(8*s), float(8*s))
        path.addRoundedRect(float(cx - neck_w/2), float(y0 - cap_h), float(neck_w), float(cap_h), float(6*s), float(6*s))

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, alpha))
        p.drawPath(path)

        hx = int(cx - body_w * 0.22)
        hy = int(y0 + neck_h + body_h * 0.12)
        hw = int(body_w * 0.14)
        hh = int(body_h * 0.60)
        rr = int(max(2, 7 * s))
        p.setBrush(QColor(255, 255, 255, int(alpha * 0.55)))
        p.drawRoundedRect(hx, hy, hw, hh, rr, rr)

        pen = QPen(QColor(255, 255, 255, int(alpha * 0.55)), max(1, int(2 * s)))
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        start_y = y0 + neck_h + (body_h * 0.22)
        for i in range(5):
            yy = start_y + i * (body_h * 0.10)
            p.drawLine(int(x0 + body_w * 0.14), int(yy), int(x0 + body_w * 0.86), int(yy))

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        w = self.width()
        h = self.height()

        grad = QLinearGradient(0, 0, 0, h)
        grad.setColorAt(0.0, self._top)
        grad.setColorAt(1.0, self._bottom)
        p.fillRect(self.rect(), grad)

        fade = QLinearGradient(0, 0, 0, int(h * 0.24))
        fade.setColorAt(0.0, QColor(255, 255, 255, 22))
        fade.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillRect(0, 0, w, int(h * 0.24), fade)

        for b in self._bottles:
            sway = math.sin((b.y * 0.01) + b.sway_phase + self._phase) * b.sway_amp
            cx = b.x + sway
            cy = b.y
            s = 0.9 * b.scale

            if self._bottle_pm:
                target_h = int(120 * s)
                target_w = int(60 * s)
                pm = self._bottle_pm.scaled(
                    target_w, target_h,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation
                )
                p.setOpacity(BOTTLE_ALPHA / 255.0)
                p.drawPixmap(int(cx - pm.width()/2), int(cy - pm.height()/2), pm)
                p.setOpacity(1.0)
            else:
                self._draw_ridged_vector_bottle(p, cx, cy, s, BOTTLE_ALPHA)

        def wave_path(y_base, amp, freq, shift):
            path = QPainterPath()
            path.moveTo(0, h)
            path.lineTo(0, y_base)

            step = max(8, w // 80)
            for x in range(0, w + step, step):
                y = y_base + amp * math.sin((x * freq) + self._phase + shift)
                path.lineTo(x, y)

            path.lineTo(w, h)
            path.closeSubpath()
            return path

        c1 = QColor(255, 255, 255, int(255 * WAVE_OPACITY))
        c2 = QColor(255, 255, 255, int(255 * (WAVE_OPACITY * 0.70)))

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c1)
        p.drawPath(wave_path(int(h * 0.70), 28, 0.016, 0.0))
        p.setBrush(c2)
        p.drawPath(wave_path(int(h * 0.78), 18, 0.020, 1.4))


# ============================================================
# Secret Exit Corner (tap 5x)
# ============================================================

class SecretExitCorner(QPushButton):
    def __init__(self, on_exit, parent=None):
        super().__init__("", parent)
        self.on_exit = on_exit
        self.setFixedSize(90, 90)
        self.setStyleSheet("background: transparent; border: none;")

        self._count = 0
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._reset)
        self.clicked.connect(self._tap)

    def _tap(self):
        if self._count == 0:
            self._timer.start(3000)
        self._count += 1
        if self._count >= 5:
            self._reset()
            self.on_exit()

    def _reset(self):
        self._count = 0
        self._timer.stop()


# ============================================================
# Themed confirmation dialog
# ============================================================

class ThemedConfirmDialog(QDialog):
    def __init__(self, title: str, message: str, yes_text: str = "YES", no_text: str = "NO", parent=None):
        super().__init__(parent)
        self._accepted = False
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setModal(True)
        self.setObjectName("ThemedConfirmDialog")
        self.setStyleSheet("""
            QDialog#ThemedConfirmDialog {
                background: rgba(17, 128, 145, 0.98);
                border: 2px solid rgba(255,255,255,0.20);
                border-radius: 28px;
            }
            QLabel {
                color: white;
                background: transparent;
            }
            QPushButton {
                min-width: 180px;
                min-height: 64px;
                border-radius: 20px;
                font-size: 20px;
                font-weight: bold;
                padding: 6px 16px;
            }
        """)

        self.setFixedSize(760, 360)

        root = QVBoxLayout(self)
        root.setContentsMargins(44, 36, 44, 30)
        root.setSpacing(16)

        title_lbl = QLabel(title)
        title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_lbl.setFont(QFont(FONT_FAMILY, 30, QFont.Weight.Bold))

        msg_lbl = QLabel(message)
        msg_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        msg_lbl.setWordWrap(True)
        msg_lbl.setFont(QFont(FONT_FAMILY, 22))
        msg_lbl.setStyleSheet('color: rgba(255,255,255,0.94);')

        btn_row = QHBoxLayout()
        btn_row.setSpacing(18)

        no_btn = QPushButton(no_text)
        no_btn.setStyleSheet("""
            QPushButton {
                background: rgba(255,255,255,0.20);
                color: white;
                border: 2px solid rgba(255,255,255,0.34);
            }
            QPushButton:pressed { background: rgba(255,255,255,0.28); }
        """)

        yes_btn = QPushButton(yes_text)
        yes_btn.setStyleSheet("""
            QPushButton {
                background: rgba(255,255,255,0.94);
                color: #0B7A3B;
                border: none;
            }
            QPushButton:pressed { background: rgba(255,255,255,0.80); }
        """)

        no_btn.clicked.connect(self.reject)
        yes_btn.clicked.connect(self.accept)

        btn_row.addStretch(1)
        btn_row.addWidget(no_btn)
        btn_row.addWidget(yes_btn)
        btn_row.addStretch(1)

        root.addSpacing(6)
        root.addWidget(title_lbl)
        root.addWidget(msg_lbl)
        root.addStretch(1)
        root.addLayout(btn_row)

    def showEvent(self, event):
        super().showEvent(event)
        if self.parent() is not None:
            parent_rect = self.parent().geometry()
            self.move(
                parent_rect.x() + (parent_rect.width() - self.width()) // 2,
                parent_rect.y() + (parent_rect.height() - self.height()) // 2
            )

# ============================================================
# Idle countdown indicator
# ============================================================

class IdleRing(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._progress = 1.0
        self._seconds = 0
        self.setFixedSize(88, 88)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def set_countdown(self, remaining_ms: int, total_ms: int):
        total_ms = max(1, int(total_ms))
        remaining_ms = max(0, int(remaining_ms))
        self._progress = clamp(remaining_ms / total_ms, 0.0, 1.0)
        self._seconds = math.ceil(remaining_ms / 1000.0) if remaining_ms > 0 else 0
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        r = self.rect().adjusted(6, 6, -6, -6)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 26))
        p.drawEllipse(r)

        track = QPen(QColor(255, 255, 255, 75), 7)
        track.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(track)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(r, 0, 360 * 16)

        arc = QPen(QColor(255, 255, 255, 210), 7)
        arc.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(arc)
        span = int(-360 * 16 * self._progress)
        p.drawArc(r, 90 * 16, span)

        p.setPen(QColor(255, 255, 255, 230))
        p.setFont(QFont(FONT_FAMILY, 16, QFont.Weight.Bold))
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, str(self._seconds))


# ============================================================
# Animated number label
# ============================================================

class AnimatedNumberLabel(QLabel):
    def __init__(self, prefix: str, parent=None):
        super().__init__(parent)
        self._prefix = prefix
        self._value = 0.0
        self._anim = QPropertyAnimation(self, b"value", self)
        self._anim.setDuration(420)
        self._anim.setEasingCurve(QEasingCurve.Type.OutBack)
        self._sync()

    def _sync(self):
        self.setText(f"{self._prefix}{int(round(self._value))}")

    def getValue(self):
        return self._value

    def setValue(self, v):
        self._value = float(v)
        self._sync()

    value = pyqtProperty(float, fget=getValue, fset=setValue)

    def animate_to(self, target: int):
        self._anim.stop()
        self._anim.setStartValue(self._value)
        self._anim.setEndValue(float(target))
        self._anim.start()


# ============================================================
# Colored EcoByte title widget
# ============================================================

class ColoredEcoByteTitle(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(220)

    def sizeHint(self):
        return QSize(980, 220)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        font = QFont(FONT_FAMILY, 122, QFont.Weight.Bold)
        p.setFont(font)
        fm = p.fontMetrics()

        parts = ["Eco", "B", "yte"]
        widths = [fm.horizontalAdvance(t) for t in parts]
        total = sum(widths)

        x = (self.width() - total) // 2
        y = (self.height() + fm.ascent() - fm.descent()) // 2

        def draw_part(text, x_pos, color):
            path = QPainterPath()
            path.addText(float(x_pos), float(y), font, text)

            shadow = QPainterPath(path)
            shadow.translate(0, 7)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 55))
            p.drawPath(shadow)

            p.setBrush(color)
            p.drawPath(path)

        eco_color = QColor(80, 240, 120)
        b_color   = QColor(90, 190, 255)
        yte_color = QColor(10, 155, 165)

        draw_part("Eco", x, eco_color)
        x += widths[0]
        draw_part("B", x, b_color)
        x += widths[1]
        draw_part("yte", x, yte_color)


# ============================================================
# Button styling
# ============================================================

def make_primary_button(text: str) -> QPushButton:
    b = QPushButton(text)
    b.setFixedSize(BTN_W, BTN_H)
    b.setFont(QFont(FONT_FAMILY, 26, QFont.Weight.Bold))
    b.setStyleSheet("""
        QPushButton {
            background: rgba(255,255,255,0.18);
            color: white;
            border: 2px solid rgba(255,255,255,0.34);
            border-radius: 28px;
            letter-spacing: 1px;
        }
        QPushButton:pressed { background: rgba(255,255,255,0.26); }
    """)
    return b

def make_secondary_button(text: str) -> QPushButton:
    b = QPushButton(text)
    b.setFixedSize(BTN_W, BTN_H)
    b.setFont(QFont(FONT_FAMILY, 24, QFont.Weight.Bold))
    b.setStyleSheet("""
        QPushButton {
            background: rgba(255,255,255,0.94);
            color: #0B7A3B;
            border-radius: 28px;
            letter-spacing: 0.5px;
        }
        QPushButton:pressed { background: rgba(255,255,255,0.78); }
    """)
    return b

def make_small_button(text: str) -> QPushButton:
    b = QPushButton(text)
    b.setFixedSize(SMALL_BTN_W, SMALL_BTN_H)
    b.setFont(QFont(FONT_FAMILY, 18, QFont.Weight.Bold))
    b.setStyleSheet("""
        QPushButton {
            background: rgba(255,255,255,0.94);
            color: #0B7A3B;
            border-radius: 20px;
        }
        QPushButton:pressed { background: rgba(255,255,255,0.78); }
    """)
    return b

def make_card() -> QWidget:
    c = QWidget()
    c.setStyleSheet("""
        QWidget {
            background: rgba(255,255,255,0.16);
            border: 2px solid rgba(255,255,255,0.24);
            border-radius: 36px;
        }
    """)
    return c


# ============================================================
# Optional ONNX Bottle Verifier
# ============================================================

class ONNXBottleVerifier:
    def __init__(self, base_dir: str):
        self.enabled = bool(USE_ONNX_VERIFIER)
        self.available = False
        self.reason = "disabled"
        self.model_path = os.path.join(base_dir, ONNX_MODEL_PATH)
        self._net = None
        self._cap = None
        self._lock = threading.RLock()
        self._last_qimage = None

        if not self.enabled:
            return
        if cv2 is None or np is None:
            self.reason = "opencv or numpy missing"
            return
        if not os.path.exists(self.model_path):
            self.reason = f"model not found: {self.model_path}"
            return

        try:
            self._net = cv2.dnn.readNetFromONNX(self.model_path)
            self.available = True
            self.reason = "ready"
            print(f"[ONNX] loaded {self.model_path}")
        except Exception as e:
            self.reason = str(e)
            print(f"[ONNX] failed to load: {e}")

    def _ensure_camera(self) -> bool:
        if self._cap is not None and self._cap.isOpened():
            return True
        try:
            self._cap = cv2.VideoCapture(CAMERA_INDEX)
            start = time.monotonic()
            while not self._cap.isOpened() and (time.monotonic() - start) < ONNX_OPEN_TIMEOUT_S:
                time.sleep(0.05)
            return self._cap.isOpened()
        except Exception:
            self._cap = None
            return False

    def release(self):
        with self._lock:
            try:
                if self._cap is not None:
                    self._cap.release()
            except Exception:
                pass
            self._cap = None

    def _detect_and_annotate(self, frame):
        h, w = frame.shape[:2]
        blob = cv2.dnn.blobFromImage(
            frame,
            scalefactor=1/255.0,
            size=(ONNX_INPUT_SIZE, ONNX_INPUT_SIZE),
            swapRB=True,
            crop=False
        )
        self._net.setInput(blob)
        outputs = self._net.forward()

        outputs = np.squeeze(outputs)
        if outputs.ndim == 1:
            outputs = np.expand_dims(outputs, 0)
        outputs = outputs.T

        boxes = []
        scores = []
        best_conf = 0.0

        for row in outputs:
            if len(row) < 5:
                continue
            x, y, bw, bh = row[0:4]
            score = float(row[4])
            best_conf = max(best_conf, score)
            if score < ONNX_CONF_THRESHOLD:
                continue

            left = int((x - bw/2) * w / ONNX_INPUT_SIZE)
            top = int((y - bh/2) * h / ONNX_INPUT_SIZE)
            width = int(bw * w / ONNX_INPUT_SIZE)
            height = int(bh * h / ONNX_INPUT_SIZE)

            left = max(0, left)
            top = max(0, top)
            width = max(1, min(width, w - left if left < w else 1))
            height = max(1, min(height, h - top if top < h else 1))

            boxes.append([left, top, width, height])
            scores.append(score)

        detected = False
        if len(boxes) > 0:
            indices = cv2.dnn.NMSBoxes(boxes, scores, ONNX_CONF_THRESHOLD, ONNX_NMS_THRESHOLD)
            if len(indices) > 0:
                detected = True
                for i in indices:
                    i = i[0] if isinstance(i, (tuple, list, np.ndarray)) else i
                    x, y, bw, bh = boxes[i]
                    cv2.rectangle(frame, (x, y), (x + bw, y + bh), (87, 255, 140), 3)
                    cv2.putText(frame, f"Bottle {scores[i]:.2f}", (x, max(28, y - 10)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.75, (87, 255, 140), 2)

        return frame, detected, best_conf

    def _store_qimage(self, frame_bgr):
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape
        bytes_per_line = ch * w
        self._last_qimage = QImage(rgb.data, w, h, bytes_per_line, QImage.Format.Format_RGB888).copy()

    def get_preview_qimage(self):
        if not self.available or self._net is None:
            return None
        with self._lock:
            if not self._ensure_camera():
                return self._last_qimage
            ok, frame = self._cap.read()
            if not ok or frame is None:
                return self._last_qimage
            annotated, _, _ = self._detect_and_annotate(frame)
            self._store_qimage(annotated)
            return self._last_qimage

    def verify_once(self) -> bool:
        if not self.available or self._net is None:
            return True

        with self._lock:
            if not self._ensure_camera():
                print('[ONNX] camera failed to open; allowing ultrasonic fallback')
                return True

            start = time.monotonic()
            frames = 0
            positives = 0
            best = 0.0

            while (time.monotonic() - start) < ONNX_VERIFY_SECONDS:
                ok, frame = self._cap.read()
                if not ok or frame is None:
                    continue
                annotated, detected, conf = self._detect_and_annotate(frame)
                self._store_qimage(annotated)
                frames += 1
                if detected:
                    positives += 1
                best = max(best, conf)
                time.sleep(0.02)

            if frames == 0:
                print('[ONNX] no frames during verify; allowing ultrasonic fallback')
                return True

            ratio = positives / frames
            print(f'[ONNX] verify positives={positives}/{frames} ratio={ratio:.2f} best={best:.2f}')
            return ratio >= ONNX_VERIFY_RATIO

# ============================================================
# Hardware Worker
# ============================================================

class HardwareWorker(QThread):
    ui_mode = pyqtSignal(str)
    dropped = pyqtSignal()
    wake_requested = pyqtSignal()

    def __init__(self, verifier_fn=None):
        super().__init__()
        self.verifier_fn = verifier_fn
        self.running = True
        self.session_enabled = False
        self._verifying = False
        self._latched = False
        self._verify_start = None
        self._pi = None
        self._wake_latched = False
        self._waiting_clear = False
        self._clear_wait_start = None

    def stop(self):
        self.running = False

    def set_session(self, enabled: bool):
        self.session_enabled = enabled
        self._verifying = False
        self._latched = False
        self._wake_latched = False
        self._waiting_clear = False
        self._clear_wait_start = None
        if enabled:
            self.ui_mode.emit("WAITING")

    def _distance_cm(self):
        """Read HC-SR04 distance. Returns None on timeout/noisy read."""
        try:
            GPIO.output(GPIO_TRIG, False)
            time.sleep(0.000002)
            GPIO.output(GPIO_TRIG, True)
            time.sleep(0.00001)
            GPIO.output(GPIO_TRIG, False)

            start_wait = time.monotonic()
            while GPIO.input(GPIO_ECHO) == 0:
                if time.monotonic() - start_wait > ULTRA_TIMEOUT_S:
                    return None
            pulse_start = time.monotonic()

            while GPIO.input(GPIO_ECHO) == 1:
                if time.monotonic() - pulse_start > ULTRA_TIMEOUT_S:
                    return None
            pulse_end = time.monotonic()

            pulse_duration = pulse_end - pulse_start
            distance_cm = pulse_duration * 17150.0
            if 0.5 <= distance_cm <= 400:
                return distance_cm
            return None
        except Exception:
            return None

    def _object_present(self):
        d1 = self._distance_cm()
        if d1 is None:
            return False
        if not (ULTRA_MIN_CM <= d1 <= ULTRA_MAX_CM):
            return False

        # Small second check to reduce random spikes.
        time.sleep(0.01)
        d2 = self._distance_cm()
        if d2 is None:
            return False
        return ULTRA_MIN_CM <= d2 <= ULTRA_MAX_CM

    def run(self):
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)

        GPIO.setup(GPIO_TRIG, GPIO.OUT)
        GPIO.setup(GPIO_ECHO, GPIO.IN)
        GPIO.output(GPIO_TRIG, False)

        self._pi = pigpio.pi()
        if not self._pi.connected:
            raise RuntimeError("pigpio daemon not running. Start it with: sudo pigpiod")

        def servo_pulse(pulse_us, settle_s=0.35, hold=False):
            self._pi.set_servo_pulsewidth(GPIO_SERVO, int(pulse_us))
            time.sleep(settle_s)
            if not hold:
                self._pi.set_servo_pulsewidth(GPIO_SERVO, 0)

        # Always hold the gate flat/closed while idle
        servo_pulse(SERVO_CLOSED_US, hold=True)

        try:
            while self.running:
                ready = self._object_present()

                if not self.session_enabled:
                    self._pi.set_servo_pulsewidth(GPIO_SERVO, int(SERVO_CLOSED_US))
                    if ready and (not self._wake_latched):
                        self._wake_latched = True
                        self.wake_requested.emit()
                    elif not ready:
                        self._wake_latched = False
                    self.msleep(POLL_MS)
                    continue

                if self._waiting_clear:
                    self._pi.set_servo_pulsewidth(GPIO_SERVO, int(SERVO_CLOSED_US))

                    if not ready:
                        self._waiting_clear = False
                        self._latched = False
                        self._clear_wait_start = None
                        self.ui_mode.emit("WAITING")
                    elif self._clear_wait_start is not None and (time.monotonic() - self._clear_wait_start) >= CLEAR_BOTH_TIMEOUT_S:
                        self._waiting_clear = False
                        self._latched = False
                        self._clear_wait_start = None
                        self.ui_mode.emit("WAITING")
                    else:
                        self.ui_mode.emit("DROPPING")

                    self.msleep(POLL_MS)
                    continue

                if ready and (not self._verifying) and (not self._latched):
                    self._verifying = True
                    self._latched = True
                    self._verify_start = time.monotonic()
                    self.ui_mode.emit("VERIFYING")

                if self._verifying:
                    self.ui_mode.emit("VERIFYING")
                    if (time.monotonic() - self._verify_start) >= VERIFY_SECONDS:
                        allow_drop = True
                        if self.verifier_fn is not None:
                            try:
                                allow_drop = bool(self.verifier_fn())
                            except Exception as e:
                                print(f"[VERIFY] verifier callback error: {e}")
                                allow_drop = True

                        self._verifying = False

                        if not allow_drop:
                            self._latched = False
                            self.ui_mode.emit("WAITING")
                        else:
                            self.ui_mode.emit("DROPPING")

                            servo_pulse(SERVO_OPEN_US)
                            time.sleep(GATE_OPEN_MS / 1000.0)
                            servo_pulse(SERVO_CLOSED_US, hold=True)

                            self._waiting_clear = True
                            self._clear_wait_start = time.monotonic()
                            self.dropped.emit()
                else:
                    self._pi.set_servo_pulsewidth(GPIO_SERVO, int(SERVO_CLOSED_US))
                    self.ui_mode.emit("WAITING")

                self.msleep(POLL_MS)

        finally:
            try:
                if self._pi is not None:
                    self._pi.set_servo_pulsewidth(GPIO_SERVO, 0)
                    self._pi.stop()
            except Exception:
                pass
            GPIO.cleanup()


# ============================================================
# Redeem Arrow (glow)

# ============================================================

class BouncingArrow(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._offset = 0.0
        self._dir = 1
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(16)
        self.setFixedHeight(260)

    def _tick(self):
        self._offset += 1.2 * self._dir
        if self._offset > 24:
            self._dir = -1
        elif self._offset < -8:
            self._dir = 1
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        w = self.width()
        h = self.height()

        cx = w * 0.70 + self._offset
        cy = h * 0.53

        arrow_w = 260
        arrow_h = 140

        path = QPainterPath()
        x0 = cx - arrow_w / 2
        y0 = cy - arrow_h / 2

        path.addRoundedRect(float(x0), float(y0 + arrow_h*0.25), float(arrow_w*0.62), float(arrow_h*0.50), 24, 24)

        hx = x0 + arrow_w*0.62
        head = QPainterPath()
        head.moveTo(float(hx), float(y0))
        head.lineTo(float(x0 + arrow_w), float(cy))
        head.lineTo(float(hx), float(y0 + arrow_h))
        head.closeSubpath()
        path = path.united(head)

        glow_pen = QPen(QColor(255, 255, 255, 70), 20)
        p.setPen(glow_pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 190))
        p.drawPath(path)

        edge_pen = QPen(QColor(255, 255, 255, 140), 6)
        p.setPen(edge_pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)


# ============================================================
# QR Widget (scale+fade)
# ============================================================

class QRScaleWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._pm = None
        self._scale = 0.86

        self._opacity = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._opacity)
        self._opacity.setOpacity(0.0)

        self._fade = QPropertyAnimation(self._opacity, b"opacity", self)
        self._fade.setDuration(420)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._scale_anim = QPropertyAnimation(self, b"scale", self)
        self._scale_anim.setDuration(520)
        self._scale_anim.setEasingCurve(QEasingCurve.Type.OutBack)

        self.setFixedSize(720, 720)

    def setPixmap(self, pm: QPixmap):
        self._pm = pm
        self.play()

    def play(self):
        self._fade.stop()
        self._scale_anim.stop()

        self._opacity.setOpacity(0.0)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()

        self._scale = 0.86
        self._scale_anim.setStartValue(0.86)
        self._scale_anim.setEndValue(1.0)
        self._scale_anim.start()

        self.update()

    def getScale(self):
        return self._scale

    def setScale(self, v):
        self._scale = float(v)
        self.update()

    scale = pyqtProperty(float, fget=getScale, fset=setScale)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = self.rect()
        p.setPen(QPen(QColor(255, 255, 255, 70), 2))
        p.setBrush(QColor(255, 255, 255, 36))
        p.drawRoundedRect(rect.adjusted(6, 6, -6, -6), 34, 34)

        if not self._pm:
            return

        base = min(rect.width(), rect.height()) - 100
        size = int(base * self._scale)
        x = (rect.width() - size) // 2
        y = (rect.height() - size) // 2

        pm = self._pm.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        p.drawPixmap(x, y, pm)


# ============================================================
# Screens
# ============================================================

class MainScreen(WaterBackground):
    def __init__(self, kiosk, bottle_png_path: str):
        super().__init__(BG_TOP, BG_BOTTOM, bottle_png_path)
        self.kiosk = kiosk

        root = QVBoxLayout(self)
        root.setContentsMargins(70, 95, 70, 70)
        root.setSpacing(14)

        title = ColoredEcoByteTitle()

        subtitle = QLabel("From Plastic, to Fantastic!")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setFont(QFont(FONT_FAMILY, 28))
        subtitle.setStyleSheet("color: rgba(255,255,255,0.88);")

        card = make_card()
        card.setFixedHeight(410)
        card.setFixedWidth(860)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(56, 44, 56, 44)
        cl.setSpacing(18)

        start_btn = make_primary_button("START")
        redeem_btn = make_secondary_button("REDEEM LOAD")

        start_btn.clicked.connect(self.kiosk.start_session)
        redeem_btn.clicked.connect(self.kiosk.go_redeem)
        start_btn.clicked.connect(self.kiosk.snd.tap)
        redeem_btn.clicked.connect(self.kiosk.snd.tap)

        cl.addStretch(1)
        cl.addWidget(start_btn, alignment=Qt.AlignmentFlag.AlignCenter)
        cl.addWidget(redeem_btn, alignment=Qt.AlignmentFlag.AlignCenter)
        cl.addStretch(1)

        root.addStretch(2)
        root.addWidget(title, alignment=Qt.AlignmentFlag.AlignCenter)
        root.addWidget(subtitle)
        root.addSpacing(10)
        root.addWidget(card, alignment=Qt.AlignmentFlag.AlignCenter)
        root.addStretch(3)

        self._corner = SecretExitCorner(self.kiosk.exit_app, self)
        self._corner.move(0, 0)


class DepositScreen(WaterBackground):
    def __init__(self, kiosk, bottle_png_path: str, verifier=None):
        super().__init__(BG_TOP, BG_BOTTOM, bottle_png_path)
        self.kiosk = kiosk
        self.verifier = verifier

        root = QVBoxLayout(self)
        root.setContentsMargins(70, 70, 70, 50)
        root.setSpacing(12)

        header = QLabel("Insert Bottle")
        header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.setFont(QFont(FONT_FAMILY, 60, QFont.Weight.Bold))
        header.setStyleSheet("color: rgba(255,255,255,0.98);")

        self.status = QLabel("Waiting for bottle...")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setFont(QFont(FONT_FAMILY, 28))
        self.status.setStyleSheet("color: rgba(255,255,255,0.90);")

        card = make_card()
        card.setFixedHeight(650)
        card.setFixedWidth(900)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(40, 34, 40, 34)
        cl.setSpacing(16)

        self.camera_title = QLabel("Camera Verification")
        self.camera_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.camera_title.setFont(QFont(FONT_FAMILY, 22, QFont.Weight.Bold))
        self.camera_title.setStyleSheet("color: rgba(255,255,255,0.90);")

        self.camera_label = QLabel()
        self.camera_label.setFixedSize(760, 300)
        self.camera_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.camera_label.setStyleSheet(
            "background: rgba(0,0,0,0.22); border: 2px solid rgba(255,255,255,0.18); border-radius: 24px; color: rgba(255,255,255,0.70);"
        )
        self.camera_label.setText("Camera preview will appear here")

        self.bottles_lbl = AnimatedNumberLabel("Bottles: ")
        self.bottles_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.bottles_lbl.setFont(QFont(FONT_FAMILY, 48, QFont.Weight.Bold))
        self.bottles_lbl.setStyleSheet("color: rgba(255,255,255,0.98);")

        self.points_lbl = AnimatedNumberLabel("EcoPoints: ")
        self.points_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.points_lbl.setFont(QFont(FONT_FAMILY, 42, QFont.Weight.Bold))
        self.points_lbl.setStyleSheet("color: rgba(232,255,242,1);")

        cl.addWidget(self.camera_title)
        cl.addWidget(self.camera_label, alignment=Qt.AlignmentFlag.AlignCenter)
        cl.addSpacing(8)
        cl.addWidget(self.bottles_lbl)
        cl.addWidget(self.points_lbl)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(18)
        back_btn = make_small_button("BACK")
        finish_btn = make_small_button("FINISH")

        back_btn.clicked.connect(self.kiosk.confirm_back_from_deposit)
        finish_btn.clicked.connect(self.kiosk.finish_session)
        back_btn.clicked.connect(self.kiosk.snd.tap)
        finish_btn.clicked.connect(self.kiosk.snd.tap)

        btn_row.addStretch(1)
        btn_row.addWidget(back_btn)
        btn_row.addWidget(finish_btn)
        btn_row.addStretch(1)

        root.addWidget(header)
        root.addWidget(self.status)
        root.addWidget(card, alignment=Qt.AlignmentFlag.AlignCenter)
        root.addStretch(1)
        root.addLayout(btn_row)

        self._preview_timer = QTimer(self)
        self._preview_timer.timeout.connect(self._update_preview)
        self._preview_timer.start(90)

        self._corner = SecretExitCorner(self.kiosk.exit_app, self)
        self._corner.move(0, 0)

    def _update_preview(self):
        if self.verifier is None or not getattr(self.verifier, 'available', False):
            return
        qimg = self.verifier.get_preview_qimage()
        if qimg is None or qimg.isNull():
            return
        pm = QPixmap.fromImage(qimg).scaled(
            self.camera_label.size(),
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation
        )
        self.camera_label.setPixmap(pm)

    def set_mode(self, mode: str):
        if mode == "WAITING":
            self.status.setText("Waiting for bottle...")
        elif mode == "VERIFYING":
            self.status.setText("Confirming bottle... Please hold it steady")
        elif mode == "DROPPING":
            self.status.setText("Bottle accepted. Processing...")

    def animate_counts(self, bottles: int):
        self.bottles_lbl.animate_to(bottles)
        self.points_lbl.animate_to(bottles * POINTS_PER_BOTTLE)


class QRScreen(WaterBackground):
    def __init__(self, kiosk, bottle_png_path: str):
        super().__init__(BG_TOP, BG_BOTTOM, bottle_png_path)
        self.kiosk = kiosk

        root = QVBoxLayout(self)
        root.setContentsMargins(70, 70, 70, 60)
        root.setSpacing(12)

        title = QLabel("Scan to Collect EcoPoints")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Font size adjusted and native margins added
        title.setFont(QFont(FONT_FAMILY, 42, QFont.Weight.Bold))
        title.setStyleSheet("color: rgba(255,255,255,0.98);")
        title.setContentsMargins(40, 0, 40, 0)

        self.subtitle = QLabel("")
        self.subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.subtitle.setWordWrap(True)
        self.subtitle.setFont(QFont(FONT_FAMILY, 30))
        self.subtitle.setStyleSheet("color: rgba(255,255,255,0.92);")
        self.subtitle.setContentsMargins(40, 0, 40, 0)

        self.qr_widget = QRScaleWidget()

        done_btn = make_small_button("DONE")
        done_btn.clicked.connect(self.kiosk.go_main)
        done_btn.clicked.connect(self.kiosk.snd.tap)

        root.addStretch(1)
        root.addWidget(title)
        root.addWidget(self.subtitle)
        root.addWidget(self.qr_widget, alignment=Qt.AlignmentFlag.AlignCenter)
        root.addStretch(1)
        root.addWidget(done_btn, alignment=Qt.AlignmentFlag.AlignCenter)

        self._corner = SecretExitCorner(self.kiosk.exit_app, self)
        self._corner.move(0, 0)

    def set_qr(self, payload_text: str, bottles: int):
        pts = bottles * POINTS_PER_BOTTLE
        self.subtitle.setText(f"Bottles: {bottles}  -  EcoPoints: {pts}")
        pm = qr_pixmap_from_text(payload_text, size_px=560)
        self.qr_widget.setPixmap(pm)


class RedeemScreen(WaterBackground):
    scanned_text = pyqtSignal(str)

    def __init__(self, kiosk, bottle_png_path: str):
        super().__init__(BG_TOP, BG_BOTTOM, bottle_png_path)
        self.kiosk = kiosk

        root = QVBoxLayout(self)
        root.setContentsMargins(70, 85, 70, 60)
        root.setSpacing(12)

        title = QLabel("Redeem Load")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setFont(QFont(FONT_FAMILY, 68, QFont.Weight.Bold))
        title.setStyleSheet("color: rgba(255,255,255,0.98);")

        card = make_card()
        card.setFixedHeight(620)
        card.setFixedWidth(860)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(56, 52, 56, 52)
        cl.setSpacing(12)

        instr = QLabel("Show your Redeem QR Code\non the EcoByte MIT App\nthen scan it on the RIGHT.")
        instr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        instr.setWordWrap(True)
        instr.setFont(QFont(FONT_FAMILY, 34))
        instr.setStyleSheet("color: rgba(255,255,255,0.92);")
        instr.setContentsMargins(18, 0, 18, 0)

        self.status = QLabel("Scanner ready...")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setFont(QFont(FONT_FAMILY, 26))
        self.status.setStyleSheet("color: rgba(255,255,255,0.82);")

        arrow = BouncingArrow()

        self._input = QLineEdit()
        self._input.setFixedSize(2, 2)
        self._input.setStyleSheet("background: transparent; border: none; color: transparent;")
        self._input.returnPressed.connect(self._on_return)

        self._input.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, False)
        self._input.setInputMethodHints(
            Qt.InputMethodHint.ImhNoAutoUppercase |
            Qt.InputMethodHint.ImhNoPredictiveText |
            Qt.InputMethodHint.ImhHiddenText
        )

        cl.addStretch(1)
        cl.addWidget(instr)
        cl.addSpacing(6)
        cl.addWidget(arrow)
        cl.addWidget(self.status)
        cl.addWidget(self._input, alignment=Qt.AlignmentFlag.AlignLeft)
        cl.addStretch(1)

        back_btn = make_small_button("BACK")
        back_btn.clicked.connect(self.kiosk.confirm_back_from_deposit)
        back_btn.clicked.connect(self.kiosk.snd.tap)

        root.addStretch(1)
        root.addWidget(title)
        root.addWidget(card, alignment=Qt.AlignmentFlag.AlignCenter)
        root.addStretch(1)
        root.addWidget(back_btn, alignment=Qt.AlignmentFlag.AlignCenter)

        self._corner = SecretExitCorner(self.kiosk.exit_app, self)
        self._corner.move(0, 0)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(80, self._focus_input)

    def _focus_input(self):
        self._input.clear()
        self._input.setFocus(Qt.FocusReason.OtherFocusReason)

    def _on_return(self):
        s = self._input.text().strip()
        self._input.clear()
        if s:
            self.scanned_text.emit(s)
        self._focus_input()

    def set_scanned_ok(self):
        self.status.setText("SCANNED OK")
        self.status.setStyleSheet("color: rgba(232,255,242,1);")

    def set_scanned_bad(self):
        self.status.setText("INVALID / USED X")
        self.status.setStyleSheet("color: rgba(255,220,220,1);")


# ============================================================
# Idle activity watcher
# ============================================================

class IdleEventFilter(QObject):
    activity = pyqtSignal()

    def eventFilter(self, obj, event):
        et = event.type()
        if et in (QEvent.Type.MouseButtonPress,
                  QEvent.Type.MouseMove,
                  QEvent.Type.TouchBegin,
                  QEvent.Type.TouchUpdate,
                  QEvent.Type.TouchEnd,
                  QEvent.Type.KeyPress):
            self.activity.emit()
        return False



# ============================================================
# LED Controller (WS2812B)
# ============================================================

class LEDController(QObject):
    """Non-blocking LED controller for a WS2812B strip.

    Behavior:
      - Ready/Idle (WAITING): solid green
      - Busy (VERIFYING/DROPPING): solid red
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self._pixels = None

        if neopixel is None or board is None:
            print("[LED] neopixel/board not available - LEDs disabled")
            return

        try:
            self._pixels = neopixel.NeoPixel(
                board.D18, LED_COUNT,
                brightness=LED_BRIGHTNESS,
                auto_write=True,
                pixel_order=neopixel.GRB
            )
            self.set_idle()
        except Exception as e:
            print("[LED] init failed - LEDs disabled:", e)
            self._pixels = None

    def _set_color(self, rgb):
        if self._pixels is None:
            return
        try:
            self._pixels.fill(rgb)
        except Exception as e:
            print("[LED] write error:", e)

    def set_idle(self):
        self._set_color(LED_IDLE_COLOR)

    def set_busy(self):
        self._set_color(LED_VERIFY_COLOR)

    def set_off(self):
        self._set_color((0, 0, 0))

    def apply_mode(self, mode: str):
        # mode comes from HardwareWorker.ui_mode
        if mode == "WAITING":
            self.set_idle()
        elif mode in ("VERIFYING", "DROPPING"):
            self.set_busy()
        else:
            # safe default
            self.set_idle()

# ============================================================
# Themed Confirm Dialog
# ============================================================

class ThemedConfirmDialog(QDialog):
    def __init__(self, parent, title: str, message: str, yes_text: str = "Yes", no_text: str = "No"):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setModal(True)
        self.setObjectName("ThemedConfirmDialog")
        self.setFixedSize(760, 360)

        root = QVBoxLayout(self)
        root.setContentsMargins(34, 30, 34, 26)
        root.setSpacing(18)

        title_lbl = QLabel(title)
        title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_lbl.setFont(QFont(FONT_FAMILY, 28, QFont.Weight.Bold))
        title_lbl.setStyleSheet("color: rgba(255,255,255,0.98);")

        msg_lbl = QLabel(message)
        msg_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        msg_lbl.setWordWrap(True)
        msg_lbl.setFont(QFont(FONT_FAMILY, 21))
        msg_lbl.setStyleSheet("color: rgba(255,255,255,0.92);")

        btn_row = QHBoxLayout()
        btn_row.setSpacing(16)
        btn_row.addStretch(1)

        no_btn = make_small_button(no_text)
        yes_btn = make_small_button(yes_text)
        no_btn.clicked.connect(self.reject)
        yes_btn.clicked.connect(self.accept)

        btn_row.addWidget(no_btn)
        btn_row.addWidget(yes_btn)
        btn_row.addStretch(1)

        root.addStretch(1)
        root.addWidget(title_lbl)
        root.addWidget(msg_lbl)
        root.addStretch(1)
        root.addLayout(btn_row)

        self.setStyleSheet("""
            QDialog#ThemedConfirmDialog {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                            stop:0 rgba(35,175,215,245),
                                            stop:1 rgba(35,215,190,245));
                border: 2px solid rgba(255,255,255,0.24);
                border-radius: 30px;
            }
        """)

# ============================================================
# Kiosk Controller
# ============================================================

class Kiosk(QStackedWidget):
    def __init__(self, verifier_fn=None):
        super().__init__()
        self.verifier_fn = verifier_fn

        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.showFullScreen()
        self.setCursor(Qt.CursorShape.BlankCursor)

        screen = QApplication.primaryScreen().geometry()
        self.setGeometry(screen)
        self.setFixedSize(screen.width(), screen.height())

        base_dir = os.path.dirname(os.path.abspath(__file__))
        try_load_tt_hoves(base_dir)

        self.snd = SoundManager(base_dir)
        self.snd.play_idle()

        self.fb = FirebaseClient(FIREBASE_URL)

        self.verifier = ONNXBottleVerifier(base_dir)
        if self.verifier.enabled:
            print(f"[ONNX] verifier status: {self.verifier.reason}")
        self.fb.log.connect(lambda s: print("[Firebase]", s))
        self.fb.ok.connect(self._on_firebase_ok)

        bottle_png = os.path.join(base_dir, "assets", "bottle.png")

        self.main = MainScreen(self, bottle_png)
        self.deposit = DepositScreen(self, bottle_png, self.verifier)
        self.qr = QRScreen(self, bottle_png)
        self.redeem = RedeemScreen(self, bottle_png)

        for page in (self.main, self.deposit, self.qr, self.redeem):
            page.setFixedSize(screen.width(), screen.height())

        self.addWidget(self.main)
        self.addWidget(self.deposit)
        self.addWidget(self.qr)
        self.addWidget(self.redeem)
        self.setCurrentWidget(self.main)

        self.session_bottles = 0

        # LEDs: solid green = ready/idle, solid red = busy
        self.led = LEDController(self)
        self.led.set_idle()  # always green on startup/main screen

        self.worker = HardwareWorker(verifier_fn=self.verifier.verify_once if self.verifier.available else None)
        self.worker.ui_mode.connect(self.deposit.set_mode)
        self.worker.ui_mode.connect(self.led.apply_mode)
        self.worker.dropped.connect(self.on_bottle_dropped)
        self.worker.wake_requested.connect(self.start_session_from_sensor)
        self.worker.start()

        self.redeem.scanned_text.connect(self.on_redeem_scanned)

        self.idle_timer = QTimer(self)
        self.idle_timer.timeout.connect(self.go_main)

        self.idle_indicator = IdleRing(self)
        self.idle_indicator.raise_()

        self._idle_visual_timer = QTimer(self)
        self._idle_visual_timer.timeout.connect(self.update_idle_indicator)
        self._idle_visual_timer.start(100)

        self.reset_idle()

        self._filter = IdleEventFilter()
        self._filter.activity.connect(self.reset_idle)
        QApplication.instance().installEventFilter(self._filter)

    def reset_idle(self):
        self.idle_timer.start(IDLE_TIMEOUT_MS)
        self.update_idle_indicator()

    def update_idle_indicator(self):
        if not hasattr(self, "idle_indicator"):
            return
        remaining = self.idle_timer.remainingTime()
        if remaining < 0:
            remaining = IDLE_TIMEOUT_MS
        self.idle_indicator.set_countdown(remaining, IDLE_TIMEOUT_MS)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not hasattr(self, "idle_indicator"):
            return
        margin = 22
        self.idle_indicator.move(self.width() - self.idle_indicator.width() - margin, margin)
        self.idle_indicator.raise_()

    def confirm_back_from_deposit(self):
        self.snd.tap()

        current = self.currentWidget()
        if current not in (self.deposit, self.redeem):
            self.go_main()
            return

        if current is self.redeem:
            dlg = ThemedConfirmDialog(
                self,
                "Go Back?",
                "Are you sure you want to go back to the home screen?",
                yes_text="YES",
                no_text="NO"
            )
            if dlg.exec() == QDialog.DialogCode.Accepted:
                self.go_main()
            else:
                self.reset_idle()
            return

        if self.session_bottles <= 0:
            self.go_main()
            return

        dlg = ThemedConfirmDialog(
            self,
            "Leave This Session?",
            "Are you sure you want to go back?\nAll pending EcoPoints from this session will be lost.",
            yes_text="YES",
            no_text="NO"
        )

        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.go_main()
        else:
            self.reset_idle()

    def go_main(self):
        self.worker.set_session(False)
        self.led.set_idle()
        self.session_bottles = 0
        self.deposit.animate_counts(0)
        self.setCurrentWidget(self.main)
        self.reset_idle()

    def start_session_from_sensor(self):
        if self.currentWidget() is self.main:
            self.start_session()

    def start_session(self):
        self.session_bottles = 0
        self.deposit.animate_counts(0)
        self.worker.set_session(True)
        self.setCurrentWidget(self.deposit)
        self.reset_idle()

    def go_redeem(self):
        self.worker.set_session(False)
        self.led.set_idle()
        self.setCurrentWidget(self.redeem)
        self.reset_idle()

    def on_bottle_dropped(self):
        self.session_bottles += 1
        self.deposit.animate_counts(self.session_bottles)

        self.snd.bottle()
        QTimer.singleShot(40, self.snd.success)

        self.reset_idle()

    def finish_session(self):
        self.worker.set_session(False)
        self.led.set_idle()

        bottles = self.session_bottles
        points = bottles * POINTS_PER_BOTTLE
        if bottles <= 0:
            self.go_main()
            return

        token_id = str(uuid.uuid4())
        payload = {
            "type": "deposit",
            "token": token_id,
            "bottles": bottles,
            "points": points,
            "ts": int(time.time()),
            "used": False
        }
        payload_text = json.dumps(payload, separators=(",", ":"))

        self.qr.set_qr(payload_text, bottles)
        self.setCurrentWidget(self.qr)
        QTimer.singleShot(80, self.snd.qr_show)

        self.reset_idle()
        self.fb.create_deposit_token_async(token_id, payload)

    def on_redeem_scanned(self, scanned: str):
        self.reset_idle()
        self.snd.tap()

        token = scanned
        try:
            if scanned.startswith("{") and scanned.endswith("}"):
                obj = json.loads(scanned)
                token = obj.get("token") or obj.get("id") or scanned
        except Exception:
            token = scanned

        token = token.strip()
        self.redeem.status.setText("Checking token...")
        self.redeem.status.setStyleSheet("color: rgba(255,255,255,0.82);")

        self.fb.consume_redeem_token_async(token)

    def _on_firebase_ok(self, msg: str):
        if msg == "deposit_saved":
            return

        if msg == "redeem_ok":
            self.redeem.set_scanned_ok()
            self.snd.success()
            QTimer.singleShot(1800, self.go_main)
            return

        if msg == "redeem_invalid":
            self.redeem.set_scanned_bad()
            QTimer.singleShot(2200, self.go_main)
            return

    def exit_app(self):
        try:
            self.led.set_off()
        except Exception:
            pass
        try:
            self.snd.stop_idle()
        except Exception:
            pass
        try:
            self.worker.stop()
            self.worker.wait(1200)
        except Exception:
            pass
        try:
            self.verifier.release()
        except Exception:
            pass
        QApplication.instance().quit()


# ============================================================
# Run
# ============================================================

if __name__ == "__main__":
    app = QApplication(sys.argv)

    base_dir = os.path.dirname(os.path.abspath(__file__))
    try_load_tt_hoves(base_dir)
    app.setFont(QFont(FONT_FAMILY, 18))

    kiosk = Kiosk()
    kiosk.show()
    sys.exit(app.exec())
