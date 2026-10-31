// BlindSpot's NASA FIRMS relay. On 2026-10-31 FIRMS stopped answering connections from GitHub's
// runners (every attempt timed out) while it still answered from India, so the data jobs ask for
// FIRMS's keyless active-fire files through this function, which runs in Mumbai (bom1), and fall
// back to FIRMS itself when the relay fails. It forwards GET and HEAD for
// /api/firms?path=data/active_fire/<product>/csv/<file>.csv only, with FIRMS's ETag, which the
// job compares to skip unchanged files. The files are public: no key passes through here.
const PATH = /^data\/active_fire\/[a-z0-9.-]+\/csv\/[A-Za-z0-9_]+\.csv$/;
const PASSED = ['etag', 'last-modified', 'content-type'];

module.exports = async (req, res) => {
  res.setHeader('Cache-Control', 'no-store');
  if (req.method !== 'GET' && req.method !== 'HEAD') return res.status(405).json({ error: 'GET or HEAD only' });
  const path = typeof req.query.path === 'string' ? req.query.path : '';
  if (!PATH.test(path)) return res.status(404).json({ error: 'not a FIRMS active-fire file' });
  try {
    const answer = await fetch(`https://firms.modaps.eosdis.nasa.gov/${path}`, {
      method: req.method,
      headers: { 'User-Agent': req.headers['user-agent'] || 'BlindSpot relay' },
      signal: AbortSignal.timeout(25_000),
    });
    res.status(answer.status);
    for (const name of PASSED) {
      const value = answer.headers.get(name);
      if (value) res.setHeader(name, value);
    }
    if (req.method === 'HEAD') return res.end();
    return res.send(Buffer.from(await answer.arrayBuffer()));
  } catch (err) {
    return res.status(502).json({ error: `FIRMS did not answer the relay: ${(err && err.name) || 'error'}` });
  }
};
