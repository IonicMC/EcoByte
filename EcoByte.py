import serial
import time

# --- CONFIGURATION ---
USB_PORT = "/dev/ttyUSB0"   # Change if your ls command showed something else
BAUD_RATE = 9600            # Standard speed for SIM800C
PHONE_NUMBER = "+639694837544"  # Replace with your actual phone number!
TEST_MESSAGE = "Hello from EcoByte! Your SIM800C is working perfectly."

def send_at_command(ser, command, wait_time=0.5):
    """Sends an AT command and prints the module's response."""
    print(f"Sending: {command.strip()}")
    ser.write((command + "\r").encode())
    time.sleep(wait_time)
    
    response = ""
    while ser.in_waiting > 0:
        response += ser.read(ser.in_waiting).decode('utf-8', errors='ignore')
    
    print(f"Response: \n{response}")
    return response

def test_sms():
    print(f"Opening connection to {USB_PORT}...")
    try:
        # Open the serial port
        ser = serial.Serial(USB_PORT, BAUD_RATE, timeout=1)
        time.sleep(2) # Give it a second to initialize
        
        print("\n--- Checking Module Status ---")
        send_at_command(ser, "AT") # Check if the module is responding
        
        print("\n--- Setting up SMS Mode ---")
        send_at_command(ser, "AT+CMGF=1") # Set SMS to Text Mode (instead of PDU hex mode)
        
        print(f"\n--- Sending Message to {PHONE_NUMBER} ---")
        # Start the SMS prompt
        ser.write((f'AT+CMGS="{PHONE_NUMBER}"\r').encode())
        time.sleep(1)
        
        # Send the actual message text
        ser.write(TEST_MESSAGE.encode())
        time.sleep(0.5)
        
        # Send Ctrl+Z (ASCII 26) to tell the module to send the text
        print("Sending Ctrl+Z to dispatch message...")
        ser.write(bytes([26])) 
        
        # Give it up to 5 seconds to send over the cellular network
        time.sleep(5)
        
        response = ""
        while ser.in_waiting > 0:
            response += ser.read(ser.in_waiting).decode('utf-8', errors='ignore')
        print(f"Final Response: \n{response}")

        if "OK" in response:
            print("\nSUCCESS: The text message was sent!")
        else:
            print("\nERROR: Message may not have sent. Check the response above.")

        ser.close()

    except serial.SerialException as e:
        print(f"\nUSB Error: Could not connect to {USB_PORT}.")
        print(f"Details: {e}")
        print("Try running with 'sudo python test_sms.py' if it's a permissions issue.")

if __name__ == "__main__":
    test_sms()
