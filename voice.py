"""Entrypoint for `python voice.py --port 8084`: the voice service (see studio/voice/server.py)."""

if __name__ == '__main__':
    from studio.voice.server import main
    main()
