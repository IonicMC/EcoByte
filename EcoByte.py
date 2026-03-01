import os
os.environ["QT_QPA_PLATFORM"] = "xcb"
os.environ["QT_AUTO_SCREEN_SCALE_FACTOR"] = "0"
os.environ["QT_SCALE_FACTOR"] = "1"
os.environ["QT_FONT_DPI"] = "96"

import sys
import time
import uuid
import json
import math
import random
import pygame
import RPi.GPIO as GPIO
import qrcode

from PyQt6.QtCore import *
from PyQt6.QtGui import *
from PyQt6.QtWidgets import *

# ============================================================
# CONFIG
# ============================================================

IDLE_TIMEOUT_MS = 15000
POINTS_PER_BOTTLE = 5

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

BTN_W = 600
BTN_H = 124
SMALL_BTN_W = 320
SMALL_BTN_H = 98

# ============================================================
# SOUND MANAGER
# ============================================================

class SoundManager:
    def __init__(self, base_dir):
        try:
            pygame.mixer.init()
            self.ok = True
        except:
            self.ok = False
            print("Sound disabled")

        def p(name): return os.path.join(base_dir, "sounds", name)

        self.idle = p("idle.wav")
        self.tap_s = p("tap.wav")
        self.bottle_s = p("bottle.wav")
        self.success_s = p("success.wav")

    def play_idle(self):
        if not self.ok: return
        pygame.mixer.music.load(self.idle)
        pygame.mixer.music.set_volume(0.5)
        pygame.mixer.music.play(-1)

    def stop_idle(self):
        if self.ok:
            pygame.mixer.music.stop()

    def sfx(self, path, vol=0.9):
        if not self.ok: return
        s = pygame.mixer.Sound(path)
        s.set_volume(vol)
        s.play()

    def tap(self): self.sfx(self.tap_s)
    def bottle(self): self.sfx(self.bottle_s)
    def success(self): self.sfx(self.success_s)

# ============================================================
# HELPERS
# ============================================================

def clamp(x, lo, hi):
    return max(lo, min(hi, x))

def angle_to_duty(angle):
    return 2.5 + (angle / 180.0) * 10.0

# ============================================================
# GRADIENT TITLE
# ============================================================

class GradientTitle(QWidget):
    def __init__(self, text="EcoByte"):
        super().__init__()
        self.text = text
        self.setFixedHeight(170)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        font = QFont("Arial", 100, QFont.Weight.Bold)
        p.setFont(font)
        fm = p.fontMetrics()

        tw = fm.horizontalAdvance(self.text)
        th = fm.height()

        x = (self.width() - tw)//2
        y = (self.height() + th)//2 - fm.descent()

        grad = QLinearGradient(x,0,x+tw,0)
        grad.setColorAt(0,QColor(80,220,120))
        grad.setColorAt(1,QColor(30,140,255))

        path = QPainterPath()
        path.addText(float(x),float(y),font,self.text)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0,0,0,45))
        shadow = QPainterPath(path)
        shadow.translate(0,6)
        p.drawPath(shadow)

        p.setBrush(grad)
        p.drawPath(path)

# ============================================================
# BACKGROUND
# ============================================================

class WaterBackground(QWidget):
    def __init__(self):
        super().__init__()
        self.phase = 0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(33)

    def tick(self):
        self.phase += 0.08
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        w,h = self.width(), self.height()

        grad = QLinearGradient(0,0,0,h)
        grad.setColorAt(0,QColor(0,153,170))
        grad.setColorAt(1,QColor(34,197,94))
        p.fillRect(self.rect(),grad)

        # soft top fade
        fade = QLinearGradient(0,0,0,int(h*0.3))
        fade.setColorAt(0,QColor(255,255,255,25))
        fade.setColorAt(1,QColor(255,255,255,0))
        p.fillRect(0,0,w,int(h*0.3),fade)

        # waves
        def wave(y_base,amp,freq,shift):
            path = QPainterPath()
            path.moveTo(0,h)
            path.lineTo(0,y_base)
            for x in range(0,w,20):
                y = y_base + amp*math.sin(x*freq+self.phase+shift)
                path.lineTo(x,y)
            path.lineTo(w,h)
            path.closeSubpath()
            return path

        p.setBrush(QColor(255,255,255,40))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPath(wave(int(h*0.7),28,0.016,0))
        p.setBrush(QColor(255,255,255,30))
        p.drawPath(wave(int(h*0.78),18,0.02,1.4))

# ============================================================
# BUTTONS
# ============================================================

def primary_btn(text):
    b=QPushButton(text)
    b.setFixedSize(BTN_W,BTN_H)
    b.setFont(QFont("Arial",26,QFont.Weight.Bold))
    b.setStyleSheet("""
        QPushButton{
        background: rgba(255,255,255,0.18);
        color:white;
        border:2px solid rgba(255,255,255,0.34);
        border-radius:28px;}
        QPushButton:pressed{background:rgba(255,255,255,0.26);}
    """)
    return b

def secondary_btn(text):
    b=QPushButton(text)
    b.setFixedSize(BTN_W,BTN_H)
    b.setFont(QFont("Arial",24,QFont.Weight.Bold))
    b.setStyleSheet("""
        QPushButton{
        background: rgba(255,255,255,0.95);
        color:#0B7A3B;
        border-radius:28px;}
    """)
    return b

# ============================================================
# MAIN SCREEN
# ============================================================

class MainScreen(WaterBackground):
    def __init__(self,kiosk):
        super().__init__()
        self.kiosk=kiosk

        layout=QVBoxLayout(self)
        layout.setContentsMargins(60,80,60,60)
        layout.setSpacing(16)

        title=GradientTitle("EcoByte")

        subtitle=QLabel("From Plastic, to Fantastic!")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setFont(QFont("Arial",26))
        subtitle.setStyleSheet("color:white;")

        start=primary_btn("START")
        redeem=secondary_btn("REDEEM LOAD")

        start.clicked.connect(self.kiosk.start_session)
        start.clicked.connect(self.kiosk.snd.tap)
        redeem.clicked.connect(self.kiosk.go_redeem)
        redeem.clicked.connect(self.kiosk.snd.tap)

        layout.addStretch(2)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addSpacing(20)
        layout.addWidget(start,alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(redeem,alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addStretch(3)

# ============================================================
# HARDWARE WORKER
# ============================================================

class HardwareWorker(QThread):
    dropped=pyqtSignal()

    def run(self):
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(GPIO_CAP,GPIO.IN,pull_up_down=GPIO.PUD_DOWN)
        GPIO.setup(GPIO_SERVO,GPIO.OUT)
        pwm=GPIO.PWM(GPIO_SERVO,50)
        pwm.start(0)

        while True:
            if GPIO.input(GPIO_CAP)==1:
                pwm.ChangeDutyCycle(angle_to_duty(SERVO_OPEN_DEG))
                time.sleep(0.4)
                pwm.ChangeDutyCycle(angle_to_duty(SERVO_CLOSED_DEG))
                time.sleep(0.4)
                self.dropped.emit()
            time.sleep(0.2)

# ============================================================
# KIOSK
# ============================================================

class Kiosk(QStackedWidget):
    def __init__(self):
        super().__init__()
        self.showFullScreen()
        self.setCursor(Qt.CursorShape.BlankCursor)

        self.snd=SoundManager(os.path.dirname(os.path.abspath(__file__)))
        self.snd.play_idle()

        self.main=MainScreen(self)
        self.addWidget(self.main)
        self.setCurrentWidget(self.main)

        self.worker=HardwareWorker()
        self.worker.dropped.connect(self.bottle_dropped)
        self.worker.start()

        self.count=0

    def start_session(self):
        self.count=0

    def go_redeem(self):
        pass

    def bottle_dropped(self):
        self.count+=1
        self.snd.bottle()

# ============================================================
# RUN
# ============================================================

if __name__=="__main__":
    app=QApplication(sys.argv)
    k=Kiosk()
    k.show()
    sys.exit(app.exec())
