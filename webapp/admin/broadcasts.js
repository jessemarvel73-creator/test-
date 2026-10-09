"use strict";

const $ = (id) => document.getElementById(id);

const messageInput = $("message");
const sendButton = $("send");
const resultBox = $("result");

function showResult(message, type = "info") {
    resultBox.textContent = message;

    if (type === "success") {
        resultBox.style.color = "#15803d";
    } else if (type === "error") {
        resultBox.style.color = "#dc2626";
    } else {
        resultBox.style.color = "#334155";
    }
}

async function apiFetch(url, options = {}) {
    const response = await fetch(url, {
        credentials: "same-origin",
        cache: "no-store",
        ...options,
        headers: {
            Accept: "application/json",
            ...(options.body
                ? {
                      "Content-Type": "application/json",
                  }
                : {}),
            ...(options.headers || {}),
        },
    });

    const data = await response
        .json()
        .catch(() => ({}));

    if (!response.ok || data.ok === false) {
        throw new Error(
            data.error ||
            `Request failed (${response.status})`
        );
    }

    return data;
}

function setSendingState(sending) {
    sendButton.disabled = sending;
    sendButton.textContent = sending
        ? "Sending..."
        : "Send Broadcast";
}

async function sendBroadcast() {
    const message = messageInput.value.trim();

    if (!message) {
        showResult(
            "Please write a message first.",
            "error"
        );
        messageInput.focus();
        return;
    }

    if (message.length > 5000) {
        showResult(
            "Message is too long. Maximum length is 5000 characters.",
            "error"
        );
        return;
    }

    const confirmed = window.confirm(
        "Send this broadcast to all active YOUR SHOP customers?"
    );

    if (!confirmed) {
        return;
    }

    setSendingState(true);
    showResult("Sending broadcast...");

    try {
        const data = await apiFetch(
            "/api/admin/broadcasts",
            {
                method: "POST",
                body: JSON.stringify({
                    message,
                }),
            }
        );

        const sent = Number(data.sent || 0);
        const failed = Number(data.failed || 0);
        const total = Number(
            data.total_recipients || sent + failed
        );

        showResult(
            `Broadcast completed. Sent: ${sent}, Failed: ${failed}, Total: ${total}.`,
            failed > 0 ? "info" : "success"
        );

        messageInput.value = "";
    } catch (error) {
        console.error(
            "[ADMIN] Broadcast error:",
            error
        );

        showResult(
            error.message ||
            "Failed to send broadcast.",
            "error"
        );
    } finally {
        setSendingState(false);
    }
}

sendButton.addEventListener(
    "click",
    sendBroadcast
);

messageInput.addEventListener(
    "keydown",
    (event) => {
        if (
            event.key === "Enter" &&
            (event.ctrlKey || event.metaKey)
        ) {
            event.preventDefault();
            sendBroadcast();
        }
    }
);
