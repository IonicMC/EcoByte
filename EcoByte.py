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
    QFont, QPainter, QLinearGradient, QColor, QImage, QPixmap, QPainterPath, QPen
)
from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton,
    QVBoxLayout, QHBoxLayout, QStackedWidget,
    QGraphicsOpacityEffect, QLineEdit
)

# ============================================================
# CONFIG
# ============================================================

IDLE_TIMEOUT_MS = 15000  # 15s no touch -> go back to main

GPIO_CAP = 17
GPIO_TRIG = 23
GPIO_ECHO = 24
GPIO_SERVO = 16

GPIO_IR = None  # later: credit only when IR confirms

DIST_THRESHOLD_CM = 10.0
VERIFY_SECONDS = 2.0
POLL_MS = 30

SERVO_CLOSED_DEG = 0
SERVO_OPEN_DEG = 90
GATE_OPEN_MS = 900

POINTS_PER_BOTTLE = 5

# UI sizing (tech startup kiosk)
BTN_W = 600
BTN_H = 124

SMALL_BTN_W = 320
SMALL_BTN_H = 98

# Water animation tuning
WATER_FPS_MS = 33           # ~30fps
WAVE_SPEED = 0.065          # faster waves (was 0.035)
WAVE_OPACITY = 0.16

# Falling bottles
BOTTLE_COUNT = 10
BOTTLE_ALPHA = 38           # subtle
BOTTLE_SPEED_MIN = 0.6
BOTTLE_SPEED_MAX = 1.6


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
# Animated Water Background + Falling Bottles (guaranteed paint)
# ============================================================

class _BottleParticle:
    def __init__(self):
        self.reset(width=1080, height=1920)

    def reset(self, width, height):
        self.x = random.uniform(0.08, 0.92) * width
        self.y = random.uniform(-1.0, 1.0) * height
        self.scale = random.uniform(0.6, 1.15)
        self.speed = random.uniform(BOTTLE_SPEED_MIN, BOTTLE_SPEED_MAX) * (1.0 / self.scale)
        self.sway_phase = random.uniform(0, math.tau)
        self.sway_amp = random.uniform(4, 18)
        self.rot = random.uniform(-10, 10)

class WaterBackground(QWidget):
    """
    Paints a teal->green gradient, faster moving waves, and subtle falling bottles.
    """
    def __init__(self, top: QColor, bottom: QColor, parent=None):
        super().__init__(parent)
        self._top = top
        self._bottom = bottom
        self._phase = 0.0
        self._t = 0

        self._bottles = [_BottleParticle() for _ in range(BOTTLE_COUNT)]

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(WATER_FPS_MS)

    def _tick(self):
        self._phase += WAVE_SPEED
        self._t += 1
        w = max(1, self.width())
        h = max(1, self.height())

        for b in self._bottles:
            b.y += b.speed * 2.0  # falling speed
            if b.y > h + 120:
                b.reset(w, h)
                b.y = -120

        if self._phase > 10_000:
            self._phase = 0.0
        self.update()

    def _draw_bottle(self, p: QPainter, cx: float, cy: float, s: float, alpha: int):
        """
        Simple bottle silhouette (rounded body + neck).
        """
        body_w = 38 * s
        body_h = 78 * s
        neck_w = 18 * s
        neck_h = 24 * s
        cap_h = 8 * s

        x0 = cx - body_w / 2
        y0 = cy - body_h / 2

        path = QPainterPath()
        # body
        path.addRoundedRect(x0, y0 + neck_h, body_w, body_h - neck_h, 10*s, 10*s)
        # neck
        path.addRoundedRect(cx - neck_w/2, y0, neck_w, neck_h, 6*s, 6*s)
        # cap
        path.addRoundedRect(cx - neck_w/2, y0 - cap_h, neck_w, cap_h, 4*s, 4*s)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, alpha))
        p.drawPath(path)

        # small highlight
        p.setBrush(QColor(255, 255, 255, int(alpha * 0.55)))
        p.drawRoundedRect(cx - body_w*0.22, y0 + neck_h + body_h*0.08, body_w*0.14, body_h*0.70, 6*s, 6*s)

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

        # Sheen
        p.fillRect(0, 0, w, int(h * 0.18), QColor(255, 255, 255, 20))

        # Falling bottles (behind everything)
        for b in self._bottles:
            sway = math.sin((b.y * 0.01) + b.sway_phase + self._phase) * b.sway_amp
            self._draw_bottle(
                p,
                cx=b.x + sway,
                cy=b.y,
                s=0.8 * b.scale,
                alpha=BOTTLE_ALPHA
            )

        # Waves overlay (two layers)
        def wave_path(y_base, amp, freq, phase_shift):
            path = QPainterPath()
            path.moveTo(0, h)
            path.lineTo(0, y_base)

            step = max(8, w // 80)
            for x in range(0, w + step, step):
                y = y_base + amp * math.sin((x * freq) + self._phase + phase_shift)
                path.lineTo(x, y)

            path.lineTo(w, h)
            path.closeSubpath()
            return path

        c1 = QColor(255, 255, 255, int(255 * WAVE_OPACITY))
        c2 = QColor(255, 255, 255, int(255 * (WAVE_OPACITY * 0.70)))

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c1)
        p.drawPath(wave_path(y_base=int(h * 0.70), amp=26, freq=0.016, phase_shift=0.0))
        p.setBrush(c2)
        p.drawPath(wave_path(y_base=int(h * 0.78), amp=18, freq=0.020, phase_shift=1.4))


# ============================================================
# Hidden Exit Corner (tap 5 times)
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
# Animated Number Label (snappier + springy)
# ============================================================

class AnimatedNumberLabel(QLabel):
    def __init__(self, prefix: str, parent=None):
        super().__init__(parent)
        self._prefix = prefix
        self._value = 0.0

        self._anim = QPropertyAnimation(self, b"value", self)
        self._anim.setDuration(360)  # snappier
        self._anim.setEasingCurve(QEasingCurve.Type.OutBack)  # springy
        self._sync_text()

    def _sync_text(self):
        self.setText(f"{self._prefix}{int(round(self._value))}")

    def getValue(self):
        return self._value

    def setValue(self, v):
        self._value = float(v)
        self._sync_text()

    value = pyqtProperty(float, fget=getValue, fset=setValue)

    def animate_to(self, target: int):
        self._anim.stop()
        self._anim.setStartValue(self._value)
        self._anim.setEndValue(float(target))
        self._anim.start()


# ============================================================
# Buttons (Style A)
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
            border-radius: 28px;
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
            border-radius: 28px;
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
    dropped = pyqtSignal()      # gate cycle completed

    def __init__(self):
        super().__init__()
        self.running = True
        self.session_enabled = False
        self._verifying = False
        self._latched = False
        self._verify_start = None
        self._gate_busy = False
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

                if self._gate_busy:
                    self.msleep(POLL_MS)
                    continue

                cap = (GPIO.input(GPIO_CAP) == 1)
                dist = self._read_distance_cm()
                ultrasonic_present = (dist != float("inf")) and (dist < DIST_THRESHOLD_CM)
                ready = cap and ultrasonic_present

                if ready and (not self._verifying) and (not self._latched):
                    self._verifying = True
                    self._latched = True
                    self._verify_start = time.monotonic()
                    self.ui_mode.emit("SCANNING")

                if self._verifying:
                    self.ui_mode.emit("SCANNING")
                    if (time.monotonic() - self._verify_start) >= VERIFY_SECONDS:
                        self._verifying = False
                        self._gate_busy = True
                        self.ui_mode.emit("DROPPING")

                        servo(SERVO_OPEN_DEG)
                        time.sleep(GATE_OPEN_MS / 1000.0)
                        servo(SERVO_CLOSED_DEG)

                        self._gate_busy = False
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
# Animated arrow widget (bounces left-right)
# ============================================================

class BouncingArrow(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._offset = 0.0
        self._dir = 1
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(16)

        self.setFixedHeight(220)

    def _tick(self):
        self._offset += 1.1 * self._dir
        if self._offset > 20:
            self._dir = -1
        elif self._offset < -6:
            self._dir = 1
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        w = self.width()
        h = self.height()

        # draw a big right arrow near the right edge, moving a bit left-right
        cx = w * 0.72 + self._offset
        cy = h * 0.52

        arrow_w = 220
        arrow_h = 120

        path = QPainterPath()
        x0 = cx - arrow_w / 2
        y0 = cy - arrow_h / 2

        # arrow body
        path.addRoundedRect(x0, y0 + arrow_h*0.25, arrow_w*0.62, arrow_h*0.50, 22, 22)

        # arrow head
        head = QPainterPath()
        hx = x0 + arrow_w*0.62
        head.moveTo(hx, y0)
        head.lineTo(x0 + arrow_w, cy)
        head.lineTo(hx, y0 + arrow_h)
        head.closeSubpath()
        path = path.united(head)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 180))
        p.drawPath(path)

        # glow outline
        pen = QPen(QColor(255, 255, 255, 90), 6)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)


# ============================================================
# QR display widget with SCALE animation (no layout overlap)
# ============================================================

class QRScaleWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._pm = None
        self._scale = 0.86
        self._opacity_eff = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._opacity_eff)
        self._opacity_eff.setOpacity(0.0)

        self._fade = QPropertyAnimation(self._opacity_eff, b"opacity", self)
        self._fade.setDuration(420)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._scale_anim = QPropertyAnimation(self, b"scale", self)
        self._scale_anim.setDuration(520)
        self._scale_anim.setEasingCurve(QEasingCurve.Type.OutBack)

        self.setFixedSize(700, 700)

    def setPixmap(self, pm: QPixmap):
        self._pm = pm
        self.play()

    def play(self):
        self._fade.stop()
        self._scale_anim.stop()

        self._opacity_eff.setOpacity(0.0)
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

        # glass card background
        rect = self.rect()
        p.setPen(QPen(QColor(255, 255, 255, 70), 2))
        p.setBrush(QColor(255, 255, 255, 35))
        p.drawRoundedRect(rect.adjusted(6, 6, -6, -6), 34, 34)

        if not self._pm:
            return

        # draw pixmap scaled at center
        base = min(rect.width(), rect.height()) - 90
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
        root.setContentsMargins(60, 90, 60, 70)
        root.setSpacing(18)

        title = QLabel("EcoByte")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setFont(QFont("Arial", 98, QFont.Weight.Bold))
        title.setStyleSheet("color: rgba(255,255,255,0.98);")

        subtitle = QLabel("From Plastic, to Fantastic!")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setFont(QFont("Arial", 26))
        subtitle.setStyleSheet("color: rgba(255,255,255,0.86);")

        card = make_card()
        card.setFixedHeight(360)
        card_l = QVBoxLayout(card)
        card_l.setContentsMargins(50, 44, 50, 44)
        card_l.setSpacing(18)

        start_btn = make_primary_button("START")
        redeem_btn = make_secondary_button("REDEEM LOAD")

        start_btn.clicked.connect(self.kiosk.start_session)
        redeem_btn.clicked.connect(self.kiosk.go_redeem)

        card_l.addStretch(1)
        card_l.addWidget(start_btn, alignment=Qt.AlignmentFlag.AlignCenter)
        card_l.addWidget(redeem_btn, alignment=Qt.AlignmentFlag.AlignCenter)
        card_l.addStretch(1)

        root.addWidget(title)
        root.addWidget(subtitle)
        root.addSpacing(10)
        root.addWidget(card, alignment=Qt.AlignmentFlag.AlignCenter)
        root.addStretch(1)

        self._corner = SecretExitCorner(self.kiosk.exit_app, self)
        self._corner.move(0, 0)


class DepositScreen(WaterBackground):
    def __init__(self, kiosk):
        super().__init__(QColor(0, 153, 170), QColor(34, 197, 94))
        self.kiosk = kiosk

        root = QVBoxLayout(self)
        root.setContentsMargins(60, 80, 60, 60)
        root.setSpacing(16)

        header = QLabel("Insert Bottle")
        header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.setFont(QFont("Arial", 66, QFont.Weight.Bold))
        header.setStyleSheet("color: rgba(255,255,255,0.98);")

        self.status = QLabel("Waiting for bottle…")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setFont(QFont("Arial", 34))
        self.status.setStyleSheet("color: rgba(255,255,255,0.88);")

        card = make_card()
        card.setFixedHeight(420)
        card_l = QVBoxLayout(card)
        card_l.setContentsMargins(54, 52, 54, 52)
        card_l.setSpacing(12)

        self.bottles_lbl = AnimatedNumberLabel("Bottles: ")
        self.bottles_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.bottles_lbl.setFont(QFont("Arial", 56, QFont.Weight.Bold))
        self.bottles_lbl.setStyleSheet("color: rgba(255,255,255,0.98);")

        self.points_lbl = AnimatedNumberLabel("EcoPoints: ")
        self.points_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.points_lbl.setFont(QFont("Arial", 50, QFont.Weight.Bold))
        self.points_lbl.setStyleSheet("color: rgba(232,255,242,1);")

        card_l.addStretch(1)
        card_l.addWidget(self.bottles_lbl)
        card_l.addWidget(self.points_lbl)
        card_l.addStretch(1)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(18)
        self.back_btn = make_small_button("BACK")
        self.finish_btn = make_small_button("FINISH")
        self.back_btn.clicked.connect(self.kiosk.go_main)
        self.finish_btn.clicked.connect(self.kiosk.finish_session)

        btn_row.addStretch(1)
        btn_row.addWidget(self.back_btn)
        btn_row.addWidget(self.finish_btn)
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


class RedeemScreen(WaterBackground):
    """
    Shows instructions + big bouncing arrow pointing to right-side scanner.
    Also captures keyboard-wedge QR scan input.
    """
    scanned_text = pyqtSignal(str)

    def __init__(self, kiosk):
        super().__init__(QColor(0, 153, 170), QColor(34, 197, 94))
        self.kiosk = kiosk

        root = QVBoxLayout(self)
        root.setContentsMargins(60, 80, 60, 60)
        root.setSpacing(16)

        title = QLabel("Redeem Load")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setFont(QFont("Arial", 66, QFont.Weight.Bold))
        title.setStyleSheet("color: rgba(255,255,255,0.98);")

        card = make_card()
        card.setFixedHeight(520)
        card_l = QVBoxLayout(card)
        card_l.setContentsMargins(54, 54, 54, 54)
        card_l.setSpacing(14)

        instr = QLabel("Show your Redeem QR Code\non the EcoByte MIT App\nthen scan it on the RIGHT.")
        instr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        instr.setFont(QFont("Arial", 34))
        instr.setStyleSheet("color: rgba(255,255,255,0.92);")

        hint = QLabel("Scanner ready…")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setFont(QFont("Arial", 22))
        hint.setStyleSheet("color: rgba(255,255,255,0.78);")

        # arrow (animated)
        self.arrow = BouncingArrow()
        self.arrow.setFixedHeight(240)

        # hidden input for keyboard-wedge scanner
        self._input = QLineEdit()
        self._input.setFixedSize(1, 1)
        self._input.setStyleSheet("background: transparent; border: none; color: transparent;")
        self._input.returnPressed.connect(self._on_return)

        card_l.addWidget(instr)
        card_l.addSpacing(10)
        card_l.addWidget(self.arrow)
        card_l.addSpacing(6)
        card_l.addWidget(hint)
        card_l.addWidget(self._input, alignment=Qt.AlignmentFlag.AlignLeft)

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
        # Always focus hidden input so wedge scanner types here
        QTimer.singleShot(50, self._focus_input)

    def _focus_input(self):
        self._input.clear()
        self._input.setFocus(Qt.FocusReason.OtherFocusReason)

    def _on_return(self):
        s = self._input.text().strip()
        self._input.clear()
        if s:
            self.scanned_text.emit(s)
        self._focus_input()


class QRScreen(WaterBackground):
    def __init__(self, kiosk):
        super().__init__(QColor(0, 153, 170), QColor(34, 197, 94))
        self.kiosk = kiosk

        root = QVBoxLayout(self)
        root.setContentsMargins(60, 70, 60, 60)
        root.setSpacing(14)

        title = QLabel("Scan to Collect EcoPoints")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setFont(QFont("Arial", 56, QFont.Weight.Bold))
        title.setStyleSheet("color: rgba(255,255,255,0.98);")

        self.subtitle = QLabel("")
        self.subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.subtitle.setFont(QFont("Arial", 30))
        self.subtitle.setStyleSheet("color: rgba(255,255,255,0.90);")

        self.qr_widget = QRScaleWidget()
        self.qr_widget.setFixedSize(720, 720)

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
        self.redeem = RedeemScreen(self)
        self.qr = QRScreen(self)

        self.addWidget(self.main)
        self.addWidget(self.deposit)
        self.addWidget(self.redeem)
        self.addWidget(self.qr)

        self.setCurrentWidget(self.main)

        # Session state
        self.session_bottles = 0

        # Worker
        self.worker = HardwareWorker()
        self.worker.ui_mode.connect(self.deposit.set_mode)
        self.worker.dropped.connect(self.on_bottle_dropped)
        self.worker.start()

        # Redeem scan handler (keyboard wedge)
        self.redeem.scanned_text.connect(self.on_redeem_scanned)

        # Idle timer (touch resets)
        self.idle_timer = QTimer(self)
        self.idle_timer.timeout.connect(self.go_main)
        self.reset_idle()

    # Reset idle on any touch anywhere
    def mousePressEvent(self, event):
        self.reset_idle()
        super().mousePressEvent(event)

    def reset_idle(self):
        self.idle_timer.start(IDLE_TIMEOUT_MS)

    # Navigation
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

    # Deposit flow
    def on_bottle_dropped(self):
        # For now: count when gate cycle completes.
        # When IR arrives: change to count ONLY when IR triggers.
        self.session_bottles += 1
        self.deposit.animate_counts(self.session_bottles)
        self.setCurrentWidget(self.deposit)
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

    # Redeem scan (placeholder until Firebase + SIM800C)
    def on_redeem_scanned(self, scanned: str):
        # This is where you'll verify token + load request.
        # For now, just show that scan was received and return to main.
        # You can print to terminal for debugging:
        print("REDEEM QR SCANNED:", scanned)
        self.go_main()

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