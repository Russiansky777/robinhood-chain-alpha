from engine import *
import sqlite3
con=sqlite3.connect(ROOT/'events.sqlite');con.executescript('CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY); CREATE TABLE IF NOT EXISTS events(sig TEXT, ord INT,pool TEXT,slot INT,idx INT,time INT,data TEXT,PRIMARY KEY(sig,ord)); CREATE INDEX IF NOT EXISTS pool_time ON events(pool,time); CREATE TABLE IF NOT EXISTS blocks(slot INTEGER PRIMARY KEY,time INTEGER,txcount INTEGER);')
files=[]
for folder in ['solana_three_check','solana_wow_check','buyer_100']:
 for f in (ROOT.parent/folder).glob('block_*.json'):
  if f.stem[6:].isdigit():files.append(f)
for n,f in enumerate(files):
 if con.execute('SELECT 1 FROM files WHERE path=?',(str(f),)).fetchone():continue
 try:b=json.loads(f.read_text())['result']
 except Exception:continue
 if not b or 'transactions' not in b:continue
 slot=int(f.stem[6:]);con.execute('INSERT OR REPLACE INTO blocks VALUES(?,?,?)',(slot,b['blockTime'],len(b['transactions'])))
 for idx,r in enumerate(b['transactions']):
  for j,e in enumerate(decode_tx(r)):
   con.execute('INSERT OR REPLACE INTO events VALUES(?,?,?,?,?,?,?)',(r['transaction']['signatures'][0],j,e['pool'],slot,idx,b['blockTime'],json.dumps(e)))
 con.execute('INSERT INTO files VALUES(?)',(str(f),));con.commit()
 print(n+1,len(files),slot,flush=True)
print('events',con.execute('SELECT COUNT(*) FROM events').fetchone()[0]);con.close()
