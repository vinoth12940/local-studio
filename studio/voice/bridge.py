"""Microphone and speaker through ONE AVAudioEngine with Apple's voice processing (echo cancellation).

The assistant's speech is played by the same engine that records the microphone, so macOS subtracts it from what the
microphone hears (measured on built-in speakers: about 34 dB, below the room noise). Wiring after macvoice's
audio_mac.py (Apache-2.0). AVFoundation (PyObjC) is imported only when the microphone is opened.
"""
import threading
import time

import numpy as np

from studio.voice import config
from studio.voice.audio import Chunker, mic_event, resample


class VoiceIO:
    def __init__(self, fanout):
        import AVFoundation as AV
        self.AV, self.fanout = AV, fanout
        self.engine = AV.AVAudioEngine.alloc().init()
        self.engine.outputNode()                     # must exist before voice processing is switched on
        mixer = self.engine.mainMixerNode()
        self.input = self.engine.inputNode()
        ok, err = self.input.setVoiceProcessingEnabled_error_(True, None)
        if not ok:
            raise RuntimeError(f"voice processing could not be enabled: {err}")
        if hasattr(AV, "AVAudioVoiceProcessingOtherAudioDuckingConfiguration"):   # leave other apps' volume alone
            self.input.setVoiceProcessingOtherAudioDuckingConfiguration_(AV.AVAudioVoiceProcessingOtherAudioDuckingConfiguration(
                enableAdvancedDucking=False, duckingLevel=AV.AVAudioVoiceProcessingOtherAudioDuckingLevelMin))
        self.out_fmt = AV.AVAudioFormat.alloc().initWithCommonFormat_sampleRate_channels_interleaved_(
            AV.AVAudioPCMFormatFloat32, float(config.OUT_RATE), 1, False)
        self.player = AV.AVAudioPlayerNode.alloc().init()
        self.engine.attachNode_(self.player)
        self.engine.connect_to_format_(self.player, mixer, self.out_fmt)
        self.lock, self.generation = threading.Lock(), 0
        self.last_tap, self.taps = time.monotonic(), 0   # the service restarts an engine whose tap goes quiet (silent stall)
        fmt = self.input.outputFormatForBus_(0)      # hardware rate; often several channels, the first is used
        self.in_rate = int(fmt.sampleRate())
        chunker = Chunker(config.CHUNK)

        def tap(buf, when):                          # audio thread: keep it short
            n = buf.frameLength()
            self.last_tap = time.monotonic(); self.taps += 1
            if n:
                mic = np.frombuffer(buf.floatChannelData()[0].as_buffer(n), dtype=np.float32).copy()
                for c in chunker(resample(mic, self.in_rate, config.MIC_RATE)):
                    self.fanout.publish(mic_event(c))

        self.input.installTapOnBus_bufferSize_format_block_(0, 1600, fmt, tap)
        self.engine.prepare()
        ok, err = self.engine.startAndReturnError_(None)
        if not ok:
            self.input.removeTapOnBus_(0)
            raise RuntimeError(f"audio engine did not start: {err}")
        self.player.play()
        # Switching voice processing on reconfigures the audio hardware, and AVAudioEngine stops itself on a configuration
        # change, often right after start (measured: start OK, not running 0.2 s later, no audio). Start it again until
        # the microphone delivers audio.
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and self.taps < 3:
            time.sleep(0.1)
            self.kick()
        if self.taps < 3:
            self.close()
            raise RuntimeError("the microphone did not deliver audio (is another app using it?)")

    def running(self):
        return bool(self.engine.isRunning())

    def kick(self):
        """Start the engine again if it stopped (see above); True when it had stopped and was restarted."""
        if self.engine.isRunning():
            return False
        self.engine.prepare()
        ok, _ = self.engine.startAndReturnError_(None)
        if ok:
            self.player.play()
        return bool(ok)

    def play(self, pcm, rate, item_id):
        pcm = resample(pcm, rate, config.OUT_RATE)
        if not len(pcm):
            self.fanout.publish({"t": "played", "id": item_id, "stopped": False})
            return
        buf = self.AV.AVAudioPCMBuffer.alloc().initWithPCMFormat_frameCapacity_(self.out_fmt, len(pcm))
        np.frombuffer(buf.floatChannelData()[0].as_buffer(len(pcm)), dtype=np.float32)[:] = pcm
        buf.setFrameLength_(len(pcm))
        with self.lock:
            gen = self.generation

        def done():
            with self.lock:
                stale = gen != self.generation
            self.fanout.publish({"t": "played", "id": item_id, "stopped": stale})

        self.player.scheduleBuffer_completionHandler_(buf, done)

    def stop(self):
        with self.lock:
            self.generation += 1
        self.player.stop(); self.player.play()

    def pause(self):
        self.player.pause()

    def resume(self):
        self.player.play()

    def close(self):
        try:
            self.input.removeTapOnBus_(0)
        finally:
            self.player.stop(); self.engine.stop()
