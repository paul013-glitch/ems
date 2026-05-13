# EMS Simulator Demo

Static EMS simulator for a factory with rooftop solar, a battery, grid import/export, and production load.

## Pages

- `index.html` redirects to the simulator.
- `simulator-demo.html` contains the full interactive simulator.
- `simulator-realtime.html` contains the fake realtime device simulator.
- `netlify/functions/energy-prices.mjs` proxies the Netherlands day-ahead price API for the browser.

## Netlify

The site is configured as a static deploy with `netlify.toml`.

- Build command: none
- Publish directory: repository root
- Function endpoint: `/.netlify/functions/energy-prices`
