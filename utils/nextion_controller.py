#!/usr/bin/env python3
# utils/nextion_controller.py

from nextion import Nextion, EventType
import asyncio
import threading
import redis
import json
from datetime import datetime
from utils.wireless_utils import scan_networks, connect_wifi
import os
from typing import Optional, TYPE_CHECKING # Add Optional and TYPE_CHECKING for type hinting

if TYPE_CHECKING:
    # Import VoiceAgent only for type checking to avoid circular imports
    # Adjust the import path based on your project structure (e.g., from ..main import VoiceAgent)
    from ..voice_agent import VoiceAgent

page_list = {
    "boot": 0,
    "main": 1,
    "shoppinglist": 2,
    "timers": 3,
    "recipe": 4,
    "settings": 5,
    "sound": 6,
    "memory": 7,
    "config": 8,
    "wifi": 9,
    "wifipassword": 10,
    "content": 12
}

island_coords = (2, 5, 6) # components forming the island on page main (the island is only on page main)
island_components = {"background": "j0", "text": "g0", "icon": "p2"}
island_modes = ["recipe", "timer", "function_info"]

touch_events = {
    # page main
    (1, 4): "page settings",  # When component 4 on page 1 is touched nextion will change to page 5
    (1, 6): ("page timers", "page recipe", "page shoppinglist", "page memory", "page main"),
    (1, 5): ("page timers", "page recipe", "page shoppinglist", "page memory", "page main"),
    (1, 2): ("page timers", "page recipe", "page shoppinglist", "page memory", "page main"),
    
    # page shoppinglist
    (2, 4): "page main",

    # page timers
    (3, 3): "page main",

    # page recipe
    (4, 4): "page main",
    (4, 7): "page main", # quit recipe button

    # page settings
    (5, 2): "page main",
    (5, 4): "page memory",
    (5, 3): "page memory",
    (5, 5): "page memory",
    (5, 6): "page config",
    (5, 7): "page config",
    (5, 8): "page config",
    (5, 9): "page sound",
    (5, 10): "page sound",
    (5, 11): "page sound",

    # page sound
    (6, 4): "page settings",

    # page memory
    (7, 3): "page settings",

    # page config
    (8, 3): "page settings",
    (8, 9): "page settings",

    # page wifi
    (9, 3): "page settings",
    (9, 5): "page wifipassword",

    # page wifipassword
    (10, 3): "page settings",
    (10, 5): "page settings",

    # page content
    (12, 4): "page main"
}

island_icons = {
    "settings": 1, # the settings icon is pic with id 1
    "arrow_back": 2,
    "arrow_forward": 4,
    "timer_empty": 5,
    "timer_full": 6,
    "memory": 10,
    "recipe": 12
}
possible_languages = ["Français", "English", "Español", "Deutsch", "Italiano", "Português", "Русский", "العربية", "中文", "日本語"]

# Connexion Redis pour la lecture de la database
redis_client = redis.Redis(
    host='grateful-owl-12745.upstash.io',
    port=6379,
    password=os.environ.get("CUISTOVOICE_DATABASE_PASSWORD", "ATHJAAIjcDFlNDc5YmRlNjM5MWQ0ZmY5YTBkNzA5YmNlZDJiMmZlNHAxMA"),
    ssl=True
)
MEMORY_KEY = "cuistovoice:database"
CONFIG_KEY = "cuistovoice:main_config"

def get_memory():
    db_str = redis_client.get(MEMORY_KEY)
    if db_str:
        database_content = json.loads(db_str) or {}
        if len(database_content) == 0:
            return None
        # database content looks like this: { "zS832txvud": { "title": "Plat préféré", "content": "La pizza", "added": "2025-02-09 12:50:35" }, "U3cZrSgcQm": { "title": "Allergie", "content": "Produits laitiers", "added": "2025-02-19 13:10:36" }, "tdlhTKYCRO": { "title": "Anniversaire", "content": "20 mai", "added": "2025-02-22 12:39:03" }, "CmaChXtMRW": { "title": "Présentation au Musée des Arts et Métiers", "content": "Octave doit me présenter au Musée des Arts et Métiers le dimanche 2 mars 2025.", "added": "2025-02-27 18:18:36" }, "FblKOooaJR": { "title": "Goûts musicaux", "content": "Octave aime la chanson 'Memories' de Maroon 5.", "added": "2025-02-28 14:03:45" } }
        return database_content

def get_config():
    config_str = redis_client.get(CONFIG_KEY)
    if config_str:
        # config looks like this: { "name": "Octave", "mainLanguage": "Français", "additionalInfo": [] }
        return json.loads(config_str)
    return None

def set_config(config):
    redis_client.set(CONFIG_KEY, json.dumps(config))

def set_volume(volume):
    if os.name == "nt":
        # Windows does not support setting volume through Nextion
        print(f"[NextionController] Volume set to {volume} (Windows, no action taken)")
    else:
        # For Raspberry Pi, we can set the volume using amixer
        os.system(f"amixer -M -c 0 sset 'Speaker' {volume}%")
        print(f"[NextionController] Volume set to {volume}%")

class DummyNextionController:
    def connect(self):
        print("[DummyNextionController] connect called - no screen available")
    def set_page(self, page_id):
        print(f"[DummyNextionController] set_page called with: {page_id}")
    def set_text(self, component: str, text: str):
        print(f"[DummyNextionController] set_text called with: {component}, {text}")
    def set_global_value(self, component: str, value: str):
        print(f"[DummyNextionController] set_global_value called with: {component}, {value}")
    def run_command(self, command: str):
        print(f"[DummyNextionController] run_command called with: {command}")
    def is_listening(self, status: bool):
        print(f"[DummyNextionController] is_listening called with: {status}")
    def set_island_text(self, text: str):
        print(f"[DummyNextionController] set_island_text called with: {text}")
    def set_island_icon(self, icon_name: str):
        print(f"[DummyNextionController] set_island_icon called with: {icon_name}")
    def set_island_touch(self, command: str):
        print(f"[DummyNextionController] set_island_touch called with: {command}")
    def close(self):
        print("[DummyNextionController] close called")

class NextionControllerAsync:
    """
    Classe asynchrone pour la gestion de l'écran Nextion.
    Toutes les méthodes sont des coroutines et doivent être "await".
    """
    def __init__(self, port='/dev/ttyAMA0', baud_rate=9600):
        self.port = port
        self.baud_rate = baud_rate
        self._client = None
        self._voice_agent: Optional['VoiceAgent'] = None # Add voice agent reference

        self.current_page = None
        self.listening_mode = False
        self.island_mode = []
        self.island_text = ""
        self.island_icon = ""
        self.island_touch = ""

        self.selected_ssid = None

    async def connect(self):
        """
        Initialise la connexion avec l'écran Nextion.
        """
        self._client = Nextion(self.port, self.baud_rate, self._event_handler, reconnect_attempts=5, encoding="utf-8")
        await self._client.connect()
        await self.set_page("main")
        await self.is_listening(False)
        await self.set_island_text("")
        await self.set_island_icon(None)
        # Schedule time update loop in background
        asyncio.create_task(self.update_time_loop())

    def set_voice_agent(self, agent: 'VoiceAgent'):
        """Stores a reference to the VoiceAgent instance."""
        self._voice_agent = agent
        print("[NextionControllerAsync] VoiceAgent instance set.")

    async def _event_handler(self, evt_type, data):
        """
        Gère les événements émis par l'écran Nextion.
        """
        if evt_type == EventType.STARTUP:
            print("Écran Nextion démarré")
            await self.set_page("main")
            await self.is_listening(self.listening_mode)
            await self.set_island_text(self.island_text)
            await self.set_island_icon(self.island_icon)
        elif evt_type == EventType.TOUCH:
            print(f"Événement Touch: page: {data.page_id}, component: {data.component_id}, event: {data.touch_event}")
            # on verifie si l'evenement touch a fait changer de page le nextion, si oui, on met a jour la variable current_page
            page = touch_events.get((data.page_id, data.component_id))
            is_island = data.page_id == 1 and data.component_id in island_coords
            if is_island and self.island_touch:
                await self.run_command(self.island_touch)
            if page:
                if isinstance(page, str): # si il s'agit d'un simle bouton qui change de page dans l'interface, on met a jour la variable current_page
                    self.current_page = page.replace("page ", "")
                    print(f"YOU ARE NOW ON PAGE {self.current_page}")
                elif isinstance(page, tuple): # s'il s'agit de l'ile de l'interface, on verifie la commande a executer, on l'execute et si on change de page, on met a jour la variable current_page
                    # l'ile a été cliquée
                    if "page " in self.island_touch:
                        self.current_page = self.island_touch.replace("page ", "")
                        print(f"YOU ARE NOW ON PAGE {self.current_page}")
                if self.current_page == "wifi":
                    await self.run_command('wifi.select0.path="Chargement des réseaux..."')
                    wifi_connections = "\r\n".join([f"{net['SSID']} | ({net['Quality']}%) | {net['Encryption']}" for net in scan_networks()[:10]])
                    await self.run_command(f'wifi.select0.path="{wifi_connections}"')
                if self.current_page == "memory":
                    current_memory = get_memory()
                    if current_memory:
                        formatted_memories = "\r\n\r\n".join([f"{memory['title']} : {memory['content']}" for memory in current_memory.values()])
                        await self.run_command("memory.t1.aph=0")
                        await self.set_text("memory.slt0.txt", formatted_memories)
                    else:
                        await self.run_command("memory.t1.aph=127")
                        await self.run_command("memory.slt0.txt=\"\"")
                if self.current_page == "config":
                    config_str = redis_client.get(CONFIG_KEY)
                    if config_str:
                        config = json.loads(config_str)
                        await self.run_command(f'config.t4.txt="{config["name"]}"')
                        await self.run_command(f'config.cb0.val={possible_languages.index(config["mainLanguage"])}')
                    else:
                        await self.run_command('config.t4.txt="Octave"')
                        await self.run_command('config.cb0.val=0')
            if data.page_id == 1 and data.component_id == 3:
                # New: Interrupt assistant even when talking, as if wakeword was detected.
                # (when the display is clicked).
                if self._voice_agent:
                    print("[Nextion Touch] Simulating wake word detection...")
                    await self._voice_agent.simulate_wakeword_detection()
                else:
                    print("[Nextion Touch] Warning: VoiceAgent not set, cannot simulate wake word.")
                pass
            if data.page_id == 1 and data.component_id == 11:
                # recette express
                if self._voice_agent:
                    print("[Nextion Touch] Sending express recipe request to agent...")
                    await self._voice_agent.send_text("[SYSTEM] This is a system message: L'utilisateur a demandé une recette express.")
                else:
                    print("[Nextion Touch] Warning: VoiceAgent not set, cannot send express recipe request.")
            if data.page_id == 6 and data.component_id == 5:
                # si on ajuste le volume
                volume = await self._client.get("sound.h0.val")
                if volume:
                    set_volume(int(volume))
            if data.page_id == 8 and data.component_id == 9:
                # si on met a jour la configuration, on recupere la langue choisie et le nom de l'utilisateur et on met a jour la configuration
                new_name = await self._client.get("config.t4.txt")
                new_language = possible_languages[int(await self._client.get("config.cb0.val"))]
                config = get_config()
                config["name"] = new_name
                config["mainLanguage"] = new_language
                set_config(config)
            if data.page_id == 9 and data.component_id == 5: # si on clique sur un réseau WiFi
                self.selected_ssid = await self._client.get("wifi.select0.txt")
                # only get the ssid not the quality and encryption
                self.selected_ssid = self.selected_ssid.split(" | ")[0]
                print(f"Selected SSID: {self.selected_ssid}")
            if data.page_id == 10 and data.component_id == 5: # si on clique sur le bouton "Se connecter"
                # on recupere le mot de passe du reseau wifi
                password = self._client.get("wifipassword.t4.txt")
                print(f"Connecting to {self.selected_ssid} with password {password}")
                # on se connecte au reseau wifi
                connect_wifi(self.selected_ssid, password)
            if data.page_id == 4 and data.component_id == 7: # si on quitte la recette
                # Bouton "Quitter" sur la page recette.
                await self.set_page("main")
                await self.set_island_icon(None)
                await self.set_island_text("")
                self.island_touch = "page main"
                # Envoyer un message au realtime client indiquant l'annulation de la recette
                if self._voice_agent:
                    print("[Nextion Touch] Sending recipe cancellation message to agent...")
                    await self._voice_agent.send_text("[SYSTEM] This is a system message: The user cancelled the current recipe.")
                else:
                    print("[Nextion Touch] Warning: VoiceAgent not set, cannot send recipe cancellation message.")

    async def set_page(self, page_id):
        """
        Change la page courante de l'écran Nextion.
        """
        self.current_page = page_id
        await self._client.command(f"page {page_id}")

    async def run_command(self, command: str):
        """
        Exécute une commande sur l'écran Nextion.
        """
        if command.startswith("page "):
            parts = command.split(" ")
            if len(parts) > 1:
                self.current_page = parts[1]
        await self._client.command(command)

    async def is_listening(self, status: bool):
        """
        Active ou désactive l'écoute des événements de l'écran Nextion.
        """
        self.listening_mode = status
        if status:
            if self.current_page == "main":
                await self.run_command("p0.aph=127")
            await self.set_global_value("bloom.aph", "127")
        else:
            if self.current_page == "main":
                await self.run_command("p0.aph=0")
            await self.set_global_value("bloom.aph", "0")

    async def set_island_text(self, text: str):
        """
        Modifie le texte défilant (island text) sur l'écran Nextion (page "main").
        """
        self.island_text = text
        await self.run_command(f"main.g0.txt=\"{text}\"")

    async def set_island_icon(self, icon_name: str):
        """
        Modifie l'icône affichée sur l'île de l'écran Nextion (page "main").
        """
        if icon_name is None:
            self.island_icon = ""
            await self.run_command(f"main.{island_components['icon']}.aph=0")
            return
        else:
            await self.run_command(f"main.{island_components['icon']}.aph=127")
        if icon_name not in island_icons:
            print(f"Icon '{icon_name}' not found.")
            return
        icon_id = island_icons.get(icon_name)
        if icon_id:
            self.island_icon = icon_name
            await self.run_command(f"main.{island_components['icon']}.pic={icon_id}")

    async def set_island_touch(self, command: str):
        """
        Définit la commande à exécuter lorsqu'on touche l'île de l'écran Nextion (page "main").
        """
        self.island_touch = command

    async def update_time_loop(self):
        """
        Envoie la commande de mise à jour de l'heure sur l'écran Nextion toutes les secondes
        si on est sur la page "main". Le ":" clignote chaque seconde.
        """
        blink = True
        while True:
            await asyncio.sleep(1)
            if self.current_page == "main":
                now = datetime.now()
                sep = ":" if blink else " "
                time_str = f"{now.hour:02d}{sep}{now.minute:02d}"
                # Mettre à jour l'affichage de l'heure
                await self.run_command(f't0.txt="{time_str}"')
                blink = not blink

    # Method to send long text in chunks to the Nextion display
    async def set_text(self, component: str, text: str):
        chunk_size = 100
        for i, start in enumerate(range(0, len(text), chunk_size)):
            chunk_text = text[start:start+chunk_size]
            if i == 0:
                await self.run_command(f'{component}="{chunk_text}"')
            else:
                await self.run_command(f'{component}+="{chunk_text}"')

    async def set_global_value(self, component: str, value: str):
        # iterates over all the pages to send the value to the component. Ex: main.bloom.ap=127
        for page in page_list:
            try:
                await self.run_command(f'{page}.{component}={value}')
            except Exception as e:
                continue

class NextionController:
    """
    Classe synchrone pour la gestion de l'écran Nextion.
    Elle utilise un thread dédié et une event loop pour exécuter les méthodes asynchrones
    de NextionControllerAsync de manière synchrone.
    """
    def __init__(self, port='/dev/ttyAMA0', baud_rate=9600):
        self._async_controller = NextionControllerAsync(port, baud_rate)
        self.loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._start_loop, daemon=True)
        self._loop_thread.start()

    def _start_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _run_sync(self, coro):
        """
        Exécute la coroutine donnée de manière synchrone (bloquante)
        grâce à asyncio.run_coroutine_threadsafe.
        """
        try:
            future = asyncio.run_coroutine_threadsafe(coro, self.loop)
            return future.result()
        except Exception as e:
            print(f"[NextionController] Error: {e}")

    def set_voice_agent(self, agent: 'VoiceAgent'):
        """Sets the VoiceAgent instance in the underlying async controller."""
        # No need to run in loop, just set the attribute directly
        self._async_controller.set_voice_agent(agent)
        print("[NextionController] VoiceAgent instance set in async controller.")

    def connect(self):
        """Méthode synchrone pour établir la connexion avec l'écran Nextion."""
        return self._run_sync(self._async_controller.connect())

    def set_page(self, page_id):
        """Change la page de l'écran Nextion de manière synchrone."""
        return self._run_sync(self._async_controller.set_page(page_id))

    def run_command(self, command: str):
        """Exécute une commande sur l'écran Nextion de manière synchrone."""
        return self._run_sync(self._async_controller.run_command(command))

    def is_listening(self, status: bool):
        """Active ou désactive l'écoute des événements de l'écran Nextion de manière synchrone."""
        return self._run_sync(self._async_controller.is_listening(status))

    def set_island_text(self, text: str):
        """Modifie le texte défilant sur l'écran Nextion de manière synchrone."""
        return self._run_sync(self._async_controller.set_island_text(text))
    
    def set_island_icon(self, icon_name: str):
        """Modifie l'icône de l'île de l'écran Nextion de manière synchrone."""
        return self._run_sync(self._async_controller.set_island_icon(icon_name))
    
    def set_island_touch(self, command: str):
        """Définit la commande à exécuter lorsqu'on touche l'île de l'écran Nextion de manière synchrone."""
        return self._run_sync(self._async_controller.set_island_touch(command))

    # Synchronous wrapper for the async set_text method.
    def set_text(self, component: str, text: str):
        return self._run_sync(self._async_controller.set_text(component, text))
    
    # Synchronous wrapper for the async set_global_value method.
    def set_global_value(self, component: str, value: str):
        return self._run_sync(self._async_controller.set_global_value(component, value))

    def close(self):
        """Arrête proprement l'event loop et le thread associé."""
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._loop_thread.join()

if __name__ == '__main__':
    controller = NextionController(port="/dev/ttyAMA0", baud_rate=115200)
    controller.connect()
    controller.set_page("main")
    controller.run_command('t0.txt="Hello Nextion!"')
    controller.is_listening(True)
    controller.set_island_text("Hello Nextion !")

    import time
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Arrêt du controller...")
        controller.close()