import asyncio
import os
import json
import redis
import datetime
from voice_agent import VoiceAgent
from utils.nextion_controller import NextionController, DummyNextionController
from functions_utils import (
   set_nextion_controller,
   get_tool_handlers,
   set_voice_agent_send_text, # For timers
   cancel_all_timers,
   # --- Add new callback setters ---
   set_agent_pause_listening_callback,
   set_agent_reset_state_callback,
   set_agent_set_music_flag_callback,
   set_agent_stop_event # Import the new setter
   # --- End new callback setters ---
)
# Import SongManager to potentially initialize it here if desired
# from utils.song_manager import SongManager

# --- Configuration Redis ---
redis_client = redis.Redis(
    host='grateful-owl-12745.upstash.io',
    port=6379,
    password=os.environ.get("CUISTOVOICE_DATABASE_PASSWORD", "ATHJAAIjcDFlNDc5YmRlNjM5MWQ0ZmY5YTBkNzA5YmNlZDJiMmZlNHAxMA"),
    ssl=True
)

MEMORY_KEY = "cuistovoice:database"
SHOPPING_LIST_KEY = "cuistovoice:shopping_list"
CONFIG_KEY = "cuistovoice:main_config"
TIMERS_KEY = "cuistovoice:timers"

redis_client.set(TIMERS_KEY, json.dumps([]))

db_str = redis_client.get(MEMORY_KEY)
if db_str:
    database_content = json.loads(db_str) or {}
    if len(database_content) == 0:
        database_content = "Database is empty for now. You can add memories using the 'add_memory' tool."
else:
    database_content = "Database is empty for now. You can add memories using the 'add_memory' tool."

# on recupere le nom d'utilisateur
config_str = redis_client.get(CONFIG_KEY)
if config_str:
    config = json.loads(config_str)
    username = config.get("name", "Octave")
else:
    username = "Octave"

# On charge les instructions (prompt system)
with open('data/system_prompt.txt', 'r', encoding="utf-8") as file:
    instructions = file.read()
    instructions = instructions.format(
        database_content=database_content,
        time_info=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        username=username
    )

with open("messages_history.json", "w", encoding="utf-8") as f:
    json.dump([], f)

# --- Nextion Setup ---
if os.name == "nt":
    nextion_controller = DummyNextionController()
    keyword_paths = ["models/cuistovoice_wakeword_windows.ppn"]
    print("OS is Windows, using dummy Nextion controller and Windows keyword.")
else:
    try:
        nextion_controller = NextionController()
        print("OS is not Windows, attempting to use real Nextion controller.")
    except Exception as e:
        print(f"Failed to initialize NextionController: {e}. Using dummy controller.")
        nextion_controller = DummyNextionController()
    keyword_paths = ["models/cuistovoice_wakeword_rpi.ppn"]
    print("Using Raspberry Pi keyword.")

set_nextion_controller(nextion_controller)

# --- Clear History File ---
try:
    with open("messages_history.json", "w") as f:
        json.dump([], f)
    print("Cleared messages_history.json")
except Exception as e:
    print(f"Warning: Could not clear messages_history.json: {e}")

# --- Load Tools ---
try:
    # Ensure path is correct relative to main.py
    tools_path = os.path.join(os.path.dirname(__file__), "data", "functions.json")
    with open(tools_path, "r", encoding="utf-8") as f:
        tools = json.load(f)
    print(f"Loaded {len(tools)} tools from {tools_path}")
except (FileNotFoundError, json.JSONDecodeError) as e:
    print(f"Error loading functions.json: {e}. Exiting.")
    exit(1) # Exit if tools cannot be loaded
except Exception as e:
    print(f"An unexpected error occurred loading functions.json: {e}. Exiting.")
    exit(1)

# --- Configuration Porcupine ---
PICOVOICE_ACCESS_KEY = os.environ.get("PV_API_KEY")
if not PICOVOICE_ACCESS_KEY:
    print("Error: Picovoice Access Key (PV_API_KEY environment variable) not set. Exiting.")
    exit(1)
# Ensure model path is correct relative to main.py
model_path = os.path.join(os.path.dirname(__file__), "models", "porcupine_params_fr.pv")
if not os.path.exists(model_path):
     print(f"Error: Porcupine model file not found at {model_path}. Exiting.")
     exit(1)
# Ensure keyword paths are correct
keyword_paths = [os.path.join(os.path.dirname(__file__), p) for p in keyword_paths]
for p in keyword_paths:
    if not os.path.exists(p):
        print(f"Error: Porcupine keyword file not found at {p}. Exiting.")
        exit(1)

async def main():
    # Initialize SongManager (optional, can be done in functions_utils on demand)
    # song_manager = SongManager()
    # Pass song_manager instance if needed: set_song_manager(song_manager)

    tool_handlers = get_tool_handlers()
    if not tool_handlers:
        print("Warning: No tool handlers were loaded. Functions might not work.")
    else:
        print(f"Loaded {len(tool_handlers)} tool handlers.")
        # Verify play_music handler is loaded if expected
        if 'play_music' not in tool_handlers:
            print("Warning: 'play_music' handler not found in loaded handlers.")

    agent = VoiceAgent(
        porcupine_access_key=PICOVOICE_ACCESS_KEY,
        porcupine_keyword_paths=keyword_paths,
        porcupine_model_path=model_path,
        porcupine_sensitivity=0.5, # Adjust sensitivity if needed
        voice="ash", # Changed voice for variety, use "ash" or others as preferred
        temperature=0.6,
        instructions=instructions,
        turn_detection={"type": "semantic_vad", "eagerness": "medium"}, # Adjusted eagerness
        tools = tools,
        tool_handlers=tool_handlers,
        history_file="messages_history.json" # Pass history file path
    )

    # --- Link Agent Methods/Events via Callbacks ---
    set_voice_agent_send_text(agent.send_text)
    set_agent_pause_listening_callback(agent.pause_listening)
    set_agent_reset_state_callback(agent.reset_to_wakeword_state)
    set_agent_set_music_flag_callback(agent.set_is_playing_music)
    set_agent_stop_event(agent.stop_playback_event) # Pass the agent's stop event
    print("Linked VoiceAgent methods and events to functions_utils via callbacks.")

    await agent.start()

    print(f"Agent vocal démarré. Dites le mot-clé pour commencer ! (Ctrl+C pour quitter)")

    try:
        # Keep the main task running
        while agent.running:
            # Check agent health or perform other main loop tasks if needed
            await asyncio.sleep(1)
        print("Agent loop finished.")
    except KeyboardInterrupt:
        print("\nCtrl+C détecté. Arrêt de l'agent vocal...")
    except Exception as e:
        print(f"Erreur inattendue dans la boucle principale: {e}")
    finally:
        print("Nettoyage...")
        # Cancel any running timers before stopping the agent
        cancel_all_timers()
        # Stop the agent gracefully
        if agent.running:
            await agent.stop()
        print("Agent arrêté.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"Erreur d'exécution principale : {e}")
        # Add more detailed error logging if needed
        import traceback
        traceback.print_exc()
        exit(1)
    except KeyboardInterrupt:
        # This might be caught inside main now, but keep it as a fallback
        print("\nArrêt demandé.")
        exit(0)