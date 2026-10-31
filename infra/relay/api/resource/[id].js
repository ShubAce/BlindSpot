// BlindSpot's data.gov.in relay. data.gov.in refuses connections from the GitHub runners that run
// BlindSpot's jobs (US), so the air job reaches it through this function, which runs in Mumbai
// (bom1, infra/deploy_vercel_relay.py). It forwards GET /api/resource/<id> to
// https://api.data.gov.in/resource/<id> and nothing else. The caller sends its own data.gov.in key
// in X-Api-Key, which keeps it out of logged URLs; the relay holds no key of its own.
const ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

module.exports = async (req, res) => {
  res.setHeader('Cache-Control', 'no-store');
  if (req.method !== 'GET') return res.status(405).json({ error: 'GET only' });
  const incoming = new URL(req.url, 'https://relay.invalid');
  const id = typeof req.query.id === 'string' ? req.query.id : incoming.pathname.split('/').pop();
  const key = req.headers['x-api-key'];
  if (!ID.test(id || '')) return res.status(404).json({ error: 'unknown resource' });
  if (typeof key !== 'string' || !key) return res.status(401).json({ error: 'X-Api-Key is required' });

  const upstream = new URL(`https://api.data.gov.in/resource/${id}`);
  for (const [name, value] of incoming.searchParams) {
    if (name !== 'id' && name.toLowerCase() !== 'api-key') upstream.searchParams.append(name, value);
  }
  upstream.searchParams.set('api-key', key);
  try {
    const answer = await fetch(upstream, {
      headers: { 'User-Agent': req.headers['user-agent'] || 'BlindSpot relay', Accept: 'application/json' },
      signal: AbortSignal.timeout(25_000),
    });
    res.status(answer.status);
    res.setHeader('Content-Type', answer.headers.get('content-type') || 'application/json');
    return res.send(Buffer.from(await answer.arrayBuffer()));
  } catch (err) {
    return res.status(502).json({ error: `data.gov.in did not answer the relay: ${(err && err.name) || 'error'}` });
  }
};
