// Presage SmartSpectra bridge.
//
// tracking.py owns the webcam (one camera, no conflicts) and POSTs each raw
// RGB frame here; this process pushes them into the SmartSpectra SDK
// (headless custom-input mode) and keeps the latest vitals in memory.
// The webpage polls GET /vitals for the live heart rate.
//
//   POST /frame   raw RGB bytes, headers X-Width / X-Height / X-Ts (µs, monotonic)
//   GET  /vitals  {"bpm": ..., "breathing": ..., "status": ...}
//
// Needs SMARTSPECTRA_API_KEY in the environment (secondcheck.py passes its
// env through, so put it in the project .env / shell). Without a key or with
// node_modules missing, the server still runs and reports why in /vitals.

const http = require('http');

const PORT = process.env.VITALS_PORT || 3002;

const vitals = {
  bpm: null,
  bpmConfidence: null,
  breathing: null,
  status: 'starting',
  hint: null,           // latest SDK validation hint, e.g. "too dark"
  lastMetrics: null,    // top-level keys of the latest metrics payload (debug)
  updated: null,
};

const PROCESSING_NAMES = ['uninitialized', 'idle', 'starting', 'running', 'stopping', 'error'];

let sdk = null;
let PixelFormat = null;
const seenShapes = new Set();

function pickLast(list) {
  return Array.isArray(list) && list.length ? list[list.length - 1] : null;
}

function initSdk() {
  const key = process.env.SMARTSPECTRA_API_KEY;
  if (!key) {
    vitals.status = 'no api key (set SMARTSPECTRA_API_KEY)';
    console.error('[vitals] SMARTSPECTRA_API_KEY not set — running without Presage');
    return;
  }
  const {
    SmartSpectraSDK, PixelFormat: PF, FrameTransform,
    cardioMetrics, breathingMetrics, decodeMetrics,
  } = require('@smartspectra/node-sdk');
  PixelFormat = PF;

  sdk = new SmartSpectraSDK({
    apiKey: key,
    requestedMetrics: [...cardioMetrics, ...breathingMetrics],
  });

  sdk.on('metrics', (buf) => {
    try {
      const m = decodeMetrics(buf);
      const o = typeof m.toObject === 'function' ? m.toObject() : m;
      // Log each distinct payload shape once, so we can see if/when pulse
      // fields ever start arriving.
      const shape = Object.keys(o).sort()
        .map(k => k + (o[k] && typeof o[k] === 'object'
                       ? '(' + Object.keys(o[k]).sort() + ')' : ''))
        .join(' ');
      vitals.lastMetrics = shape;
      if (!seenShapes.has(shape)) {
        seenShapes.add(shape);
        console.log('[vitals] metrics shape:', shape,
                    '| sample:', JSON.stringify(o).slice(0, 800));
      }
      const cardio = o.cardio || {};
      const hr = pickLast(cardio.pulseRate);
      if (hr && hr.value != null) {
        vitals.bpm = Math.round(hr.value);
        vitals.bpmConfidence = hr.confidence ?? null;
        vitals.updated = Date.now();
      }
      const br = o.breathing || {};
      const bb = pickLast(br.rate || br.rateList);
      if (bb && bb.value != null) vitals.breathing = Math.round(bb.value);
    } catch (e) {
      console.error('[vitals] metrics decode failed:', e.message);
    }
  });
  sdk.on('processingStatus', (s) => {
    vitals.status = PROCESSING_NAMES[s] || String(s);
  });
  sdk.on('validationStatus', (code, ts, hint) => {
    vitals.hint = code === 0 ? null : hint;   // 0 = kOk
  });
  sdk.on('error', (code, msg, retryable) => {
    vitals.status = `error ${code}: ${msg}`;
    console.error('[vitals] SmartSpectra error', code, msg, 'retryable=', retryable);
  });

  sdk.useCustomInput(FrameTransform.kNone);
  sdk.start();
  vitals.status = 'running';
  console.log('[vitals] SmartSpectra started (custom input)');
}

try {
  initSdk();
} catch (e) {
  vitals.status = 'sdk unavailable: ' + e.message;
  console.error('[vitals] SDK init failed (run `npm install` in vitals-server/?):', e.message);
}

const server = http.createServer((req, res) => {
  if (req.method === 'POST' && req.url === '/frame') {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      if (sdk) {
        const buf = Buffer.concat(chunks);
        const w = parseInt(req.headers['x-width'], 10);
        const h = parseInt(req.headers['x-height'], 10);
        const ts = BigInt(req.headers['x-ts'] || '0');
        if (w && h && buf.length === w * h * 3) {
          try {
            sdk.sendFrame(buf, w, h, w * 3, PixelFormat.kRGB, ts);
          } catch (e) {
            console.error('[vitals] sendFrame failed:', e.message);
          }
        }
      }
      res.writeHead(204).end();
    });
    return;
  }

  if (req.method === 'GET' && req.url === '/vitals') {
    res.writeHead(200, {
      'Content-Type': 'application/json',
      'Access-Control-Allow-Origin': '*',
    });
    res.end(JSON.stringify(vitals));
    return;
  }

  res.writeHead(404).end();
});

server.listen(PORT, () => console.log(`[vitals] listening on http://localhost:${PORT}`));

process.on('SIGTERM', async () => {
  if (sdk) { try { await sdk.destroy(); } catch (_) {} }
  process.exit(0);
});
