import requests
from dotenv import load_dotenv
import os

load_dotenv()

api = os.getenv("api_key")
 
def create_contact( name, phone_number):
    url = "https://shreenikalitebackend.prioritytechnologiess.com/contacts"
    
    headers = {
        "x-api-key": api,
        "Content-Type": "application/json"
    }
    
    payload = {
        "firstName": name,
        "lastName": "ji",
        "email": "info@nishahomes.com",
        "phone": phone_number,
        "company": {
            "name": "nishahomes"
        }
    }
    
    try:
        response = requests.post(url, json=payload, headers=headers)
        
        # Raise error if request failed
        response.raise_for_status()
        
        return {
            "status": "success",
            "data": response.json()
        }
    
    except requests.exceptions.RequestException as e:
        return {
            "status": "error",
            "message": str(e),
            "response": response.text if 'response' in locals() else None
        }



