// Worker entrypoint.
//
// Two product surfaces share this Worker:
//
//   1. Push-to-Claude (existing) — single-turn voice/text chat with
//      Haiku. Keeps the original /ask, /ask-text, /reset endpoints
//      and KV-backed conversation history.
//
//   2. Cardputer Pager + Central Console (new) — fire-and-monitor
//      cloud agents using the Managed Agents API. Each session gets
//      a SessionRouter Durable Object that mirrors event history
//      and serves the Pager (poll) + Console (SSE) surfaces.
//
// Both surfaces auth via the same DEVICE_SECRET, sent as
// `x-device-secret` (device-side) or `?token=...` (browser).

import {
  authenticate,
  handleConfirm,
  handleDelete,
  handleInterrupt,
  handlePoll,
  handleRename,
  handleReply,
  handleSessions,
  handleSpawn,
} from "./pager.js";
import {
  handleConsolePage,
  handleFileDownload,
  handleFilesList,
  handleStream,
} from "./console_routes.js";

export { SessionRouter } from "./router.do.js";

// ---- Push-to-Claude (existing) -------------------------------------

const CHAT_SYSTEM_PROMPT =
  "You are an assistant responding on a 240x135 pixel handheld LCD. " +
  "Reply in 1-3 short sentences. Plain ASCII when possible. " +
  "No markdown, no lists, no code fences. " +
  "Be direct; assume the user can't scroll. " +
  "Respond in the same language the user spoke or wrote in. " +
  "If audio is provided, first transcribe the user's speech faithfully " +
  "into the 'transcript' field, then write your reply in 'response'. " +
  "For text input, copy the user's text into 'transcript' and answer in 'response'. " +
  "You may receive a few prior turns of conversation history; " +
  "treat the latest user message as the current question.";

const CHAT_MODEL = "gemini-2.5-flash";
const HISTORY_MAX_MESSAGES = 8;
const HISTORY_TTL_SECONDS = 24 * 3600;

function authOk(request, env) {
  return request.headers.get("x-device-secret") === env.DEVICE_SECRET;
}

function historyKey(deviceSecret) {
  return `turns:${deviceSecret}`;
}

async function getHistory(env, deviceSecret) {
  if (!env.HISTORY) return [];
  try {
    const raw = await env.HISTORY.get(historyKey(deviceSecret));
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

async function appendTurn(env, deviceSecret, userMsg, assistantMsg) {
  if (!env.HISTORY) return;
  const hist = await getHistory(env, deviceSecret);
  hist.push({ role: "user", content: userMsg });
  hist.push({ role: "assistant", content: assistantMsg });
  const trimmed = hist.slice(-HISTORY_MAX_MESSAGES);
  await env.HISTORY.put(historyKey(deviceSecret), JSON.stringify(trimmed), {
    expirationTtl: HISTORY_TTL_SECONDS,
  });
}

function bufferToBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode.apply(
      null,
      bytes.subarray(i, i + CHUNK),
    );
  }
  return btoa(binary);
}

const GEMINI_RESPONSE_SCHEMA = {
  type: "OBJECT",
  properties: {
    transcript: { type: "STRING" },
    response: { type: "STRING" },
  },
  required: ["transcript", "response"],
};

async function callGemini(env, deviceSecret, { text, audioB64 }) {
  const history = await getHistory(env, deviceSecret);
  const contents = history.map((m) => ({
    role: m.role === "assistant" ? "model" : "user",
    parts: [{ text: m.content }],
  }));

  const userParts = audioB64
    ? [{ inline_data: { mime_type: "audio/wav", data: audioB64 } }]
    : [{ text }];
  contents.push({ role: "user", parts: userParts });

  const url =
    `https://generativelanguage.googleapis.com/v1beta/models/${CHAT_MODEL}:generateContent` +
    `?key=${env.GEMINI_API_KEY}`;

  const resp = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      contents,
      systemInstruction: { parts: [{ text: CHAT_SYSTEM_PROMPT }] },
      generationConfig: {
        maxOutputTokens: 400,
        responseMimeType: "application/json",
        responseSchema: GEMINI_RESPONSE_SCHEMA,
      },
    }),
  });

  if (!resp.ok) {
    const detail = (await resp.text()).slice(0, 300);
    return { ok: false, status: resp.status, detail };
  }
  const data = await resp.json();
  const rawText = data.candidates?.[0]?.content?.parts?.[0]?.text || "{}";
  let parsed;
  try {
    parsed = JSON.parse(rawText);
  } catch {
    parsed = { transcript: text || "", response: rawText };
  }
  const transcript = (parsed.transcript || text || "").trim();
  const reply = (parsed.response || "").trim() || "(empty)";

  await appendTurn(env, deviceSecret, transcript || "(audio)", reply);
  return { ok: true, transcript, text: reply };
}

async function handleAsk(request, env) {
  if (!authOk(request, env)) return jsonResp({ error: "unauthorized" }, 401);

  const audioBytes = await request.arrayBuffer();
  if (audioBytes.byteLength < 200) {
    return jsonResp(
      { error: "audio too short", bytes: audioBytes.byteLength },
      400,
    );
  }

  const audioB64 = bufferToBase64(audioBytes);
  const deviceSecret = request.headers.get("x-device-secret");
  const result = await callGemini(env, deviceSecret, { audioB64 });
  if (!result.ok) {
    return jsonResp(
      {
        error: "gemini failed",
        status: result.status,
        detail: result.detail,
      },
      502,
    );
  }
  return jsonResp({ transcript: result.transcript, response: result.text });
}

async function handleAskText(request, env) {
  if (!authOk(request, env)) return jsonResp({ error: "unauthorized" }, 401);
  let data;
  try {
    data = await request.json();
  } catch {
    return jsonResp({ error: "invalid json" }, 400);
  }
  const prompt = ((data.prompt || data.text || "") + "").trim();
  if (!prompt) return jsonResp({ error: "empty prompt" }, 400);

  const deviceSecret = request.headers.get("x-device-secret");
  const result = await callGemini(env, deviceSecret, { text: prompt });
  if (!result.ok) {
    return jsonResp(
      {
        transcript: prompt,
        error: "gemini failed",
        status: result.status,
        detail: result.detail,
      },
      502,
    );
  }
  return jsonResp({
    transcript: result.transcript || prompt,
    response: result.text,
  });
}

async function handleReset(request, env) {
  if (!authOk(request, env)) return jsonResp({ error: "unauthorized" }, 401);
  const deviceSecret = request.headers.get("x-device-secret");
  if (env.HISTORY) {
    await env.HISTORY.delete(historyKey(deviceSecret));
  }
  return jsonResp({ ok: true, cleared: true });
}

// ---- Router ---------------------------------------------------------

// (method, path) → handler. The Pager handlers receive the resolved
// auth object; the chat handlers do their own auth (header-only —
// voice uploads aren't sent from a browser, so no `?token=` escape
// hatch is needed there).
const PAGER_ROUTES = {
  "POST /pager/spawn": handleSpawn,
  "GET /pager/sessions": handleSessions,
  "GET /pager/poll": handlePoll,
  "POST /pager/interrupt": handleInterrupt,
  "POST /pager/reply": handleReply,
  "POST /pager/confirm": handleConfirm,
  "POST /pager/delete": handleDelete,
  "POST /pager/rename": handleRename,

  "GET /console/stream": handleStream,
  "GET /console/sessions": handleSessions,
  "GET /console/files": handleFilesList,
  "GET /console/file": handleFileDownload,
  "POST /console/spawn": handleSpawn,
  "POST /console/reply": handleReply,
  "POST /console/interrupt": handleInterrupt,
  "POST /console/delete": handleDelete,
  "POST /console/rename": handleRename,
  "POST /console/confirm": handleConfirm,
};

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const key = `${request.method} ${url.pathname}`;

    if (request.method === "GET" && url.pathname === "/") {
      return new Response("push-to-claude relay ok\n", {
        headers: { "content-type": "text/plain" },
      });
    }

    // Push-to-Claude (single-turn). Untouched.
    if (request.method === "POST" && url.pathname === "/ask")
      return handleAsk(request, env);
    if (request.method === "POST" && url.pathname === "/ask-text")
      return handleAskText(request, env);
    if (request.method === "POST" && url.pathname === "/reset")
      return handleReset(request, env);

    // Console page (no auth at the page itself; auth is on the
    // subsequent fetch calls, where the user types the secret).
    if (
      request.method === "GET" &&
      (url.pathname === "/console" || url.pathname === "/console/")
    ) {
      return handleConsolePage();
    }

    if (PAGER_ROUTES[key]) {
      const auth = await authenticate(request, env);
      if (!auth) return jsonResp({ error: "unauthorized" }, 401);
      try {
        return await PAGER_ROUTES[key](request, env, auth);
      } catch (err) {
        // Inner handlers normally shape their own errors, but DO RPC
        // and upstream Anthropic errors can bubble. Always return
        // JSON so the Pager + Console can render something useful.
        return jsonResp(
          { error: "internal", message: String(err?.message || err) },
          500,
        );
      }
    }

    return new Response("not found\n", { status: 404 });
  },
};

function jsonResp(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json" },
  });
}
