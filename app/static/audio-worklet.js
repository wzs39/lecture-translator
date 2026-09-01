class LectureAudioProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.samples = [];
    this.port.onmessage = event => {
      if (event.data === "flush") this.flush();
    };
  }

  process(inputs) {
    const channel = inputs[0]?.[0];
    if (!channel) return true;
    const copy = new Float32Array(channel);
    let sum = 0;
    for (const sample of copy) sum += sample * sample;
    this.port.postMessage({type: "level", value: Math.sqrt(sum / copy.length)});
    this.samples.push(copy);
    if (this.samples.length >= 16) this.flush();
    return true;
  }

  flush() {
    if (!this.samples.length) return;
    const length = this.samples.reduce((total, chunk) => total + chunk.length, 0);
    const output = new Float32Array(length);
    let offset = 0;
    for (const chunk of this.samples) {
      output.set(chunk, offset);
      offset += chunk.length;
    }
    this.samples = [];
    this.port.postMessage({type: "audio", samples: output}, [output.buffer]);
  }
}
registerProcessor("lecture-audio", LectureAudioProcessor);
