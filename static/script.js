(function () {
  "use strict";

  const SESSION_KEY = "sara_session_id";

  function getSessionId() {
    try {
      let id = localStorage.getItem(SESSION_KEY);
      if (!id) {
        id = crypto.randomUUID();
        localStorage.setItem(SESSION_KEY, id);
      }
      return id;
    } catch (e) {
      // localStorage may be unavailable (private browsing, etc). Fall back
      // to a per-page-load id so the app still works.
      return crypto.randomUUID();
    }
  }

  const sessionId = getSessionId();

  const chatEl = document.getElementById("chat");
  const formEl = document.getElementById("chat-form");
  const inputEl = document.getElementById("chat-input");
  const sendButton = document.getElementById("send-button");
  const typingEl = document.getElementById("typing-indicator");
  const errorEl = document.getElementById("error-banner");

  function addBubble(text, sender) {
    const bubble = document.createElement("div");
    bubble.className = "bubble " + sender;
    bubble.textContent = text;
    chatEl.appendChild(bubble);
    chatEl.scrollTop = chatEl.scrollHeight;
  }

  function setLoading(isLoading) {
    sendButton.disabled = isLoading;
    inputEl.disabled = isLoading;
    typingEl.classList.toggle("hidden", !isLoading);
  }

  function showError(message) {
    errorEl.textContent = message;
    errorEl.classList.remove("hidden");
  }

  function clearError() {
    errorEl.classList.add("hidden");
    errorEl.textContent = "";
  }

  async function sendMessage(message) {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, message: message }),
    });

    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      throw new Error(detail.detail || "صار شي غلط، جرب مرة ثانية 🙏");
    }

    const data = await response.json();
    return data.reply;
  }

  formEl.addEventListener("submit", async function (event) {
    event.preventDefault();
    const message = inputEl.value.trim();
    if (!message) return;

    clearError();
    addBubble(message, "friend");
    inputEl.value = "";
    setLoading(true);

    try {
      const reply = await sendMessage(message);
      addBubble(reply, "sara");
    } catch (err) {
      showError(err.message || "ما قدرت أرد عليك الحين، جرب مرة ثانية 🙏");
    } finally {
      setLoading(false);
      inputEl.focus();
    }
  });

  addBubble(
    "هلا والله! أنا سارة، السكرتيرة الشخصية الذكية. وش اسمك؟ 😊",
    "sara"
  );
})();
