import os
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
    Qt, QTimer, QThread, pyqtSignal, pyqtProperty, QPropertyAnimation,
    QEasingCurve
)
from PyQt6.QtGui import (
    QFont, QPainter, QLinearGradient, QColor, QImage, QPixmap,
    QPainterPath, QPen
)
from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton,
    QVBoxLayout, QHBoxLayout, QStackedWidget,
    QGraphicsOpacityEffect, QLineEdit
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

BTN_W = 620
BTN_H = 132
SMALL_BTN_W = 330
SMALL_BTN_H = 102

WATER_FPS_MS = 33
WAVE_SPEED = 0.085
WAVE_OPACITY = 0.18

BOTTLE_COUNT = 10
BOTTLE_ALPHA = 42
BOTTLE_SPEED_MIN = 0.7
BOTTLE_SPEED_MAX = 1.9


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
# Animated Background: Waves + Falling PET Bottles
# ============================================================

class _BottleParticle:
    def __init__(self):
        self.reset(1080, 1920)

    def reset(self, width, height):
        self.x = random.uniform(0.08, 0.92) * width
        self.y = random.uniform(-1.0, 0.2) * height
        self.scale = random.uniform(0.55, 1.12)
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
            if b.y > h + 160:
                b.reset(w, h)
                b.y = -160

        if self._phase > 1e9:
            self._phase = 0.0
        self.update()

    # ADD: plastic (PET) bottle silhouette (slimmer neck + waist + ridges)
    def _draw_pet_bottle(self, p: QPainter, cx: float, cy: float, s: float, alpha: int):
        # PET proportions (taller + slimmer than glass silhouette)
        H = 112 * s
        W = 40 * s
        neck_w = 16 * s
        neck_h = 26 * s
        cap_h = 7 * s

        x0 = cx - W / 2
        y0 = cy - H / 2

        # body with slight waist
        path = QPainterPath()

        # Top shoulder curve
        shoulder_y = y0 + neck_h + 10*s
        waist_y = y0 + H*0.55
        base_y = y0 + H

        left = x0
        right = x0 + W
        mid = cx

        # neck
        path.addRoundedRect(float(mid - neck_w/2), float(y0), float(neck_w), float(neck_h),
                            float(6*s), float(6*s))
        # cap
        path.addRoundedRect(float(mid - neck_w/2), float(y0 - cap_h), float(neck_w), float(cap_h),
                            float(3*s), float(3*s))

        # body outline (custom)
        body = QPainterPath()
        body.moveTo(float(mid - neck_w/2), float(y0 + neck_h))
        body.quadTo(float(left + W*0.18), float(y0 + neck_h + 8*s), float(left + W*0.12), float(shoulder_y))
        body.quadTo(float(left + W*0.02), float(waist_y), float(left + W*0.10), float(base_y - 6*s))
        body.quadTo(float(mid), float(base_y + 2*s), float(right - W*0.10), float(base_y - 6*s))
        body.quadTo(float(right - W*0.02), float(waist_y), float(right - W*0.12), float(shoulder_y))
        body.quadTo(float(right - W*0.18), float(y0 + neck_h + 8*s), float(mid + neck_w/2), float(y0 + neck_h))
        body.closeSubpath()

        # merge
        path = path.united(body)

        # fill (subtle plastic look)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, alpha))
        p.drawPath(path)

        # ridges (horizontal lines)
        ridge_pen = QPen(QColor(255, 255, 255, int(alpha * 0.55)), max(1, int(2*s)))
        p.setPen(ridge_pen)
        for i in range(5):
            ry = int(y0 + H*0.40 + i*(H*0.09))
            lx = int(left + W*0.16)
            rx = int(right - W*0.16)
            p.drawLine(lx, ry, rx, ry)

        # highlight strip
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, int(alpha * 0.48)))
        hx = int(left + W*0.22)
        hy = int(y0 + neck_h + 16*s)
        hw = int(W*0.12)
        hh = int(H*0.62)
        rr = int(max(2, 6*s))
        p.drawRoundedRect(hx, hy, hw, hh, rr, rr)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        w = self.width()
        h = self.height()

        # Gradient base
        grad = QLinearGradient(0, 0, 0, h)
        grad.setColorAt(0.0, self._top)
        grad.setColorAt(1.0, self._bottom)
        p.fillRect(self.rect(), grad)

        # Top sheen
        p.fillRect(0, 0, w, int(h * 0.16), QColor(255, 255, 255, 18))

        # Falling PET bottles
        for b in self._bottles:
            sway = math.sin((b.y * 0.01) + b.sway_phase + self._phase) * b.sway_amp
            self._draw_pet_bottle(p, b.x + sway, b.y, 0.82 * b.scale, BOTTLE_ALPHA)

        # Waves overlay
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
        p.drawPath(wave_path(int(h * 0.78), 19, 0.020, 1.4))


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
    b.setFont(QFont("TT Hoves", 26, QFont.Weight.Bold))
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
    b.setFont(QFont("TT Hoves", 24, QFont.Weight.Bold))
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
    b.setFont(QFont("TT Hoves", 18, QFont.Weight.Bold))
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
# Redeem Arrow (BIG + EDGE OVERLAY)
# ============================================================

class EdgeArrowOverlay(QWidget):
    """
    Big arrow that sits near the RIGHT EDGE of the screen, bouncing left-right.
    This is not inside the card; it's an overlay.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self._offset = 0.0
        self._dir = 1

        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(16)

        self.resize(420, 380)

    def _tick(self):
        self._offset += 1.3 * self._dir
        if self._offset > 24:
            self._dir = -1
        elif self._offset < -10:
            self._dir = 1
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        w = self.width()
        h = self.height()

        # Bigger arrow
        cx = w * 0.58 + self._offset
        cy = h * 0.55

        arrow_w = 360
        arrow_h = 190

        path = QPainterPath()
        x0 = cx - arrow_w / 2
        y0 = cy - arrow_h / 2

        path.addRoundedRect(float(x0), float(y0 + arrow_h*0.25), float(arrow_w*0.62), float(arrow_h*0.50), 34, 34)

        head = QPainterPath()
        hx = x0 + arrow_w*0.62
        head.moveTo(float(hx), float(y0))
        head.lineTo(float(x0 + arrow_w), float(cy))
        head.lineTo(float(hx), float(y0 + arrow_h))
        head.closeSubpath()
        path = path.united(head)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 200))
        p.drawPath(path)

        pen = QPen(QColor(255, 255, 255, 110), 8)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)


# ============================================================
# QR Widget (scale + fade)
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
        super().__init__(QColor(0, 153, 170), QColor(34, 197, 94))
        self.kiosk = kiosk

        root = QVBoxLayout(self)
        root.setContentsMargins(50, 50, 50, 50)
        root.setSpacing(0)

        # ADD: central container so everything is truly centered
        center = QWidget()
        center_l = QVBoxLayout(center)
        center_l.setContentsMargins(0, 0, 0, 0)
        center_l.setSpacing(18)

        title = QLabel("EcoByte")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setFont(QFont("TT Hoves", 110, QFont.Weight.Black))  # ADD: bolder
        title.setStyleSheet("color: rgba(255,255,255,0.99);")

        subtitle = QLabel("From Plastic, to Fantastic!")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setFont(QFont("TT Hoves", 28, QFont.Weight.Medium))
        subtitle.setStyleSheet("color: rgba(255,255,255,0.86);")

        card = make_card()
        card.setFixedHeight(420)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(60, 52, 60, 52)
        cl.setSpacing(18)

        start_btn = make_primary_button("START")
        redeem_btn = make_secondary_button("REDEEM LOAD")

        start_btn.clicked.connect(self.kiosk.start_session)
        redeem_btn.clicked.connect(self.kiosk.go_redeem)

        cl.addStretch(1)
        cl.addWidget(start_btn, alignment=Qt.AlignmentFlag.AlignCenter)
        cl.addWidget(redeem_btn, alignment=Qt.AlignmentFlag.AlignCenter)
        cl.addStretch(1)

        center_l.addWidget(title)
        center_l.addWidget(subtitle)
        center_l.addSpacing(8)
        center_l.addWidget(card, alignment=Qt.AlignmentFlag.AlignCenter)

        root.addStretch(1)
        root.addWidget(center, alignment=Qt.AlignmentFlag.AlignCenter)
        root.addStretch(1)

        self._corner = SecretExitCorner(self.kiosk.exit_app, self)
        self._corner.move(0, 0)


class DepositScreen(WaterBackground):
    def __init__(self, kiosk):
        super().__init__(QColor(0, 153, 170), QColor(34, 197, 94))
        self.kiosk = kiosk

        root = QVBoxLayout(self)
        root.setContentsMargins(50, 50, 50, 50)
        root.setSpacing(10)

        header = QLabel("Insert Bottle")
        header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.setFont(QFont("TT Hoves", 72, QFont.Weight.Black))
        header.setStyleSheet("color: rgba(255,255,255,0.98);")

        self.status = QLabel("Waiting for bottle…")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setFont(QFont("TT Hoves", 34, QFont.Weight.Medium))
        self.status.setStyleSheet("color: rgba(255,255,255,0.88);")

        card = make_card()
        card.setFixedHeight(460)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(60, 58, 60, 58)
        cl.setSpacing(10)

        self.bottles_lbl = AnimatedNumberLabel("Bottles: ")
        self.bottles_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.bottles_lbl.setFont(QFont("TT Hoves", 60, QFont.Weight.Black))
        self.bottles_lbl.setStyleSheet("color: rgba(255,255,255,0.98);")

        self.points_lbl = AnimatedNumberLabel("EcoPoints: ")
        self.points_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.points_lbl.setFont(QFont("TT Hoves", 54, QFont.Weight.Black))
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

        root.addStretch(1)
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
        super().__init__(QColor(0, 153, 170), QColor(34, 197, 94))
        self.kiosk = kiosk

        root = QVBoxLayout(self)
        root.setContentsMargins(50, 50, 50, 50)
        root.setSpacing(10)

        title = QLabel("Scan to Collect EcoPoints")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setFont(QFont("TT Hoves", 58, QFont.Weight.Black))
        title.setStyleSheet("color: rgba(255,255,255,0.98);")

        self.subtitle = QLabel("")
        self.subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.subtitle.setFont(QFont("TT Hoves", 30, QFont.Weight.Medium))
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
        super().__init__(QColor(0, 153, 170), QColor(34, 197, 94))
        self.kiosk = kiosk

        root = QVBoxLayout(self)
        root.setContentsMargins(50, 50, 50, 50)
        root.setSpacing(10)

        title = QLabel("Redeem Load")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setFont(QFont("TT Hoves", 72, QFont.Weight.Black))
        title.setStyleSheet("color: rgba(255,255,255,0.98);")

        card = make_card()
        card.setFixedHeight(580)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(60, 60, 60, 60)
        cl.setSpacing(12)

        instr = QLabel("Show your Redeem QR Code\non the EcoByte MIT App\nthen scan it on the RIGHT.")
        instr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        instr.setFont(QFont("TT Hoves", 36, QFont.Weight.Medium))
        instr.setStyleSheet("color: rgba(255,255,255,0.92);")

        self.status = QLabel("Scanner ready…")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setFont(QFont("TT Hoves", 26, QFont.Weight.Medium))
        self.status.setStyleSheet("color: rgba(255,255,255,0.82);")

        self._input = QLineEdit()
        self._input.setFixedSize(1, 1)
        self._input.setStyleSheet("background: transparent; border: none; color: transparent;")
        self._input.returnPressed.connect(self._on_return)

        cl.addWidget(instr)
        cl.addSpacing(12)
        cl.addWidget(self.status)
        cl.addWidget(self._input, alignment=Qt.AlignmentFlag.AlignLeft)

        back_btn = make_small_button("BACK")
        back_btn.clicked.connect(self.kiosk.go_main)

        root.addStretch(1)
        root.addWidget(title)
        root.addWidget(card, alignment=Qt.AlignmentFlag.AlignCenter)
        root.addStretch(1)
        root.addWidget(back_btn, alignment=Qt.AlignmentFlag.AlignCenter)

        # ADD: big arrow overlay at the RIGHT EDGE (not inside card)
        self._edge_arrow = EdgeArrowOverlay(self)
        self._edge_arrow.show()

        self._corner = SecretExitCorner(self.kiosk.exit_app, self)
        self._corner.move(0, 0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Position arrow near right edge, vertically centered-ish
        margin = 10
        ax = self.width() - self._edge_arrow.width() - margin
        ay = int(self.height() * 0.46 - self._edge_arrow.height() * 0.5)
        self._edge_arrow.move(ax, ay)

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
        self.showFullScreen()
        self.setCursor(Qt.CursorShape.BlankCursor)

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

    def on_redeem_scanned(self, scanned: str):
        print("REDEEM SCANNED:", scanned)
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

    # ADD: global default font preference (TT Hoves -> fallback)
    app.setFont(QFont("TT Hoves", 16))

    kiosk = Kiosk()
    kiosk.show()
    sys.exit(app.exec())
