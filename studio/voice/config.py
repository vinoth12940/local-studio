"""Settings of the voice service process."""
import os

PORT = int(os.environ.get("LOCAL_STUDIO_VOICE_PORT", 8084))
TOKEN_ENV = "LOCAL_STUDIO_VOICE_TOKEN"          # set by studio/services/voice.py for every launch

STT_MODEL = "mlx-community/parakeet-tdt-0.6b-v3"   # Parakeet TDT 0.6B v3 (MLX), 16 kHz
TTS_LANG = "en"                                 # Phonon 31M (gradium/phonon-31m-en, Apache-2.0) is English only

MIC_RATE = 16000                                # what speech recognition and voice detection get
OUT_RATE = 24000                                # what the speaker plays (Phonon's rate)
CHUNK = 512                                     # 32 ms microphone chunks on /events

MAX_TEXT = 2000                                 # characters per /tts request (one or a few sentences)
MAX_STT_BYTES = 16_000_000                      # a WAV upload (about 8 minutes of 16 kHz mono)
MAX_PLAY_BYTES = 24_000_000                     # float32 PCM per /play (about 4 minutes at 24 kHz)
EVENT_QUEUE = 400                               # events buffered per /events stream before audio is dropped
