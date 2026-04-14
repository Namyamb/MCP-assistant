document.addEventListener("DOMContentLoaded", () => {

    // ── DOM refs ─────────────────────────────────────────────────────────────
    const chatForm         = document.getElementById("chatForm");
    const chatInput        = document.getElementById("chatInput");
    const chatHistory      = document.getElementById("chatHistory");
    const inboxList        = document.getElementById("inboxList");
    const inboxSection     = document.querySelector(".inbox-section");
    const refreshInbox     = document.getElementById("refreshInbox");
    const statusDot        = document.getElementById("statusDot");
    const statusText       = document.getElementById("statusText");
    const thinkingAnim     = document.getElementById("thinkingAnim");
    const modeBadge        = document.getElementById("modeBadge");
    const mcpDropdownBtn   = document.getElementById("mcpDropdownBtn");
    const mcpDropdownMenu  = document.getElementById("mcpDropdownMenu");
    const mcpDropdownLabel = document.getElementById("mcpDropdownLabel");
    const mcpChevronIcon   = document.getElementById("mcpChevronIcon");
    const mcpOptions       = document.querySelectorAll(".mcp-option");

    // ── Mode config ───────────────────────────────────────────────────────────
    let currentMode = "gmail";

    const MCP_CONFIG = {
        general: {
            label:      "🤖 General Assistant",
            placeholder:"Ask G-Assistant anything...",
            greeting:   "Hello! I'm **G-Assistant**, your AI assistant.\n\nI can help with analysis, writing, coding, math, general knowledge, and more. What would you like to explore?",
            showInbox:  false,
            comingSoon: false
        },
        gmail: {
            label:      "📧 Gmail MCP",
            placeholder:"Ask G-Assistant to manage your emails...",
            greeting:   "Hello! I am your local AI **Gmail Assistant**.\n\nI can read, send, draft, search, delete, archive, star, label, and summarise your emails. How can I help you today?",
            showInbox:  true,
            comingSoon: false
        },
        drive: {
            label:      "📁 Drive MCP",
            placeholder:"Google Drive MCP coming soon...",
            greeting:   "**Google Drive MCP** is coming soon!\n\nYou'll be able to browse, search, upload, and manage your Drive files directly from this chat. Stay tuned!",
            showInbox:  false,
            comingSoon: true
        },
        docs: {
            label:      "📄 Google Docs MCP",
            placeholder:"Ask G-Assistant to manage your Google Docs...",
            greeting:   "Hello! I am your **Google Docs Assistant**.\n\nI can help you:\n• **List** your recent documents\n• **Search** docs by title or content\n• **Read** any document\n• **Create** new documents\n• **Append** text to existing docs\n• **Find & Replace** content\n• **Rename** or **Delete** documents\n\nJust tell me what you need!",
            showInbox:  false,
            comingSoon: false
        },
        sheets: {
            label:      "📊 Google Sheets MCP",
            placeholder:"Ask G-Assistant to manage your spreadsheets...",
            greeting:   "Hello! I am your **Google Sheets Assistant**.\n\nI can help you:\n• **List** your recent spreadsheets\n• **Search** sheets by title or content\n• **Read** any sheet or cell range\n• **Create** new spreadsheets\n• **Write / Append** data to sheets\n• **Clear** ranges\n• **Add / Rename** tabs\n• **Delete** spreadsheets\n\nJust tell me what you need!",
            showInbox:  false,
            comingSoon: false
        }
    };

    // ── Custom dropdown ───────────────────────────────────────────────────────
    function openDropdown() {
        mcpDropdownMenu.classList.remove("hidden");
        mcpDropdownBtn.classList.add("open");
        if (mcpChevronIcon) mcpChevronIcon.style.transform = "rotate(180deg)";
    }

    function closeDropdown() {
        mcpDropdownMenu.classList.add("hidden");
        mcpDropdownBtn.classList.remove("open");
        if (mcpChevronIcon) mcpChevronIcon.style.transform = "";
    }

    mcpDropdownBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        mcpDropdownMenu.classList.contains("hidden") ? openDropdown() : closeDropdown();
    });

    // Close when clicking outside
    document.addEventListener("click", closeDropdown);
    mcpDropdownMenu.addEventListener("click", (e) => e.stopPropagation());

    mcpOptions.forEach(opt => {
        opt.addEventListener("click", () => {
            const newMode = opt.dataset.value;
            closeDropdown();
            if (newMode !== currentMode) switchMode(newMode);
        });
    });

    // ── Mode switching ────────────────────────────────────────────────────────
    async function switchMode(mode) {
        currentMode = mode;
        const cfg = MCP_CONFIG[mode];

        // Dropdown button label
        mcpDropdownLabel.textContent = cfg.label;

        // Highlight active option
        mcpOptions.forEach(o => o.classList.toggle("active", o.dataset.value === mode));

        // Chat header badge
        modeBadge.textContent = cfg.label;
        modeBadge.className   = "mode-badge" + (cfg.comingSoon ? " coming-soon" : "");

        // Input placeholder
        chatInput.placeholder = cfg.placeholder;

        // Inbox panel visibility (only for Gmail)
        inboxSection.style.display = cfg.showInbox ? "flex" : "none";

        // Clear chat and show fresh greeting for the selected mode
        chatHistory.innerHTML = "";
        appendMessage(cfg.greeting);

        // Reset server-side history for this mode
        try {
            await fetch("/api/reset_history", {
                method:  "POST",
                headers: { "Content-Type": "application/json" },
                body:    JSON.stringify({ mode })
            });
        } catch (_) { /* non-critical */ }
    }

    // ── Status indicator ──────────────────────────────────────────────────────
    function setStatus(state) {
        if (state === "thinking") {
            statusDot.className = "status-indicator thinking";
            statusText.innerText = "Thinking...";
            thinkingAnim.classList.remove("hidden");
        } else {
            statusDot.className = "status-indicator ready";
            statusText.innerText = "Ready";
            thinkingAnim.classList.add("hidden");
        }
    }

    // ── Append message ────────────────────────────────────────────────────────
    function appendMessage(text, isUser = false) {
        const msgDiv = document.createElement("div");
        msgDiv.className = `message ${isUser ? "user-msg" : "assistant-msg"}`;
        const content = isUser ? text : marked.parse(text);
        msgDiv.innerHTML = `
            <div class="avatar"><i data-lucide="${isUser ? "user" : "sparkles"}"></i></div>
            <div class="bubble">${content}</div>
        `;
        chatHistory.appendChild(msgDiv);
        lucide.createIcons();
        chatHistory.scrollTop = chatHistory.scrollHeight;
    }

    // ── Attachment handling ───────────────────────────────────────────────────
    let currentAttachment  = null;
    const fileUpload       = document.getElementById("fileUpload");
    const chatInputWrapper = document.querySelector(".chat-input-wrapper");

    const attachContainer = document.createElement("div");
    attachContainer.className = "attachment-preview";
    attachContainer.style.cssText = "padding:8px 15px;font-size:13px;background:rgba(99,102,241,0.1);border-top:1px solid rgba(255,255,255,0.05);display:none;justify-content:space-between;align-items:center;";
    chatInputWrapper.insertBefore(attachContainer, chatForm);

    fileUpload.addEventListener("change", (e) => {
        const file = e.target.files[0];
        if (!file) return;
        const reader = new FileReader();
        reader.onload = (ev) => {
            currentAttachment = { name: file.name, data: ev.target.result.split(",")[1] };
            attachContainer.innerHTML = `<span>📎 ${file.name}</span><button type="button" id="removeAttach" style="background:none;border:none;color:var(--text-muted);cursor:pointer;">✕</button>`;
            attachContainer.style.display = "flex";
            document.getElementById("removeAttach").onclick = () => {
                currentAttachment = null;
                attachContainer.style.display = "none";
                fileUpload.value = "";
            };
        };
        reader.readAsDataURL(file);
    });

    // ── Send message ──────────────────────────────────────────────────────────
    async function sendMessage(text) {
        if (!text.trim() && !currentAttachment) return;

        let display = text;
        if (currentAttachment) display += `\n[Attached: ${currentAttachment.name}]`;
        appendMessage(display, true);

        const payload = {
            message:    text,
            attachment: currentAttachment,
            mode:       currentMode
        };

        chatInput.value              = "";
        currentAttachment            = null;
        attachContainer.style.display = "none";
        fileUpload.value             = "";
        setStatus("thinking");

        try {
            const response = await fetch("/api/chat", {
                method:  "POST",
                headers: { "Content-Type": "application/json" },
                body:    JSON.stringify(payload)
            });
            const data = await response.json();
            appendMessage(response.ok ? data.reply : "Error: " + (data.error || "Unknown error"));
        } catch (_) {
            appendMessage("Failed to reach the server. Please check the backend is running.");
        } finally {
            setStatus("ready");
        }
    }

    chatForm.addEventListener("submit", (e) => {
        e.preventDefault();
        sendMessage(chatInput.value);
    });

    // ── Email modal ───────────────────────────────────────────────────────────
    const emailModal  = document.getElementById("emailModal");
    const closeModal  = document.getElementById("closeModal");
    const modalSubject= document.getElementById("modalSubject");
    const modalSender = document.querySelector("#modalSender span");
    const modalDate   = document.querySelector("#modalDate span");
    const modalLabels = document.querySelector("#modalLabels span");
    const modalBody   = document.getElementById("modalBody");

    closeModal.addEventListener("click", () => emailModal.classList.add("hidden"));
    emailModal.addEventListener("click", (e) => {
        if (e.target === emailModal) emailModal.classList.add("hidden");
    });

    async function showEmailDetail(emailId) {
        emailModal.classList.remove("hidden");
        modalSubject.innerText = "Loading...";
        modalSender.innerText = modalDate.innerText = modalLabels.innerText = "";
        modalBody.innerText = "Fetching email details...";
        try {
            const res = await fetch(`/api/email?message_id=${emailId}`);
            if (!res.ok) throw new Error();
            const { email } = await res.json();
            modalSubject.innerText  = email.subject || "(No Subject)";
            modalSender.innerHTML   = (email.from || "Unknown").replace(/</g, "&lt;").replace(/>/g, "&gt;");
            modalDate.innerText     = email.date    || "Unknown Date";
            modalLabels.innerText   = (email.labels || []).join(", ");
            modalBody.innerHTML     = email.body    || "(No content)";
        } catch (_) {
            modalSubject.innerText = "Error";
            modalBody.innerText    = "Could not load email details.";
        }
    }

    // ── Inbox ─────────────────────────────────────────────────────────────────
    async function loadInbox() {
        inboxList.innerHTML = `<p style="color:var(--text-muted);font-size:14px;text-align:center;padding:20px;">Fetching emails...</p>`;
        try {
            const res  = await fetch("/api/inbox");
            if (!res.ok) throw new Error();
            const data = await res.json();
            inboxList.innerHTML = "";
            if (data.emails?.length) {
                data.emails.forEach(email => {
                    const el = document.createElement("div");
                    el.className = "email-item";
                    let sender = email.from;
                    if (sender.includes("<")) sender = sender.split("<")[0].trim();
                    el.innerHTML = `
                        <div class="email-sender">${sender}</div>
                        <div class="email-subject">${email.subject || "(No Subject)"}</div>
                    `;
                    el.addEventListener("click", () => showEmailDetail(email.id));
                    inboxList.appendChild(el);
                });
            } else {
                inboxList.innerHTML = `<p style="color:var(--text-muted);font-size:14px;text-align:center;padding:20px;">No emails found.</p>`;
            }
        } catch (_) {
            inboxList.innerHTML = `<p style="color:#ef4444;font-size:14px;text-align:center;padding:20px;">Auth error — run python auth.py</p>`;
        }
    }

    refreshInbox.addEventListener("click", loadInbox);

    // ── Initialise with Gmail mode (matches HTML default) ─────────────────────
    mcpDropdownLabel.textContent = MCP_CONFIG.gmail.label;
    modeBadge.textContent        = MCP_CONFIG.gmail.label;
    modeBadge.className          = "mode-badge";
    chatInput.placeholder        = MCP_CONFIG.gmail.placeholder;
    inboxSection.style.display   = "flex";
    // Mark gmail as active in dropdown
    mcpOptions.forEach(o => o.classList.toggle("active", o.dataset.value === "gmail"));
    loadInbox();
});
