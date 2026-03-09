import serial
import time

USB_PORT = "/dev/ttyUSB0"

def find_module():
    print(f"Scanning {USB_PORT} for SIM800C...")
    
    # Try the two most common speeds
    for baud in [9600, 115200]:
        print(f"\n--- Trying Baud Rate: {baud} ---")
        try:
            ser = serial.Serial(USB_PORT, baud, timeout=1)
            time.sleep(1)
            
            # Spam 'AT' 3 times to sync the auto-baud feature
            for i in range(3):
                print(f"Sending AT (Attempt {i+1})...")
                ser.write(b"AT\r\n")
                time.sleep(0.5)
                
                response = ""
                while ser.in_waiting > 0:
                    response += ser.read(ser.in_waiting).decode('utf-8', errors='ignore')
                
                if response:
                    print(f"Response received:\n{response.strip()}")
                
                if "OK" in response:
                    print(f"\nSUCCESS! The module is alive and running at {baud} baud.")
                    ser.close()
                    return baud
                    
            ser.close()
        except serial.SerialException as e:
            print(f"Port error: {e}")

    print("\nFAILED: No response from module at any speed.")
    return None

if __name__ == "__main__":
    find_module()
