import RPi.GPIO as GPIO
import time

CAP_PIN = 17

GPIO.setmode(GPIO.BCM)
GPIO.setup(CAP_PIN, GPIO.IN)

try:
    while True:
        val = GPIO.input(CAP_PIN)
        print("Raw sensor value:", val)
        time.sleep(0.2)
except KeyboardInterrupt:
    GPIO.cleanup()