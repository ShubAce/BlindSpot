// Runs BlindSpot's two relay functions (infra/relay/api, written for Vercel) as one small Node
// server on the AWS box in Mumbai. Caddy serves it at https://api.blindspotin.tech/relay/... and
// strips the /relay prefix, so the paths here are the ones the Vercel deployment had:
//   GET  /api/resource/<id>          -> api/resource/[id].js (data.gov.in)
//   GET  /api/firms?path=...         -> api/firms.js (FIRMS active-fire files; HEAD too)
// The handlers are used unchanged; this file only adds the few helpers Vercel gives them.
const http = require('node:http');
const path = require('node:path');

const HANDLERS = path.join(__dirname, 'api');
const firms = require(path.join(HANDLERS, 'firms.js'));
const resource = require(path.join(HANDLERS, 'resource', '[id].js'));

function vercelish(req, res, query) {
  req.query = query;
  res.status = (code) => { res.statusCode = code; return res; };
  res.json = (body) => {
    res.setHeader('Content-Type', 'application/json; charset=utf-8');
    res.end(JSON.stringify(body));
    return res;
  };
  res.send = (body) => { res.end(body); return res; };
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://relay.local');
  const query = Object.fromEntries(url.searchParams);
  try {
    if (url.pathname === '/api/firms') {
      vercelish(req, res, query);
      return await firms(req, res);
    }
    const match = url.pathname.match(/^\/api\/resource\/([^/]+)$/);
    if (match) {
      vercelish(req, res, { ...query, id: decodeURIComponent(match[1]) });
      return await resource(req, res);
    }
    res.writeHead(404, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'not found' }));
  } catch (err) {
    if (!res.headersSent) res.writeHead(500, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: `relay failed: ${(err && err.name) || 'error'}` }));
  }
});

server.listen(Number(process.env.PORT || 8090), process.env.HOST || '127.0.0.1');
