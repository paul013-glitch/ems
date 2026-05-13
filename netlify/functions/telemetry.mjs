import { getStore } from "@netlify/blobs";

const STORE_NAME = "hmg-telemetry";
const LATEST_KEY = "latest";

export default async (req) => {
  if (req.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: corsHeaders() });
  }

  if (req.method === "GET") {
    return handleGet();
  }

  if (req.method === "POST") {
    return handlePost(req);
  }

  return jsonResponse({ error: "Method not allowed" }, 405);
};

export const config = {
  path: "/api/telemetry"
};

async function handleGet() {
  const store = getStore({ name: STORE_NAME, consistency: "strong" });
  const latest = await store.get(LATEST_KEY, { type: "json" });

  if (!latest) {
    return jsonResponse({
      paused: false,
      last_poll: null,
      poll_interval_seconds: null,
      online_window_seconds: 60,
      devices: {},
      totals: {},
      ems_control: {
        solar_setpoint_percent: null,
        battery_setpoint_percent: null,
        last_action: "Waiting for telemetry"
      }
    });
  }

  return jsonResponse(latest);
}

async function handlePost(req) {
  const configuredToken = Netlify.env.get("TELEMETRY_TOKEN");

  if (!configuredToken) {
    return jsonResponse({ error: "TELEMETRY_TOKEN is not configured" }, 500);
  }

  const authHeader = req.headers.get("authorization") || "";
  const token = authHeader.replace(/^Bearer\s+/i, "");
  if (token !== configuredToken) {
    return jsonResponse({ error: "Unauthorized" }, 401);
  }

  let payload;
  try {
    payload = await req.json();
  } catch {
    return jsonResponse({ error: "Invalid JSON" }, 400);
  }

  const now = new Date().toISOString();
  const telemetry = {
    ...payload,
    cloud_received_at: now
  };

  const store = getStore({ name: STORE_NAME, consistency: "strong" });
  await store.setJSON(LATEST_KEY, telemetry, {
    metadata: { updatedAt: now }
  });

  return jsonResponse({ ok: true, received_at: now });
}

function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: corsHeaders()
  });
}

function corsHeaders() {
  return {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization"
  };
}
