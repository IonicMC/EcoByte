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
import qrcode
import requests

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
    QGraphicsOpacityEffect, QLineEdit
)

# -----------------------------
# pygame audio (NO QtMultimedia)
# -----------------------------
import pygame


# ============================================================
# CONFIG
# ============================================================

IDLE_TIMEOUT_MS = 15000

# Hardware pins (BCM)
GPIO_CAP = 17
GPIO_TRIG = 23
GPIO_ECHO = 24
GPIO_SERVO = 16

DIST_THRESHOLD_CM = 10.0
VERIFY_SECONDS = 2.0
POLL_MS = 30

SERVO_CLOSED_DEG = 0
SERVO_OPEN_DEG = 90
GATE_OPEN_MS = 900

POINTS_PER_BOTTLE = 5

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
            pygame.mixer.init()
            pygame.mixer.set_num_channels(16)
            self.ch_ui = pygame.mixer.Channel(1)
            self.ch_fx = pygame.mixer.Channel(2)
            self.ch_success = pygame.mixer.Channel(3)
            self.ok = True
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
# Hardware Worker
# ============================================================

class HardwareWorker(QThread):
    ui_mode = pyqtSignal(str)
    dropped = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.running = True
        self.session_enabled = False
        self._verifying = False
        self._latched = False
        self._verify_start = None
        self._servo_pwm = None

    def stop(self):
        self.running = False

    def set_session(self, enabled: bool):
        self.session_enabled = enabled
        self._verifying = False
        self._latched = False
        if enabled:
            self.ui_mode.emit("WAITING")

    def _read_distance_cm(self) -> float:
        GPIO.output(GPIO_TRIG, 0)
        time.sleep(0.0002)
        GPIO.output(GPIO_TRIG, 1)
        time.sleep(0.00001)
        GPIO.output(GPIO_TRIG, 0)

        timeout = 0.03
        start_wait = time.monotonic()
        while GPIO.input(GPIO_ECHO) == 0:
            if time.monotonic() - start_wait > timeout:
                return float("inf")

        pulse_start = time.monotonic()
        while GPIO.input(GPIO_ECHO) == 1:
            if time.monotonic() - pulse_start > timeout:
                return float("inf")

        pulse_end = time.monotonic()
        pulse = pulse_end - pulse_start
        return (pulse * 34300.0) / 2.0

    def run(self):
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)

        GPIO.setup(GPIO_CAP, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
        GPIO.setup(GPIO_TRIG, GPIO.OUT)
        GPIO.setup(GPIO_ECHO, GPIO.IN)
        GPIO.setup(GPIO_SERVO, GPIO.OUT)

        self._servo_pwm = GPIO.PWM(GPIO_SERVO, 50)
        self._servo_pwm.start(0)

        def servo(angle):
            duty = angle_to_duty(angle)
            self._servo_pwm.ChangeDutyCycle(duty)
            time.sleep(0.25)
            self._servo_pwm.ChangeDutyCycle(0)

        servo(SERVO_CLOSED_DEG)

        try:
            while self.running:
                if not self.session_enabled:
                    self.msleep(POLL_MS)
                    continue

                cap = (GPIO.input(GPIO_CAP) == 1)
                dist = self._read_distance_cm()
                ultrasonic = (dist != float("inf")) and (dist < DIST_THRESHOLD_CM)
                ready = cap and ultrasonic

                if ready and (not self._verifying) and (not self._latched):
                    self._verifying = True
                    self._latched = True
                    self._verify_start = time.monotonic()
                    self.ui_mode.emit("VERIFYING")

                if self._verifying:
                    self.ui_mode.emit("VERIFYING")
                    if (time.monotonic() - self._verify_start) >= VERIFY_SECONDS:
                        self._verifying = False
                        self.ui_mode.emit("DROPPING")

                        servo(SERVO_OPEN_DEG)
                        time.sleep(GATE_OPEN_MS / 1000.0)
                        servo(SERVO_CLOSED_DEG)

                        self._latched = False
                        self.dropped.emit()
                else:
                    self.ui_mode.emit("WAITING")

                self.msleep(POLL_MS)

        finally:
            try:
                self._servo_pwm.stop()
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
    def __init__(self, kiosk, bottle_png_path: str):
        super().__init__(BG_TOP, BG_BOTTOM, bottle_png_path)
        self.kiosk = kiosk

        root = QVBoxLayout(self)
        root.setContentsMargins(70, 85, 70, 65)
        root.setSpacing(14)

        header = QLabel("Insert Bottle")
        header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.setFont(QFont(FONT_FAMILY, 68, QFont.Weight.Bold))
        header.setStyleSheet("color: rgba(255,255,255,0.98);")

        self.status = QLabel("Waiting for bottle…")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setFont(QFont(FONT_FAMILY, 34))
        self.status.setStyleSheet("color: rgba(255,255,255,0.90);")

        card = make_card()
        card.setFixedHeight(470)
        card.setFixedWidth(860)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(56, 54, 56, 54)
        cl.setSpacing(10)

        self.bottles_lbl = AnimatedNumberLabel("Bottles: ")
        self.bottles_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.bottles_lbl.setFont(QFont(FONT_FAMILY, 58, QFont.Weight.Bold))
        self.bottles_lbl.setStyleSheet("color: rgba(255,255,255,0.98);")

        self.points_lbl = AnimatedNumberLabel("EcoPoints: ")
        self.points_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.points_lbl.setFont(QFont(FONT_FAMILY, 52, QFont.Weight.Bold))
        self.points_lbl.setStyleSheet("color: rgba(232,255,242,1);")

        cl.addStretch(1)
        cl.addWidget(self.bottles_lbl)
        cl.addWidget(self.points_lbl)
        cl.addStretch(1)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(18)
        back_btn = make_small_button("BACK")
        finish_btn = make_small_button("FINISH")

        back_btn.clicked.connect(self.kiosk.go_main)
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

        self._corner = SecretExitCorner(self.kiosk.exit_app, self)
        self._corner.move(0, 0)

    def set_mode(self, mode: str):
        if mode == "WAITING":
            self.status.setText("Waiting for bottle…")
        elif mode == "VERIFYING":
            self.status.setText("Confirming… Please wait")
        elif mode == "DROPPING":
            self.status.setText("Processing…")

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

        # ✅ FIX 1: Added a blank space inside the quotes at the beginning and end
        title = QLabel(" Scan to Collect EcoPoints ")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        # ✅ FIX 2: Dropped font size slightly from 56 to 52 to ensure it fits the screen width
        title.setFont(QFont(FONT_FAMILY, 52, QFont.Weight.Bold))
        
        # ✅ FIX 3: Removed the CSS padding completely
        title.setStyleSheet("color: rgba(255,255,255,0.98);")

        self.subtitle = QLabel("")
        self.subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.subtitle.setWordWrap(True)
        self.subtitle.setFont(QFont(FONT_FAMILY, 30))
        # Removed padding here too
        self.subtitle.setStyleSheet("color: rgba(255,255,255,0.92);")
        self.subtitle.setContentsMargins(20, 0, 20, 0)

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
        self.subtitle.setText(f"Bottles: {bottles}  •  EcoPoints: {pts}")
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

        self.status = QLabel("Scanner ready…")
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
        back_btn.clicked.connect(self.kiosk.go_main)
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
        self.status.setText("SCANNED ✓")
        self.status.setStyleSheet("color: rgba(232,255,242,1);")

    def set_scanned_bad(self):
        self.status.setText("INVALID / USED ✕")
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
# Kiosk Controller
# ============================================================

class Kiosk(QStackedWidget):
    def __init__(self):
        super().__init__()

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
        self.fb.log.connect(lambda s: print("[Firebase]", s))
        self.fb.ok.connect(self._on_firebase_ok)

        bottle_png = os.path.join(base_dir, "assets", "bottle.png")

        self.main = MainScreen(self, bottle_png)
        self.deposit = DepositScreen(self, bottle_png)
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

        self.worker = HardwareWorker()
        self.worker.ui_mode.connect(self.deposit.set_mode)
        self.worker.dropped.connect(self.on_bottle_dropped)
        self.worker.start()

        self.redeem.scanned_text.connect(self.on_redeem_scanned)

        self.idle_timer = QTimer(self)
        self.idle_timer.timeout.connect(self.go_main)
        self.reset_idle()

        self._filter = IdleEventFilter()
        self._filter.activity.connect(self.reset_idle)
        QApplication.instance().installEventFilter(self._filter)

    def reset_idle(self):
        self.idle_timer.start(IDLE_TIMEOUT_MS)

    def go_main(self):
        self.worker.set_session(False)
        self.session_bottles = 0
        self.deposit.animate_counts(0)
        self.setCurrentWidget(self.main)
        self.reset_idle()

    def start_session(self):
        self.session_bottles = 0
        self.deposit.animate_counts(0)
        self.worker.set_session(True)
        self.setCurrentWidget(self.deposit)
        self.reset_idle()

    def go_redeem(self):
        self.worker.set_session(False)
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
        self.redeem.status.setText("Checking token…")
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
            self.snd.stop_idle()
        except Exception:
            pass
        try:
            self.worker.stop()
            self.worker.wait(1200)
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
