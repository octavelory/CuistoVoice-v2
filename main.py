import asyncio
import os
import json
import datetime
from dotenv import load_dotenv
load_dotenv()
from voice_agent import VoiceAgent
from utils.nextion_controller import NextionController, DummyNextionController
from utils.api_client import api_client
from utils.env_utils import save_credentials_to_env_file
from functions_utils import (
   set_nextion_controller,
   get_tool_handlers,
   set_voice_agent_send_text, # For timers
   cancel_all_timers,
   set_agent_pause_listening_callback,
   set_agent_reset_state_callback,
   set_agent_set_music_flag_callback,
   set_agent_stop_event
)

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

nextion_controller.connect()
### set nextion controller in functions_utils
set_nextion_controller(nextion_controller)

client_status = api_client.login(
    email=os.environ.get("CUISTOVOICE_EMAIL"),
    password=os.environ.get("CUISTOVOICE_PASSWORD")
)

while not client_status:
    print("[DEBUG] Login failed, retrying...")
    email, password = nextion_controller.ask_login()
    save_credentials_to_env_file(email, password)
    load_dotenv()
    client_status = api_client.login(email=email, password=password)

database_content = api_client.get_memories()
user_config = api_client.get_config()
if not database_content or len(database_content) == 0:
    database_content = "La base de données est actuellement vide. Tu peux rajouter des données en utilisant la fonction correspondante."
if not user_config or len(user_config) == 0:
    nextion_controller.initiate_config()

with open('data/system_prompt.txt', 'r', encoding="utf-8") as file:
    instructions = file.read()
    instructions = instructions.format(
        database_content=database_content,
        time_info=datetime.datetime.now().strftime("%A %Y-%m-%d %H:%M:%S"),
        username=user_config.get("name")
    )

with open("messages_history.json", "w", encoding="utf-8") as f:
    json.dump([], f)

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

with open("data/speaker_profile.bin", "rb") as main_speaker_file:
    main_speaker_profile = main_speaker_file.read()

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
        porcupine_sensitivity=0.9,
        voice="marin",
        speed = 1.05,
        temperature=0.6,
        instructions=instructions,
        turn_detection={"type": "semantic_vad", "eagerness": "medium"},
        noise_reduction=None,
        tools = tools,
        tool_handlers=tool_handlers,
        history_file="messages_history.json",
        nextion_controller = nextion_controller # Pass controller instance to agent
    )

    # --- Link Agent Methods/Events via Callbacks ---
    set_voice_agent_send_text(agent.send_text)
    set_agent_pause_listening_callback(agent.pause_listening)
    set_agent_reset_state_callback(agent.reset_to_wakeword_state)
    set_agent_set_music_flag_callback(agent.set_is_playing_music)
    set_agent_stop_event(agent.stop_playback_event) # Pass the agent's stop event
    print("Linked VoiceAgent methods and events to functions_utils via callbacks.")

    # --- Link Agent to Nextion Controller ---
    if nextion_controller:
        # Check if it's the dummy or real controller; both should have the method
        if hasattr(nextion_controller, 'set_voice_agent'):
            nextion_controller.set_voice_agent(agent)
            print("Linked VoiceAgent instance to NextionController.")
        else:
            print("Warning: Nextion controller does not have set_voice_agent method.")

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