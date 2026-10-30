SESSIONS_SQL = """
WITH main AS (
  SELECT MAX(id) AS id, session_id FROM source WHERE agent_id IS NULL GROUP BY session_id
),
span AS (
  SELECT s.session_id,
         MIN(t.first_ts) AS first_ts,
         MAX(t.last_ts)  AS last_ts,
         COUNT(DISTINCT s.agent_id) AS subagents
    FROM source s JOIN transcript t ON t.source_id = s.id
   GROUP BY s.session_id
)
SELECT m.session_id, t.cwd, t.custom_title,
       (SELECT d.body FROM doc d
         WHERE d.session_id = m.session_id AND d.role = :desktop
         ORDER BY d.mtime_ns DESC LIMIT 1) AS desktop,
       t.ai_title,
       p.source_id AS prompt_source, p.line_off AS prompt_off, p.block AS prompt_block,
       span.first_ts, span.last_ts,
       (SELECT COUNT(*) FROM entry e WHERE e.source_id = m.id AND e.kind = :prompt) AS prompts,
       span.subagents, s.path
  FROM main m
  JOIN source s ON s.id = m.id
  JOIN transcript t ON t.source_id = m.id
  JOIN span ON span.session_id = m.session_id
  LEFT JOIN entry p ON p.id = (SELECT e.id FROM entry e
                                WHERE e.source_id = m.id AND e.kind = :prompt
                                ORDER BY e.line_off, e.block LIMIT 1)
 WHERE (:project IS NULL OR instr(casefold(t.cwd), casefold(:project)) > 0)
   AND (:since IS NULL OR span.last_ts >= :since)
   AND (:until IS NULL OR span.first_ts < :until)
   AND (:prefix IS NULL OR m.session_id GLOB :prefix || '*')
   AND (:ids IS NULL OR m.session_id IN (SELECT value FROM json_each(:ids)))
 ORDER BY span.last_ts DESC, m.session_id
 LIMIT :limit
"""

SEARCH_SQL = """
WITH term (i, expr) AS (SELECT key, value FROM json_each(:exprs)),
hit AS (
  SELECT t.i AS term, f.rowid AS entry_id, f.rank AS rank, s.session_id
    FROM term t
    JOIN entry_fts f ON f.entry_fts MATCH t.expr
    JOIN entry e ON e.id = f.rowid
    JOIN source s ON s.id = e.source_id
   WHERE e.kind IN (SELECT value FROM json_each(:kinds))
     AND (:since IS NULL OR e.ts >= :since)
     AND (:until IS NULL OR e.ts < :until)
),
matched AS (
  SELECT session_id, SUM(rank) AS score, COUNT(DISTINCT entry_id) AS hits
    FROM hit
   GROUP BY session_id
  HAVING COUNT(DISTINCT term) = (SELECT COUNT(*) FROM term)
),
main AS (
  SELECT MAX(id) AS id, session_id FROM source WHERE agent_id IS NULL GROUP BY session_id
),
kept AS (
  SELECT m.session_id, m.score, m.hits
    FROM matched m
    JOIN main ON main.session_id = m.session_id
    JOIN transcript t ON t.source_id = main.id
   WHERE :project IS NULL OR instr(casefold(t.cwd), casefold(:project)) > 0
   ORDER BY m.score, m.session_id
   LIMIT :limit
),
best AS (
  SELECT session_id, entry_id,
         ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY COUNT(DISTINCT term) DESC, SUM(rank), entry_id) AS place
    FROM hit
   WHERE session_id IN (SELECT session_id FROM kept)
   GROUP BY session_id, entry_id
)
SELECT k.session_id, k.hits, e.source_id, e.line_off, e.block, e.kind, e.ts, s.agent_id
  FROM kept k
  JOIN best b ON b.session_id = k.session_id AND b.place = 1
  JOIN entry e ON e.id = b.entry_id
  JOIN source s ON s.id = e.source_id
 ORDER BY k.score, k.session_id
"""
