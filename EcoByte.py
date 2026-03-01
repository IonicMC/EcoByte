# ==============================
# ECOBYTE KIOSK (FULL VERSION)
# ==============================

import os
import sys
import time
import uuid
import json
import math
import random

os.environ["QT_QPA_PLATFORM"] = "xcb"
os.environ["QT_QPA_PLATFORMTHEME"] = ""

import pygame
import RPi.GPIO as GPIO

from PyQt6.QtCore import *
from PyQt6.QtGui import *
from PyQt6.QtWidgets import *

# ============================================================
# SOUND MANAGER (pygame)
# ============================================================

class SoundManager:
    def __init__(self):
        pygame.mixer.init()
        self.base_path = os.path.join(os.path.dirname(__file__), "sounds")

        def load(name):
            path = os.path.join(self.base_path, name)
            if os.path.exists(path):
                return pygame.mixer.Sound(path)
            return None

        # Music
        self.music_path = os.path.join(self.base_path, "idle.mp3")
        if os.path.exists(self.music_path):
            pygame.mixer.music.load(self.music_path)
            pygame.mixer.music.set_volume(0.7)
            pygame.mixer.music.play(-1)

        # SFX
        self.tap = load("tap.mp3")
        self.success = load("success.mp3")
        self.scan_ok = load("scan_ok.mp3")
        self.qr_show = load("qr_show.mp3")

    def set_main_volume(self):
        pygame.mixer.music.set_volume(0.7)

    def set_low_volume(self):
        pygame.mixer.music.set_volume(0.2)

    def play_tap(self):
        if self.tap:
            self.tap.play()

    def play_success(self):
        if self.success:
            self.success.play()

    def play_scan(self):
        if self.scan_ok:
            self.scan_ok.play()

    def play_qr(self):
        if self.qr_show:
            self.qr_show.play()

# ============================================================
# WATER BACKGROUND (NO HARD LINE)
# ============================================================

class WaterBackground(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.phase = 0
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_anim)
        self.timer.start(33)

    def update_anim(self):
        self.phase += 0.07
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()

        # Smooth gradient (no hard line)
        grad = QLinearGradient(0, 0, 0, h)
        grad.setColorAt(0, QColor(0,153,170))
        grad.setColorAt(1, QColor(34,197,94))
        p.fillRect(self.rect(), grad)

        # Soft top fade (no hard band)
        fade = QLinearGradient(0,0,0,int(h*0.3))
        fade.setColorAt(0,QColor(255,255,255,40))
        fade.setColorAt(1,QColor(255,255,255,0))
        p.fillRect(0,0,w,int(h*0.3),fade)

# ============================================================
# MAIN SCREEN
# ============================================================

class MainScreen(WaterBackground):
    def __init__(self, kiosk):
        super().__init__()
        self.kiosk = kiosk

        layout = QVBoxLayout(self)
        layout.setContentsMargins(60,100,60,80)
        layout.setSpacing(30)

        title = QLabel("EcoByte")
        title.setFont(QFont("Arial",100,QFont.Weight.Bold))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("color:white;")

        start = QPushButton("START")
        start.setFixedSize(600,120)
        start.clicked.connect(self.start_clicked)

        redeem = QPushButton("REDEEM LOAD")
        redeem.setFixedSize(600,120)
        redeem.clicked.connect(self.redeem_clicked)

        layout.addWidget(title)
        layout.addStretch()
        layout.addWidget(start, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(redeem, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addStretch()

    def start_clicked(self):
        self.kiosk.sound.play_tap()
        self.kiosk.start_session()

    def redeem_clicked(self):
        self.kiosk.sound.play_tap()
        self.kiosk.go_redeem()

# ============================================================
# REDEEM SCREEN (ARROW FIXED + GLOW)
# ============================================================

class RedeemScreen(WaterBackground):
    def __init__(self, kiosk):
        super().__init__()
        self.kiosk = kiosk

        layout = QVBoxLayout(self)
        layout.setContentsMargins(60,100,60,80)

        title = QLabel("Redeem Load")
        title.setFont(QFont("Arial",64,QFont.Weight.Bold))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("color:white;")

        instruction = QLabel("Show your Redeem QR Code\non the MIT App\nScan on the RIGHT →")
        instruction.setWordWrap(True)
        instruction.setAlignment(Qt.AlignmentFlag.AlignCenter)
        instruction.setFont(QFont("Arial",32))
        instruction.setStyleSheet("color:white;")

        self.arrow_offset = 0
        self.arrow_dir = 1

        self.timer = QTimer()
        self.timer.timeout.connect(self.animate_arrow)
        self.timer.start(16)

        self.scan_input = QLineEdit()
        self.scan_input.setFixedSize(1,1)
        self.scan_input.returnPressed.connect(self.qr_scanned)

        layout.addWidget(title)
        layout.addWidget(instruction)
        layout.addStretch()
        layout.addWidget(self.scan_input)

    def animate_arrow(self):
        self.arrow_offset += self.arrow_dir * 1.5
        if self.arrow_offset > 20:
            self.arrow_dir = -1
        if self.arrow_offset < -5:
            self.arrow_dir = 1
        self.update()

    def paintEvent(self, e):
        super().paintEvent(e)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()

        cx = min(w-200, w*0.8 + self.arrow_offset)
        cy = h*0.55

        path = QPainterPath()
        path.moveTo(cx-80, cy-40)
        path.lineTo(cx+40, cy)
        path.lineTo(cx-80, cy+40)
        path.closeSubpath()

        # Glow effect
        for i in range(6):
            pen = QPen(QColor(255,255,255,40 - i*6), 10+i*4)
            p.setPen(pen)
            p.drawPath(path)

        p.setBrush(QColor(255,255,255,200))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPath(path)

    def qr_scanned(self):
        self.kiosk.sound.play_scan()
        QTimer.singleShot(2000, self.kiosk.go_main)

# ============================================================
# KIOSK CONTROLLER
# ============================================================

class Kiosk(QStackedWidget):
    def __init__(self):
        super().__init__()
        self.showFullScreen()
        self.setCursor(Qt.CursorShape.BlankCursor)

        self.sound = SoundManager()

        self.main = MainScreen(self)
        self.redeem = RedeemScreen(self)

        self.addWidget(self.main)
        self.addWidget(self.redeem)

        self.setCurrentWidget(self.main)

    def start_session(self):
        self.sound.set_low_volume()
        # deposit logic here

    def go_redeem(self):
        self.sound.set_low_volume()
        self.setCurrentWidget(self.redeem)

    def go_main(self):
        self.sound.set_main_volume()
        self.setCurrentWidget(self.main)

# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    app = QApplication(sys.argv)
    kiosk = Kiosk()
    kiosk.show()
    sys.exit(app.exec())
