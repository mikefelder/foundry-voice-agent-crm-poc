const SAMPLE_RATE = 24000;

const gate = document.getElementById("gate");
const gateForm = document.getElementById("gate-form");
const gateError = document.getElementById("gate-error");
const app = document.getElementById("app");
const statusLine = document.getElementById("status");
const transcript = document.getElementById("transcript");
const composer = document.getElementById("composer");
const messageBox = document.getElementById("message");
const micButton = document.getElementById("mic");
const micLabel = document.getElementById("mic-label");

let socket = null;
let audioContext = null;
let micStream = null;
let micNode = null;
let listening = false;

// Playback is scheduled ahead on a moving cursor; barge-in resets both.
let playCursor = 0;
let scheduled = [];

/** Captures 24 kHz mono and posts Int16 frames to the main thread. */
const CAPTURE_WORKLET = `
class Capture extends AudioWorkletProcessor {
  process(inputs) {
    const input = inputs[0][0];
    if (!input) return true;
    const pcm = new Int16Array(input.length);
    for (let i = 0; i < input.length; i++) {
      const s = Math.max(-1, Math.min(1, input[i]));
      pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    this.port.postMessage(pcm.buffer, [pcm.buffer]);
    return true;
  }
}
registerProcessor('capture', Capture);
`;

function setStatus(text, kind = "") {
  statusLine.textContent = text;
  statusLine.className = `status ${kind}`.trim();
}

function addBubble(role, text) {
  if (!text) return;
  const node = document.createElement("div");
  node.className = `bubble ${role}`;
  node.textContent = text;
  transcript.append(node);
  transcript.scrollTop = transcript.scrollHeight;
}

function addLink(label, url) {
  let safeUrl;
  try {
    const parsed = new URL(url, location.href);
    if (parsed.protocol !== "https:" && parsed.protocol !== "http:") return;
    safeUrl = parsed.href;
  } catch {
    return;
  }
  const node = document.createElement("a");
  node.className = "bubble link";
  node.href = safeUrl;
  node.target = "_blank";
  node.rel = "noopener noreferrer";
  node.textContent = `${label} — open record`;
  transcript.append(node);
  transcript.scrollTop = transcript.scrollHeight;
}

// ---- playback -------------------------------------------------------------

function playChunk(buffer) {
  if (!audioContext) return;
  const pcm = new Int16Array(buffer);
  if (!pcm.length) return;

  const frame = audioContext.createBuffer(1, pcm.length, SAMPLE_RATE);
  const channel = frame.getChannelData(0);
  for (let i = 0; i < pcm.length; i++) channel[i] = pcm[i] / 0x8000;

  const source = audioContext.createBufferSource();
  source.buffer = frame;
  source.connect(audioContext.destination);

  const now = audioContext.currentTime;
  if (playCursor < now) playCursor = now;
  source.start(playCursor);
  playCursor += frame.duration;

  scheduled.push(source);
  source.onended = () => {
    scheduled = scheduled.filter((s) => s !== source);
  };
}

function stopPlayback() {
  for (const source of scheduled) {
    try {
      source.stop();
    } catch {
      /* already finished */
    }
  }
  scheduled = [];
  playCursor = 0;
}

// ---- microphone -----------------------------------------------------------

async function startListening() {
  micStream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
  });

  const moduleUrl = URL.createObjectURL(
    new Blob([CAPTURE_WORKLET], { type: "application/javascript" }),
  );
  await audioContext.audioWorklet.addModule(moduleUrl);
  URL.revokeObjectURL(moduleUrl);

  micNode = new AudioWorkletNode(audioContext, "capture");
  micNode.port.onmessage = ({ data }) => {
    if (socket?.readyState === WebSocket.OPEN) socket.send(data);
  };
  audioContext.createMediaStreamSource(micStream).connect(micNode);
  // Keep the node pulling without routing microphone audio back to the speakers.
  micNode.connect(audioContext.createGain()).connect(audioContext.destination);
}

function stopListening() {
  micNode?.port && (micNode.port.onmessage = null);
  micNode?.disconnect();
  micNode = null;
  micStream?.getTracks().forEach((track) => track.stop());
  micStream = null;
}

async function toggleMic() {
  if (listening) {
    stopListening();
    listening = false;
    micButton.setAttribute("aria-pressed", "false");
    micLabel.textContent = "Talk";
    setStatus("connected", "live");
    return;
  }

  try {
    await audioContext.resume();
    await startListening();
    listening = true;
    micButton.setAttribute("aria-pressed", "true");
    micLabel.textContent = "Listening";
    setStatus("listening — just talk", "live");
  } catch (error) {
    addBubble("error", `Microphone unavailable: ${error.message}`);
  }
}

// ---- socket ---------------------------------------------------------------

function connect(key) {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${scheme}://${location.host}/ws/voice`);
  socket.binaryType = "arraybuffer";

  socket.onopen = () => socket.send(JSON.stringify({ key }));

  socket.onmessage = ({ data }) => {
    if (data instanceof ArrayBuffer) {
      playChunk(data);
      return;
    }
    const event = JSON.parse(data);
    if (event.type === "ready") {
      setStatus("connected", "live");
      addBubble("system", "Connected. Type a message or press Talk.");
    } else if (event.type === "transcript") {
      addBubble(event.role === "user" ? "user" : "agent", event.text);
    } else if (event.type === "link") {
      addLink(event.label, event.url);
    } else if (event.type === "speech_started") {
      stopPlayback();
    } else if (event.type === "error") {
      addBubble("error", event.text);
    }
  };

  socket.onclose = (event) => {
    if (event.code === 4401) {
      gate.hidden = false;
      app.hidden = true;
      gateError.hidden = false;
      gateError.textContent = "That key was rejected.";
      return;
    }
    setStatus("disconnected", "error");
    if (listening) toggleMic();
  };

  socket.onerror = () => setStatus("connection error", "error");
}

// ---- wiring ---------------------------------------------------------------

gateForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const key = document.getElementById("key").value.trim();
  if (!key) return;

  audioContext = new AudioContext({ sampleRate: SAMPLE_RATE });
  gate.hidden = true;
  app.hidden = false;
  setStatus("connecting…");
  connect(key);
});

composer.addEventListener("submit", (event) => {
  event.preventDefault();
  const text = messageBox.value.trim();
  if (!text || socket?.readyState !== WebSocket.OPEN) return;
  addBubble("user", text);
  stopPlayback();
  socket.send(JSON.stringify({ type: "text", text }));
  messageBox.value = "";
});

micButton.addEventListener("click", toggleMic);
