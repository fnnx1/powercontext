import express from 'express';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { SeekDB } from './seekdb-js/index.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const app = express();
app.use(express.json({ limit: '8mb' }));
app.use(express.static(path.join(__dirname, 'public')));

const db = new SeekDB({
  host: process.env.SEEKDB_HOST || '127.0.0.1',
  port: Number(process.env.SEEKDB_PORT || 2881),
  user: process.env.SEEKDB_USER || 'root',
  password: process.env.SEEKDB_PASSWORD || '',
  database: process.env.SEEKDB_DATABASE || 'stepfun_kb',
});

const DIM = 384;

async function ensureSchema() {
  await db.query(`CREATE TABLE IF NOT EXISTS docs (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    title VARCHAR(512) NOT NULL DEFAULT '',
    body MEDIUMTEXT NOT NULL,
    searchable_text MEDIUMTEXT NOT NULL,
    embedding VECTOR(${DIM}) NOT NULL,
    source VARCHAR(512) NOT NULL DEFAULT 'local',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
  )`);
  const idx = await db.query(
    "SELECT COUNT(*) AS n FROM information_schema.statistics WHERE table_schema = DATABASE() AND table_name = 'docs' AND index_name = 'ft_docs'"
  );
  if (Number(idx[0]?.n || 0) === 0) {
    await db.query('CREATE FULLTEXT INDEX ft_docs ON docs(searchable_text) WITH PARSER ik');
  }
}

function vectorParam(values) {
  if (!Array.isArray(values) || values.length !== DIM || values.some((v) => !Number.isFinite(Number(v)))) {
    throw new Error(`embedding must contain ${DIM} finite numbers`);
  }
  return `[${values.map((v) => Number(v).toFixed(7)).join(',')}]`;
}

app.get('/api/health', async (_req, res) => {
  try {
    const rows = await db.query('SELECT COUNT(*) AS n FROM docs');
    res.json({ ok: true, documents: Number(rows[0].n), dimension: DIM });
  } catch (error) {
    res.status(500).json({ ok: false, error: String(error.message || error) });
  }
});

app.post('/api/docs', async (req, res) => {
  try {
    const docs = Array.isArray(req.body?.documents) ? req.body.documents : [];
    if (!docs.length) return res.status(400).json({ error: 'documents is required' });
    const values = docs.map((doc) => {
      const title = String(doc.title || '').slice(0, 500);
      const body = String(doc.text || doc.body || '');
      if (!body.trim()) throw new Error('document text is empty');
      return [title, body, `${title}\n${body}`, vectorParam(doc.embedding), String(doc.source || 'local')];
    });
    await db.query(
      'INSERT INTO docs (title, body, searchable_text, embedding, source) VALUES ?',
      [values]
    );
    res.json({ ok: true, inserted: values.length });
  } catch (error) {
    res.status(500).json({ error: String(error.message || error) });
  }
});

app.post('/api/search', async (req, res) => {
  try {
    const query = String(req.body?.query || '').trim();
    const embedding = vectorParam(req.body?.embedding);
    const limit = Math.max(1, Math.min(50, Number(req.body?.limit || 10)));
    const candidateLimit = Math.max(limit * 3, 30);
    // One seekdb statement performs both the FTS and ANN candidate scans.
    // The outer aggregation is still executed by seekdb (UNION ALL + GROUP BY).
    const rows = await db.query(
      `SELECT id, title, body, source,
              MAX(fts_score) AS fts_score,
              MIN(distance) AS distance,
              MAX(fts_score) / (1 + MIN(distance)) AS hybrid_score
         FROM (
           (SELECT id, title, body, source,
                   MATCH(searchable_text) AGAINST (? IN NATURAL LANGUAGE MODE) AS fts_score,
                   NULL AS distance
              FROM docs
             WHERE MATCH(searchable_text) AGAINST (? IN NATURAL LANGUAGE MODE)
             ORDER BY fts_score DESC
             LIMIT ?)
           UNION ALL
           (SELECT id, title, body, source,
                   NULL AS fts_score,
                   l2_distance(embedding, ?) AS distance
              FROM docs
             ORDER BY distance APPROXIMATE
             LIMIT ?)
         ) candidates
        GROUP BY id, title, body, source
        ORDER BY hybrid_score DESC, fts_score DESC, distance ASC
        LIMIT ?`,
      [query, query, candidateLimit, embedding, candidateLimit, limit]
    );
    res.json({ ok: true, query, results: rows });
  } catch (error) {
    res.status(500).json({ error: String(error.message || error) });
  }
});

const port = Number(process.env.PORT || 7860);
ensureSchema()
  .then(() => app.listen(port, () => console.log(`StepFun mini KB: http://127.0.0.1:${port}`)))
  .catch((error) => {
    console.error('failed to initialize seekdb schema:', error);
    process.exit(1);
  });
