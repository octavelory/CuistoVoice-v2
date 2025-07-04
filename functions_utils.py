import redis
import json
import random
import datetime
import os
import inspect
import asyncio
import uuid
import time # Added
import threading # Added
import numpy as np # Added
import sounddevice as sd # Added
import re
from pydub import AudioSegment # Added
from openai import OpenAI
from utils.song_manager import SongManager # Added
from typing import Optional, Callable, Any # Added Any
from utils.api_client import api_client

openai_client = OpenAI()

recipe_schema = {
    "name": "recette_de_cuisine",
    "description": "Un schéma pour décrire une recette de cuisine",
    "schema": {
        "type": "object",
        "properties": {
            "status": {
                "type": "boolean",
                "description": "Définit si la recette est faisable ou non. Si elle est infaisable, tu dois mettre false, et ne donner aucune autre information sur la recette (pas d'ingrédients, pas d'étapes, etc.)"
            },
            "title": {
                "type": "string",
                "description": "Le titre de la recette"
            },
            "ingredients": {
                "type": "array",
                "description": "Les ingrédients nécessaires pour la recette",
                "items": {"type": "string"}
            },
            "steps": {
                "type": "array",
                "description": "Les étapes de la recette",
                "items": {"type": "string"}
            },
            "materiel": {
                "type": "array",
                "description": "Le matériel nécessaire pour la recette",
                "items": {"type": "string"}
            },
            "time": {
                "type": "string",
                "description": "Le temps de préparation de la recette"
            },
            "difficulty": {
                "type": "string",
                "description": "La difficulté de la recette",
                "enum": ["facile", "moyen", "difficile"]
            },
        },
        "additionalProperties": False,
        "required": ["status", "title", "ingredients", "steps", "materiel", "time", "difficulty"]
    },
    "strict": True
}

recipe_format = """
Recette pour {num_people} personne(s)
Temps de préparation: {time}
Difficulté: {difficulty}

Matériel nécessaire:

 - {materiel}

Ingrédients:

 - {ingredients}

Étapes:

{etapes}
"""

# ---------------------------------------------------------------------------
# NEXTION / AGENT CALLBACKS / GLOBALS
# ---------------------------------------------------------------------------
nextion_controller = None
voice_agent_send_text = None # To hold the agent's send_text method for timers
# voice_agent_instance = None # REMOVED - Use callbacks instead
active_timers = {}
song_manager = None

# --- Agent Callbacks ---
_agent_pause_listening_callback: Optional[Callable[[], None]] = None
_agent_reset_state_callback: Optional[Callable[[], None]] = None
_agent_set_music_flag_callback: Optional[Callable[[bool], None]] = None

# --- Music Playback Globals ---
music_interrupt_event = threading.Event()
playback_finished_event = threading.Event()
playback_interrupted = False
playback_start_time = None
current_playback_thread: Optional[threading.Thread] = None # Explicitly type hint
_playback_cleanup_scheduled = False # Flag to prevent double cleanup calls
_playback_lock = threading.Lock() # Lock for managing thread start/stop/cleanup

def set_nextion_controller(controller):
    global nextion_controller
    nextion_controller = controller

# REMOVED set_voice_agent_instance

def set_voice_agent_send_text(send_text_func):
    """Stores the agent's send_text method for timer callbacks."""
    global voice_agent_send_text
    voice_agent_send_text = send_text_func

# --- New Agent Callback Setters ---
def set_agent_pause_listening_callback(callback: Callable[[], None]):
    global _agent_pause_listening_callback
    _agent_pause_listening_callback = callback

def set_agent_reset_state_callback(callback: Callable[[], None]):
    global _agent_reset_state_callback
    _agent_reset_state_callback = callback

def set_agent_set_music_flag_callback(callback: Callable[[bool], None]):
    global _agent_set_music_flag_callback
    _agent_set_music_flag_callback = callback
# --- End New Agent Callback Setters ---

def display_shopping_list(shopping_list):
    nextion_controller.set_page("shoppinglist")
    if len(shopping_list) == 0:
        nextion_controller.run_command("t1.aph=127")
        nextion_controller.run_command('slt0.txt=""')
    else:
        nextion_controller.run_command("t1.aph=0")
        items = "\r\n".join([
            f"{item}: {data['quantity']}" + (f" ({data['additional_info']})" if data['additional_info'] not in [None, 'None'] else "")
            for item, data in shopping_list.items()
        ])
        nextion_controller.run_command(f"slt0.txt=\"{items}\"")

# ---------------------------------------------------------------------------
# TIMER FUNCTIONS
# ---------------------------------------------------------------------------

async def _timer_task(timer_id: str, duration: int, name: str):
    """The actual task that waits and sends notification."""
    try:
        await asyncio.sleep(duration)
        # Check if timer still exists (wasn't cancelled)
        if timer_id in active_timers:
            print(f"Timer '{name}' (ID: {timer_id}) finished.")
            if voice_agent_send_text:
                message = f"[SYSTEM] This is a system message: the timer '{name}' for {duration} seconds has ended."
                # Schedule send_text on the main event loop
                asyncio.create_task(voice_agent_send_text(message))
            else:
                print("Error: voice_agent_send_text not set. Cannot send timer notification.")
            # Clean up finished timer
            del active_timers[timer_id]
    except asyncio.CancelledError:
        print(f"Timer '{name}' (ID: {timer_id}) was cancelled.")
        # Ensure cleanup if cancelled externally
        if timer_id in active_timers:
            del active_timers[timer_id]
    except Exception as e:
        print(f"Error in timer task '{name}' (ID: {timer_id}): {e}")
        # Ensure cleanup on error
        if timer_id in active_timers:
            del active_timers[timer_id]

def add_timer(time_in_seconds: int = None, timer_name: str = None):
    """Adds and starts a new timer."""
    if time_in_seconds is None or timer_name is None:
        return {"status": "error", "message": "Time in seconds or timer name is missing."}
    if not isinstance(time_in_seconds, int) or time_in_seconds <= 0:
        return {"status": "error", "message": "Time in seconds must be a positive integer."}
    if not voice_agent_send_text:
        return {"status": "error", "message": "Timer system not ready (agent not linked)."}

    timer_id = str(uuid.uuid4())
    start_time = datetime.datetime.now()
    print(f"Starting timer '{timer_name}' for {time_in_seconds}s (ID: {timer_id})")

    # Create and schedule the timer task
    task = asyncio.create_task(_timer_task(timer_id, time_in_seconds, timer_name))
    api_timer_id = api_client.add_timer(name=timer_name, duration_seconds=time_in_seconds).get("timer").get("id")

    active_timers[timer_id] = {
        "task": task,
        "name": timer_name,
        "duration": time_in_seconds,
        "start_time": start_time,
        "api_id": api_timer_id
    }
    return {"status": "success", "message": f"Timer '{timer_name}' started for {time_in_seconds} seconds.", "timer_id": timer_id}

def edit_timer(timer_id: str = None, new_time_in_seconds: int = None, new_timer_name: str = None):
    """Edits an existing timer by cancelling and restarting it."""
    if timer_id is None:
        return {"status": "error", "message": "Timer ID is missing."}
    if timer_id not in active_timers:
        return {"status": "error", "message": f"Timer with ID '{timer_id}' not found."}
    if new_time_in_seconds is None and new_timer_name is None:
        return {"status": "error", "message": "No new time or name provided for editing."}

    # Get old details
    old_timer_info = active_timers[timer_id]
    old_task = old_timer_info["task"]
    old_name = old_timer_info["name"]

    # Cancel the old task
    print(f"Cancelling old timer task for '{old_name}' (ID: {timer_id}) for edit.")
    old_task.cancel()
    api_client.delete_timer(timer_id=old_timer_info["api_id"])  # Cancel via API
    # Note: The cancellation cleanup happens within _timer_task or here if needed immediately
    if timer_id in active_timers: # Check again in case cancellation was instant
        del active_timers[timer_id]

    # Determine new parameters
    final_time = new_time_in_seconds if new_time_in_seconds is not None else old_timer_info["duration"]
    final_name = new_timer_name if new_timer_name is not None else old_name

    if not isinstance(final_time, int) or final_time <= 0:
        # Optionally restart the original timer or just return error
        return {"status": "error", "message": "New time in seconds must be a positive integer."}

    # Add the timer again with new details (generates a new ID implicitly via add_timer)
    print(f"Restarting timer as '{final_name}' for {final_time}s after edit.")
    return add_timer(time_in_seconds=final_time, timer_name=final_name) # Returns new timer info

def delete_timer(timer_id: str = None):
    """Deletes (cancels) an active timer."""
    if timer_id is None:
        return {"status": "error", "message": "Timer ID is missing."}
    if timer_id not in active_timers:
        return {"status": "error", "message": f"Timer with ID '{timer_id}' not found."}

    timer_info = active_timers[timer_id]
    task = timer_info["task"]
    name = timer_info["name"]

    print(f"Cancelling timer '{name}' (ID: {timer_id}).")
    task.cancel()
    api_client.delete_timer(timer_id=timer_info["api_id"])  # Cancel via API
    # Cleanup might happen in the task's exception handler, or force remove here:
    if timer_id in active_timers:
        del active_timers[timer_id]

    return {"status": "success", "message": f"Timer '{name}' (ID: {timer_id}) cancelled successfully."}

def cancel_all_timers():
    """Cancels all active timers, usually called on shutdown."""
    print("Cancelling all active timers...")
    timer_ids = list(active_timers.keys()) # Avoid modification during iteration
    for timer_id in timer_ids:
        if timer_id in active_timers:
            try:
                active_timers[timer_id]["task"].cancel()
                # Optionally wait for cancellation briefly?
                # del active_timers[timer_id] # Let the task handler remove it
            except Exception as e:
                print(f"Error cancelling timer {timer_id}: {e}")
    api_client.clear_all_timers()  # Clear all timers via API
    # active_timers.clear() # Clear remaining just in case
    print(f"Cancelled {len(timer_ids)} timer(s).")

# ---------------------------------------------------------------------------
# FONCTIONS DE L'ASSISTANT
# ---------------------------------------------------------------------------

def google_search(query=None):
    print("[Function: google_search] Starting Google search...")
    if query is None:
        return {"status": "error", "message": "Query is missing."}
    try:
        response = openai_client.responses.create(
            model="gpt-4.1-mini",
            tools=[{
                "type": "web_search_preview",
                "user_location": {
                    "type": "approximate",
                    "city": api_client.location,
                }
            }],
            tool_choice={"type": "web_search_preview"},
            input=[
                {
                    "role": "user",
                    "content": f"Effectue une recherche Google sur la requête suivante : {query}."
                }
            ]
        )
        output_text = response.output[1].content[0].text
        output_text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1', output_text)
        output_text = re.sub(r'https?://[^\s]+', lambda m: m.group(0).split('/')[2], output_text)

        return {
            "status": "success",
            "search_response": output_text,
            "instructions_reminder": "Tu n'es pas obligé de donner les sources, mais si tu le fais, ne donne pas des liens, juste le noms des sites. Par exemple : \"Wikipedia, Le Monde, etc.\""
        }
    except Exception as e:
        print(f"[Function: google_search] Error during Google search: {e}")
        return {"status": "error", "message": "Une erreur s'est produite lors de la recherche sur Google."}

def calculate(expression=None):
    if expression is None:
        return {"status": "error", "message": "Expression is missing."}
    try:
        result = eval(expression)
        return {"status": "success", "message": f"Result of the expression '{expression}' is {result}."}
    except Exception as e:
        return {"status": "error", "message": "Invalid expression."}

def add_memory(title=None, content=None):
    if title is None or content is None:
        return {"status": "error", "message": "Title or content is missing."}
    data = api_client.get_memories()
    mem_id = ''.join(random.choices('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', k=10))
    while mem_id in data:
        mem_id = ''.join(random.choices('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', k=10))
    data[mem_id] = {"title": title, "content": content, "added": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    api_client.add_memory(title=title, content=content, memory_id=mem_id)
    return {"status": "success", "message": f"Memory with title '{title}' added successfully.", "memory_id": mem_id, "updated_database_content": data}

def delete_memory(memory_id=None):
    if memory_id is None:
        return {"status": "error", "message": "Memory ID is missing."}
    data = api_client.get_memories()
    if memory_id not in data:
        return {"status": "error", "message": "Memory ID not found in the database! The memory has not been updated. You can retry with the correct memory ID."}
    title = data[memory_id]["title"]
    del data[memory_id]
    api_client.delete_memory(memory_id=memory_id)
    return {"status": "success", "message": f"Memory with title '{title}' deleted successfully.", "updated_database_content": data}

def edit_memory(memory_id=None, new_title=None, new_content=None):
    if memory_id is None:
        return {"status": "error", "message": "Memory ID is missing."}
    data = api_client.get_memories()
    if memory_id not in data:
        return {"status": "error", "message": "Memory ID not found."}
    if new_title is not None:
        data[memory_id]["title"] = new_title
    if new_content is not None:
        data[memory_id]["content"] = new_content
    api_client.edit_memory(memory_id=memory_id, title=data[memory_id]["title"], content=data[memory_id]["content"])
    return {"status": "success", "message": f"Memory with ID '{memory_id}' edited successfully.", "updated_database_content": data}

# Global controller for music playback state.
MUSIC_CONTROL = {"paused": False, "stop": False, "played_duration": 0}

def format_duration(seconds):
    # Converts seconds into H:MM:SS format.
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}"

def get_shopping_list():
    shopping_list = api_client.get_shopping_list()
    display_shopping_list(shopping_list)
    return {"status": "success", "shopping_list": shopping_list}

def add_to_shopping_list(item=None, quantity=1, additional_info=None):
    if item is None or quantity is None:
        return {"status": "error", "message": "Item or quantity is missing."}
    data = api_client.get_shopping_list()
    if item in data:
        return {"status": "error", "message": "Item already in the shopping list."}
    data[str(item)] = {"quantity": str(quantity), "additional_info": str(additional_info)}
    api_client.add_to_shopping_list(item=str(item), quantity=str(quantity), additional_info=str(additional_info))
    display_shopping_list(data)
    return {"status": "success", "message": f"Item '{item}' added to the shopping list.", "updated_shopping_list": data}

def remove_from_shopping_list(item=None):
    if item is None:
        return {"status": "error", "message": "Item is missing."}
    data = api_client.get_shopping_list()
    if item not in data:
        return {"status": "error", "message": "Item not found in the shopping list."}
    del data[str(item)]
    api_client.remove_from_shopping_list(item=str(item))
    display_shopping_list(data)
    return {"status": "success", "message": f"Item '{item}' removed from the shopping list.", "updated_shopping_list": data}

def edit_item_from_shopping_list(item=None, new_item=None, new_quantity=None, new_additional_info=None):
    if item is None:
        return {"status": "error", "message": "Item is missing."}
    data = api_client.get_shopping_list()
    if item not in data:
        return {"status": "error", "message": "Item not found in the shopping list."}
    if new_item is not None:
        data[str(new_item)] = data.pop(str(item))
        item = new_item  # on met à jour la clé utilisée
    if new_quantity is not None:
        data[str(item)]["quantity"] = str(new_quantity)
    if new_additional_info is not None:
        data[str(item)]["additional_info"] = str(new_additional_info)
    api_client.edit_item_from_shopping_list(item=str(item), quantity=str(data[str(item)]["quantity"]), additional_info=str(data[str(item)]["additional_info"]))
    display_shopping_list(data)
    return {"status": "success", "message": f"Item '{item}' edited successfully.", "updated_shopping_list": data}

def clear_shopping_list():
    api_client.clear_shopping_list()
    display_shopping_list({})
    return {"status": "success", "message": "Shopping list cleared successfully.", "updated_shopping_list": {}}

def create_recipe(description = None, num_people = 1):
    # Set nextion page to 'recipe' if the controller is available.
    if nextion_controller:
        nextion_controller.set_page("recipe")
        nextion_controller.run_command("vis t0,1")
        nextion_controller.run_command('slt0.txt=""')
        nextion_controller.run_command('g0.txt=""')
    print("[Function]: Creating recipe...")
    if description is None:
        return {"status": "error", "message": "Description is missing."}
    # On lit les mémoires de l'utilisateur pour passer le contexte
    data = api_client.get_memories()
    try:
        response = openai_client.chat.completions.create(
            model = "gpt-4.1",
            temperature = 0.1,
            response_format = {"type": "json_schema", "json_schema": recipe_schema},
            messages = [
                {"role": "system", "content": "Tu es expert dans la création de recette. L'utilisateur va te donner une description de la recette qu'il veut et tu vas lui donner la recette complète, avec ingrédients et étapes. Attention : tu ne dois retourner que la recette, pas de texte en plus ! Par ailleurs, on te donnera également une base de données contenant des préférences et informations sur l'utilisateur, tu dois t'en servir comme contexte."},
                {"role": "user", "content": description + f"\n\n Quantité: pour {num_people} personne(s). \n\nBase de données sur l'utilisateur: {data} \n\n À toi de jouer !"}
            ]
        ).choices[0].message.content
    except Exception as e:
        nextion_controller.set_page("main")
        return {"status": "error", "message": f"Erreur lors de la génération de la recette: {e}"}
    recipe_data = json.loads(response)
    if recipe_data["status"] == False:
        nextion_controller.set_page("main")
        return {
            "status": "error",
            "message": "Désolé, cette recette est trop complexe pour moi ou infaisable. Je ne peux pas la réaliser. C'est quelque chose qui arrive très rarement, mais je te conseille de reformuler ta demande pour qu'elle soit plus simple."
        }
    else:
        nextion_controller.set_page("recipe")
        nextion_controller.run_command("vis t0,0")
        formatted_recipe = recipe_format.format(
            num_people=num_people,
            time=recipe_data["time"],
            difficulty=recipe_data["difficulty"],
            materiel="\n - ".join([f"{material}" for i, material in enumerate(recipe_data["materiel"])]),
            ingredients="\n - ".join([f"{ingredient}" for i, ingredient in enumerate(recipe_data["ingredients"])]),
            etapes="\n\n".join([f"{i+1}. {step}" for i, step in enumerate(recipe_data["steps"])])
        )
        formatted_recipe = formatted_recipe.replace("\n", "\r\n")
        nextion_controller.set_text("slt0.txt", formatted_recipe)
        nextion_controller.run_command("slt0.val_y=0")
        nextion_controller.run_command(f"g0.txt=\"{recipe_data['title']}\"")
        nextion_controller.set_island_text("Recette en cours... Appuyer pour revenir à la recette...")
        nextion_controller.set_island_icon("recipe")
        nextion_controller.set_island_touch("page recipe")
        return {
            "status": "success",
            "generated_recipe": recipe_data,
            "instructions_reminder": "N'oublie pas de donner les ingrédients et les étapes de la recette un par un. Autrement dit, donne **seulement** le premier ingrédient puis, lorsque l'utilisateur indique qu'il l'a bien en main, tu peux lui donner le second, et pareil pour les étapes, tu ne les dis que une par une."
        }

# ---------------------------------------------------------------------------
# MUSIC PLAYBACK FUNCTIONS
# ---------------------------------------------------------------------------

def format_duration(seconds):
    """Converts seconds into H:MM:SS format."""
    if seconds is None:
        return "0:00:00"
    try:
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        return f"{h:d}:{m:02d}:{s:02d}"
    except Exception:
        return "0:00:00"

def _playback_thread_func(audio_data_pcm, sample_rate, loop: asyncio.AbstractEventLoop, cleanup_callback: Callable[[bool], None], stop_event: threading.Event):
    """
    Internal function to handle audio playback in a separate thread.
    Handles pause/resume via music_interrupt_event and termination via stop_event.
    Calls cleanup_callback on finish/interrupt/stop.
    """
    global playback_interrupted, playback_start_time
    playback_interrupted = False # Reset flag: Has an interruption occurred *during this playback*?
    playback_start_time = time.time()
    stream = None
    is_paused = False # Current state: Is the stream paused?
    finished_naturally = False
    audio_array = None
    total_frames = 0
    current_pos = 0 # Keep track of position

    try:
        # Ensure audio_data_pcm is numpy array int16
        audio_array = np.frombuffer(audio_data_pcm, dtype=np.int16)
        total_frames = len(audio_array)
        current_pos = 0 # Start at the beginning
        block_size = int(0.05 * sample_rate)

        def callback(outdata, frames, time_info, status):
            nonlocal current_pos
            if status:
                print(f"[Playback Callback] Status: {status}")

            # Calculate how much data is left and how much fits in this block
            remaining_data = total_frames - current_pos
            chunk_size = min(remaining_data, frames)

            # Copy the audio chunk to the output buffer
            if chunk_size > 0:
                # Use current_pos for slicing
                outdata[:chunk_size] = audio_array[current_pos:current_pos + chunk_size].reshape(-1, 1)

            # If this chunk is smaller than the requested frames, fill the rest with silence
            if chunk_size < frames:
                outdata[chunk_size:] = 0
                # If we've reached the end of the audio data, stop the callback
                if remaining_data <= frames:
                    print("[Playback Callback] End of audio data reached.")
                    # Signal the main loop via the finished event
                    # Check if already set to avoid race conditions? Unlikely needed here.
                    if not playback_finished_event.is_set():
                         playback_finished_event.set()
                    raise sd.CallbackStop

            # Move the current position forward
            current_pos += chunk_size

        # Create the output stream
        stream = sd.OutputStream(
            samplerate=sample_rate,
            channels=1,
            dtype=np.int16,
            callback=callback,
            blocksize=block_size,
            # finished_callback=playback_finished_event.set # Remove this - rely on manual check
        )

        print("[Playback Thread] Starting music playback stream...")
        playback_finished_event.clear() # Ensure clear before start
        stream.start() # Start the stream initially

        while not stop_event.is_set():
            # Check for natural finish based on position first
            if current_pos >= total_frames:
                 print("[Playback Thread] Playback finished naturally (position check).")
                 finished_naturally = True
                 playback_finished_event.set() # Ensure event is set on natural finish
                 break

            if is_paused:
                # --- Currently Paused ---
                if not music_interrupt_event.is_set():
                    # Resume signal received
                    print("[Playback Thread] Resuming stream.")
                    playback_finished_event.clear() # Ensure clear before resume
                    stream.start() # Restart the stream
                    is_paused = False
                else:
                    # Still paused, wait briefly
                    time.sleep(0.1)
            else:
                # --- Currently Playing ---
                if music_interrupt_event.is_set():
                    # Pause signal received
                    print("[Playback Thread] Interrupt detected, pausing stream.")
                    if stream.active: # Check if active before stopping
                        stream.stop() # Stop the stream (pause)
                    is_paused = True
                    playback_interrupted = True # Mark that an interruption occurred
                    # Crucially, clear the finished event *after* stopping,
                    # in case stop() incorrectly triggered it via a potential internal finished_callback.
                    playback_finished_event.clear()
                else:
                    # Still playing, wait briefly before next check
                    time.sleep(0.1)

            # Check stop event again after sleep
            if stop_event.is_set():
                 break

        # Loop exited
        if stop_event.is_set():
            print("[Playback Thread] Stop event detected, terminating.")
        elif finished_naturally:
            print("[Playback Thread] Playback completed naturally.")
        elif is_paused:
             print("[Playback Thread] Loop exited while paused (likely due to stop_event).")
        else:
            # Should not happen if loop logic is correct
            print("[Playback Thread] Loop exited unexpectedly.")

    except Exception as e:
        print(f"[Playback Thread] Error during playback: {e}")
        playback_interrupted = True # Assume error means interruption
    finally:
        # Ensure stream is stopped and closed regardless of exit reason
        if stream:
            if not stream.closed:
                try:
                    if stream.active:
                        stream.stop()
                    stream.close()
                    print("[Playback Thread] Stream closed.")
                except Exception as e:
                    print(f"[Playback Thread] Error closing stream: {e}") # Log error during close

        # Determine final state for cleanup callback
        final_interrupted_state = playback_interrupted and not finished_naturally and not stop_event.is_set()

        # Schedule cleanup ONLY if the thread is exiting naturally, interrupted, or stopped by agent
        # Use lock to safely set the flag indicating cleanup is needed
        with _playback_lock:
            global _playback_cleanup_scheduled
            # Check if cleanup is still relevant (e.g., wasn't already stopped/cleaned up by a new request)
            if current_playback_thread == threading.current_thread():
                 print(f"[Playback Thread] Scheduling cleanup callback... Final interrupted state: {final_interrupted_state}")
                 _playback_cleanup_scheduled = True # Mark that cleanup needs to run
                 if loop and cleanup_callback:
                     loop.call_soon_threadsafe(cleanup_callback, final_interrupted_state)
            else:
                 # This thread instance is outdated, cleanup likely handled elsewhere
                 print("[Playback Thread] Cleanup skipped, thread instance is outdated.")

        print("[Playback Thread] Exiting.")

def _music_playback_cleanup(interrupted_final: bool):
    """
    Callback function executed in the main loop after playback thread finishes.
    Uses a lock and flag to prevent race conditions and double calls.
    """
    global current_playback_thread, _playback_cleanup_scheduled
    with _playback_lock:
        if not _playback_cleanup_scheduled:
            print("[Cleanup Callback] Cleanup already handled or not needed.")
            return # Already handled or not needed

        print(f"[Cleanup Callback] Playback thread finished. Final Interrupted State: {interrupted_final}")

        # Reset agent state ONLY if music wasn't just interrupted/stopped by a new request
        # The reset should happen after the *final* operation completes.
        # If interrupted_final is True, it means wake word interrupted, wait for interaction/timeout.
        # If interrupted_final is False, it means natural end or agent stop.
        if not interrupted_final:
             print("[Cleanup Callback] Playback ended naturally or stopped by agent. Resetting agent state.")
             if _agent_set_music_flag_callback:
                 _agent_set_music_flag_callback(False) # Reset agent's music flag
             if _agent_reset_state_callback:
                 _agent_reset_state_callback() # Reset agent state
        else:
            # If interrupted by wake word or screen touch (simulated wake word).
            print("[Cleanup Callback] Playback interrupted by user. Agent manages state. Resetting music flag and sending notification.")
            if _agent_set_music_flag_callback:
                _agent_set_music_flag_callback(False) # Reset agent's music flag

            # Send system message to agent about the interruption
            if voice_agent_send_text:
                message = "[SYSTEM] Music playback was interrupted by the user."
                print(f"[Cleanup Callback] Sending system message: {message}")
                # Schedule the async send_text function in the main loop
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(voice_agent_send_text(message))
                except RuntimeError as e:
                    print(f"[Cleanup Callback] Error getting running loop to send message: {e}")
                except Exception as e:
                     print(f"[Cleanup Callback] Error scheduling send_text: {e}")
            else:
                print("[Cleanup Callback] Warning: voice_agent_send_text not available, cannot notify agent of interruption.")
            # DO NOT call _agent_reset_state_callback() here. The agent is handling the state transition.

        current_playback_thread = None # Clear the reference
        _playback_cleanup_scheduled = False # Mark cleanup as done
        print("[Cleanup Callback] Cleanup finished.")

def stop_music():
    """
    Stops the currently playing or paused music playback thread gracefully.
    Returns status dictionary. Can be called as a tool or internally.
    """
    global current_playback_thread, _playback_cleanup_scheduled
    print("[Function: stop_music] Attempting to stop music playback...")
    thread_to_join = None
    thread_name = None

    with _playback_lock: # First lock block
        if current_playback_thread and current_playback_thread.is_alive():
            print(f"[Function: stop_music] Playback thread {current_playback_thread.name} is active. Signaling stop.")
            thread_to_join = current_playback_thread # Store ref
            thread_name = thread_to_join.name

            # Signal the thread to stop using the agent's main stop event
            if agent_stop_event:
                agent_stop_event.set()
            else:
                print("[Function: stop_music] Error: Agent stop event not available.")
                # Cannot reliably stop without the event, but try interrupting anyway
                music_interrupt_event.set() # Also set interrupt to wake it if paused
                # Return error immediately while still holding the lock
                return {"status": "error", "message": "Cannot stop music: agent stop event missing.", "no_response_needed": True}

            # Also set interrupt event to ensure the thread wakes up if paused
            music_interrupt_event.set()
            print("[Function: stop_music] Stop and interrupt events set.")
            # thread_to_join and thread_name are now set
        else:
            # Handle case where no active thread was found within the lock
            print("[Function: stop_music] No active playback thread found.")
            # Ensure flags are clear if no thread exists
            if _agent_set_music_flag_callback:
                _agent_set_music_flag_callback(False)
            _playback_cleanup_scheduled = False
            music_interrupt_event.clear()
            # No need to reset agent state here, as nothing was playing.
            return {"status": "success", "message": "No music was playing.", "no_response_needed": True}
        # --- End of first lock block ---

    # If we reach here, it means a thread was found and signaled.
    # thread_to_join and thread_name are guaranteed to be assigned.
    print(f"[Function: stop_music] Waiting for thread {thread_name} to join...")
    thread_to_join.join(timeout=5.0) # Wait up to 5 seconds

    # --- Second lock block for final check and cleanup ---
    with _playback_lock:
        # Check if thread actually finished
        if thread_to_join.is_alive():
            print(f"[Function: stop_music] Warning: Playback thread {thread_name} did not stop within timeout.")
            # Force cleanup state if thread is stuck
            current_playback_thread = None # Assume it's dead or unusable
            _playback_cleanup_scheduled = False
            if _agent_set_music_flag_callback: _agent_set_music_flag_callback(False)
            if _agent_reset_state_callback: _agent_reset_state_callback()
            # music_interrupt_event.clear() # Optional: clear interrupt event

            return {"status": "warning", "message": "Music thread did not stop cleanly.", "no_response_needed": True}
        else:
            print(f"[Function: stop_music] Thread {thread_name} joined successfully.")
            # Cleanup should have been scheduled by the thread itself.
            # Double-check and force cleanup if the callback might have failed.
            if _playback_cleanup_scheduled:
                 print("[Function: stop_music] Warning: Cleanup callback might not have run. Forcing state reset.")
                 if _agent_set_music_flag_callback: _agent_set_music_flag_callback(False)
                 if _agent_reset_state_callback: _agent_reset_state_callback()
                 _playback_cleanup_scheduled = False # Force clear flag
            current_playback_thread = None # Ensure cleared
            # Clear interrupt event after successful stop
            music_interrupt_event.clear()

            return {"status": "success", "message": "Music stopped.", "no_response_needed": True}
        # --- End of second lock block ---
    # Note: The floating 'else' block from the original code is removed as its logic is now handled correctly.

def play_music(search=None):
    """
    Downloads and plays a song. Stops any existing playback first.
    """
    global song_manager, current_playback_thread, agent_stop_event, _playback_cleanup_scheduled
    print(f"[Function: play_music] Received request for: '{search}'")

    # --- Stop Existing Playback ---
    with _playback_lock:
        if current_playback_thread and current_playback_thread.is_alive():
            print("[Function: play_music] Existing playback thread found. Stopping it first...")
            # Need to run stop_music outside the lock to allow joining
            needs_stop = True
        else:
            needs_stop = False

    if needs_stop:
        stop_result = stop_music() # Call stop_music without holding the lock
        print(f"[Function: play_music] stop_music result: {stop_result}")
        if stop_result.get("status") not in ["success", "warning"]: # Allow warning (timeout) but proceed
            # Failed to stop previous playback, abort new request
            return {"status": "error", "message": f"Failed to stop previous music: {stop_result.get('message')}", "no_response_needed": False}
        # Short delay to ensure resources are released (optional)
        time.sleep(0.2)

    # --- Pre-checks (Agent Stop Event, Callbacks, Search Query) ---
    if 'agent_stop_event' not in globals() or agent_stop_event is None:
         print("Error: Agent stop event not set. Cannot start playback.")
         return {"status": "error", "message": "Internal error: Agent stop event not configured.", "no_response_needed": False}
    if not _agent_pause_listening_callback or not _agent_reset_state_callback or not _agent_set_music_flag_callback:
        return {"status": "error", "message": "Agent callbacks not configured.", "no_response_needed": False}
    if search is None:
        return {"status": "error", "message": "Music search query is missing.", "no_response_needed": False}

    # --- Initialize SongManager ---
    # ... (rest of the initialization logic remains the same) ...
    if not song_manager:
        try:
            song_manager = SongManager()
        except Exception as e:
            print(f"Failed to initialize SongManager: {e}")
            return {"status": "error", "message": f"Failed to initialize SongManager: {e}", "no_response_needed": False}

    print(f"[Function: play_music] Proceeding with download for: '{search}'")

    # --- Download Track ---
    # Pass the 10-second timeout here
    download_result = song_manager.download_track(search, timeout=10)

    # Check for success, timeout, or other errors
    if not download_result or download_result.get("status") not in ["success"]:
        if download_result and download_result.get("status") == "timeout":
             error_msg = f"Le téléchargement de la musique pour '{search}' a dépassé le délai imparti (10 secondes). Veuillez réessayer ou choisir une autre chanson."
             print(f"[Function: play_music] Download timed out: {error_msg}")
             # Return error message to the assistant (no_response_needed=False)
             return {"status": "error", "message": error_msg, "no_response_needed": False}
        else:
             # Handle other download errors
             error_msg = f"Failed to download track for '{search}'. Reason: {download_result.get('message', 'Unknown error')}"
             print(f"[Function: play_music] {error_msg}")
             # Return error message to the assistant (no_response_needed=False)
             return {"status": "error", "message": error_msg, "no_response_needed": False}


    # ... rest of the function remains the same (getting title, decoding, starting thread) ...
    title = download_result.get("title", "Unknown Title")
    artist = download_result.get("artist", "Unknown Artist")
    file_path = download_result.get("file")

    if not file_path or not os.path.exists(file_path):
        # ... (error handling) ...
        error_msg = f"Audio file path missing or invalid for '{title}' after successful download task."
        print(f"[Function: play_music] {error_msg}")
        return {"status": "error", "message": error_msg, "no_response_needed": False}

    # --- Decode and Prepare PCM Data ---
    # ... (decoding logic remains the same) ...
    try:
        audio_segment = AudioSegment.from_file(file_path)
        audio_segment = audio_segment.set_channels(1).set_sample_width(2)
        sample_rate = audio_segment.frame_rate
        pcm_data = audio_segment.raw_data
        duration_seconds = len(pcm_data) / (sample_rate * 2)
        print(f"[Function: play_music] Audio decoded: {len(pcm_data)} bytes, Rate: {sample_rate} Hz, Duration: {format_duration(duration_seconds)}")
    except Exception as e:
        # ... (error handling) ...
        error_msg = f"Failed to decode audio file '{file_path}' for '{title}': {e}"
        print(f"[Function: play_music] {error_msg}")
        return {"status": "error", "message": error_msg, "no_response_needed": False}

    # --- Start Playback Thread ---
    with _playback_lock: # Use lock for thread creation and assignment
        # Clear events before starting new thread
        music_interrupt_event.clear()
        playback_finished_event.clear()
        # Crucially, clear the agent stop event *IF* it was set by stop_music and the agent isn't actually stopping.
        # This assumes stop_music was called internally, not due to agent shutdown.
        if agent_stop_event and agent_stop_event.is_set():
             # Check if agent is actually running - if yes, clear the stop event
             # This requires passing agent running state or using another flag. Complex.
             # Safer alternative: Rely on agent.start() to clear the stop event initially.
             # If stop_music is called, it sets the event. The thread stops.
             # When play_music starts a *new* thread, it passes the *current* state of the event.
             # If the agent is still running, the event should ideally be clear.
             # Let's clear it here assuming the agent isn't stopping.
             print("[Function: play_music] Clearing agent stop event before starting new thread.")
             agent_stop_event.clear()

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            print("Error: Could not get running asyncio loop.")
            return {"status": "error", "message": "Internal error: Could not get event loop.", "no_response_needed": False}

        # Create the new thread
        new_thread = threading.Thread(
            target=_playback_thread_func,
            args=(pcm_data, sample_rate, loop, _music_playback_cleanup, agent_stop_event),
            daemon=True,
            name=f"PlaybackThread-{title[:10]}" # Give thread a name
        )

        # Assign the new thread and set cleanup flag
        current_playback_thread = new_thread
        _playback_cleanup_scheduled = False # Reset flag for the new thread

        # Notify agent state changes *before* starting thread
        if _agent_pause_listening_callback: _agent_pause_listening_callback()
        if _agent_set_music_flag_callback: _agent_set_music_flag_callback(True)

        print(f"[Function: play_music] Starting new playback thread {current_playback_thread.name}...")
        current_playback_thread.start()

    # --- Return Immediately ---
    start_message = f"Starting playback of '{title}' by '{artist}'. Duration: {format_duration(duration_seconds)}."
    print(f"[Function: play_music] {start_message}")
    # Return status success, the start message, and no_response_needed=True
    return {"status": "success", "message": start_message, "no_response_needed": True}

# --- Add function to set the global stop event ---
agent_stop_event: Optional[threading.Event] = None

def set_agent_stop_event(event: threading.Event):
    """Stores the agent's stop event globally for the playback thread."""
    global agent_stop_event
    agent_stop_event = event
# --- End ---

# ---------------------------------------------------------------------------
# TOOL HANDLER MAPPING (DYNAMIC)
# ---------------------------------------------------------------------------

FUNCTIONS_JSON_PATH = os.path.join(os.path.dirname(__file__), "data", "functions.json")

def get_tool_handlers():
    """
    Dynamically builds the tool handlers dictionary by mapping function names
    from functions.json to the actual function objects in this module.
    """
    tool_handlers = {}
    try:
        with open(FUNCTIONS_JSON_PATH, "r", encoding="utf-8") as f:
            tools_config = json.load(f)

        # Get all functions defined in the current module
        current_module_functions = {name: obj for name, obj in globals().items() if inspect.isfunction(obj)}

        for tool in tools_config:
            function_name = tool.get("name")
            if function_name:
                handler_function = current_module_functions.get(function_name)
                if handler_function:
                    tool_handlers[function_name] = handler_function
                else:
                    # This warning helps catch mismatches during development
                    print(f"Warning: Handler function '{function_name}' defined in functions.json not found in functions_utils.py")

    except FileNotFoundError:
        print(f"Error: functions.json not found at {FUNCTIONS_JSON_PATH}")
        # Decide how to handle: return empty dict, raise error, etc.
    except json.JSONDecodeError:
        print(f"Error: Could not decode functions.json at {FUNCTIONS_JSON_PATH}")
    except Exception as e:
        print(f"An unexpected error occurred while building tool handlers: {e}")
    return tool_handlers