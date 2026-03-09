import requests

# --- YOUR ECOBYTE CREDENTIALS ---
BOT_TOKEN = "8421998322:AAFlinuF5YnQXbxdzga27ntV8Db6KO4Ut1Q"
CHAT_ID = "7122838385"

MESSAGE = "🌿 EcoByte Alert: ₱10.00 Regular Load has been successfully credited! Thank you for recycling."

def test_telegram_sms():
    print("Sending request to Telegram Servers...")
    
    # The official Telegram API URL
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    
    # The data we are sending
    payload = {
        "chat_id": CHAT_ID,
        "text": MESSAGE
    }
    
    try:
        response = requests.post(url, json=payload, timeout=5)
        
        if response.status_code == 200:
            print("\nSUCCESS! The message was sent.")
            print("Check your Telegram app, your phone should have just buzzed!")
        else:
            print(f"\nERROR: Telegram rejected it. Code: {response.status_code}")
            print(f"Details: {response.text}")
            print("\nDid you remember to press 'Start' or message the bot first?")
            
    except requests.exceptions.RequestException as e:
        print(f"\nNetwork Error: {e}")

if __name__ == "__main__":
    test_telegram_sms()
