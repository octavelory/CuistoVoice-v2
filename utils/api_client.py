import requests
import json
from urllib.parse import quote

class APIclient:
    """
    Un client Python pour interagir avec l'API CuistoVoice.
    Gère l'authentification et la session pour effectuer des actions
    au nom d'un utilisateur authentifié.
    """

    def __init__(self, base_url: str = "https://cuistovoice.vercel.app"):
        """
        Initialise le client.
        :param base_url: L'URL de base de votre application Next.js (ex: "http://localhost:3000")
        """
        if base_url.endswith('/'):
            base_url = base_url[:-1]
        self.base_url = base_url
        # La session requests qui stockera automatiquement les cookies
        self.session = requests.Session()
        self.user_id = None
        self.user_email = None
        # Cache pour la configuration utilisateur
        self._config = None

    def _get_csrf_token(self) -> str:
        """
        Récupère le token CSRF nécessaire pour la connexion.
        NextAuth le requiert pour les soumissions de formulaire.
        """
        try:
            url = f"{self.base_url}/api/auth/csrf"
            response = self.session.get(url)
            response.raise_for_status()
            return response.json().get("csrfToken")
        except requests.exceptions.RequestException as e:
            print(f"Erreur lors de la récupération du token CSRF: {e}")
            raise ConnectionError("Impossible de récupérer le token CSRF du serveur.")

    def login(self, email: str, password: str) -> bool:
        """
        Authentifie le client auprès de l'API CuistoVoice.
        :param email: L'email de l'utilisateur.
        :param password: Le mot de passe de l'utilisateur.
        :return: True si la connexion est réussie, False sinon.
        """
        try:
            csrf_token = self._get_csrf_token()
            if not csrf_token:
                return False

            login_url = f"{self.base_url}/api/auth/callback/credentials"
            payload = {
                "email": email,
                "password": password,
                "csrfToken": csrf_token,
                "json": "true"
            }
            
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
        if not self.session:
            return {}
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

    # --- Méthodes pour la Configuration Utilisateur ---

    def get_config(self, force_refresh: bool = False) -> dict:
        """
        Récupère la configuration de l'utilisateur (nom, langue, etc.).
        Met en cache le résultat pour éviter les appels répétés.
        :param force_refresh: Si True, force le rechargement depuis l'API.
        """
        if self._config is None or force_refresh:
            self._config = self._make_request("GET", "config")
        return self._config

    @property
    def name(self) -> str:
        """Retourne le nom de l'utilisateur depuis la configuration."""
        return self.get_config().get("name", "")

    @property
    def location(self) -> str:
        """Retourne la localisation de l'utilisateur depuis la configuration."""
        return self.get_config().get("location", "")

    @property
    def main_language(self) -> str:
        """Retourne la langue principale de l'utilisateur depuis la configuration."""
        return self.get_config().get("mainLanguage", "")

    # --- Méthodes pour les Minuteurs (Timers) ---

    def get_timers(self) -> list:
        """Récupère la liste des minuteurs actifs."""
        return self._make_request("GET", "timers")

    def add_timer(self, name: str, duration_seconds: int) -> dict:
        """
        Ajoute un nouveau minuteur.
        :param name: Le nom du minuteur.
        :param duration_seconds: La durée en secondes.
        """
        payload = {"name": name, "duration": duration_seconds}
        return self._make_request("POST", "timers", json=payload)

    def delete_timer(self, timer_id: str) -> dict:
        """Supprime un minuteur par son ID."""
        return self._make_request("DELETE", f"timers/{timer_id}")

    def clear_all_timers(self) -> dict:
        """Supprime tous les minuteurs de l'utilisateur."""
        return self._make_request("POST", "timers/clear")

    # --- Méthodes pour les Souvenirs (Memories) ---
    
    def get_memories(self) -> dict:
        """Récupère tous les souvenirs de l'utilisateur."""
        return self._make_request("GET", "memories")

    def add_memory(self, title: str, content: str) -> dict:
        """Ajoute un nouveau souvenir."""
        payload = {"title": title, "content": content}
        return self._make_request("POST", "memories", json=payload)

    def delete_memory(self, memory_id: str) -> dict:
        """Supprime un souvenir par son ID."""
        return self._make_request("DELETE", f"memories/{memory_id}")

    # --- Méthodes pour la Liste de Courses (Shopping) ---

    def get_shopping_list(self) -> dict:
        """Récupère la liste de courses de l'utilisateur."""
        return self._make_request("GET", "shopping")

    def add_shopping_item(self, item: str, quantity: str, additional_info: str = "") -> dict:
        """Ajoute un article à la liste de courses."""
        payload = {"item": item, "quantity": quantity, "additionalInfo": additional_info}
        return self._make_request("POST", "shopping", json=payload)

    def delete_shopping_item(self, item_name: str) -> dict:
        """Supprime un article de la liste de courses par son nom."""
        return self._make_request("POST", f"shopping/delete/{quote(item_name)}")