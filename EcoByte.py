import RPi.GPIO as GPIO
import time

# Pin configuration
GPIO_IR = 13

def test_sensor():
    GPIO.setmode(GPIO.BCM)
    # Pull-up resistor ensures it reads HIGH when disconnected
    GPIO.setup(GPIO_IR, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    
    print("Testing HW-201 IR Sensor on GPIO 13...")
    print("Press Ctrl+C to stop.")
    print("-" * 30)

    try:
        while True:
            # Read the sensor state
            state = GPIO.input(GPIO_IR)
            
            if state == GPIO.LOW:
                print("[0] LOW  - Object DETECTED!")
            else:
                print("[1] HIGH - Path is clear.")
                
            time.sleep(0.2) # Update 5 times a second
            
    except KeyboardInterrupt:
        print("\nTest stopped.")
    finally:
        GPIO.cleanup()

if __name__ == "__main__":
    test_sensor()
