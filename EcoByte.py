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

BTN_W = 600
BTN_H = 124
SMALL_BTN_W = 320
SMALL_BTN_H = 98

WATER_FPS_MS = 33
WAVE_SPEED = 0.07
WAVE_OPACITY = 0.18

# ============================================================
# HELPERS
# ============================================================

def angle_to_duty(angle):
    return 2.5 + (angle / 180.0) * 10.0

def qr_pixmap_from_text(text, size_px=560):
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    img = img.resize((size_px, size_px))
    w, h = img.size
    data = img.tobytes("raw", "RGB")
    qimg = QImage(data, w, h, 3*w, QImage.Format.Format_RGB888)
    return QPixmap.fromImage(qimg)

# ============================================================
# WATER BACKGROUND (FIXED DRAWING)
# ============================================================

class WaterBackground(QWidget):
    def __init__(self, top, bottom):
        super().__init__()
        self._top = top
        self._bottom = bottom
        self._phase = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(WATER_FPS_MS)

    def _tick(self):
        self._phase += WAVE_SPEED
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()

        grad = QLinearGradient(0, 0, 0, h)
        grad.setColorAt(0, self._top)
        grad.setColorAt(1, self._bottom)
        p.fillRect(self.rect(), grad)

        # Waves
        def wave(y_base, amp, freq):
            path = QPainterPath()
            path.moveTo(0, h)
            path.lineTo(0, y_base)
            step = max(10, w // 80)
            for x in range(0, w+step, step):
                y = y_base + amp * math.sin((x * freq) + self._phase)
                path.lineTo(x, y)
            path.lineTo(w, h)
            path.closeSubpath()
            return path

        p.setBrush(QColor(255,255,255,int(255*WAVE_OPACITY)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPath(wave(int(h*0.72), 26, 0.015))
        p.drawPath(wave(int(h*0.80), 18, 0.02))

# ============================================================
# SECRET EXIT
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
# NUMBER ANIMATION
# ============================================================

class AnimatedNumberLabel(QLabel):
    def __init__(self, prefix):
        super().__init__()
        self._prefix = prefix
        self._value = 0
        self._anim = QPropertyAnimation(self, b"value")
        self._anim.setDuration(360)
        self._anim.setEasingCurve(QEasingCurve.Type.OutBack)
        self._update_text()

    def _update_text(self):
        self.setText(f"{self._prefix}{int(self._value)}")

    def getValue(self):
        return self._value

    def setValue(self, v):
        self._value = v
        self._update_text()

    value = pyqtProperty(float, fget=getValue, fset=setValue)

    def animate_to(self, target):
        self._anim.stop()
        self._anim.setStartValue(self._value)
        self._anim.setEndValue(target)
        self._anim.start()

# ============================================================
# HARDWARE WORKER
# ============================================================

class HardwareWorker(QThread):
    ui_mode = pyqtSignal(str)
    dropped = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.running = True
        self.session_enabled = False

    def stop(self):
        self.running = False

    def set_session(self, enabled):
        self.session_enabled = enabled

    def run(self):
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)

        GPIO.setup(GPIO_CAP, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
        GPIO.setup(GPIO_TRIG, GPIO.OUT)
        GPIO.setup(GPIO_ECHO, GPIO.IN)
        GPIO.setup(GPIO_SERVO, GPIO.OUT)

        pwm = GPIO.PWM(GPIO_SERVO, 50)
        pwm.start(0)

        def servo(angle):
            pwm.ChangeDutyCycle(angle_to_duty(angle))
            time.sleep(0.25)
            pwm.ChangeDutyCycle(0)

        servo(SERVO_CLOSED_DEG)

        while self.running:
            if not self.session_enabled:
                self.msleep(POLL_MS)
                continue

            cap = GPIO.input(GPIO_CAP) == 1

            GPIO.output(GPIO_TRIG, 0)
            time.sleep(0.0002)
            GPIO.output(GPIO_TRIG, 1)
            time.sleep(0.00001)
            GPIO.output(GPIO_TRIG, 0)

            start = time.monotonic()
            while GPIO.input(GPIO_ECHO) == 0:
                if time.monotonic() - start > 0.03:
                    break
            pulse_start = time.monotonic()
            while GPIO.input(GPIO_ECHO) == 1:
                if time.monotonic() - pulse_start > 0.03:
                    break
            pulse_end = time.monotonic()

            dist = (pulse_end - pulse_start) * 34300 / 2
            ultrasonic = dist < DIST_THRESHOLD_CM

            if cap and ultrasonic:
                self.ui_mode.emit("SCANNING")
                time.sleep(VERIFY_SECONDS)
                self.ui_mode.emit("DROPPING")
                servo(SERVO_OPEN_DEG)
                time.sleep(GATE_OPEN_MS/1000)
                servo(SERVO_CLOSED_DEG)
                self.dropped.emit()

            self.msleep(POLL_MS)

        GPIO.cleanup()

# ============================================================
# UI SCREENS
# ============================================================

class MainScreen(WaterBackground):
    def __init__(self, kiosk):
        super().__init__(QColor(0,153,170), QColor(34,197,94))
        self.kiosk = kiosk

        layout = QVBoxLayout(self)
        layout.setContentsMargins(60,90,60,70)

        title = QLabel("EcoByte")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setFont(QFont("Arial", 98, QFont.Weight.Bold))
        title.setStyleSheet("color:white;")

        start = QPushButton("START")
        start.setFixedSize(BTN_W, BTN_H)
        start.clicked.connect(self.kiosk.start_session)

        redeem = QPushButton("REDEEM LOAD")
        redeem.setFixedSize(BTN_W, BTN_H)
        redeem.clicked.connect(self.kiosk.go_redeem)

        layout.addWidget(title)
        layout.addStretch()
        layout.addWidget(start, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(redeem, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addStretch()

        SecretExitCorner(self.kiosk.exit_app, self)

class DepositScreen(WaterBackground):
    def __init__(self, kiosk):
        super().__init__(QColor(0,153,170), QColor(34,197,94))
        self.kiosk = kiosk

        layout = QVBoxLayout(self)
        layout.setContentsMargins(60,80,60,60)

        header = QLabel("Insert Bottle")
        header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.setFont(QFont("Arial", 66, QFont.Weight.Bold))
        header.setStyleSheet("color:white;")

        self.status = QLabel("Waiting for bottle...")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setFont(QFont("Arial", 34))
        self.status.setStyleSheet("color:white;")

        self.bottles = AnimatedNumberLabel("Bottles: ")
        self.bottles.setFont(QFont("Arial", 56, QFont.Weight.Bold))
        self.bottles.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.points = AnimatedNumberLabel("EcoPoints: ")
        self.points.setFont(QFont("Arial", 50, QFont.Weight.Bold))
        self.points.setAlignment(Qt.AlignmentFlag.AlignCenter)

        finish = QPushButton("FINISH")
        finish.setFixedSize(SMALL_BTN_W, SMALL_BTN_H)
        finish.clicked.connect(self.kiosk.finish_session)

        layout.addWidget(header)
        layout.addWidget(self.status)
        layout.addWidget(self.bottles)
        layout.addWidget(self.points)
        layout.addStretch()
        layout.addWidget(finish, alignment=Qt.AlignmentFlag.AlignCenter)

    def set_mode(self, mode):
        if mode == "SCANNING":
            self.status.setText("Scanning...")
        elif mode == "DROPPING":
            self.status.setText("Processing...")
        else:
            self.status.setText("Waiting for bottle...")

# ============================================================
# KIOSK CONTROLLER
# ============================================================

class Kiosk(QStackedWidget):
    def __init__(self):
        super().__init__()
        self.showFullScreen()
        self.setCursor(Qt.CursorShape.BlankCursor)

        self.main = MainScreen(self)
        self.deposit = DepositScreen(self)

        self.addWidget(self.main)
        self.addWidget(self.deposit)

        self.session_bottles = 0

        self.worker = HardwareWorker()
        self.worker.ui_mode.connect(self.deposit.set_mode)
        self.worker.dropped.connect(self.on_drop)
        self.worker.start()

        self.idle_timer = QTimer(self)
        self.idle_timer.timeout.connect(self.go_main)
        self.reset_idle()

    def mousePressEvent(self, e):
        self.reset_idle()
        super().mousePressEvent(e)

    def reset_idle(self):
        self.idle_timer.start(IDLE_TIMEOUT_MS)

    def go_main(self):
        self.worker.set_session(False)
        self.session_bottles = 0
        self.deposit.bottles.animate_to(0)
        self.deposit.points.animate_to(0)
        self.setCurrentWidget(self.main)

    def start_session(self):
        self.session_bottles = 0
        self.deposit.bottles.animate_to(0)
        self.deposit.points.animate_to(0)
        self.worker.set_session(True)
        self.setCurrentWidget(self.deposit)

    def on_drop(self):
        self.session_bottles += 1
        self.deposit.bottles.animate_to(self.session_bottles)
        self.deposit.points.animate_to(self.session_bottles * POINTS_PER_BOTTLE)

    def finish_session(self):
        self.go_main()

    def exit_app(self):
        self.worker.stop()
        QApplication.instance().quit()

# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    app = QApplication(sys.argv)
    kiosk = Kiosk()
    kiosk.show()
    sys.exit(app.exec())