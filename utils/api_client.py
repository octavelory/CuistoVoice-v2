import requests
import json
from urllib.parse import quote
import os
from dotenv import load_dotenv

load_dotenv()

class APIClient:
    """
    Un client Python complet pour interagir avec l'API CuistoVoice.
    Gère l'authentification et la session pour effectuer toutes les actions
    disponibles au nom d'un utilisateur authentifié.
    """
    def __init__(self, base_url: str = os.environ.get("BASE_URL", "http://localhost:3000")):
        """
        Initialise le client.
        :param base_url: L'URL de base de votre application Next.js
        """
        if base_url.endswith('/'):
            base_url = base_url[:-1]
        self.base_url = base_url
        self.session = requests.Session()
        self.user_id = None
        self.user_email = None
        self._config = None

    def _get_csrf_token(self) -> str:
        """Récupère le token CSRF nécessaire pour la connexion."""
        try:
            url = f"{self.base_url}/api/auth/csrf"
            response = self.session.get(url)
            response.raise_for_status()
            return response.json().get("csrfToken")
        except requests.exceptions.RequestException as e:
            print(f"Erreur lors de la récupération du token CSRF: {e}")
            raise ConnectionError("Impossible de récupérer le token CSRF du serveur.")

    def login(self, email: str, password: str) -> bool:
        """Authentifie le client auprès de l'API CuistoVoice."""
        try:
            csrf_token = self._get_csrf_token()
            if not csrf_token: return False

            login_url = f"{self.base_url}/api/auth/callback/credentials"
            payload = {"email": email, "password": password, "csrfToken": csrf_token, "json": "true"}
            
            response = self.session.post(login_url, data=payload)
            response.raise_for_status()

            session_data = self.get_session()
            if session_data and session_data.get("user"):
                self.user_id = session_data["user"].get("id")
                self.user_email = session_data["user"].get("email")
                print(f"Connexion réussie en tant que {self.user_email} (ID: {self.user_id})")
                return True
            else:
                print("Échec de la connexion: email ou mot de passe incorrect.")
                return False
        except requests.exceptions.RequestException as e:
            print(f"Erreur lors de la tentative de connexion: {e}")
            return False

    def get_session(self) -> dict:
        """Récupère les informations de la session actuelle."""
        if not self.session: return {}
        try:
            url = f"{self.base_url}/api/auth/session"
            response = self.session.get(url)
            response.raise_for_status()
            return response.json()
        except (requests.exceptions.RequestException, json.JSONDecodeError):
            return {}

    def _make_request(self, method: str, endpoint: str, **kwargs) -> dict:
        """Méthode helper pour effectuer des requêtes API authentifiées."""
        if not self.user_id:
            raise PermissionError("Vous devez être connecté pour effectuer cette action.")
        
        url = f"{self.base_url}/api/{endpoint}"
        try:
            response = self.session.request(method, url, **kwargs)
            response.raise_for_status()
            if response.status_code == 204 or not response.content:
                return {"success": True, "status_code": response.status_code}
            return response.json()
        except requests.exceptions.HTTPError as e:
            print(f"Erreur HTTP: {e.response.status_code} - {e.response.text}")
            raise
        except requests.exceptions.RequestException as e:
            print(f"Erreur de connexion: {e}")
            raise

    # --- Configuration Utilisateur (user:<userId>:config, Hash) ---
    def get_config(self, force_refresh: bool = False) -> dict:
        """
        Récupère la configuration de l'utilisateur (clé Redis : user:<userId>:config).
        Champs : name, mainLanguage, location.
        """
        if self._config is None or force_refresh:
            self._config = self._make_request("GET", "config")
        return self._config

    def update_config(self, name: str, main_language: str, location: str) -> dict:
        """
        Met à jour la configuration de l'utilisateur (clé Redis : user:<userId>:config).
        """
        payload = {"name": name, "mainLanguage": main_language, "location": location}
        response = self._make_request("POST", "config", json=payload)
        self.get_config(force_refresh=True)
        return response

    @property
    def name(self) -> str:
        return self.get_config().get("name", "")

    @property
    def location(self) -> str:
        return self.get_config().get("location", "")

    @property
    def main_language(self) -> str:
        return self.get_config().get("mainLanguage", "")

    # --- Minuteurs (Timers) (user:<userId>:timers, Hash, TTL par champ via HEXPIRE) ---
    def get_timers(self) -> dict:
        """
        Récupère la liste des minuteurs actifs (clé Redis : user:<userId>:timers).
        Chaque champ = timer_id, valeur = JSON (id, name, duration, start).
        """
        return self._make_request("GET", "timers")

    def add_timer(self, name: str, duration_seconds: int) -> dict:
        """
        Ajoute un nouveau minuteur (clé Redis : user:<userId>:timers).
        """
        return self._make_request("POST", "timers", json={"name": name, "duration": duration_seconds})

    def delete_timer(self, timer_id: str) -> dict:
        """
        Supprime un minuteur par son ID (clé Redis : user:<userId>:timers).
        """
        return self._make_request("DELETE", f"timers/{timer_id}")

    def clear_all_timers(self) -> dict:
        """
        Supprime tous les minuteurs de l'utilisateur (clé Redis : user:<userId>:timers).
        """
        return self._make_request("POST", "timers/clear")

    # --- Souvenirs (Memories) (user:<userId>:memories, Hash) ---
    def get_memories(self) -> dict:
        """
        Récupère tous les souvenirs de l'utilisateur (clé Redis : user:<userId>:memories).
        Chaque champ = memory_id, valeur = JSON (title, content, added).
        """
        return self._make_request("GET", "memories")

    def add_memory(self, title: str, content: str) -> dict:
        """
        Ajoute un nouveau souvenir (clé Redis : user:<userId>:memories).
        """
        return self._make_request("POST", "memories", json={"title": title, "content": content})

    def update_memory(self, memory_id: str, title: str, content: str) -> dict:
        """
        Met à jour un souvenir existant (clé Redis : user:<userId>:memories).
        """
        return self._make_request("POST", f"memories/edit/{memory_id}", json={"title": title, "content": content})

    def delete_memory(self, memory_id: str) -> dict:
        """
        Supprime un souvenir par son ID (clé Redis : user:<userId>:memories).
        """
        return self._make_request("DELETE", f"memories/{memory_id}")

    # --- Liste de Courses (Shopping List) (user:<userId>:shopping_list, Hash) ---
    def get_shopping_list(self) -> dict:
        """
        Récupère la liste de courses de l'utilisateur (clé Redis : user:<userId>:shopping_list).
        Chaque champ = nom de l'article, valeur = JSON (quantity, additionalInfo).
        """
        return self._make_request("GET", "shopping_list")

    def add_shopping_item(self, item: str, quantity: str, additional_info: str = "") -> dict:
        """
        Ajoute un article à la liste de courses (clé Redis : user:<userId>:shopping_list).
        Le nom de l'article est la clé du hash.
        """
        payload = {"item": item, "quantity": quantity, "additionalInfo": additional_info}
        return self._make_request("POST", "shopping_list", json=payload)

    def update_shopping_item(self, original_item_name: str, new_item_name: str, quantity: str, additional_info: str = "") -> dict:
        """
        Met à jour un article de la liste de courses (clé Redis : user:<userId>:shopping_list).
        """
        payload = {"newItem": new_item_name, "quantity": quantity, "additionalInfo": additional_info}
        return self._make_request("POST", f"shopping_list/edit/{quote(original_item_name)}", json=payload)

    def delete_shopping_item(self, item_name: str) -> dict:
        """
        Supprime un article de la liste de courses par son nom (clé Redis : user:<userId>:shopping_list).
        """
        return self._make_request("POST", f"shopping_list/delete/{quote(item_name)}")

    # --- Clés Publiques JWT (jwt_public_keys, Hash global) ---
    def get_jwt_public_keys(self) -> dict:
        """
        Récupère la liste des clés publiques JWT (clé Redis : jwt_public_keys, hash global).
        Chaque champ = kid, valeur = clé publique PEM.
        """
        return self._make_request("GET", "jwt_public_keys")

    def add_jwt_public_key(self, kid: str, public_key: str) -> dict:
        """
        Ajoute une nouvelle clé publique JWT (clé Redis : jwt_public_keys).
        kid = identifiant de la clé (Key ID).
        """
        return self._make_request("POST", "jwt_public_keys", json={"kid": kid, "publicKey": public_key})

    def delete_jwt_public_key(self, kid: str) -> dict:
        """
        Supprime une clé publique JWT par son kid (clé Redis : jwt_public_keys).
        """
        return self._make_request("DELETE", f"jwt_public_keys/{kid}")

api_client = APIClient()
api_client.login(email=os.environ.get("CUISTOVOICE_EMAIL"), password=os.environ.get("CUISTOVOICE_PASSWORD"))
#print(api_client.add_memory("Test Memory", "This is a test memory content."))  # Example usage