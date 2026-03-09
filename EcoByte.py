import requests

# --- CONFIGURATION ---
SEMAPHORE_API_KEY = "6ae5cec324c152ff42011d3aa598b26a"  # Your API key from the screenshot
PHONE_NUMBER = "09694837544"                            # <-- REPLACE THIS with your real Globe/Smart/DITO number
MESSAGE = "EcoByte Alert: ₱10 Load has been successfully credited to your number! Thank you for recycling."

def test_semaphore_sms():
    print(f"Sending Web API request to Semaphore for {PHONE_NUMBER}...")
    
    url = "https://api.semaphore.co/api/v4/messages"
    payload = {
        "apikey": SEMAPHORE_API_KEY,
        "number": PHONE_NUMBER,
        "message": MESSAGE
    }
    
    try:
        # Send the request to Semaphore
        response = requests.post(url, data=payload, timeout=5)
        
        # Check if it was successful
        if response.status_code == 200:
            print("\nSUCCESS! Semaphore accepted the request.")
            print("Check your phone inbox!")
            print(f"Semaphore Server Reply: {response.json()}")
        else:
            print(f"\nERROR: Semaphore rejected it. Code: {response.status_code}")
            print(f"Reply: {response.text}")
            
    except requests.exceptions.RequestException as e:
        print(f"\nNetwork Error: Could not reach Semaphore. Check Wi-Fi.\nDetails: {e}")

if __name__ == "__main__":
    test_semaphore_sms()
