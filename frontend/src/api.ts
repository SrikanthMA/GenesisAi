const API_BASE = "/api/v1";

export interface ConversationSummary {
  id: string;
  title: string | null;
  preferred_language: string;
  model_name: string | null;
  created_at: string;
  updated_at: string;
}

export interface MessageRead {
  id: string;
  role: string;
  content: string;
  created_at: string;
}

export interface ConversationDetail extends ConversationSummary {
  messages: MessageRead[];
}

async function json<T>(response: Response): Promise<T> {
  if (!response.ok) {
    throw new Error(`Request failed: ${response.status} ${response.statusText}`);
  }
  return (await response.json()) as T;
}

export function listConversations(): Promise<ConversationSummary[]> {
  return fetch(`${API_BASE}/conversations`).then((r) => json(r));
}

export function createConversation(title?: string): Promise<ConversationSummary> {
  return fetch(`${API_BASE}/conversations`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title: title ?? null }),
  }).then((r) => json(r));
}

export function getConversation(id: string): Promise<ConversationDetail> {
  return fetch(`${API_BASE}/conversations/${id}`).then((r) => json(r));
}

export function stopGeneration(id: string): Promise<{ stopped: boolean }> {
  return fetch(`${API_BASE}/conversations/${id}/stop`, { method: "POST" }).then((r) => json(r));
}

interface SseEvent {
  event: string;
  data: Record<string, unknown>;
}

function parseSseBlock(block: string): SseEvent | null {
  let eventType = "message";
  let dataLine: string | null = null;

  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) {
      eventType = line.slice("event:".length).trim();
    } else if (line.startsWith("data:")) {
      dataLine = line.slice("data:".length).trim();
    }
  }

  if (dataLine === null) return null;
  return { event: eventType, data: JSON.parse(dataLine) };
}

async function streamSse(
  response: Response,
  onToken: (token: string) => void,
  onDone: () => void,
  onError: (message: string) => void,
): Promise<void> {
  if (!response.ok || !response.body) {
    throw new Error(`Request failed: ${response.status} ${response.statusText}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let separatorIndex: number;
    while ((separatorIndex = buffer.indexOf("\n\n")) !== -1) {
      const block = buffer.slice(0, separatorIndex);
      buffer = buffer.slice(separatorIndex + 2);
      const parsed = parseSseBlock(block);
      if (!parsed) continue;
      if (parsed.event === "message" && typeof parsed.data.token === "string") {
        onToken(parsed.data.token);
      } else if (parsed.event === "error" && typeof parsed.data.message === "string") {
        onError(parsed.data.message);
      } else if (parsed.event === "done") {
        onDone();
      }
    }
  }
}

export function sendMessage(
  conversationId: string,
  content: string,
  onToken: (token: string) => void,
  onDone: () => void,
  onError: (message: string) => void,
): Promise<void> {
  return fetch(`${API_BASE}/conversations/${conversationId}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content }),
  }).then((response) => streamSse(response, onToken, onDone, onError));
}

export function regenerateMessage(
  conversationId: string,
  onToken: (token: string) => void,
  onDone: () => void,
  onError: (message: string) => void,
): Promise<void> {
  return fetch(`${API_BASE}/conversations/${conversationId}/regenerate`, {
    method: "POST",
  }).then((response) => streamSse(response, onToken, onDone, onError));
}
