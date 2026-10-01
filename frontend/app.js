const chat = document.getElementById("chat");
const quick = document.getElementById("quick");
const input = document.getElementById("input");
const sendBtn = document.getElementById("send");
const micBtn = document.getElementById("mic");
const resetBtn = document.getElementById("reset");
const statusEl = document.getElementById("status");
const toast = document.getElementById("toast");

let sessionId = localStorage.getItem("stl_session") || null;
let busy = false;

const LABELS = [
  ["story", "Story"],
  ["materials", "Materials"],
  ["care", "Care"],
  ["production", "Production time"],
  ["variations", "Natural variations"],
  ["cultural_note", "Cultural note"],
  ["photo_note", "The exact piece"],
  ["buyer_faq", "Buyer FAQ"],
];

function esc(value) {
  return String(value || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function now() {
  return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function addBubble(text, out) {
  const wrap = document.createElement("div");
  wrap.className = "msg" + (out ? " out" : "");
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  const body = document.createElement("div");
  body.className = "text";
  body.innerHTML = esc(text).replace(/\*(.+?)\*/g, "<strong>$1</strong>");
  const meta = document.createElement("div");
  meta.className = "meta";
  meta.textContent = now();
  bubble.appendChild(body);
  bubble.appendChild(meta);
  wrap.appendChild(bubble);
  chat.appendChild(wrap);
  chat.scrollTop = chat.scrollHeight;
  return wrap;
}

function addTyping() {
  const wrap = document.createElement("div");
  wrap.className = "msg typing";
  wrap.innerHTML = '<div class="bubble"><div class="text">typing...</div></div>';
  chat.appendChild(wrap);
  chat.scrollTop = chat.scrollHeight;
  return wrap;
}

function addListingCard(listing) {
  const card = document.createElement("div");
  card.className = "card";
  const safe = listing.safe || {};
  let html = `<h3>${esc(safe.title || "Your listing")}</h3><div class="body">`;
  for (const [field, label] of LABELS) {
    if (safe[field]) {
      html += `<div class="row"><div class="label">${label}</div><div class="value">${esc(safe[field])}</div></div>`;
    }
  }
  html += "</div>";
  if (listing.removed && listing.removed.length) {
    html += '<div class="flagged"><h4>Blocked by the source-truth guard</h4>';
    for (const claim of listing.removed) {
      html += `<div class="claim"><span class="tag ${esc(claim.status)}">${esc(claim.status)}</span><span>${esc(claim.claim)}</span></div>`;
    }
    html += "</div>";
  }
  card.innerHTML = html;
  if (listing.published && listing.buyer_path) {
    const link = document.createElement("a");
    link.className = "buyer-link";
    link.href = listing.buyer_path;
    link.target = "_blank";
    link.rel = "noopener";
    link.textContent = "Open buyer page";
    card.appendChild(link);
  }
  chat.appendChild(card);
  chat.scrollTop = chat.scrollHeight;
}

function setQuick(replies) {
  quick.innerHTML = "";
  (replies || []).forEach((text) => {
    const chip = document.createElement("button");
    chip.className = "chip";
    chip.textContent = text;
    chip.onclick = () => send(text);
    quick.appendChild(chip);
  });
}

function showToast(message) {
  toast.textContent = message;
  toast.classList.add("show");
  setTimeout(() => toast.classList.remove("show"), 2200);
}

function handleResponse(resp) {
  if (resp.session_id) {
    sessionId = resp.session_id;
    localStorage.setItem("stl_session", sessionId);
  }
  if (resp.buyer_url && resp.listing && resp.listing.published) {
    resp.listing.buyer_path = resp.buyer_url;
  }
  const messages = resp.messages || [];
  messages.forEach((text) => addBubble(text, false));
  if (resp.listing) addListingCard(resp.listing);
  setQuick(resp.quick_replies);
  if (resp.mock_llm) statusEl.textContent = "maker assistant · offline mock";
}

async function send(text) {
  if (busy || !text) return;
  busy = true;
  setQuick([]);
  addBubble(text, true);
  const typing = addTyping();
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, message: text }),
    });
    const data = await res.json();
    typing.remove();
    handleResponse(data);
  } catch (err) {
    typing.remove();
    addBubble("Connection error. Is the server running?", false);
  } finally {
    busy = false;
  }
}

sendBtn.onclick = () => {
  const value = input.value.trim();
  input.value = "";
  send(value);
};

input.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    const value = input.value.trim();
    input.value = "";
    send(value);
  }
});

resetBtn.onclick = async () => {
  await fetch("/api/reset", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId }),
  });
  localStorage.removeItem("stl_session");
  sessionId = null;
  chat.innerHTML = "";
  setQuick([]);
  send("hi");
};

let recorder = null;
let chunks = [];

micBtn.onclick = async () => {
  if (recorder && recorder.state === "recording") {
    recorder.stop();
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    recorder = new MediaRecorder(stream);
    chunks = [];
    recorder.ondataavailable = (event) => chunks.push(event.data);
    recorder.onstop = async () => {
      micBtn.classList.remove("recording");
      stream.getTracks().forEach((track) => track.stop());
      const blob = new Blob(chunks, { type: "audio/webm" });
      await sendVoice(blob);
    };
    recorder.start();
    micBtn.classList.add("recording");
    showToast("Recording... tap again to send");
  } catch (err) {
    showToast("Microphone not available");
  }
};

async function sendVoice(blob) {
  busy = true;
  addBubble("[voice note]", true);
  const typing = addTyping();
  const form = new FormData();
  form.append("session_id", sessionId || "");
  form.append("audio", blob, "voice.webm");
  try {
    const res = await fetch("/api/voice", { method: "POST", body: form });
    const data = await res.json();
    typing.remove();
    if (data.transcript) addBubble("Transcript: " + data.transcript, false);
    handleResponse(data);
  } catch (err) {
    typing.remove();
    addBubble("Could not send voice note.", false);
  } finally {
    busy = false;
  }
}

window.addEventListener("DOMContentLoaded", () => {
  send("hi");
});
