import requests
import time
import json

# 🔐 Credentials
API_KEY = "c46296c5-9ed7-4bb9-84f7-f4201d11c1ba"
API_URL = "https://api-smartflo.tatateleservices.com/v1/click_to_call_support"
DID_NUMBER = "8065251632"       # Your Smartflo number
CUSTOMER_NUMBER = "8920419130"    # Hardcoded target
AGENT_NUMBER = "917303515710"


def make_call():
    # Note: changed 'customer_number' to 'destination_number'
    # as per standard Smartflo V1 documentation
    payload = {
        "agent_number": AGENT_NUMBER,
        "destination_number": CUSTOMER_NUMBER, 
        "caller_id": DID_NUMBER,
        "api_key": "trans"
    }

    headers = {
        "Authorization": API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    try:
        response = requests.post(API_URL, data=json.dumps(payload), headers=headers)
        
        print(f"Status Code: {response.status_code}")
        print(f"Response: {response.text}")
        
    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    make_call()