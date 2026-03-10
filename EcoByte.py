import RPi.GPIO as GPIO
import time
import json
import requests
from threading import Thread

# --- GPIO SETUP ---
GPIO_IR = 17         # IR Sensor for bottle drop
GPIO_SERVO = 18      # Servo for the separator arm
# ... (Keep your Ultrasonic and Other GPIO pins here)

class EcoByteKiosk:
    def __init__(self):
        self.is_processing_bottle = False  # Lock to stop other sensors
        self.used_redeem_tokens = []       # Prevent double-scanning phone QR
        self.points = 0
        self.bottles = 0
        self.session_token = f"T-{int(time.time())}" # Unique ID for this scan session
        
        # ... (Initialize your GPIO and UI here)

    def main_loop(self):
        """The brain of your sensor logic"""
        while True:
            # ONLY run YOLO/Ultrasonic if we aren't waiting for a drop
            if not self.is_processing_bottle:
                # 1. Ultrasonic check
                if self.get_distance() < 10: 
                    # 2. YOLO check
                    if self.detect_bottle():
                        self.accept_bottle()
            
            time.sleep(0.1)

    def accept_bottle(self):
        """Called when YOLO confirms a bottle"""
        self.is_processing_bottle = True  # STOPS all other scanning
        print("Bottle accepted! Waiting for drop...")
        
        # Open Servo
        self.set_servo_angle(90) 
        time.sleep(0.5)
        
        # Wait for IR Trigger (The "Faster" Logic)
        start_wait = time.time()
        while GPIO.input(GPIO_IR) == GPIO.HIGH: # Wait for beam break
            if time.time() - start_wait > 5: # 5 second timeout safety
                print("Drop timeout")
                break
            time.sleep(0.01)

        # IR Triggered!
        self.points += 5
        self.bottles += 1
        self.update_firebase_session()
        
        # Close Servo & Resume
        self.set_servo_angle(0)
        time.sleep(0.5) # Wait for arm to clear
        self.is_processing_bottle = False # RESUMES sensors
        print("Ready for next bottle.")

    def update_firebase_session(self):
        """Sends points to Firebase so the App can scan the Kiosk QR"""
        data = {
            "points": self.points,
            "bottles": self.bottles,
            "used": False,
            "token": self.session_token
        }
        # Path: tokens/T-12345678
        requests.put(f"{FB_URL}/tokens/{self.session_token}.json", json=data)

    def on_redeem_scanned(self, scanned_text):
        """Handles the QR from the User's Phone"""
        try:
            # 1. Prevent Double-Scan of the same phone QR
            if scanned_text in self.used_redeem_tokens:
                print("QR Already Used!")
                self.ui_show_error("ALREADY CLAIMED")
                return

            data = json.loads(scanned_text)
            if data.get("type") == "redeem":
                amount = data.get("amount")
                number = data.get("number")

                # 2. Add to history so it can't be used again
                self.used_redeem_tokens.append(scanned_text)

                # 3. Process the load and send Telegram
                self.send_telegram_load(number, amount)
                self.ui_show_success(amount)
                
                # 4. Auto-Reset Machine after 7 seconds
                from PyQt5.QtCore import QTimer
                QTimer.singleShot(7000, self.go_main_screen)

        except Exception as e:
            print(f"Invalid QR: {e}")

    def go_main_screen(self):
        """Resets everything for the next user"""
        self.points = 0
        self.bottles = 0
        self.session_token = f"T-{int(time.time())}"
        self.is_processing_bottle = False
        self.update_ui_to_home()
