import requests
import json
import html
import time
import ast
import os
import concurrent.futures
import logging

# Setup basic logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class SongManager:
    """
    Encapsule les opérations de recherche et de téléchargement de chansons.
    """
    def __init__(self, base_url="http://127.0.0.1:5000", base_folder="/home/octave/Music"):
        self.base_url = base_url
        if os.name == "nt":
            # base folder is the location of the main script
            self.base_folder = os.path.dirname(os.path.abspath(__file__).replace("\\utils", "\\music"))
            os.makedirs(self.base_folder, exist_ok=True)
        else:
            self.base_folder = base_folder
            os.makedirs(self.base_folder, exist_ok=True)
        logging.info(f"SongManager initialized. Music folder: {self.base_folder}")

    def _search_track_request(self, url, query):
        """
        Méthode auxiliaire qui effectue une requête POST pour rechercher une chanson.
        """
        try:
            response = requests.post(url, json={"type": "track", "query": query}, timeout=10)
            response.raise_for_status()
            data = response.json()
            if data and isinstance(data, list) and len(data) > 0:
                logging.info(f"Search success for '{query}': Found '{data[0].get('title')}' by '{data[0].get('artist')}' (ID: {data[0].get('id')})")
                return data[0]
            else:
                logging.warning(f"Search for '{query}' returned no results or invalid data: {data}")
                return None
        except requests.exceptions.RequestException as e:
            logging.error(f"Error in _search_track_request for '{query}': {e}")
            return None
        except Exception as e:
            logging.error(f"Unexpected error in _search_track_request for '{query}': {e}")
            return None

    def search_track(self, query):
        """
        Rechercher une chanson à partir d'une chaîne de requête.
        Trois requêtes simultanées sont lancées et la première réponse valide est retournée.
        """
        url = f"{self.base_url}/search"
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(self._search_track_request, url, query) for _ in range(3)]
            result = None
            for future in concurrent.futures.as_completed(futures):
                try:
                    song = future.result()
                    if song is not None:
                        result = song
                        for f in futures:
                            if not f.done():
                                f.cancel()
                        break
                except concurrent.futures.CancelledError:
                    logging.debug("Search future cancelled.")
                except Exception as e:
                    logging.error(f"Error during concurrent search execution: {e}")
            if result is not None:
                return result
            else:
                logging.error(f"Error: All search_track requests failed for query '{query}'.")
                return None

    def start_download(self, song_id: int):
        """
        Démarrer le téléchargement d'une chanson à partir de son ID.
        """
        url = f"{self.base_url}/download"
        payload = {
            "type": "track",
            "music_id": song_id,
            "add_to_playlist": False,
            "create_zip": False
        }
        try:
            response = requests.post(url, json=payload, timeout=15)
            response.raise_for_status()
            response_data = response.json()
            task_id = response_data.get("task_id")
            if task_id:
                logging.info(f"Download started successfully for song ID {song_id}. Task ID: {task_id}")
                return task_id
            else:
                logging.error(f"Failed to start download for song ID {song_id}. Response: {response.text}")
                return None
        except requests.exceptions.RequestException as e:
            logging.error(f"Error starting download for song ID {song_id}: {e}")
            return None
        except Exception as e:
            logging.error(f"Unexpected error starting download for song ID {song_id}: {e}")
            return None

    def _get_queue_request(self, url):
        """
        Méthode auxiliaire pour effectuer une requête GET sur la file d'attente.
        """
        try:
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logging.error(f"Error in _get_queue_request: {e}")
            return None
        except Exception as e:
            logging.error(f"Unexpected error in _get_queue_request: {e}")
            return None

    def get_queue(self):
        """
        Récupérer la file d'attente actuelle en lançant trois requêtes simultanées,
        et retourner la première réponse valide.
        """
        url = f"{self.base_url}/queue"
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(self._get_queue_request, url) for _ in range(3)]
            result = None
            for future in concurrent.futures.as_completed(futures):
                try:
                    queue_data = future.result()
                    if queue_data is not None:
                        result = queue_data
                        for f in futures:
                            if not f.done():
                                f.cancel()
                        break
                except concurrent.futures.CancelledError:
                    logging.debug("Get queue future cancelled.")
                except Exception as e:
                    logging.error(f"Error during concurrent get_queue execution: {e}")
            if result is not None:
                return result
            else:
                logging.error("Error: All get_queue requests failed.")
                return []

    def download_track(self, search, timeout=300): # Added timeout parameter with default
        """
        Recherche une piste et lance son téléchargement, en surveillant son état jusqu'à complétion.
        Pour démarrer le téléchargement, trois requêtes simultanées sont lancées et la première réponse valide est utilisée.
        Returns a dictionary with status, audio_data, file path, and metadata on success.
        """
        logging.info(f"Starting download process for search: '{search}' with timeout {timeout}s") # Log timeout
        song = self.search_track(search)
        if not song:
            logging.error(f"Search failed for '{search}'. Cannot proceed with download.")
            return {"status": "error", "message": f"Could not find track for '{search}'."}

        song_id = int(song["id"])
        logging.info(f"Track found: '{song.get('title')}' by '{song.get('artist')}' (ID: {song_id}). Attempting to start download.")

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(self.start_download, song_id) for _ in range(3)]
            task_id = None
            for future in concurrent.futures.as_completed(futures):
                try:
                    result = future.result()
                    if result:
                        task_id = result
                        for f in futures:
                            if not f.done():
                                f.cancel()
                        break
                except concurrent.futures.CancelledError:
                    logging.debug("Start download future cancelled.")
                except Exception as e:
                    logging.error(f"Error during concurrent start_download execution: {e}")

        if not task_id:
            logging.error(f"Failed to initiate download for song ID {song_id} after multiple attempts.")
            return {"status": "error", "message": "Failed to start download task."}

        logging.info(f"Download task {task_id} started. Monitoring queue...")

        done = False
        # Use the provided timeout parameter
        max_wait_time = timeout
        start_wait_time = time.time()
        file_path = None

        while not done and (time.time() - start_wait_time) < max_wait_time:
            current_queue = self.get_queue()
            if current_queue is None:
                logging.warning("Failed to get queue status, retrying...")
                time.sleep(2)
                continue
            if not isinstance(current_queue, list):
                logging.error(f"Received invalid queue data: {current_queue}")
                time.sleep(2)
                continue

            task_found = False
            for item in current_queue:
                if item.get("id") == task_id:
                    task_found = True
                    state = item.get("state")
                    logging.debug(f"Task {task_id} state: {state}")
                    if state == "mission accomplished":
                        logging.info(f"Task {task_id} completed successfully.")
                        result_str = item.get("result")
                        if result_str:
                            try:
                                decoded_str = html.unescape(result_str)
                                result_list = ast.literal_eval(decoded_str)
                                if isinstance(result_list, list) and len(result_list) > 0:
                                    relative_path = result_list[0]
                                    file_path = os.path.join(self.base_folder, relative_path)
                                    logging.info(f"Download result points to file: {file_path}")
                                    done = True
                                else:
                                    logging.error(f"Task {task_id} result format unexpected: {result_list}")
                                    return {"status": "error", "message": "Download completed but result format is invalid."}
                            except (SyntaxError, ValueError, TypeError) as e:
                                logging.error(f"Error parsing task {task_id} result string '{result_str}': {e}")
                                return {"status": "error", "message": "Download completed but failed to parse result."}
                        else:
                            logging.error(f"Task {task_id} completed but no result string found.")
                            return {"status": "error", "message": "Download completed but result missing."}
                        break
                    elif state in ["error", "cancelled"]:
                        logging.error(f"Task {task_id} failed with state: {state}. Result: {item.get('result')}")
                        return {"status": "error", "message": f"Download task failed with state: {state}"}
                    break

            if not task_found and current_queue != []:
                # Check elapsed time more explicitly within the loop if needed,
                # but the main while condition handles the overall timeout.
                if (time.time() - start_wait_time) >= max_wait_time:
                    logging.error(f"Download task {task_id} timed out after {max_wait_time} seconds (check within loop).")
                    return {"status": "timeout", "message": f"Download task timed out after {max_wait_time} seconds."} # Specific status for timeout
                logging.warning(f"Task {task_id} not found in the current queue. Waiting...")

            if done:
                break

            time.sleep(1) # Keep the sleep

        if not done:
            # Check if timeout was the reason for loop exit
            if (time.time() - start_wait_time) >= max_wait_time:
                 logging.error(f"Download task {task_id} timed out after {max_wait_time} seconds (check after loop).")
                 return {"status": "timeout", "message": f"Download task timed out after {max_wait_time} seconds."} # Specific status for timeout
            else:
                 # If loop exited for another reason without done=True (shouldn't happen ideally)
                 logging.error(f"Download task {task_id} monitoring loop exited unexpectedly.")
                 return {"status": "error", "message": "Download task monitoring failed unexpectedly."}

        if file_path and os.path.exists(file_path):
            try:
                with open(file_path, "rb") as f:
                    audio_data = f.read()
                logging.info(f"Successfully read audio data from {file_path}")
                return {
                    "status": "success",
                    "audio_data": audio_data,
                    "file": file_path,
                    "title": song.get("title"),
                    "artist": song.get("artist"),
                    "album": song.get("album")
                }
            except IOError as e:
                logging.error(f"Error reading downloaded file '{file_path}': {e}")
                return {"status": "error", "message": f"Failed to read downloaded file: {e}"}
        else:
            logging.error(f"Downloaded file path '{file_path}' not found or invalid after task completion.")
            return {"status": "error", "message": "Downloaded file not found."}

if __name__ == "__main__":
    manager = SongManager()
    result = manager.download_track("The Weeknd Blinding Lights")
    if result and result.get("status") == "success":
        print(f"Téléchargement réussi pour '{result.get('title')}'.")
        print(f"Fichier: {result.get('file')}")
        print(f"Taille des données audio: {len(result.get('audio_data', b''))} bytes")
    else:
        print(f"Échec du téléchargement. Raison: {result.get('message', 'Inconnue')}")