import serial
import time

USB_PORT = "/dev/ttyUSB0"
BAUD_RATE = 9600

def jumpstart_module():
    print(f"Attempting to jumpstart LC SIM800C V3 on {USB_PORT}...")
    
    try:
        # Open the serial port
        ser = serial.Serial(USB_PORT, BAUD_RATE, timeout=1)
        
        # --- THE DIGITAL POWER BUTTON PRESS ---
        print("Holding the virtual power button...")
        ser.dtr = True
        ser.rts = True
        time.sleep(2)  # Hold for 2 seconds
        
        print("Releasing the power button...")
        ser.dtr = False
        ser.rts = False
        time.sleep(2)  # Wait for it to boot
        
        # --------------------------------------
        
        print("\nSending 'AT' Wake-up calls...")
        # Spam AT to sync the baud rate
        for i in range(5):
            ser.write(b"AT\r\n")
            time.sleep(0.5)
            
            response = ""
            while ser.in_waiting > 0:
                response += ser.read(ser.in_waiting).decode('utf-8', errors='ignore')
            
            if response.strip():
                print(f"Response: {response.strip()}")
                
            if "OK" in response:
                print("\nSUCCESS! The chip is awake and responding!")
                ser.close()
                return

        print("\nFailed to get 'OK'. Look at the USB module, is the LED flashing?")
        ser.close()

    except serial.SerialException as e:
        print(f"Error opening port: {e}")
        print("Make sure you are running this with: sudo python jumpstart.py")

if __name__ == "__main__":
    jumpstart_module()
