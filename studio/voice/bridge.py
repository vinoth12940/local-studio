"""Microphone and speaker through ONE AVAudioEngine with Apple's voice processing (echo cancellation).

The assistant's speech is played by the same engine that records the microphone, so macOS subtracts it from what the
microphone hears (measured on built-in speakers: about 34 dB, below the room noise). Wiring after macvoice's
audio_mac.py (Apache-2.0). AVFoundation (PyObjC) is imported only when the microphone is opened.
"""
import threading

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
        fmt = self.input.outputFormatForBus_(0)      # hardware rate; often several channels, the first is used
        self.in_rate = int(fmt.sampleRate())
        chunker = Chunker(config.CHUNK)

        def tap(buf, when):                          # audio thread: keep it short
            n = buf.frameLength()
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
