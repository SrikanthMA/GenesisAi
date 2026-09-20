import { useEffect, useState } from "react";
import {
  type ConversationSummary,
  type MessageRead,
  createConversation,
  getConversation,
  listConversations,
  regenerateMessage,
  sendMessage,
  stopGeneration,
} from "./api";

export default function App() {
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<MessageRead[]>([]);
  const [streamingText, setStreamingText] = useState<string>("");
  const [isStreaming, setIsStreaming] = useState(false);
  const [input, setInput] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void refreshConversations();
  }, []);

  async function refreshConversations() {
    try {
      setConversations(await listConversations());
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function openConversation(id: string) {
    setActiveId(id);
    setStreamingText("");
    try {
      const detail = await getConversation(id);
      setMessages(detail.messages);
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function newChat() {
    try {
      const conversation = await createConversation();
      setConversations((prev) => [conversation, ...prev]);
      setActiveId(conversation.id);
      setMessages([]);
      setStreamingText("");
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function handleSend() {
    const content = input.trim();
    if (!content || isStreaming) return;

    let conversationId = activeId;
    if (!conversationId) {
      const conversation = await createConversation();
      setConversations((prev) => [conversation, ...prev]);
      conversationId = conversation.id;
      setActiveId(conversationId);
    }

    setInput("");
    setMessages((prev) => [
      ...prev,
      { id: `local-${Date.now()}`, role: "user", content, created_at: new Date().toISOString() },
    ]);
    setIsStreaming(true);
    setStreamingText("");
    setError(null);

    try {
      await sendMessage(
        conversationId,
        content,
        (token) => setStreamingText((prev) => prev + token),
        () => void finishStreaming(conversationId!),
        (message) => setError(message),
      );
    } catch (e) {
      setError((e as Error).message);
      setIsStreaming(false);
    }
  }

  async function handleRegenerate() {
    if (!activeId || isStreaming) return;
    setIsStreaming(true);
    setStreamingText("");
    setError(null);
    try {
      await regenerateMessage(
        activeId,
        (token) => setStreamingText((prev) => prev + token),
        () => void finishStreaming(activeId),
        (message) => setError(message),
      );
    } catch (e) {
      setError((e as Error).message);
      setIsStreaming(false);
    }
  }

  async function finishStreaming(conversationId: string) {
    try {
      const detail = await getConversation(conversationId);
      setMessages(detail.messages);
    } finally {
      setStreamingText("");
      setIsStreaming(false);
      void refreshConversations();
    }
  }

  async function handleStop() {
    if (!activeId) return;
    await stopGeneration(activeId);
  }

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">Genesis</div>
        <button className="new-chat" onClick={() => void newChat()}>
          + New chat
        </button>
        <div className="conversation-list">
          {conversations.map((c) => (
            <button
              key={c.id}
              className={`conversation-item ${c.id === activeId ? "active" : ""}`}
              onClick={() => void openConversation(c.id)}
            >
              {c.title || "Untitled conversation"}
            </button>
          ))}
        </div>
      </aside>

      <main className="chat">
        <div className="messages">
          {messages.map((m) => (
            <div key={m.id} className={`bubble ${m.role}`}>
              {m.content}
            </div>
          ))}
          {isStreaming && (
            <div className="bubble assistant streaming">{streamingText || "…"}</div>
          )}
        </div>

        {error && <div className="error-banner">{error}</div>}

        <div className="composer">
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void handleSend();
              }
            }}
            placeholder="Message Genesis…"
            rows={2}
          />
          <div className="composer-actions">
            <button onClick={() => void handleRegenerate()} disabled={!activeId || isStreaming}>
              Regenerate
            </button>
            {isStreaming ? (
              <button onClick={() => void handleStop()}>Stop</button>
            ) : (
              <button onClick={() => void handleSend()} disabled={!input.trim()}>
                Send
              </button>
            )}
          </div>
        </div>
      </main>
    </div>
  );
}
