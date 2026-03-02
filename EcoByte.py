import os
# --- Qt on Raspberry Pi (avoid Wayland theme/plugin issues) ---
os.environ["QT_QPA_PLATFORM"] = "xcb"
os.environ["QT_QPA_PLATFORMTHEME"] = ""
os.environ["QT_LOGGING_RULES"] = "*.debug=false;qt.qpa.*=false"

import sys
import time
import uuid
import json
import math
import random

import RPi.GPIO as GPIO
import qrcode

from PyQt6.QtCore import (
    Qt, QTimer, QThread, pyqtSignal, pyqtProperty, QPropertyAnimation, QEasingCurve
)
from PyQt6.QtGui import (
    QFont, QPainter, QLinearGradient, QColor, QImage, QPixmap, QPainterPath, QPen
)
from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton,
    QVBoxLayout, QHBoxLayout, QStackedWidget,
    QGraphicsOpacityEffect, QLineEdit
)

# --- Audio (no PyQt6.QtMultimedia needed) ---
import pygame


# ============================================================
# CONFIG
# ============================================================

IDLE_TIMEOUT_MS = 15000

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
WATER_FPS_MS = 33           # ~30fps
WAVE_SPEED = 0.075
WAVE_OPACITY = 0.18

# Falling bottles
BOTTLE_COUNT = 10
BOTTLE_ALPHA = 38
BOTTLE_SPEED_MIN = 0.7
BOTTLE_SPEED_MAX = 1.8

# Sound files (put them in /home/rayshan/EcoByte/sounds/ OR same folder as EcoByte.py)
SOUNDS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sounds")
SND_IDLE = "idle.wav"
SND_CLICK = "click.wav"
SND_QR_SHOW = "qr_show.wav"
SND_SUCCESS = "success.wav"


# ============================================================
# Helpers
# ============================================================

def clamp(x, lo, hi):
    return max(lo, min(hi, x))

def angle_to_duty(angle_deg: float) -> float:
    a = clamp(angle_deg, 0, 180)
    return 2.5 + (a / 180.0) * 10.0

def _sound_path(name: str) -> str:
    p1 = os.path.join(SOUNDS_DIR, name)
    if os.path.isfile(p1):
        return p1
    p2 = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
    return p2

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

def set_font_tthoves(lbl: QLabel, px: int, bold: bool = False):
    f = QFont("TT Hoves")
    f.setPixelSize(px)
    if bold:
        f.setWeight(QFont.Weight.Bold)
    lbl.setFont(f)


# ============================================================
# Audio Manager (pygame)
# ============================================================

class Audio:
    def __init__(self):
        self.ok = False
        self.idle_channel = None
        self.sfx_channel = None
        self._idle_loaded = None

        try:
            # Smaller buffer can reduce latency; too small can crackle.
            pygame.mixer.pre_init(frequency=44100, size=-16, channels=2, buffer=1024)
            pygame.init()
            pygame.mixer.init()
            self.ok = True
            self.idle_channel = pygame.mixer.Channel(0)
            self.sfx_channel = pygame.mixer.Channel(1)
        except Exception as e:
            print("Audio init failed:", e)
            self.ok = False

    def play_idle(self):
        if not self.ok:
            return
        try:
            path = _sound_path(SND_IDLE)
            if not os.path.isfile(path):
                return
            if self._idle_loaded != path:
                self._idle_loaded = path
                self._idle_sound = pygame.mixer.Sound(path)
            if not self.idle_channel.get_busy():
                self.idle_channel.play(self._idle_sound, loops=-1)
        except Exception as e:
            print("Idle play error:", e)

    def stop_idle(self):
        if not self.ok:
            return
        try:
            self.idle_channel.stop()
        except Exception:
            pass

    def sfx(self, name: str, volume: float = 1.0):
        if not self.ok:
            return
        try:
            path = _sound_path(name)
            if not os.path.isfile(path):
                return
            s = pygame.mixer.Sound(path)
            s.set_volume(clamp(volume, 0.0, 1.0))
            self.sfx_channel.play(s)
        except Exception as e:
            print("SFX error:", e)


# ============================================================
# Animated Background: Waves + Falling Bottles
# ============================================================

class _BottleParticle:
    def __init__(self):
        self.reset(1080, 1920)

    def reset(self, width, height):
        self.x = random.uniform(0.08, 0.92) * width
        self.y = random.uniform(-1.0, 0.2) * height
        self.scale = random.uniform(0.6, 1.15)
        self.speed = random.uniform(BOTTLE_SPEED_MIN, BOTTLE_SPEED_MAX) * (1.0 / self.scale)
        self.sway_phase = random.uniform(0, math.tau)
        self.sway_amp = random.uniform(4, 18)

class WaterBackground(QWidget):
    def __init__(self, top: QColor, bottom: QColor, parent=None):
        super().__init__(parent)
        self._top = top
        self._bottom = bottom
        self._phase = 0.0
        self._bottles = [_BottleParticle() for _ in range(BOTTLE_COUNT)]

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(WATER_FPS_MS)

    def _tick(self):
        self._phase += WAVE_SPEED
        w = max(1, self.width())
        h = max(1, self.height())

        for b in self._bottles:
            b.y += b.speed * 2.0
            if b.y > h + 140:
                b.reset(w, h)
                b.y = -140

        if self._phase > 1e9:
            self._phase = 0.0
        self.update()

    def _draw_bottle(self, p: QPainter, cx: float, cy: float, s: float, alpha: int):
        """
        Shorter neck + "ridges" to avoid the 'glass bottle' look.
        """
        body_w = 44 * s
        body_h = 82 * s
        neck_w = 20 * s
        neck_h = 16 * s   # shorter neck
        cap_h  = 7 * s

        x0 = cx - body_w / 2
        y0 = cy - body_h / 2

        path = QPainterPath()
        path.addRoundedRect(float(x0), float(y0 + neck_h), float(body_w), float(body_h - neck_h), float(12*s), float(12*s))
        path.addRoundedRect(float(cx - neck_w/2), float(y0), float(neck_w), float(neck_h), float(7*s), float(7*s))
        path.addRoundedRect(float(cx - neck_w/2), float(y0 - cap_h), float(neck_w), float(cap_h), float(4*s), float(4*s))

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, alpha))
        p.drawPath(path)

        # highlight strip
        hx = int(cx - body_w * 0.25)
        hy = int(y0 + neck_h + body_h * 0.10)
        hw = int(body_w * 0.12)
        hh = int(body_h * 0.68)
        rr = int(max(2, 6 * s))
        p.setBrush(QColor(255, 255, 255, int(alpha * 0.55)))
        p.drawRoundedRect(hx, hy, hw, hh, rr, rr)

        # ridges (subtle horizontal lines)
        pen = QPen(QColor(255, 255, 255, int(alpha * 0.55)), max(1, int(2*s)))
        p.setPen(pen)
        for i in range(3):
            yy = int(y0 + neck_h + (body_h * (0.30 + i*0.18)))
            xL = int(x0 + body_w * 0.18)
            xR = int(x0 + body_w * 0.82)
            p.drawLine(xL, yy, xR, yy)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        w = self.width()
        h = self.height()

        grad = QLinearGradient(0, 0, 0, h)
        grad.setColorAt(0.0, self._top)
        grad.setColorAt(1.0, self._bottom)
        p.fillRect(self.rect(), grad)

        p.fillRect(0, 0, w, int(h * 0.16), QColor(255, 255, 255, 18))

        for b in self._bottles:
            sway = math.sin((b.y * 0.01) + b.sway_phase + self._phase) * b.sway_amp
            self._draw_bottle(p, b.x + sway, b.y, 0.85 * b.scale, BOTTLE_ALPHA)

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
        self._anim.setDuration(360)
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
# Button styling
# ============================================================

def make_primary_button(text: str) -> QPushButton:
    b = QPushButton(text)
    b.setFixedSize(BTN_W, BTN_H)
    f = QFont("TT Hoves")
    f.setPixelSize(48)
    f.setWeight(QFont.Weight.Bold)
    b.setFont(f)
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
    f = QFont("TT Hoves")
    f.setPixelSize(44)
    f.setWeight(QFont.Weight.Bold)
    b.setFont(f)
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
    f = QFont("TT Hoves")
    f.setPixelSize(30)
    f.setWeight(QFont.Weight.Bold)
    b.setFont(f)
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
# Hardware Worker (cap+ultra latch -> delay -> gate)
# ============================================================

class HardwareWorker(QThread):
    ui_mode = pyqtSignal(str)   # WAITING / SCANNING / DROPPING
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
                    self.ui_mode.emit("SCANNING")

                if self._verifying:
                    self.ui_mode.emit("SCANNING")
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
# Redeem Arrow (glow + bounce)
# ============================================================

class BouncingArrow(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._offset = 0.0
        self._dir = 1
        self._glow_phase = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(16)
        self.setFixedHeight(240)

    def _tick(self):
        self._offset += 1.15 * self._dir
        if self._offset > 22:
            self._dir = -1
        elif self._offset < -6:
            self._dir = 1
        self._glow_phase += 0.06
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        w = self.width()
        h = self.height()

        cx = w * 0.70 + self._offset
        cy = h * 0.52

        arrow_w = 260
        arrow_h = 140

        path = QPainterPath()
        x0 = cx - arrow_w / 2
        y0 = cy - arrow_h / 2

        path.addRoundedRect(float(x0), float(y0 + arrow_h*0.25), float(arrow_w*0.62), float(arrow_h*0.50), 24, 24)

        head = QPainterPath()
        hx = x0 + arrow_w*0.62
        head.moveTo(float(hx), float(y0))
        head.lineTo(float(x0 + arrow_w), float(cy))
        head.lineTo(float(hx), float(y0 + arrow_h))
        head.closeSubpath()
        path = path.united(head)

        # glow
        glow = 120 + int(70 * (0.5 + 0.5*math.sin(self._glow_phase)))
        for k, a in enumerate([glow, glow-25, glow-45]):
            pen = QPen(QColor(255, 255, 255, max(0, a)), 18 - k*5)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)

        # solid
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 190))
        p.drawPath(path)

        pen = QPen(QColor(255, 255, 255, 120), 6)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)


# ============================================================
# QR Widget (scale+fade animation)
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

class EcoByteTitleWidget(QWidget):
    """
    Custom painted title: Eco (bright green) + B (light blue) + yte (blue-green/dark teal)
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(150)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        w = self.width()
        text = "EcoByte"

        font = QFont("TT Hoves")
        font.setPixelSize(112)
        font.setWeight(QFont.Weight.Bold)
        p.setFont(font)

        fm = p.fontMetrics()
        tw = fm.horizontalAdvance(text)
        th = fm.height()
        x = (w - tw) // 2
        y = (self.height() + th) // 2 - fm.descent()

        # draw segments
        eco = "Eco"
        b = "B"
        yte = "yte"

        x0 = x
        p.setPen(QColor(80, 220, 120))     # bright green
        p.drawText(int(x0), int(y), eco)

        x1 = x0 + fm.horizontalAdvance(eco)
        p.setPen(QColor(140, 210, 255))    # light blue
        p.drawText(int(x1), int(y), b)

        x2 = x1 + fm.horizontalAdvance(b)
        p.setPen(QColor(30, 140, 155))     # dark green-blue
        p.drawText(int(x2), int(y), yte)


class MainScreen(WaterBackground):
    def __init__(self, kiosk):
        super().__init__(QColor(18, 170, 205), QColor(40, 215, 145))
        self.kiosk = kiosk

        root = QVBoxLayout(self)
        root.setContentsMargins(60, 80, 60, 70)
        root.setSpacing(14)

        title = EcoByteTitleWidget()
        subtitle = QLabel("From Plastic, to Fantastic!")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        set_font_tthoves(subtitle, 34, False)
        subtitle.setStyleSheet("color: rgba(255,255,255,0.86);")

        card = make_card()
        card.setFixedHeight(390)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(56, 44, 56, 44)
        cl.setSpacing(18)

        start_btn = make_primary_button("START")
        redeem_btn = make_secondary_button("REDEEM LOAD")

        start_btn.clicked.connect(self.kiosk.start_session)
        redeem_btn.clicked.connect(self.kiosk.go_redeem)

        cl.addStretch(1)
        cl.addWidget(start_btn, alignment=Qt.AlignmentFlag.AlignCenter)
        cl.addWidget(redeem_btn, alignment=Qt.AlignmentFlag.AlignCenter)
        cl.addStretch(1)

        # Center the whole block better (use stretches)
        root.addStretch(2)
        root.addWidget(title)
        root.addWidget(subtitle)
        root.addSpacing(10)
        root.addWidget(card, alignment=Qt.AlignmentFlag.AlignCenter)
        root.addStretch(3)

        self._corner = SecretExitCorner(self.kiosk.exit_app, self)
        self._corner.move(0, 0)


class DepositScreen(WaterBackground):
    def __init__(self, kiosk):
        super().__init__(QColor(18, 170, 205), QColor(40, 215, 145))
        self.kiosk = kiosk

        root = QVBoxLayout(self)
        root.setContentsMargins(60, 60, 60, 60)
        root.setSpacing(12)

        header = QLabel("Insert Bottle")
        header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        set_font_tthoves(header, 84, True)
        header.setStyleSheet("color: rgba(255,255,255,0.98);")

        self.status = QLabel("Waiting for bottle…")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        set_font_tthoves(self.status, 40, False)
        self.status.setStyleSheet("color: rgba(255,255,255,0.88);")

        card = make_card()
        card.setFixedHeight(430)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(56, 54, 56, 54)
        cl.setSpacing(10)

        self.bottles_lbl = AnimatedNumberLabel("Bottles: ")
        self.bottles_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        set_font_tthoves(self.bottles_lbl, 72, True)
        self.bottles_lbl.setStyleSheet("color: rgba(255,255,255,0.98);")

        self.points_lbl = AnimatedNumberLabel("EcoPoints: ")
        self.points_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        set_font_tthoves(self.points_lbl, 66, True)
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

        btn_row.addStretch(1)
        btn_row.addWidget(back_btn)
        btn_row.addWidget(finish_btn)
        btn_row.addStretch(1)

        # ✅ FIX: Center the whole content block vertically
        root.addStretch(3)
        root.addWidget(header)
        root.addWidget(self.status)
        root.addWidget(card, alignment=Qt.AlignmentFlag.AlignCenter)
        root.addStretch(2)
        root.addLayout(btn_row)

        self._corner = SecretExitCorner(self.kiosk.exit_app, self)
        self._corner.move(0, 0)

    def set_mode(self, mode: str):
        if mode == "WAITING":
            self.status.setText("Waiting for bottle…")
        elif mode == "SCANNING":
            self.status.setText("Scanning… Please wait")
        elif mode == "DROPPING":
            self.status.setText("Processing…")

    def animate_counts(self, bottles: int):
        self.bottles_lbl.animate_to(bottles)
        self.points_lbl.animate_to(bottles * POINTS_PER_BOTTLE)


class QRScreen(WaterBackground):
    def __init__(self, kiosk):
        super().__init__(QColor(18, 170, 205), QColor(40, 215, 145))
        self.kiosk = kiosk

        root = QVBoxLayout(self)
        root.setContentsMargins(60, 60, 60, 60)
        root.setSpacing(10)

        # ✅ FIX: Make sure it never clips/cuts: wordWrap + slightly smaller + extra top space
        title = QLabel("Scan to Collect EcoPoints")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setWordWrap(True)
        set_font_tthoves(title, 62, True)
        title.setStyleSheet("color: rgba(255,255,255,0.98);")

        self.subtitle = QLabel("")
        self.subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        set_font_tthoves(self.subtitle, 34, False)
        self.subtitle.setStyleSheet("color: rgba(255,255,255,0.90);")

        self.qr_widget = QRScaleWidget()
        done_btn = make_small_button("DONE")
        done_btn.clicked.connect(self.kiosk.go_main)

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

    def __init__(self, kiosk):
        super().__init__(QColor(18, 170, 205), QColor(40, 215, 145))
        self.kiosk = kiosk

        root = QVBoxLayout(self)
        root.setContentsMargins(60, 60, 60, 60)
        root.setSpacing(12)

        title = QLabel("Redeem Load")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        set_font_tthoves(title, 76, True)
        title.setStyleSheet("color: rgba(255,255,255,0.98);")

        card = make_card()
        card.setFixedHeight(580)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(56, 56, 56, 56)
        cl.setSpacing(10)

        instr = QLabel("Show your Redeem QR Code\non the EcoByte MIT App\nthen scan it on the RIGHT.")
        instr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        set_font_tthoves(instr, 40, True)
        instr.setStyleSheet("color: rgba(255,255,255,0.92);")

        self.status = QLabel("Scanner ready…")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        set_font_tthoves(self.status, 30, False)
        self.status.setStyleSheet("color: rgba(255,255,255,0.80);")

        arrow = BouncingArrow()

        self._input = QLineEdit()
        self._input.setFixedSize(1, 1)
        self._input.setStyleSheet("background: transparent; border: none; color: transparent;")
        self._input.returnPressed.connect(self._on_return)

        cl.addWidget(instr)
        cl.addSpacing(6)
        cl.addWidget(arrow, alignment=Qt.AlignmentFlag.AlignCenter)
        cl.addWidget(self.status)
        cl.addWidget(self._input, alignment=Qt.AlignmentFlag.AlignLeft)

        back_btn = make_small_button("BACK")
        back_btn.clicked.connect(self.kiosk.go_main)

        root.addStretch(1)
        root.addWidget(title)
        root.addWidget(card, alignment=Qt.AlignmentFlag.AlignCenter)
        root.addStretch(1)
        root.addWidget(back_btn, alignment=Qt.AlignmentFlag.AlignCenter)

        self._corner = SecretExitCorner(self.kiosk.exit_app, self)
        self._corner.move(0, 0)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(60, self._focus_input)

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


# ============================================================
# Kiosk Controller
# ============================================================

class Kiosk(QStackedWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("KioskStack")
        self.setStyleSheet("#KioskStack { background: transparent; }")

        self.showFullScreen()
        self.setCursor(Qt.CursorShape.BlankCursor)

        self.audio = Audio()

        self.main = MainScreen(self)
        self.deposit = DepositScreen(self)
        self.qr = QRScreen(self)
        self.redeem = RedeemScreen(self)

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

        # Start idle music
        self.audio.play_idle()

    def mousePressEvent(self, event):
        self.reset_idle()
        # click sfx (optional)
        self.audio.sfx(SND_CLICK, 0.65)
        super().mousePressEvent(event)

    def reset_idle(self):
        self.idle_timer.start(IDLE_TIMEOUT_MS)

    def go_main(self):
        self.worker.set_session(False)
        self.session_bottles = 0
        self.deposit.animate_counts(0)
        self.setCurrentWidget(self.main)
        self.reset_idle()
        self.audio.play_idle()

    def start_session(self):
        self.audio.stop_idle()
        self.session_bottles = 0
        self.deposit.animate_counts(0)
        self.worker.set_session(True)
        self.setCurrentWidget(self.deposit)
        self.reset_idle()

    def go_redeem(self):
        self.worker.set_session(False)
        self.audio.stop_idle()
        self.setCurrentWidget(self.redeem)
        self.reset_idle()

    def on_bottle_dropped(self):
        # ✅ play success as soon as bottle is dropped through chute
        self.audio.sfx(SND_SUCCESS, 1.0)

        self.session_bottles += 1
        self.deposit.animate_counts(self.session_bottles)
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
        }
        payload_text = json.dumps(payload, separators=(",", ":"))

        self.qr.set_qr(payload_text, bottles)
        self.setCurrentWidget(self.qr)
        self.reset_idle()

        # QR show sound
        self.audio.sfx(SND_QR_SHOW, 0.95)

    def on_redeem_scanned(self, scanned: str):
        print("REDEEM SCANNED:", scanned)
        self.redeem.set_scanned_ok()
        # success sound here too (optional)
        self.audio.sfx(SND_SUCCESS, 0.95)
        QTimer.singleShot(2000, self.go_main)

    def exit_app(self):
        try:
            self.worker.stop()
            self.worker.wait(1200)
        except Exception:
            pass
        try:
            self.audio.stop_idle()
        except Exception:
            pass
        QApplication.instance().quit()


# ============================================================
# Run
# ============================================================

if __name__ == "__main__":
    app = QApplication(sys.argv)
    kiosk = Kiosk()
    kiosk.show()
    sys.exit(app.exec())
