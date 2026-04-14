document.addEventListener("DOMContentLoaded", () => {
    const chatForm = document.getElementById("chatForm");
    const chatInput = document.getElementById("chatInput");
    const chatHistory = document.getElementById("chatHistory");
    const inboxList = document.getElementById("inboxList");
    const refreshInbox = document.getElementById("refreshInbox");
    const promptBtns = document.querySelectorAll(".prompt-btn");
    const statusDot = document.getElementById("statusDot");
    const statusText = document.getElementById("statusText");
    const thinkingAnim = document.getElementById("thinkingAnim");

    function setStatus(state) {
        if (state === 'thinking') {
            statusDot.className = "status-indicator thinking";
            statusText.innerText = "Thinking...";
            thinkingAnim.classList.remove("hidden");
        } else {
            statusDot.className = "status-indicator ready";
            statusText.innerText = "Ready";
            thinkingAnim.classList.add("hidden");
        }
    }

    function appendMessage(text, isUser = false) {
        const msgDiv = document.createElement("div");
        msgDiv.className = `message ${isUser ? 'user-msg' : 'assistant-msg'}`;
        
        // Parse markdown if it's the assistant or if user wants formatting
        const content = isUser ? text : marked.parse(text);
        
        msgDiv.innerHTML = `
            <div class="avatar"><i data-lucide="${isUser ? 'user' : 'sparkles'}"></i></div>
            <div class="bubble">${content}</div>
        `;
        chatHistory.appendChild(msgDiv);
        lucide.createIcons();
        chatHistory.scrollTop = chatHistory.scrollHeight;
    }

    let currentAttachment = null;
    const fileUpload = document.getElementById("fileUpload");
    const chatInputWrapper = document.querySelector(".chat-input-wrapper");
    
    // Create attachment pill container
    const attachContainer = document.createElement("div");
    attachContainer.className = "attachment-preview";
    attachContainer.style.cssText = "padding: 8px 15px; font-size: 13px; background: rgba(99,102,241,0.1); border-top: 1px solid rgba(255,255,255,0.05); display: none; justify-content: space-between; align-items: center;";
    chatInputWrapper.insertBefore(attachContainer, chatForm);

    fileUpload.addEventListener("change", (e) => {
        const file = e.target.files[0];
        if (!file) return;
        const reader = new FileReader();
        reader.onload = (ev) => {
            currentAttachment = {
                name: file.name,
                data: ev.target.result.split(',')[1] // Get base64 part
            };
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

    async function sendMessage(text) {
        if (!text.trim() && !currentAttachment) return;
        
        let msgExt = text;
        if (currentAttachment) msgExt += `\n[Attached: ${currentAttachment.name}]`;
        appendMessage(msgExt, true);
        
        const payload = {
            message: text,
            attachment: currentAttachment
        };
        
        chatInput.value = "";
        currentAttachment = null;
        attachContainer.style.display = "none";
        fileUpload.value = "";
        setStatus("thinking");

        try {
            const response = await fetch("/api/chat", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload)
            });
            const data = await response.json();
            if (response.ok) {
                appendMessage(data.reply);
            } else {
                appendMessage("Error communicating with agent: " + (data.error || "Unknown"));
            }
        } catch (e) {
            appendMessage("Failed to send message.");
        } finally {
            setStatus("ready");
        }
    }

    chatForm.addEventListener("submit", (e) => {
        e.preventDefault();
        sendMessage(chatInput.value);
    });

    promptBtns.forEach(btn => {
        btn.addEventListener("click", () => sendMessage(btn.dataset.prompt));
    });

    const emailModal = document.getElementById("emailModal");
    const closeModal = document.getElementById("closeModal");
    const modalSubject = document.getElementById("modalSubject");
    const modalSender = document.querySelector("#modalSender span");
    const modalDate = document.querySelector("#modalDate span");
    const modalLabels = document.querySelector("#modalLabels span");
    const modalBody = document.getElementById("modalBody");

    closeModal.addEventListener("click", () => {
        emailModal.classList.add("hidden");
    });
    
    // Auto-close when clicking outside modal logic
    emailModal.addEventListener("click", (e) => {
        if (e.target === emailModal) {
            emailModal.classList.add("hidden");
        }
    });

    async function showEmailDetail(emailId) {
        emailModal.classList.remove("hidden");
        modalSubject.innerText = "Loading...";
        modalSender.innerText = "";
        modalDate.innerText = "";
        modalLabels.innerText = "";
        modalBody.innerText = "Fetching email details from your inbox...";
        
        try {
            const res = await fetch(`/api/email?message_id=${emailId}`);
            if (!res.ok) throw new Error("Failed to fetch email");
            const data = await res.json();
            const eDetail = data.email;
            
            modalSubject.innerText = eDetail.subject || '(No Subject)';
            let senderRaw = eDetail.from || 'Unknown Sender';
            senderRaw = senderRaw.replace(/</g, "&lt;").replace(/>/g, "&gt;");
            modalSender.innerHTML = senderRaw;
            modalDate.innerText = eDetail.date || 'Unknown Date';
            modalLabels.innerText = (eDetail.labels || []).join(", ");
            modalBody.innerHTML = eDetail.body || '(No content)';
        } catch (e) {
            modalSubject.innerText = "Error";
            modalBody.innerText = "Error loading email details.";
        }
    }

    async function loadInbox() {
        inboxList.innerHTML = `<p class="loading-text" style="color: var(--text-muted); font-size: 14px; text-align: center; padding: 20px;">Fetching emails...</p>`;
        try {
            const res = await fetch("/api/inbox");
            if (!res.ok) {
                throw new Error(await res.text());
            }
            const data = await res.json();
            inboxList.innerHTML = "";
            if (data.emails && data.emails.length > 0) {
                data.emails.forEach(email => {
                    const el = document.createElement("div");
                    el.className = "email-item";
                    let senderName = email.from;
                    if (senderName.includes("<")) {
                        senderName = senderName.split("<")[0].trim();
                    }
                    el.innerHTML = `
                        <div class="email-sender">${senderName}</div>
                        <div class="email-subject">${email.subject || '(No Subject)'}</div>
                    `;
                    el.addEventListener("click", () => {
                        showEmailDetail(email.id);
                    });
                    inboxList.appendChild(el);
                });
            } else {
                inboxList.innerHTML = `<p class="loading-text" style="color: var(--text-muted); font-size: 14px; text-align: center; padding: 20px;">No emails found.</p>`;
            }
        } catch (e) {
            inboxList.innerHTML = `<p class="loading-text" style="color: #ef4444; font-size: 14px; text-align: center; padding: 20px;">Auth error. Run python auth.py</p>`;
        }
    }

    refreshInbox.addEventListener("click", () => {
        loadInbox();
    });

    // Initial load
    loadInbox();
});
