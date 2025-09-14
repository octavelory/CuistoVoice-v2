from pathlib import Path
import time
import jwt
import requests
import os
from cryptography.hazmat.primitives import serialization
from utils.api_client import api_client

def _read_cpu_serial_number():
    try:
        with open("/proc/cpuinfo", "r") as f:
            for line in f:
                if line.startswith("Serial"):
                    return line.split(":")[1].strip()
    except FileNotFoundError:
        print(f"Mode Windows activé, impossible de lire /proc/cpuinfo.")
        return "dev-device"
    return "unknown"

def _load_private_key(key_path: Path):
    """Charge la clé privée depuis un fichier et la retourne comme un objet utilisable."""
    if not key_path.exists():
        print(f"Erreur: Fichier de clé privée non trouvé à '{key_path}'!")
        return None
    
    with open(key_path, "rb") as f:
        # On charge la clé en utilisant la bibliothèque de cryptographie
        # pour obtenir un objet clé, pas juste une chaîne de caractères.
        private_key = serialization.load_pem_private_key(
            f.read(),
            password=None # Si votre clé n'est pas protégée par un mot de passe
        )
    return private_key

def award_badge(badge_id: str) -> bool:
    private_key_path = Path("device_private.key")
    
    # 1. Charger la clé privée correctement
    private_key = _load_private_key(private_key_path)
    if not private_key:
        return None

    # 2. Définir le Key ID (kid)
    # Il est crucial que ce 'kid' corresponde à celui que vous avez utilisé
    # pour enregistrer la clé publique dans Redis.
    key_id = f"cuistovoice-{_read_cpu_serial_number()}"
    # 3. Préparer le payload et les en-têtes du JWT
    payload = {
        "iss": key_id,  # L'issuer est souvent le kid lui-même
        "iat": int(time.time()),
        "exp": int(time.time()) + 60,  # Expiration dans 60 secondes
        "sub": api_client.user_id
    }
    
    headers = {
        "kid": key_id  # L'en-tête 'kid' est essentiel pour le serveur
    }

    # 4. Encoder le token avec le BON algorithme (ES256)
    try:
        token = jwt.encode(
            payload,
            private_key,
            algorithm="ES256",
            headers=headers
        )
    except Exception as e:
        print(f"Erreur lors de l'encodage du JWT: {e}")
        return None

    request_headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
    api_url = os.environ.get("BASE_URL", "http://localhost:3000") + "/api/badges/award"

    # 6. Envoyer la requête
    try:
        response = requests.post(
            api_url,
            json={"badgeId": badge_id},
            headers=request_headers
        )
    except requests.RequestException as e:
        print(f"Erreur lors de la requête pour le badge: {e}")
        return None

    if response.status_code == 200:
        print("[award_badge] Badge attribué avec succès.")
        return response.json().get("ephemeral_key")
    else:
        print(f"Erreur lors de la récupération du badge (status {response.status_code}): {response.text}")
        return None
    
# Test de la fonction award_badge
if __name__ == "__main__":
    badge_id = "cooking_pro"  # Remplacez par un ID de badge valide pour le test
    result = award_badge(badge_id)
    if result:
        print(f"Badge attribué avec succès, clé éphémère: {result}")
    else:
        print("Échec de l'attribution du badge.")