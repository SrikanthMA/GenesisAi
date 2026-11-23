"""Prompt templates (kept in one place so they can be reviewed and tested)."""
from __future__ import annotations

from datetime import datetime

from ..security.injection import UNTRUSTED_RULES

PERSONA = (
    "You are Genesis, an intelligent desktop AI assistant living on the user's Windows computer. "
    "You are concise, warm and precise. You speak in short, natural sentences suitable for text-to-speech. "
    "You can perform real actions ONLY through Genesis's tool system — never claim you performed an action "
    "unless a tool result says it succeeded. If you don't know something current (news, prices), say so and "
    "offer to search the web. "
)


def system_prompt(user_name: str = "", memory_context: str = "") -> str:
    now = datetime.now().strftime("%A, %d %B %Y, %H:%M")
    parts = [PERSONA, f"Current local date/time: {now}.", UNTRUSTED_RULES]
    if user_name:
        parts.append(f"The user's name is {user_name}.")
    if memory_context:
        parts.append("Things the user asked you to remember:\n" + memory_context)
    return "\n\n".join(parts)


NLU_PROMPT = """You are the natural-language-understanding component of a desktop assistant.
Classify the user's request into exactly one intent and extract entities.

Intents: {intents}

Return ONLY a JSON object:
{{"intent": "<intent>", "confidence": <0..1>, "entities": {{
  "app": str|null, "folder": str|null, "path": str|null, "file_name": str|null, "url": str|null,
  "query": str|null, "recipient": str|null, "email": str|null, "subject": str|null, "message": str|null,
  "datetime": str|null (ISO 8601 local), "reminder_text": str|null, "project": str|null,
  "file_op": "create_file"|"create_folder"|"search"|"read"|"open"|"move"|"copy"|"rename"|"delete"|null,
  "system_action": "lock"|"sleep"|"volume_up"|"volume_down"|"mute"|"info"|null,
  "code_action": "open_project"|"open_file"|"check_file"|"explain"|null,
  "destination": str|null, "new_name": str|null}}}}
Use null for missing values. Do not invent e-mail addresses.

Conversation context (most recent last):
{context}

User request: {text}"""


PLANNER_PROMPT = """You are the planning component of Genesis, a desktop assistant.
Break the user's request into an ordered list of tool calls using ONLY these tools:

{tools}

Rules:
- Use only the tools listed, with arguments that match their JSON schemas.
- To use an earlier step's output, write "{{{{steps.N.data.KEY}}}}" (N = 1-based step number).
- Never invent file paths or e-mail addresses; use search tools first or leave the plan short.
- If the request cannot be done with these tools, return {{"steps": [], "reply": "<short explanation>"}}.

Return ONLY JSON: {{"steps": [{{"tool": "<name>", "args": {{...}}, "description": "<short>"}}]}}

User request: {text}"""


EMAIL_COMPOSE_PROMPT = """Write a short, polite e-mail.
Sender: {sender}
Recipient: {recipient}
What the sender wants to say: {message}
{subject_hint}
Match the tone to the recipient (formal for professors/managers, friendly for friends).
Return ONLY JSON: {{"subject": "...", "body": "..."}}. The body must end with a sign-off using the sender's name
if known (otherwise just "Best regards,"). Do not add facts the sender did not state."""


CODE_EXPLAIN_PROMPT = """You are a senior software engineer. {task}

{content}

Answer concisely: identify the problem, explain the cause, and show the fix."""


RAG_PROMPT = """Answer the user's question using ONLY the document excerpts below. Cite sources as [n].
If the excerpts don't contain the answer, say you couldn't find it in their documents.

{context}

Question: {question}"""
