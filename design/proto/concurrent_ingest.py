import os, sys, time, json, random, sqlite3, multiprocessing as mp, compression.zstd as z
HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, 'p.db'); T = os.path.join(HERE, 't.jsonl')
SCHEMA = open(sys.argv[1]).read().split('-- @projection')[0]

def connect():
    db = sqlite3.connect(DB, autocommit=True, timeout=60)
    db.execute('PRAGMA journal_mode=WAL'); db.execute('PRAGMA synchronous=NORMAL')
    return db

def ingest(db, path):
    db.execute('BEGIN IMMEDIATE')
    try:
        src = db.execute('SELECT id, head FROM source WHERE path=? ORDER BY generation DESC LIMIT 1', (path,)).fetchone()
        fd = os.open(path, os.O_RDONLY)
        try:
            size = os.fstat(fd).st_size
            cur = db.execute('SELECT byte_end FROM segment WHERE source_id=? ORDER BY byte_start DESC LIMIT 1', (src[0],)).fetchone()[0] if src else 0
            buf = os.pread(fd, size - cur, cur)
        finally: os.close(fd)
        take = buf.rfind(b'\n') + 1
        if take:
            body = buf[:take]
            if src is None:
                sid = db.execute('INSERT INTO source(path,session_id,head,first_seen) VALUES(?,?,?,0) RETURNING id', (path, 's', body[:1024])).fetchone()[0]
            else: sid = src[0]
            s = 0
            while s < len(body):
                e = body.find(b'\n', s + 4096); e = len(body) if e < 0 else e + 1
                db.execute('INSERT INTO segment VALUES(?,?,?,?,0)', (sid, cur + s, cur + e, z.compress(body[s:e]))); s = e
        db.execute('COMMIT'); return take
    except BaseException:
        db.execute('ROLLBACK'); raise

def writer(stop_at):
    rnd = random.Random(7)
    with open(T, 'ab', buffering=0) as f:
        i = 0
        while time.time() < stop_at:
            line = json.dumps({'type': 'user', 'i': i, 'pad': 'x' * rnd.randint(10, 20000)}).encode() + b'\n'
            cut = rnd.randint(1, len(line) - 1)
            f.write(line[:cut]); time.sleep(rnd.random() / 500); f.write(line[cut:]); i += 1
    return i

def sweeper(stop_at, q):
    db = connect(); n = 0; errs = 0
    while time.time() < stop_at + 0.3:
        try: n += ingest(db, T)
        except sqlite3.IntegrityError as e: errs += 1
    q.put((n, errs))

if __name__ == '__main__':
    for p in (DB, DB + '-wal', DB + '-shm', T):
        if os.path.exists(p): os.remove(p)
    open(T, 'wb').close()
    db = connect(); db.executescript(SCHEMA)
    stop = time.time() + 3
    q = mp.Queue(); ps = [mp.Process(target=sweeper, args=(stop, q)) for _ in range(4)]
    for p in ps: p.start()
    lines = writer(stop)
    for p in ps: p.join()
    res = [q.get() for _ in ps]
    ingest(db, T)
    data = open(T, 'rb').read()
    arch = b''.join(z.decompress(r[0]) for r in db.execute('SELECT zstd FROM segment ORDER BY byte_start'))
    segs = db.execute('SELECT count(*) FROM segment').fetchone()[0]
    print('lines written', lines, 'file bytes', len(data), 'segments', segs)
    print('per-sweeper (bytes archived, integrity errors)', res, 'sum', sum(r[0] for r in res))
    print('archive == file:', arch == data)
    try:
        db.execute('BEGIN IMMEDIATE'); db.execute("INSERT INTO segment VALUES(1,0,10,x'00',0)")
    except sqlite3.IntegrityError as e: print('replayed segment rejected:', e); db.execute('ROLLBACK')
