import pveagle
from pvrecorder import PvRecorder
import os

recorder = PvRecorder(device_index=-1, frame_length=512)

access_key = os.environ.get("PV_API_KEY")
with open("speaker_profile.bin", "rb") as f:
    speaker_profile = f.read()
eagle_profiler = pveagle.create_recognizer(access_key, speaker_profiles=[pveagle.EagleProfile.from_bytes(speaker_profile)])
percentage = 0.0
recorder.start()
print("Recording... Press Ctrl+C to stop.")
while percentage < 100.0:
    score = eagle_profiler.process(recorder.read())
    print(f"Recognition score: {score}")