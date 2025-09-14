import pveagle
from pvrecorder import PvRecorder
import os

recorder = PvRecorder(device_index=-1, frame_length=6144)

access_key = os.environ.get("PV_API_KEY")
eagle_profiler = pveagle.create_profiler(access_key)
print(eagle_profiler.min_enroll_samples)
percentage = 0.0
recorder.start()
print("Recording... Press Ctrl+C to stop.")
while percentage < 100.0:
    percentage, feedback = eagle_profiler.enroll(recorder.read())
    print(feedback.name)
speaker_profile = eagle_profiler.export()
with open("speaker_profile.bin", "wb") as f:
    f.write(speaker_profile.to_bytes())