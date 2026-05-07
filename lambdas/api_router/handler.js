const { LambdaClient, InvokeCommand } = require("@aws-sdk/client-lambda");

const client = new LambdaClient({});

const ALLOWED_ORIGINS = (process.env.ALLOWED_ORIGINS || "*")
  .split(",")
  .map(s => s.trim())
  .filter(Boolean);

function corsHeaders(origin) {
  const allowOrigin = ALLOWED_ORIGINS.includes("*")
    ? "*"
    : (ALLOWED_ORIGINS.includes(origin) ? origin : ALLOWED_ORIGINS[0] || "*");

  return {
    "content-type": "application/json",
    "access-control-allow-origin": allowOrigin,
    "access-control-allow-headers": "Content-Type, Authorization",
    "access-control-allow-methods": "POST, GET, OPTIONS",
  };
}

function json(statusCode, body) {
  return { statusCode, headers: corsHeaders(), body: JSON.stringify(body) };
}

function parseJsonBody(event) {
  if (!event || !event.body) return null;
  let raw = event.body;
  if (event.isBase64Encoded) raw = Buffer.from(raw, "base64").toString("utf-8");
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

async function invoke(functionName, payloadObj) {
  const cmd = new InvokeCommand({
    FunctionName: functionName,
    Payload: Buffer.from(JSON.stringify(payloadObj)),
  });
  const resp = await client.send(cmd);
  const payloadStr = resp.Payload ? Buffer.from(resp.Payload).toString("utf-8") : "{}";
  return JSON.parse(payloadStr);
}

exports.handler = async (event) => {
  const method = event?.requestContext?.http?.method || "GET";
  const path = event?.rawPath || "/";

  // CORS preflight
  if (method === "OPTIONS") {
    return { statusCode: 204, headers: corsHeaders(), body: "" };
  }

  const qs = event?.queryStringParameters || {};
  const bodyJson = parseJsonBody(event) || {};

  try {
    // health
    if (path === "/" || path === "") {
      return json(200, { ok: true, routes: ["/search", "/qa"] });
    }

    // /search
    if (path === "/search" || path.startsWith("/search/")) {
      const q = String(bodyJson.q ?? qs.q ?? "").trim();
      if (!q) return json(400, { ok: false, error: "Missing query 'q'." });

      const payload = {
        q,
        size: bodyJson.size ?? (qs.size != null ? Number(qs.size) : undefined),
        k: bodyJson.k ?? (qs.k != null ? Number(qs.k) : undefined),
        filters: bodyJson.filters ?? {},
      };

      const out = await invoke(process.env.SEARCH_FN_ARN, payload);

      // If downstream already returned an API-style response, pass it through
      if (out && typeof out === "object" && out.statusCode && out.headers && out.body) {
        return out;
      }       

      return json(200, out);

    }

    // /qa
    if (path === "/qa" || path.startsWith("/qa/")) {
      const question = String(bodyJson.question ?? bodyJson.q ?? qs.question ?? "").trim();
      if (!question) return json(400, { ok: false, error: "Missing 'question' in JSON body." });

      const payload = {
        question,
        size: bodyJson.size ?? undefined,
        k: bodyJson.k ?? undefined,
        filters: bodyJson.filters ?? {},
      };

      const out = await invoke(process.env.QA_FN_ARN, payload);

      // If downstream already returned an API-style response, pass it through
      if (out && typeof out === "object" && out.statusCode && out.headers && out.body) {
        return out;
      }

      return json(200, out);
    }

    return json(404, { ok: false, error: "Not Found", routes: ["/search", "/qa"] });
  } catch (e) {
    console.error("Router error:", e);
    return json(500, { ok: false, error: "Router error", detail: String(e) });
  }
};
