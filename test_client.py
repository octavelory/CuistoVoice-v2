from utils.api_client import APIclient

DEV_SERVER_URL = "http://localhost:3000"
USER_EMAIL = "octave.lory@gmail.com"
USER_PASSWORD = "octave1234"

def run_demo():
    client = APIclient(base_url=DEV_SERVER_URL)

    if not client.login(email=USER_EMAIL, password=USER_PASSWORD):
        print("Impossible de continuer. Vérifiez vos identifiants.")
        return
    
    client