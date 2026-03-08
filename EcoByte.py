import RPi.GPIO as GPIO
import time

SENSOR_PIN = 17  # GPIO17 (Pin 11)

GPIO.setmode(GPIO.BCM)
GPIO.setup(SENSOR_PIN, GPIO.IN)

print("Capacitive sensor test running...")

try:
    while True:
        if GPIO.input(SENSOR_PIN):
            print("Bottle detected")
        else:
            print("No bottle")

        time.sleep(0.5)

except KeyboardInterrupt:
    print("Program stopped")

finally:
    GPIO.cleanup()
