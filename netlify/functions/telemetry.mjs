import { getStore } from "@netlify/blobs";

const STORE_NAME = "hmg-telemetry";
const LATEST_KEY = "latest";
const MAX_HISTORY_POINTS = 3000;

export default async (req) => {
  if (req.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: corsHeaders() });
  }

  if (req.method === "GET") {
    return handleGet(req);
  }

  if (req.method === "POST") {
    return handlePost(req);
  }

  return jsonResponse({ error: "Method not allowed" }, 405);
};

export const config = {
  path: "/api/telemetry"
};

async function handleGet(req) {
  const store = getStore({ name: STORE_NAME, consistency: "strong" });
  const url = new URL(req.url);

  if (url.searchParams.get("history") === "today") {
    const key = historyKey(new Date());
    const history = (await store.get(key, { type: "json" })) || { date: key.replace("history/", ""), points: [] };
    return jsonResponse(history);
  }

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
  await appendHistoryPoint(store, telemetry, now);

  return jsonResponse({ ok: true, received_at: now });
}

async function appendHistoryPoint(store, telemetry, now) {
  const key = historyKey(new Date(now));
  const history = (await store.get(key, { type: "json" })) || { date: key.replace("history/", ""), points: [] };
  const point = compactPoint(telemetry, now);

  history.points.push(point);
  history.points = history.points
    .filter((item) => item && typeof item.ts === "string")
    .slice(-MAX_HISTORY_POINTS);

  await store.setJSON(key, history, {
    metadata: { updatedAt: now }
  });
}

function compactPoint(telemetry, now) {
  const gridKw = telemetry?.devices?.grid_meter?.values?.current_kw;
  const solarKw = telemetry?.totals?.solar_production_kw;
  const batteryKw = telemetry?.totals?.battery_power_kw;

  return {
    ts: telemetry.last_poll || now,
    solar_kw: finiteNumber(solarKw),
    grid_kw: finiteNumber(gridKw),
    battery_kw: finiteNumber(batteryKw)
  };
}

function finiteNumber(value) {
  return Number.isFinite(value) ? value : null;
}

function historyKey(date) {
  return `history/${date.toISOString().slice(0, 10)}`;
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
