from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import threading # Added
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, cast

import numpy as np
import sounddevice as sd  # Added
import pvporcupine # Added
from openai import AsyncOpenAI
from openai.types.beta.realtime.session import Session
from openai.resources.beta.realtime.realtime import AsyncRealtimeConnection
from pydub import AudioSegment

# Import the event and the new resume signal function from functions_utils
# Removed the try...except block to ensure proper import or raise error
from functions_utils import music_interrupt_event

# Paramètres audio
RECORDER_SAMPLE_RATE = 16000 # Default, will be updated by Porcupine init
OPENAI_SAMPLE_RATE = 24000 # OpenAI requires 24kHz
CHANNELS = 1
SAMPLE_WIDTH = 2 # Bytes per sample (16-bit)

class VoiceAgent:
    """
    Un agent vocal personnalisable qui envoie automatiquement l'audio à l'API OpenAI.
    Il maintient une conversation continue avec l'utilisateur.
    """

    def __init__(
        self,
        porcupine_access_key: str,
        porcupine_keyword_paths: List[str],
        porcupine_model_path: str = None,
        porcupine_sensitivity: float = 0.5,
        api_key: Optional[str] = None,
        model: str = "gpt-4o-realtime-preview",
        temperature: float = 0.6,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_handlers: Optional[Dict[str, Callable]] = None,
        voice: str = "ash",
        instructions: str = "Tu es un assistant vocal. Réponds d'un ton enjoué et amical !",
        auto_reconnect: bool = True,
        turn_detection: Dict[str, Any] = {"type": "semantic_vad"},
        on_response_start: Optional[Callable] = None,
        on_response_done: Optional[Callable] = None,
        on_transcript: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
        history_file: str = "messages_history.json",
        nextion_controller: Optional[object] = None
    ):
        """
        Initialise un agent vocal.

        Args:
            porcupine_access_key: Clé d'accès pour l'API Picovoice Porcupine.
            porcupine_keyword_paths: Liste des chemins vers les fichiers de mots-clés Porcupine (.ppn).
            porcupine_sensitivity: Sensibilité de détection du mot-clé (0.0 à 1.0).
            api_key: Clé API OpenAI. Si None, utilise la variable d'environnement OPENAI_API_KEY.
            model: Modèle à utiliser.
            voice: Voix à utiliser pour la réponse.
            auto_reconnect: Reconnexion automatique en cas d'erreur.
            turn_detection: Configuration de la détection de tour de parole.
            on_response_start: Fonction appelée au début d'une réponse.
            on_response_done: Fonction appelée à la fin d'une réponse.
            on_transcript: Fonction appelée à chaque mise à jour de la transcription.
            on_error: Fonction appelée en cas d'erreur.
        """
        if not porcupine_access_key:
            raise ValueError("Porcupine Access Key is required.")
        if not porcupine_keyword_paths:
            raise ValueError("Porcupine keyword paths are required.")

        self.client = AsyncOpenAI(api_key=api_key)
        self.model = model
        self.temperature = temperature
        self.tools = tools if tools else []
        self.tool_handlers = tool_handlers if tool_handlers else {}
        self.voice = voice
        self.instructions = instructions
        self.auto_reconnect = auto_reconnect
        self.turn_detection = turn_detection
        self.nextion_controller = nextion_controller
        
        # Callbacks
        self.on_response_start = on_response_start
        self.on_response_done = on_response_done
        self.on_transcript = on_transcript
        self.on_error = on_error
        
        # Porcupine Initialization
        try:
            self.porcupine = pvporcupine.create(
                access_key=porcupine_access_key,
                model_path=porcupine_model_path,
                keyword_paths=porcupine_keyword_paths,
                sensitivities=[porcupine_sensitivity] * len(porcupine_keyword_paths)
            )
            global RECORDER_SAMPLE_RATE
            RECORDER_SAMPLE_RATE = self.porcupine.sample_rate # Use Porcupine's sample rate for recorder
            self.porcupine_frame_length = self.porcupine.frame_length
            print(f"Porcupine initialisé avec sample_rate={RECORDER_SAMPLE_RATE}, frame_length={self.porcupine_frame_length}")
        except pvporcupine.PorcupineError as e:
            print(f"Erreur d'initialisation de Porcupine: {e}")
            raise

        # Nextion initialization
        if self.nextion_controller:
            try:
                self.nextion_controller.connect()
                print("Nextion controller initialized.")
            except Exception as e:
                print(f"Error initializing Nextion controller: {e}")
                raise
        
        # Sounddevice InputStream Initialization
        self.input_stream: Optional[sd.InputStream] = None
        self._input_buffer = bytearray() # Buffer for Porcupine processing
        self._input_block_size = 512 # Adjust as needed, smaller might be more responsive
        try:
            # Ensure Porcupine's sample rate is used for input stream
            self.input_stream = sd.InputStream(
                samplerate=RECORDER_SAMPLE_RATE,
                blocksize=self._input_block_size, # Process audio in smaller chunks
                channels=CHANNELS,
                dtype=np.int16, # 16-bit PCM
                callback=self._audio_input_callback,
                device=None # Use default input device
            )
            print(f"Sounddevice InputStream initialisé avec sample_rate={RECORDER_SAMPLE_RATE}, blocksize={self._input_block_size}")
        except Exception as e:
            print(f"Erreur d'initialisation de sounddevice InputStream: {e}")
            sd.check_input_settings(samplerate=RECORDER_SAMPLE_RATE, channels=CHANNELS, dtype=np.int16) # Check settings for more info
            if hasattr(self, 'porcupine'):
                self.porcupine.delete()
            raise

        # État interne
        self.connection: Optional[AsyncRealtimeConnection] = None
        self.session: Optional[Session] = None
        self.running = False
        self._should_send_audio = False # Start by not sending audio
        self._waiting_for_wakeword = True # Start by waiting for wake word
        self.is_playing_music = False # Flag to indicate music playback status
        self.stop_playback_event = threading.Event() # Added: Event to signal playback thread termination
        self.audio_player = AudioPlayerAsync() # Will now use OPENAI_SAMPLE_RATE
        self.last_audio_item_id: Optional[str] = None
        self.transcript_items: Dict[str, str] = {}
        self.current_assistant_response: str = "" # Added for accumulating response text
        self._pending_music_interrupt_check: bool = False # Added for false wake word check during music
        self._interrupt_check_start_time: Optional[float] = None # Added
        self._pending_non_music_wakeword_check: bool = False # Added for false wake word check without music
        self._non_music_check_start_time: Optional[float] = None # Added

        # Historique de la conversation
        self.messages_history: List[Dict[str, Any]] = [] # Added
        self.current_user_audio_chunks: List[bytes] = [] # Added
        self.is_user_speaking: bool = False # Added
        self._needs_history_injection: bool = False # Added for session expiration handling

        # État de l'IA parlante
        self._is_speaking = False
        self._last_frame_count = 0
        self._speaking_check_task = None
        
        # Threading & Async Loop
        # self.audio_thread: Optional[threading.Thread] = None # REMOVED - No separate thread needed
        self.loop: Optional[asyncio.AbstractEventLoop] = None

        # Gestion des outils
        if len(self.tools) != len(self.tool_handlers):
            raise ValueError("tools and tool_handlers must have the same length and be non-empty.")

        self.history_file = history_file # Added

    def _load_history(self):
        """Charge l'historique depuis le fichier JSON."""
        try:
            if os.path.exists(self.history_file):
                with open(self.history_file, 'r', encoding='utf-8') as f:
                    self.messages_history = json.load(f)
                print(f"Historique chargé depuis {self.history_file}")
        except (FileNotFoundError, json.JSONDecodeError, Exception) as e:
            print(f"Impossible de charger l'historique depuis {self.history_file}: {e}. Un nouveau fichier sera créé.")
            self.messages_history = []

    def _save_history(self):
        """Sauvegarde l'historique dans le fichier JSON."""
        try:
            with open(self.history_file, 'w', encoding='utf-8') as f:
                json.dump(self.messages_history, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"Erreur lors de la sauvegarde de l'historique dans {self.history_file}: {e}")


    async def start(self):
        """Démarre l'agent vocal et la détection de mot-clé."""
        if self.running:
            return

        self._load_history()
        self.running = True
        self.loop = asyncio.get_running_loop() # Capture the loop

        # Start Sounddevice InputStream
        try:
            if self.input_stream:
                self.input_stream.start()
                print("Sounddevice InputStream démarré.")
            else:
                raise RuntimeError("Input stream not initialized.")
        except Exception as e:
            print(f"Erreur au démarrage de Sounddevice InputStream: {e}")
            self.running = False
            if hasattr(self, 'porcupine'):
                self.porcupine.delete()
            return

        # Start main agent task (connects to OpenAI etc.)
        self.main_task = asyncio.create_task(self._run_agent())
        print("Tâche principale de l'agent démarrée.")
        # Ensure interrupt event is clear on start
        music_interrupt_event.clear()
        self.stop_playback_event.clear() # Clear stop event on start
        
    async def stop(self):
        """Arrête l'agent vocal, Porcupine et PvRecorder."""
        print("Arrêt de l'agent...")
        self.running = False # Signal loops to stop

        # Signal playback thread to stop completely
        print("Signaling playback thread to terminate...")
        self.stop_playback_event.set()

        # Signal interruption if music is playing
        if self.is_playing_music:
            print("Stopping music playback due to agent shutdown...")
            music_interrupt_event.set()
            # Give playback thread a moment to react
            await asyncio.sleep(0.5)

        # Stop main task
        if hasattr(self, 'main_task') and not self.main_task.done():
            self.main_task.cancel()
            try:
                await self.main_task
            except asyncio.CancelledError:
                print("Tâche principale annulée.")
            except Exception as e:
                print(f"Erreur lors de l'arrêt de la tâche principale: {e}")


        # Stop sounddevice stream
        if self.input_stream:
            try:
                self.input_stream.stop()
                self.input_stream.close()
                print("Sounddevice InputStream arrêté et fermé.")
            except Exception as e:
                print(f"Erreur à l'arrêt/fermeture de Sounddevice InputStream: {e}")
            finally:
                self.input_stream = None

        # Delete Porcupine
        if hasattr(self, 'porcupine') and self.porcupine:
            self.porcupine.delete()
            print("Porcupine supprimé.")

        # Stop audio player
        self.audio_player.terminate()
        print("Lecteur audio terminé.")

        # Close connection if exists
        if self.connection:
            try:
                await self.connection.close()
                print("Connexion OpenAI fermée.")
            except Exception as e:
                print(f"Erreur lors de la fermeture de la connexion OpenAI: {e}")
            finally:
                self.connection = None

        self._save_history() # Save history on stop
        print("Agent arrêté.")
            
    async def _run_agent(self):
        """Exécute la boucle principale de l'agent."""
        reconnect_attempts = 0
        max_reconnect_attempts = 3 if self.auto_reconnect else 1
        
        while self.running:
            # Reset reconnect attempts if the last disconnect was due to session expiration
            if self._needs_history_injection:
                print("Session expirée détectée, tentative de reconnexion avec injection de l'historique.")
                reconnect_attempts = 0 # Allow immediate retry for expiration

            try:
                await self._connect_and_process_events()
                # Si on arrive ici et que l'agent est toujours en cours d'exécution,
                # c'est que la connexion a été fermée mais l'utilisateur n'a pas explicitement arrêté l'agent
                if self.running:
                    if self.auto_reconnect:
                        print("La connexion a été fermée. Tentative de reconnexion...")
                        await asyncio.sleep(2)  # Attente avant reconnexion
                    else:
                        # Si auto_reconnect est désactivé mais que l'agent est censé continuer, 
                        # c'est probablement une déconnexion inattendue
                        print("Session fermée de façon inattendue. Utilisez Ctrl+C pour quitter complètement.")
                        self.running = False  # Arrêter l'agent proprement
            except Exception as e:
                reconnect_attempts += 1
                if self.on_error:
                    self.on_error(e)
                else:
                    print(f"Erreur dans l'agent vocal: {str(e)}")
                    
                # Check if the error was session expiration; if so, the flag is already set
                # and reconnect_attempts was reset at the start of the loop iteration.

                if not self.auto_reconnect or reconnect_attempts >= max_reconnect_attempts:
                    print(f"Arrêt de l'agent après {reconnect_attempts} tentatives d'erreur non liées à l'expiration.")
                    self.running = False
                    if not self.auto_reconnect and not self._needs_history_injection: # Only raise if not auto-reconnecting AND not a planned expiration reconnect
                        raise
                else:
                    # Attendre avant de reconnecter avec un délai exponentiel
                    wait_time = min(2 ** reconnect_attempts, 30)
                    print(f"Tentative de reconnexion dans {wait_time} secondes...")
                    await asyncio.sleep(wait_time)
                
    async def _connect_and_process_events(self):
        """Établit une connexion, injecte l'historique si nécessaire, et traite les événements."""
        async with self.client.beta.realtime.connect(model=self.model) as conn:
            self.connection = conn
            
            # Configuration de la session avec les outils si définis
            session_config = {
                "voice": self.voice,
                "turn_detection": self.turn_detection,
                "tools": self.tools,
                "temperature": self.temperature,
                "instructions": self.instructions,
            }

            await conn.session.update(session=session_config)
            print("Session initialisée, en attente de la confirmation...")
            
            # Inject history if needed (e.g., after session expiration)
            if self._needs_history_injection:
                await self._inject_history(conn)
                self._needs_history_injection = False # Reset flag after successful injection attempt

            try:
                # Traiter les événements entrants
                async for event in conn:
                    await self._handle_event(event)
            except Exception as e:
                print(f"Erreur dans le traitement des événements: {str(e)}")
                # Check if this specific error is session expiration to set the flag
                # Note: This might be redundant if the error event is handled below,
                # but serves as a fallback. Consider if specific connection errors need flag setting.
                # Example: if isinstance(e, SpecificConnectionClosedError) and reason == 'expired':
                #    self._needs_history_injection = True
                raise # Re-raise to be caught by _run_agent
            finally:
                self.connection = None
                    
    async def _handle_event(self, event):
        """Gère les événements de l'API."""
        if event.type == "session.created":
            self.session = event.session
            print(f"Session créée: {event.session.id}")
            
        elif event.type == "session.updated":
            self.session = event.session
            
        elif event.type == "response.audio.delta":
            if event.item_id != self.last_audio_item_id:
                self.audio_player.reset_frame_count()
                self.last_audio_item_id = event.item_id
                if self.on_response_start:
                    self.on_response_start()
                    
            bytes_data = base64.b64decode(event.delta)
            self.audio_player.add_data(bytes_data)
            
        elif event.type == "response.audio_transcript.delta":
            if event.item_id not in self.transcript_items:
                self.transcript_items[event.item_id] = ""
                
            self.transcript_items[event.item_id] += event.delta
            print(event.delta, end="", flush=True)
            current_transcript = self.transcript_items[event.item_id]
            
            if self.on_transcript:
                self.on_transcript(current_transcript)

            self.current_assistant_response += event.delta # Accumulate assistant response

        elif event.type == "response.audio_transcript.done":
            print("\n")
            # Reset transcript item for potential reuse, but keep accumulated response
            if event.item_id in self.transcript_items:
                del self.transcript_items[event.item_id]
                
        elif event.type == "response.done":
            # If there's remaining accumulated text, it means it was a text-only response
            if self.current_assistant_response:
                assistant_message = {
                    "role": "assistant",
                    "content": self.current_assistant_response.strip()
                }
                # Avoid adding empty messages
                if assistant_message["content"]:
                    self.messages_history.append(assistant_message)
                    self._save_history()
                self.current_assistant_response = "" # Reset after logging

            if self.on_response_done:
                self.on_response_done()

            # Reset state unless we are specifically waiting for speech after a music interrupt.
            # If the interaction following the interrupt is done, we should reset.
            if not self._pending_music_interrupt_check:
                 print("[Response Done] Resetting to wake word state.")
                 self.reset_to_wakeword_state()
            else:
                 # This case should ideally not happen if speech_started cleared the flag,
                 # but added for robustness.
                 print("[Response Done] Pending music interrupt check active, state not reset.")
        
        elif event.type == "response.output_item.done":
            # Traiter les appels de fonction
            if event.item and event.item.type == "function_call":
                function_name = event.item.name
                call_id = event.item.call_id
                arguments = event.item.arguments # This is already a JSON string

                # Log the function call initiated by the assistant, including any preceding text
                function_call_message = {
                    "role": "assistant",
                    "content": None, # Default to null content
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": function_name,
                                "arguments": arguments
                            }
                        }
                    ]
                }
                # If there was text before the tool call, add it to the same message
                if self.current_assistant_response:
                    function_call_message["content"] = self.current_assistant_response.strip()
                    self.current_assistant_response = "" # Reset after using it

                self.messages_history.append(function_call_message)
                self._save_history() # Save after logging the call

                if function_name in self.tool_handlers:
                    # Exécuter la fonction et envoyer le résultat (or handle no-response case)
                    await self._execute_tool(function_name, call_id, arguments)
                else:
                     print(f"Warning: No handler found for tool '{function_name}'")
                     # Send error back to OpenAI?
                     await self._send_tool_error(call_id, function_name, "Handler not implemented.")

        elif event.type == "input_audio_buffer.speech_started":
            print("[User starts speaking]")
            # If we were waiting for speech after a music interrupt, confirm it.
            if self._pending_music_interrupt_check:
                print("Speech detected after music interrupt. Proceeding with interaction.")
                self._pending_music_interrupt_check = False # Cancel the check
                self._interrupt_check_start_time = None
                # Transition to listening state (already done when interrupt occurred)
                # self._waiting_for_wakeword = False
                # self._should_send_audio = True
                # The music playback thread should stop itself based on the interrupt event

            # If we were waiting for speech after a non-music wake word, confirm it.
            if self._pending_non_music_wakeword_check:
                print("Speech detected after non-music wake word. Proceeding with interaction.")
                self._pending_non_music_wakeword_check = False # Cancel the check
                self._non_music_check_start_time = None
                # State is already listening

            self.is_user_speaking = True
            self.current_user_audio_chunks = [] # Clear previous chunks

        elif event.type == "input_audio_buffer.speech_stopped":
            print("[User stops speaking]")
            self.is_user_speaking = False
            # Remove listening indicator from Nextion screen
            if self.nextion_controller:
                self.nextion_controller.is_listening(False)
            # Stop sending audio *unless* music is playing or pending check
            # Go back to waiting for wake word *unless* music is playing or pending check
            if not self._pending_music_interrupt_check and not self._pending_non_music_wakeword_check:
                self._should_send_audio = False
                self._waiting_for_wakeword = True
                print("Parole arrêtée. En attente du mot-clé...")
            else:
                # If speech stops *during* a pending check window, keep listening state active until timeout.
                print("Parole arrêtée (during pending check). Listening state remains active.")

            if self.current_user_audio_chunks:
                # Combine, encode, and save user audio message
                full_audio = b"".join(self.current_user_audio_chunks)
                # Ensure audio is pcm16 before encoding
                # Assuming chunks are already pcm16 from pvrecorder
                audio_base64 = base64.b64encode(full_audio).decode("utf-8")
                user_message = {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "audio": audio_base64
                        }
                    ]
                }
                self.messages_history.append(user_message)
                self._save_history()
                self.current_user_audio_chunks = [] # Clear chunks after saving

        elif event.type == "error":
            print(f"Erreur dans la réponse: {event.error}")
            # Check for session expiration
            if hasattr(event.error, 'code') and event.error.code == "session_expired":
                print("--- Session expirée détectée par l'événement d'erreur ---")
                self._needs_history_injection = True
                # The connection will likely close soon after this error.
                # The _run_agent loop will handle the reconnection attempt.
            # If an error occurs, reset music state just in case
            if self.is_playing_music:
                print("Resetting music state due to API error.")
                music_interrupt_event.set() # Signal interruption
                self.is_playing_music = False
                self.reset_to_wakeword_state()

    async def _execute_tool(self, function_name: str, call_id: str, arguments_json: str):
        """Exécute un outil, gère le résultat (envoi ou non), et loggue."""
        if not self.connection:
            print("Cannot execute tool, no connection.")
            return

        result_output = ""
        tool_result = None
        no_response_needed = False

        # --- Pre-execution state changes ---
        if function_name == 'play_music':
            # Clear any previous interrupt signal before starting playback
            music_interrupt_event.clear()
            # Flag is set inside play_music after download success, before thread start

        try:
            # Analyser les arguments JSON
            arguments = json.loads(arguments_json)

            # Exécuter la fonction
            handler = self.tool_handlers[function_name]
            print(f"Executing tool: {function_name} with args: {arguments}")
            # Tool execution might block (like play_music waiting for thread)
            tool_result = handler(**arguments) # Expects a dictionary

            # Process result
            if isinstance(tool_result, dict):
                result_output = tool_result.get("message", "") # Get message for logging
                no_response_needed = tool_result.get("no_response_needed", False)
                status = tool_result.get("status", "unknown")
                print(f"Tool '{function_name}' finished with status: {status}. Message: {result_output}")
            else:
                # Handle unexpected return type
                result_output = str(tool_result)
                print(f"Warning: Tool '{function_name}' returned unexpected type. Treating as string output.")


            # --- Send result back to OpenAI ONLY if response is needed ---
            if not no_response_needed:
                print(f"Sending tool output for {function_name} to OpenAI.")
                await self.connection.send({
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": result_output # Send the message part as output
                    }
                })
            else:
                print(f"Tool '{function_name}' indicated no OpenAI response needed. Skipping output send.")

        except Exception as e:
            # En cas d'erreur pendant l'exécution de l'outil
            error_result = f"Error executing {function_name}: {str(e)}"
            print(error_result)
            result_output = error_result # Log the error as output
            no_response_needed = False # Assume error needs reporting back

            # Send error back to OpenAI
            await self._send_tool_error(call_id, function_name, error_result)

            if self.on_error:
                self.on_error(e)

        # --- Post-execution state changes ---
        # is_playing_music flag is reset within play_music itself after join()

        # Log the function output message regardless of whether it was sent to OpenAI
        function_output_message = {
            "role": "tool",
            "tool_call_id": call_id,
            "name": function_name,
            "content": result_output # Log the message/error
        }
        self.messages_history.append(function_output_message)
        self._save_history()

        # --- Trigger next response ONLY if needed ---
        if not no_response_needed:
            print(f"Requesting OpenAI response after tool '{function_name}'.")
            await self.connection.send({"type": "response.create"})
        else:
            # If no response is needed (e.g., after music started),
            # the agent state is managed by the music playback lifecycle and wake word detection.
            # Do NOT reset state here if the tool was play_music.
            if function_name == 'play_music':
                 print(f"Tool '{function_name}' finished. Agent state managed by music playback lifecycle.")
            else:
                 # For other tools that don't need a response, reset state.
                 print(f"Tool '{function_name}' finished. Agent returning to wake word state.")
                 # Reset state only if not potentially checking for music interrupt/resume
                 if not self._pending_music_interrupt_check: # Check added
                     self.reset_to_wakeword_state()

    async def _send_tool_error(self, call_id: str, function_name: str, error_message: str):
        """Sends a function call output indicating an error."""
        if not self.connection: return
        try:
            await self.connection.send({
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": error_message
                }
            })
        except Exception as e:
            print(f"Failed to send tool error output for {function_name}: {e}")

    def register_tool(self, tool_config: Dict[str, Any], handler: Callable):
        """
        Enregistre un outil qui peut être appelé par le modèle.
        
        Args:
            tool_config: Configuration de l'outil au format JSON compatible avec l'API OpenAI.
            handler: Fonction à exécuter lorsque l'outil est appelé.
        """
        self.tools.append(tool_config)
        self.tool_handlers[tool_config["name"]] = handler
        
        # Mettre à jour la session si elle existe déjà
        if self.connection and self.session:
            asyncio.create_task(self._update_session_tools())
            
    async def _update_session_tools(self):
        """Met à jour les outils dans la session existante."""
        if self.connection:
            await self.connection.session.update(session={
                "tools": self.tools
            })

    async def _send_audio_chunk(self, pcm_chunk_16khz: bytes):
        """Resample audio chunk to 24kHz and send to OpenAI."""
        if self.connection and self._should_send_audio:
            try:
                # Create an AudioSegment from the raw 16kHz PCM data
                audio_segment = AudioSegment(
                    data=pcm_chunk_16khz,
                    sample_width=SAMPLE_WIDTH,
                    frame_rate=RECORDER_SAMPLE_RATE,
                    channels=CHANNELS
                )

                # Resample to 24kHz
                resampled_segment = audio_segment.set_frame_rate(OPENAI_SAMPLE_RATE)

                # Get the raw bytes of the resampled audio
                pcm_chunk_24khz = resampled_segment.raw_data

                # Send the 24kHz audio
                await self.connection.input_audio_buffer.append(
                    audio=base64.b64encode(pcm_chunk_24khz).decode("utf-8")
                )
            except Exception as e:
                # Handle potential connection or resampling errors during send
                print(f"Erreur lors du rééchantillonnage/envoi du chunk audio: {e}")
                # Consider stopping audio sending or triggering reconnect logic
                # self._should_send_audio = False
                # self._waiting_for_wakeword = True

    def _audio_input_callback(self, indata: np.ndarray, frames: int, time_info, status):
        """
        Callback function for the sounddevice InputStream.
        Processes incoming audio for wake word detection and streaming to OpenAI.
        Runs on a separate thread managed by sounddevice.
        """
        if not self.running:
            return # Stop processing if agent is stopping

        if status:
            print(f"[Audio Callback] Status: {status}")
            # Consider handling specific statuses like sd.CallbackFlags.input_overflow

        try:
            # Convert numpy array (int16) to bytes
            pcm_bytes_16khz = indata.tobytes()

            # --- Check for music interrupt timeout (Run this check periodically, e.g., here) ---
            # Note: This check now runs frequently within the audio callback.
            if self._pending_music_interrupt_check and \
               self._interrupt_check_start_time is not None and \
               (time.time() - self._interrupt_check_start_time > 5.0):
                print("No speech detected within 5 seconds after music interrupt.")
                # Reset check state *before* attempting resume
                self._pending_music_interrupt_check = False
                self._interrupt_check_start_time = None
                if self.is_playing_music: # Check the flag before resuming
                    print("Attempting to resume music playback...")
                    self.resume_music_playback() # Signal music to resume
                    # If music resumes, go back to waiting for wake word state
                    self.reset_to_wakeword_state() # Reset state after signaling resume
                else:
                    print("Music was not playing, no resume needed.")
                    # Ensure state is reset if timeout happens without music playing
                    self.reset_to_wakeword_state()

            # --- Check for non-music wake word timeout ---
            if self._pending_non_music_wakeword_check and \
               self._non_music_check_start_time is not None and \
               (time.time() - self._non_music_check_start_time > 5.0): # 5 second timeout
                print("No speech detected within 5 seconds after non-music wake word. Assuming false positive.")
                self._pending_non_music_wakeword_check = False
                self._non_music_check_start_time = None
                self.reset_to_wakeword_state() # Go back to waiting

            # --- Process Audio Frame ---
            if self._waiting_for_wakeword:
                # Don't process for wake word if we are pending any check
                if not self._pending_music_interrupt_check and not self._pending_non_music_wakeword_check:
                    # Buffer audio data for Porcupine
                    self._input_buffer.extend(pcm_bytes_16khz)

                    # Process in chunks matching Porcupine frame length
                    while len(self._input_buffer) >= self.porcupine_frame_length * SAMPLE_WIDTH: # Multiply by bytes per sample
                        frame_bytes = self._input_buffer[:self.porcupine_frame_length * SAMPLE_WIDTH]
                        del self._input_buffer[:self.porcupine_frame_length * SAMPLE_WIDTH]

                        # Convert bytes back to list of int16 for Porcupine
                        frame_int16 = np.frombuffer(frame_bytes, dtype=np.int16).tolist()

                        try:
                            keyword_index = self.porcupine.process(frame_int16)
                            if keyword_index >= 0:
                                print(f"Mot-clé détecté (index {keyword_index})!")
                                # Display listening indicator on nextion
                                if self.nextion_controller:
                                    self.nextion_controller.is_listening(True)
                                if self.is_playing_music:
                                    print("--- Interruption de la musique par mot-clé ---")
                                    music_interrupt_event.set() # Signal playback thread to pause
                                    self._pending_music_interrupt_check = True
                                    self._interrupt_check_start_time = time.time()
                                    print("Waiting 5s for speech before resuming music...")
                                    self._waiting_for_wakeword = False
                                    self._should_send_audio = True
                                    print("En écoute (après interruption musique)...")
                                else:
                                    # --- Wake word detected without music ---
                                    self._waiting_for_wakeword = False
                                    self._should_send_audio = True
                                    self._pending_non_music_wakeword_check = True # Start check
                                    self._non_music_check_start_time = time.time() # Record time
                                    print("En écoute (waiting 5s for speech confirmation)...")

                                # Clear buffer after wake word detection? Optional.
                                # self._input_buffer.clear()
                                break # Exit inner loop once wake word detected in this callback cycle
                        except pvporcupine.PorcupineError as e:
                             print(f"Erreur de traitement Porcupine: {e}")
                             self.reset_to_wakeword_state() # Reset state on error
                        except Exception as e:
                             print(f"Erreur inattendue dans le traitement Porcupine: {e}")
                             self.reset_to_wakeword_state() # Reset state on error

            else: # Not waiting for wake word (actively listening)
                if self._should_send_audio:
                    # Send the raw 16kHz PCM data received in this callback
                    if self.loop:
                        # Use pcm_bytes_16khz directly
                        asyncio.run_coroutine_threadsafe(self._send_audio_chunk(pcm_bytes_16khz), self.loop)

                    # Store audio chunk if OpenAI has detected speech start
                    if self.is_user_speaking:
                        self.current_user_audio_chunks.append(pcm_bytes_16khz)
                    else:
                        # If not actively speaking but listening, clear the buffer used for wake word detection
                        # to avoid processing old data if we switch back to wake word state.
                        self._input_buffer.clear()
        except Exception as e:
            print(f"Erreur dans le callback audio: {e}")
            # Consider resetting state or logging more details
            # self.reset_to_wakeword_state()

    def pause_listening(self):
        """Pause l'envoi d'audio (ne désactive pas la détection de mot-clé)."""
        print("Pause de l'écoute demandée (audio sending stopped).")
        self._should_send_audio = False
        # Wake word detection continues in the audio loop if _waiting_for_wakeword is True

    def resume_listening(self):
        """Reprend l'envoi d'audio si le mot-clé a déjà été détecté."""
        # This might be less relevant now, state is managed by wake word / speech stop
        if not self._waiting_for_wakeword:
            print("Reprise de l'écoute demandée (audio sending enabled).")
            self._should_send_audio = True
        else:
            print("Impossible de reprendre l'écoute, en attente du mot-clé.")

    def reset_to_wakeword_state(self):
        """Explicitly sets the agent state back to waiting for the wake word."""
        print("Réinitialisation de l'état: en attente du mot-clé.")
        self._waiting_for_wakeword = True
        self._should_send_audio = False
        self.is_user_speaking = False # Ensure this is reset too
        self._pending_music_interrupt_check = False # Cancel any pending music check
        self._interrupt_check_start_time = None
        self._pending_non_music_wakeword_check = False # Cancel any pending non-music check
        self._non_music_check_start_time = None
        # Clear any partial user audio chunks if resetting state
        self.current_user_audio_chunks = []
        self._input_buffer.clear() # Clear Porcupine buffer as well
        # Clear listening indicator on Nextion
        if self.nextion_controller:
            self.nextion_controller.is_listening(False)

    def resume_music_playback(self):
        """Signals the music playback thread to resume by clearing the interrupt event."""
        print("Signaling music playback to resume...")
        music_interrupt_event.clear() # Clear the event to allow playback to continue
        # The playback thread should detect this clear and restart the stream

    async def send_text(self, text: str):
        """
        Envoie du texte à l'API. Handles interruption if music is playing.
        """
        if not self.connection:
            print("Cannot send text, no connection.")
            return

        print(f"Sending text: '{text}'")

        # --- Interrupt Music if Playing ---
        if self.is_playing_music:
            print("--- Interruption de la musique par envoi de texte ---")
            music_interrupt_event.set()
            # Give playback thread a moment to react and stop
            await asyncio.sleep(0.2) # Short delay might be needed
            # The playback thread join in play_music handles full stop
            # Reset flag here might be premature if play_music hasn't finished cleanup
            # self.is_playing_music = False # Let play_music handle this on thread exit

        # Log the user text message
        user_text_message = {
            "role": "user",
            "content": text
        }
        self.messages_history.append(user_text_message)
        self._save_history()

        # Annuler la réponse audio en cours si elle existe (e.g., assistant was speaking)
        try:
            await self.connection.response.cancel()
            print("Cancelled any ongoing response.")
        except Exception as e:
            print(f"Note: Failed to cancel ongoing response (may be none): {e}")

        # Envoyer le texte comme une entrée
        await self.connection.send({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}]
            }
        })

        # Déclencher la création d'une réponse
        print("Requesting response for the sent text.")
        await self.connection.response.create()

        # After sending text, the agent should process the response.
        # The response.done event should handle state reset if needed.
        # We don't immediately go to wake word state here, we wait for the response.

    def _format_history_message_for_injection(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Formate un message de l'historique pour l'injection via conversation.item.create."""
        role = message.get("role")
        content = message.get("content")
        tool_calls = message.get("tool_calls")
        tool_call_id = message.get("tool_call_id")
        name = message.get("name") # For tool role

        item = {"type": "message"} # Default type

        if role == "user":
            item["role"] = "user"
            if isinstance(content, str): # Text input
                 item["content"] = [{"type": "input_text", "text": content}]
            elif isinstance(content, list) and content and content[0].get("type") == "input_audio": # Audio input
                 # History stores 16kHz audio, need to resample before injection
                 audio_base64_16khz = content[0].get("audio")
                 if audio_base64_16khz:
                     try:
                         pcm_chunk_16khz = base64.b64decode(audio_base64_16khz)
                         audio_segment = AudioSegment(
                             data=pcm_chunk_16khz,
                             sample_width=SAMPLE_WIDTH,
                             frame_rate=RECORDER_SAMPLE_RATE, # History audio is at recorder rate
                             channels=CHANNELS
                         )
                         resampled_segment = audio_segment.set_frame_rate(OPENAI_SAMPLE_RATE)
                         pcm_chunk_24khz = resampled_segment.raw_data
                         audio_base64_24khz = base64.b64encode(pcm_chunk_24khz).decode("utf-8")
                         item["content"] = [{"type": "input_audio", "audio": audio_base64_24khz}]
                     except Exception as e:
                         print(f"Erreur de rééchantillonnage de l'historique audio: {e}")
                         return None # Skip this item if resampling fails
                 else:
                     return None # Skip if audio data is missing
        elif role == "assistant":
            if tool_calls and isinstance(tool_calls, list) and tool_calls: # Function call initiated by assistant
                tool_call = tool_calls[0] # Assuming one tool call per message for simplicity
                func_info = tool_call.get("function", {})
                item = {
                    "type": "function_call",
                    "call_id": tool_call.get("id"),
                    "name": func_info.get("name"),
                    "arguments": func_info.get("arguments") # Arguments are already a JSON string
                }
            elif isinstance(content, str): # Regular text response
                item["role"] = "assistant"
                item["content"] = [{"type": "text", "text": content}]
            else:
                return None # Unsupported assistant content format (e.g., cannot inject assistant audio)
        elif role == "tool": # Function call output
             item = {
                 "type": "function_call_output",
                 "call_id": tool_call_id,
                 "output": str(content) # Ensure output is string
             }
        else:
            return None # Ignore system messages or other types for now

        return {
            "type": "conversation.item.create",
            "item": item
        }

    async def _inject_history(self, conn: AsyncRealtimeConnection):
        """Injecte l'historique de la conversation dans la nouvelle session."""
        print(f"Injection de {len(self.messages_history)} éléments de l'historique...")
        for message in self.messages_history:
            formatted_item = self._format_history_message_for_injection(message)
            if formatted_item:
                try:
                    # print(f"Injecting: {json.dumps(formatted_item)}") # Debug print
                    await conn.send(formatted_item)
                    await asyncio.sleep(0.01) # Small delay between items
                except Exception as e:
                    print(f"Erreur lors de l'injection de l'élément d'historique: {e}")
                    # Decide whether to stop injection or continue
                    # break # Option: Stop injection on first error
            else:
                print(f"Skipping unsupported history item for injection: {message}")
        print("Injection de l'historique terminée.")

    def set_is_playing_music(self, is_playing: bool):
        """Sets the internal flag indicating music playback status."""
        # This method allows functions_utils to update the agent's state via callback
        self.is_playing_music = is_playing
        print(f"[Agent Callback] is_playing_music set to: {is_playing}")

class AudioPlayerAsync:
    """Lecteur audio asynchrone pour lire les réponses audio."""
    
    def __init__(self):
        self.queue = []
        self.lock = threading.Lock()
        # Ensure the OutputStream uses the correct OPENAI_SAMPLE_RATE (24kHz)
        try:
            self.stream = sd.OutputStream(
                callback=self.callback,
                samplerate=OPENAI_SAMPLE_RATE, # Use 24kHz for playback
                channels=CHANNELS,
                dtype=np.int16,
                blocksize=int(0.05 * OPENAI_SAMPLE_RATE), # Keep 50ms blocksize relative to 24kHz
            )
        except Exception as e:
            print(f"Erreur d'initialisation de sounddevice OutputStream: {e}")
            # Fallback or re-raise depending on desired behavior
            raise
        self.playing = False
        self._frame_count = 0

    def callback(self, outdata, frames, time, status):
        with self.lock:
            data = np.empty(0, dtype=np.int16)

            # Récupérer le prochain élément de la file d'attente
            while len(data) < frames and len(self.queue) > 0:
                item = self.queue.pop(0)
                frames_needed = frames - len(data)
                data = np.concatenate((data, item[:frames_needed]))
                if len(item) > frames_needed:
                    self.queue.insert(0, item[frames_needed:])

            self._frame_count += len(data)

            # Remplir le reste des trames avec des zéros s'il n'y a plus de données
            if len(data) < frames:
                data = np.concatenate((data, np.zeros(frames - len(data), dtype=np.int16)))

        outdata[:] = data.reshape(-1, 1)

    def reset_frame_count(self):
        """Resets the frame count and clears any pending audio data."""
        self._frame_count = 0
        with self.lock:
            self.queue = []
            print("[AudioPlayer] Queue cleared for new response.") # Optional: for debugging

    def get_frame_count(self):
        return self._frame_count
        
    def get_queue_size(self):
        """Retourne la taille actuelle de la file d'attente audio."""
        with self.lock:
            return len(self.queue)

    def add_data(self, data: bytes):
        with self.lock:
            # Les bytes sont des données audio pcm16 mono, conversion en tableau numpy
            np_data = np.frombuffer(data, dtype=np.int16)
            self.queue.append(np_data)
            if not self.playing:
                self.start()

    def start(self):
        self.playing = True
        self.stream.start()

    def stop(self):
        """Stops the audio stream and clears the queue."""
        self.playing = False
        # Clear queue before stopping the stream to avoid callback issues
        with self.lock:
            self.queue = []
        self.stream.stop()

    def terminate(self):
        if self.playing:
            self.stop()
        self.stream.close()

def audio_to_pcm16_base64(audio_bytes: bytes) -> bytes:
    """
    Convertit les données audio en PCM 16 bits, 24kHz, mono, base64 encodé.
    Utile pour envoyer des fichiers audio préenregistrés.
    """
    try:
        audio = AudioSegment.from_file(io.BytesIO(audio_bytes))
        # Rééchantillonner à 24kHz mono pcm16
        pcm_audio = audio.set_frame_rate(OPENAI_SAMPLE_RATE).set_channels(CHANNELS).set_sample_width(SAMPLE_WIDTH)
        return base64.b64encode(pcm_audio.raw_data)
    except Exception as e:
        print(f"Erreur lors de la conversion audio en base64 PCM: {e}")
        return b"" # Return empty bytes on error