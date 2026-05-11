const ENERGY_CHARTS_URL = "https://api.energy-charts.info/price?bzn=NL";

export default async () => {
  try {
    const response = await fetch(ENERGY_CHARTS_URL, {
      headers: {
        Accept: "application/json"
      }
    });

    if (!response.ok) {
      return jsonResponse(
        {
          error: `Energy-Charts returned HTTP ${response.status}`,
          source: ENERGY_CHARTS_URL
        },
        response.status
      );
    }

    const data = await response.json();

    return jsonResponse({
      ...data,
      source: ENERGY_CHARTS_URL
    });
  } catch (error) {
    return jsonResponse(
      {
        error: error instanceof Error ? error.message : "Unable to load energy prices",
        source: ENERGY_CHARTS_URL
      },
      502
    );
  }
};

function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "public, max-age=300",
      "Access-Control-Allow-Origin": "*"
    }
  });
}
