"""The voice service (voice.py, its own process like search.py): echo-cancelled microphone and speaker, Parakeet speech
recognition and Phonon speech. The UI backend never imports this package (its dependencies are installed only when the
user turns Voice on); it starts voice.py and talks to it over loopback HTTP with a per-launch token.
"""
