import os
import sys
import time
import uuid
import json
import math
import random

# Qt env
os.environ["QT_QPA_PLATFORM"] = "xcb"
os.environ["QT_QPA_PLATFORMTHEME"] = ""
os.environ["QT_LOGGING_RULES"] = "*.debug=false;qt.qpa.*=false"

# Make pygame audio reliable under desktop sessions (HDMI/BT via PulseAudio/PipeWire)
os.environ.setdefault("SDL_AUDIODRIVER", "pulseaudio")

import pygame
import RPi.GPIO as GPIO
import qrcode

from PyQt6.QtCore import (
    Qt, QTimer, QThread, pyqtSignal, pyqtProperty, QPropertyAnimation, QEasingCurve
)
from PyQt6.QtGui import (
    QFont, QPainter, QLinearGradient, QColor, QImage, QPixmap, QPainterPath, QPen
)
from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QStackedWidget, QGraphicsOpacityEffect, QLineEdit
)

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

# UI sizing (bigger, less empty)
BTN_W = 640
BTN_H = 132
SMALL_BTN_W = 360
SMALL_BTN_H = 104

# Background animation
WATER_FPS_MS = 33
WAVE_SPEED = 0.085     # faster waves
WAVE_OPACITY = 0.20

# PET bottle particles
BOTTLE_COUNT = 12
BOTTLE_ALPHA = 34
BOTTLE_SPEED_MIN = 0.8
BOTTLE_SPEED_MAX = 2.0


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
# Sound (pygame) - WAV
# ============================================================

class SoundManager:
    def __init__(self):
        self.ok = False
        self.base = os.path.join(os.path.dirname(__file__), "sounds")

        # Larger buffer -> less crackle/jitter
        try:
            pygame.mixer.pre_init(44100, -16, 2, 2048)
            pygame.mixer.init()
            self.ok = True
        except Exception as e:
            print("Audio init failed:", e)
            self.ok = False
            return

        def p(name): return os.path.join(self.base, name)

        def load_sfx(name):
            path = p(name)
            if os.path.exists(path):
                try:
                    return pygame.mixer.Sound(path)
                except Exception as e:
                    print(f"Failed to load {name}:", e)
            return None

        # Music
        self.music_path = p("idle.wav")
        if os.path.exists(self.music_path):
            try:
                pygame.mixer.music.load(self.music_path)
                pygame.mixer.music.set_volume(0.70)
                pygame.mixer.music.play(-1)
            except Exception as e:
                print("Music load/play failed:", e)

        # SFX
        self.tap = load_sfx("tap.wav")
        self.success = load_sfx("success.wav")
        self.scan_ok = load_sfx("scan_ok.wav")
        self.qr_show = load_sfx("qr_show.wav")

        # volumes for sfx (per sound instance)
        for s, vol in [
            (self.tap, 0.55),
            (self.success, 0.75),
            (self.scan_ok, 0.85),
            (self.qr_show, 0.65),
        ]:
            if s is not None:
                try:
                    s.set_volume(vol)
                except Exception:
                    pass

    def set_main_volume(self):
        if self.ok:
            try:
                pygame.mixer.music.set_volume(0.70)
            except Exception:
                pass

    def set_low_volume(self):
        if self.ok:
            try:
                pygame.mixer.music.set_volume(0.20)
            except Exception:
                pass

    def _play(self, sfx):
        if sfx is not None:
            try:
                sfx.play()
            except Exception:
                pass

    def play_tap(self): self._play(self.tap)
    def play_success(self): self._play(self.success)
    def play_scan_ok(self): self._play(self.scan_ok)
    def play_qr_show(self): self._play(self.qr_show)


# ============================================================
# Animated background: smooth gradient + waves + PET bottles
# ============================================================

class _BottleParticle:
    def __init__(self):
        self.reset(1080, 1920)

    def reset(self, width, height):
        self.x = random.uniform(0.06, 0.94) * width
        self.y = random.uniform(-1.0, 0.25) * height
        self.scale = random.uniform(0.55, 1.05)
        self.speed = random.uniform(BOTTLE_SPEED_MIN, BOTTLE_SPEED_MAX) * (1.0 / self.scale)
        self.sway_phase = random.uniform(0, math.tau)
        self.sway_amp = random.uniform(3, 14)
        self.rot = random.uniform(-8, 8)

class WaterBackground(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
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
            if b.y > h + 160:
                b.reset(w, h)
                b.y = -160

        self.update()

    def _draw_pet_bottle(self, p: QPainter, cx: float, cy: float, s: float, alpha: int, rot_deg: float):
        """
        PET-style silhouette (shorter neck, wider body) to avoid 'wine bottle' look.
        """
        body_w = 44 * s
        body_h = 78 * s
        neck_w = 22 * s
        neck_h = 18 * s
        cap_h = 7 * s

        x0 = cx - body_w / 2
        y0 = cy - body_h / 2

        p.save()
        p.translate(cx, cy)
        p.rotate(rot_deg)
        p.translate(-cx, -cy)

        path = QPainterPath()

        # body (wider, rounded)
        path.addRoundedRect(
            float(x0), float(y0 + neck_h),
            float(body_w), float(body_h - neck_h),
            float(12*s), float(12*s)
        )

        # slight "waist" indentation using two transparent overlays (cheap trick)
        # neck
        path.addRoundedRect(
            float(cx - neck_w/2), float(y0),
            float(neck_w), float(neck_h),
            float(7*s), float(7*s)
        )

        # cap
        path.addRoundedRect(
            float(cx - neck_w/2), float(y0 - cap_h),
            float(neck_w), float(cap_h),
            float(4*s), float(4*s)
        )

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, alpha))
        p.drawPath(path)

        # highlight (int overload safety)
        hx = int(cx - body_w * 0.22)
        hy = int(y0 + neck_h + body_h * 0.12)
        hw = int(body_w * 0.14)
        hh = int(body_h * 0.64)
        rr = int(max(2, 7 * s))
        p.setBrush(QColor(255, 255, 255, int(alpha * 0.55)))
        p.drawRoundedRect(hx, hy, hw, hh, rr, rr)

        p.restore()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        w = self.width()
        h = self.height()

        # base gradient
        grad = QLinearGradient(0, 0, 0, h)
        grad.setColorAt(0.0, QColor(0, 150, 175))
        grad.setColorAt(1.0, QColor(34, 197, 94))
        p.fillRect(self.rect(), grad)

        # soft top fade (no hard line)
        fade = QLinearGradient(0, 0, 0, int(h * 0.34))
        fade.setColorAt(0.0, QColor(255, 255, 255, 38))
        fade.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillRect(0, 0, w, int(h * 0.34), fade)

        # falling bottles (behind waves)
        for b in self._bottles:
            sway = math.sin((b.y * 0.01) + b.sway_phase + self._phase) * b.sway_amp
            rot = b.rot + math.sin(self._phase + b.sway_phase) * 2.2
            self._draw_pet_bottle(p, b.x + sway, b.y, 0.88 * b.scale, BOTTLE_ALPHA, rot)

        # waves overlay
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
        p.drawPath(wave_path(int(h * 0.70), 30, 0.016, 0.0))
        p.setBrush(c2)
        p.drawPath(wave_path(int(h * 0.79), 20, 0.020, 1.4))


# ============================================================
# Secret exit (tap 5x in top-left corner)
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
# Animated numbers
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
# Styled UI components (modern)
# ============================================================

def make_primary_button(text: str) -> QPushButton:
    b = QPushButton(text)
    b.setFixedSize(BTN_W, BTN_H)
    b.setFont(QFont("Arial", 26, QFont.Weight.Bold))
    b.setStyleSheet("""
        QPushButton {
            background: rgba(255,255,255,0.18);
            color: white;
            border: 2px solid rgba(255,255,255,0.34);
            border-radius: 30px;
            letter-spacing: 1px;
        }
        QPushButton:pressed { background: rgba(255,255,255,0.26); }
    """)
    return b

def make_secondary_button(text: str) -> QPushButton:
    b = QPushButton(text)
    b.setFixedSize(BTN_W, BTN_H)
    b.setFont(QFont("Arial", 24, QFont.Weight.Bold))
    b.setStyleSheet("""
        QPushButton {
            background: rgba(255,255,255,0.94);
            color: #0B7A3B;
            border-radius: 30px;
            letter-spacing: 0.5px;
        }
        QPushButton:pressed { background: rgba(255,255,255,0.78); }
    """)
    return b

def make_small_button(text: str) -> QPushButton:
    b = QPushButton(text)
    b.setFixedSize(SMALL_BTN_W, SMALL_BTN_H)
    b.setFont(QFont("Arial", 18, QFont.Weight.Bold))
    b.setStyleSheet("""
        QPushButton {
            background: rgba(255,255,255,0.94);
            color: #0B7A3B;
            border-radius: 22px;
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
            border-radius: 38px;
        }
    """)
    return c


# ============================================================
# Hardware worker (cap+ultra latch -> 2s delay -> gate)
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
# Arrow with real glow (multi-pass), never clipped
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
        if self._offset > 22:
            self._dir = -1
        elif self._offset < -7:
            self._dir = 1
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        w = self.width()
        h = self.height()

        arrow_w = 290
        arrow_h = 155
        margin = 60

        cx = w * 0.76 + self._offset
        cx = clamp(cx, margin + arrow_w/2, w - margin - arrow_w/2)
        cy = h * 0.54

        x0 = cx - arrow_w/2
        y0 = cy - arrow_h/2

        path = QPainterPath()
        path.addRoundedRect(float(x0), float(y0 + arrow_h*0.25), float(arrow_w*0.62), float(arrow_h*0.50), 26, 26)

        head = QPainterPath()
        hx = x0 + arrow_w*0.62
        head.moveTo(float(hx), float(y0))
        head.lineTo(float(x0 + arrow_w), float(cy))
        head.lineTo(float(hx), float(y0 + arrow_h))
        head.closeSubpath()
        path = path.united(head)

        # stronger glow
        p.setBrush(Qt.BrushStyle.NoBrush)
        for i in range(7, 0, -1):
            alpha = 18 + i * 12
            width = 10 + i * 4
            p.setPen(QPen(QColor(255, 255, 255, alpha), width, Qt.PenStyle.SolidLine,
                          Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            p.drawPath(path)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 220))
        p.drawPath(path)


# ============================================================
# QR widget (scale+fade)
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

        self.setFixedSize(740, 740)

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

    def getScale(self): return self._scale
    def setScale(self, v):
        self._scale = float(v)
        self.update()

    scale = pyqtProperty(float, fget=getScale, fset=setScale)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = self.rect()
        p.setPen(QPen(QColor(255, 255, 255, 74), 2))
        p.setBrush(QColor(255, 255, 255, 38))
        p.drawRoundedRect(rect.adjusted(6, 6, -6, -6), 36, 36)

        if not self._pm:
            return

        base = min(rect.width(), rect.height()) - 110
        size = int(base * self._scale)
        x = (rect.width() - size) // 2
        y = (rect.height() - size) // 2

        pm = self._pm.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        p.drawPixmap(x, y, pm)


# ============================================================
# Screens
# ============================================================

class MainScreen(WaterBackground):
    def __init__(self, kiosk):
        super().__init__()
        self.kiosk = kiosk

        root = QVBoxLayout(self)
        root.setContentsMargins(60, 90, 60, 70)
        root.setSpacing(10)

        # Colored text logo (two-tone)
        title_row = QHBoxLayout()
        title_row.setSpacing(0)

        eco = QLabel("Eco")
        eco.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        eco.setFont(QFont("Arial", 106, QFont.Weight.Bold))
        eco.setStyleSheet("color: rgba(255,255,255,0.98);")

        byte = QLabel("Byte")
        byte.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        byte.setFont(QFont("Arial", 106, QFont.Weight.Bold))
        # mint tint (still white-ish but colored)
        byte.setStyleSheet("color: rgba(210,255,235,0.98);")

        title_row.addStretch(1)
        title_row.addWidget(eco)
        title_row.addWidget(byte)
        title_row.addStretch(1)

        subtitle = QLabel("From Plastic, to Fantastic!")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setFont(QFont("Arial", 28))
        subtitle.setStyleSheet("color: rgba(255,255,255,0.86);")

        card = make_card()
        card.setFixedHeight(430)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(62, 54, 62, 54)
        cl.setSpacing(18)

        start_btn = make_primary_button("START")
        redeem_btn = make_secondary_button("REDEEM LOAD")

        start_btn.clicked.connect(self.kiosk.start_session)
        redeem_btn.clicked.connect(self.kiosk.go_redeem)

        cl.addStretch(1)
        cl.addWidget(start_btn, alignment=Qt.AlignmentFlag.AlignCenter)
        cl.addWidget(redeem_btn, alignment=Qt.AlignmentFlag.AlignCenter)
        cl.addStretch(1)

        root.addLayout(title_row)
        root.addWidget(subtitle)
        root.addSpacing(18)
        root.addWidget(card, alignment=Qt.AlignmentFlag.AlignCenter)
        root.addStretch(1)

        self._corner = SecretExitCorner(self.kiosk.exit_app, self)
        self._corner.move(0, 0)


class DepositScreen(WaterBackground):
    def __init__(self, kiosk):
        super().__init__()
        self.kiosk = kiosk

        root = QVBoxLayout(self)
        root.setContentsMargins(60, 78, 60, 60)
        root.setSpacing(12)

        header = QLabel("Insert Bottle")
        header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.setFont(QFont("Arial", 72, QFont.Weight.Bold))
        header.setStyleSheet("color: rgba(255,255,255,0.98);")

        self.status = QLabel("Waiting for bottle…")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setFont(QFont("Arial", 36))
        self.status.setStyleSheet("color: rgba(255,255,255,0.88);")

        card = make_card()
        card.setFixedHeight(480)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(64, 62, 64, 62)
        cl.setSpacing(12)

        self.bottles_lbl = AnimatedNumberLabel("Bottles: ")
        self.bottles_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.bottles_lbl.setFont(QFont("Arial", 64, QFont.Weight.Bold))
        self.bottles_lbl.setStyleSheet("color: rgba(255,255,255,0.98);")

        self.points_lbl = AnimatedNumberLabel("EcoPoints: ")
        self.points_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.points_lbl.setFont(QFont("Arial", 56, QFont.Weight.Bold))
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
        elif mode == "SCANNING":
            self.status.setText("Scanning… Please wait")
        elif mode == "DROPPING":
            self.status.setText("Processing…")

    def animate_counts(self, bottles: int):
        self.bottles_lbl.animate_to(bottles)
        self.points_lbl.animate_to(bottles * POINTS_PER_BOTTLE)


class QRScreen(WaterBackground):
    def __init__(self, kiosk):
        super().__init__()
        self.kiosk = kiosk

        root = QVBoxLayout(self)
        root.setContentsMargins(60, 70, 60, 60)
        root.setSpacing(10)

        # Safe, never clipped
        title = QLabel("Scan to Collect\nEcoPoints")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setWordWrap(True)
        title.setFont(QFont("Arial", 56, QFont.Weight.Bold))
        title.setStyleSheet("color: rgba(255,255,255,0.98);")

        self.subtitle = QLabel("")
        self.subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.subtitle.setWordWrap(True)
        self.subtitle.setFont(QFont("Arial", 32))
        self.subtitle.setStyleSheet("color: rgba(255,255,255,0.90);")

        self.qr_widget = QRScaleWidget()
        done_btn = make_small_button("DONE")
        done_btn.clicked.connect(self.kiosk.go_main)

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
        super().__init__()
        self.kiosk = kiosk

        root = QVBoxLayout(self)
        root.setContentsMargins(60, 80, 60, 60)
        root.setSpacing(14)

        title = QLabel("Redeem Load")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setFont(QFont("Arial", 70, QFont.Weight.Bold))
        title.setStyleSheet("color: rgba(255,255,255,0.98);")

        card = make_card()
        card.setFixedHeight(600)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(62, 62, 62, 62)
        cl.setSpacing(12)

        instr = QLabel("Show your Redeem QR Code\non the EcoByte MIT App\nthen scan it on the RIGHT.")
        instr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        instr.setWordWrap(True)
        instr.setFont(QFont("Arial", 36))
        instr.setStyleSheet("color: rgba(255,255,255,0.92);")

        self.status = QLabel("Scanner ready…")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setFont(QFont("Arial", 26))
        self.status.setStyleSheet("color: rgba(255,255,255,0.80);")

        arrow = BouncingArrow()

        self._input = QLineEdit()
        self._input.setFixedSize(1, 1)
        self._input.setStyleSheet("background: transparent; border: none; color: transparent;")
        self._input.returnPressed.connect(self._on_return)

        cl.addWidget(instr)
        cl.addSpacing(8)
        cl.addWidget(arrow)
        cl.addWidget(self.status)
        cl.addWidget(self._input, alignment=Qt.AlignmentFlag.AlignLeft)

        back_btn = make_small_button("BACK")
        back_btn.clicked.connect(self.kiosk.go_main)

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
# Kiosk controller
# ============================================================

class Kiosk(QStackedWidget):
    def __init__(self):
        super().__init__()
        self.showFullScreen()
        self.setCursor(Qt.CursorShape.BlankCursor)

        self.sound = SoundManager()

        self.main = MainScreen(self)
        self.deposit = DepositScreen(self)
        self.qr = QRScreen(self)
        self.redeem = RedeemScreen(self)

        self.addWidget(self.main)
        self.addWidget(self.deposit)
        self.addWidget(self.qr)
        self.addWidget(self.redeem)

        self.setCurrentWidget(self.main)
        self.sound.set_main_volume()

        self.session_bottles = 0

        self.worker = HardwareWorker()
        self.worker.ui_mode.connect(self.deposit.set_mode)
        self.worker.dropped.connect(self.on_bottle_dropped)
        self.worker.start()

        self.redeem.scanned_text.connect(self.on_redeem_scanned)

        self.idle_timer = QTimer(self)
        self.idle_timer.timeout.connect(self.go_main)
        self.reset_idle()

    def mousePressEvent(self, event):
        self.reset_idle()
        super().mousePressEvent(event)

    def reset_idle(self):
        self.idle_timer.start(IDLE_TIMEOUT_MS)

    def go_main(self):
        self.worker.set_session(False)
        self.session_bottles = 0
        self.deposit.animate_counts(0)
        self.setCurrentWidget(self.main)
        self.sound.set_main_volume()
        self.reset_idle()

    def start_session(self):
        self.sound.play_tap()
        self.sound.set_low_volume()
        self.session_bottles = 0
        self.deposit.animate_counts(0)
        self.worker.set_session(True)
        self.setCurrentWidget(self.deposit)
        self.reset_idle()

    def go_redeem(self):
        self.sound.play_tap()
        self.sound.set_low_volume()
        self.worker.set_session(False)
        self.setCurrentWidget(self.redeem)
        self.reset_idle()

    def on_bottle_dropped(self):
        self.session_bottles += 1
        self.deposit.animate_counts(self.session_bottles)
        self.sound.play_success()
        self.reset_idle()

    def finish_session(self):
        self.sound.play_tap()
        self.sound.set_low_volume()
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
        self.sound.play_qr_show()
        self.reset_idle()

    def on_redeem_scanned(self, scanned: str):
        print("REDEEM SCANNED:", scanned)
        self.sound.play_scan_ok()
        self.redeem.set_scanned_ok()
        QTimer.singleShot(2000, self.go_main)

    def exit_app(self):
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
    kiosk = Kiosk()
    kiosk.show()
    sys.exit(app.exec())
