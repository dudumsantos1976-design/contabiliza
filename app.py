from __future__ import annotations
import base64, hashlib, hmac, json, mimetypes, os, secrets, sqlite3, threading, time, urllib.parse, webbrowser, sys
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# Caminhos compatíveis com execução em código-fonte e com PyInstaller.
if getattr(sys, 'frozen', False):
    # Arquivos empacotados (HTML/CSS/JS) são lidos do diretório temporário do PyInstaller.
    BUNDLE_ROOT = Path(getattr(sys, '_MEIPASS', Path(sys.executable).resolve().parent))
    # Dados persistentes ficam fora do executável, no perfil do usuário do Windows.
    data_base = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'ContabilizaPlus'
else:
    BUNDLE_ROOT = Path(__file__).resolve().parent
    data_base = BUNDLE_ROOT

STATIC = BUNDLE_ROOT / 'static'
# Em hospedagem na web, o disco pode ser efêmero; DATA_DIR pode ser sobrescrito
# por variável de ambiente (ex.: um disco persistente montado pelo provedor).
data_base = Path(os.environ.get('DATA_DIR', data_base))
data_base.mkdir(parents=True, exist_ok=True)
DB_PATH = data_base / 'contabiliza_plus.db'
# 0.0.0.0 é necessário em serviços de hospedagem (Render, Railway, etc.).
# A porta também deve vir da variável de ambiente PORT quando o provedor a define.
HOST = os.environ.get('HOST', '0.0.0.0')
PORT = int(os.environ.get('PORT', 8765))

# ---------------- Database ----------------
def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    return conn

def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 220_000)
    return f"pbkdf2_sha256$220000${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"

def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, rounds, salt_b64, digest_b64 = encoded.split('$', 3)
        if scheme != 'pbkdf2_sha256': return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, int(rounds))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False

def init_db(reset=False):
    if reset and DB_PATH.exists(): DB_PATH.unlink()
    con = db()
    con.executescript('''
    CREATE TABLE IF NOT EXISTS empresas (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      cnpj TEXT NOT NULL UNIQUE,
      razao_social TEXT NOT NULL,
      nome_fantasia TEXT NOT NULL,
      regime_tributario TEXT NOT NULL,
      endereco TEXT,
      inscricao_estadual TEXT,
      inscricao_municipal TEXT,
      email_contato TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS usuarios (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      empresa_id INTEGER NOT NULL REFERENCES empresas(id),
      nome TEXT NOT NULL,
      email TEXT NOT NULL UNIQUE,
      senha_hash TEXT NOT NULL,
      ativo INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS sessoes (
      token TEXT PRIMARY KEY,
      usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
      criado_em TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS pendencias (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      empresa_id INTEGER NOT NULL REFERENCES empresas(id),
      tipo TEXT NOT NULL,
      descricao TEXT NOT NULL,
      valor REAL NOT NULL CHECK(valor > 0),
      data_vencimento TEXT NOT NULL,
      status TEXT NOT NULL CHECK(status IN ('PENDENTE','VENCIDA','PAGA')),
      data_pagamento TEXT,
      criado_em TEXT NOT NULL,
      atualizado_em TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS notas_fiscais (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      empresa_id INTEGER NOT NULL REFERENCES empresas(id),
      numero INTEGER NOT NULL,
      cliente_tomador TEXT NOT NULL,
      descricao TEXT NOT NULL,
      valor REAL NOT NULL CHECK(valor > 0),
      data_emissao TEXT NOT NULL,
      status TEXT NOT NULL CHECK(status IN ('EMITIDA','CANCELADA')),
      data_cancelamento TEXT,
      UNIQUE(empresa_id, numero)
    );
    CREATE TABLE IF NOT EXISTS lancamentos (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      empresa_id INTEGER NOT NULL REFERENCES empresas(id),
      tipo TEXT NOT NULL CHECK(tipo IN ('GANHO','CUSTO')),
      descricao TEXT NOT NULL,
      categoria TEXT NOT NULL,
      valor REAL NOT NULL CHECK(valor > 0),
      data TEXT NOT NULL,
      criado_em TEXT NOT NULL
    );
    ''')
    if con.execute('SELECT COUNT(*) c FROM empresas').fetchone()['c'] == 0:
        cur = con.execute('''INSERT INTO empresas(cnpj,razao_social,nome_fantasia,regime_tributario,endereco,inscricao_estadual,inscricao_municipal,email_contato)
            VALUES(?,?,?,?,?,?,?,?)''', (
            '12.345.678/0001-90','Alfa Contabilidade e Serviços LTDA.','Alfa Contábil','Simples Nacional',
            'Rua das Empresas, 100 - Centro - São Paulo/SP - 01000-000','110.042.490.114','8.765.432-1','financeiro@empresa.com.br'))
        empresa_id = cur.lastrowid
        con.execute('INSERT INTO usuarios(empresa_id,nome,email,senha_hash,ativo) VALUES(?,?,?,?,1)',
                    (empresa_id,'Responsável Financeiro','financeiro@empresa.com.br',hash_password('Contabiliza@2026')))
        hoje = date.today()
        pend = [
            ('ISS','ISS mensal',1250.00,(hoje+timedelta(days=7)).isoformat(),'PENDENTE',None),
            ('ICMS','ICMS da competência atual',980.50,(hoje-timedelta(days=3)).isoformat(),'VENCIDA',None),
            ('Taxa de licenciamento','Licenciamento municipal anual',430.00,(hoje+timedelta(days=25)).isoformat(),'PENDENTE',None),
            ('DAS','DAS do mês anterior',640.00,(hoje-timedelta(days=30)).isoformat(),'PAGA',(hoje-timedelta(days=32)).isoformat()),
        ]
        now=datetime.now().isoformat(timespec='seconds')
        for tipo,desc,valor,venc,status,pag in pend:
            con.execute('INSERT INTO pendencias(empresa_id,tipo,descricao,valor,data_vencimento,status,data_pagamento,criado_em,atualizado_em) VALUES(?,?,?,?,?,?,?,?,?)',
                        (empresa_id,tipo,desc,valor,venc,status,pag,now,now))
        notas=[
            (1001,'Empresa Beta LTDA','Serviços de consultoria contábil',2600.00,hoje-timedelta(days=3),'EMITIDA',None),
            (1002,'Comercial Gama ME','Assessoria fiscal mensal',1800.00,hoje-timedelta(days=9),'EMITIDA',None),
            (1003,'Delta Serviços','Regularização cadastral',850.00,hoje-timedelta(days=15),'CANCELADA',hoje-timedelta(days=14)),
        ]
        for n,cli,desc,val,dt,st,cancel in notas:
            con.execute('INSERT INTO notas_fiscais(empresa_id,numero,cliente_tomador,descricao,valor,data_emissao,status,data_cancelamento) VALUES(?,?,?,?,?,?,?,?)',
                        (empresa_id,n,cli,desc,val,dt.isoformat(),st,cancel.isoformat() if cancel else None))
        lanc=[
            ('GANHO','Mensalidade cliente Beta','Serviços',2600,hoje-timedelta(days=3)),
            ('GANHO','Mensalidade cliente Gama','Serviços',1800,hoje-timedelta(days=9)),
            ('CUSTO','Material de escritório','Materiais',420,hoje-timedelta(days=6)),
            ('CUSTO','Impostos do período','Impostos',700,hoje-timedelta(days=12)),
            ('CUSTO','Licença de software','Tecnologia',250,hoje-timedelta(days=2)),
        ]
        for tp,desc,cat,val,dt in lanc:
            con.execute('INSERT INTO lancamentos(empresa_id,tipo,descricao,categoria,valor,data,criado_em) VALUES(?,?,?,?,?,?,?)',
                        (empresa_id,tp,desc,cat,val,dt.isoformat(),now))
    con.commit(); con.close()

def update_overdue(con, empresa_id):
    con.execute("UPDATE pendencias SET status='VENCIDA', atualizado_em=? WHERE empresa_id=? AND status='PENDENTE' AND data_vencimento < ?",
                (datetime.now().isoformat(timespec='seconds'), empresa_id, date.today().isoformat()))

# ---------------- Helpers ----------------
def json_body(handler):
    try:
        n=int(handler.headers.get('Content-Length','0')); raw=handler.rfile.read(n) if n else b'{}'
        return json.loads(raw.decode('utf-8'))
    except Exception:
        raise ValueError('JSON inválido.')

def money(v): return round(float(v),2)

def require_text(data,key,min_len=1,max_len=200):
    v=str(data.get(key,'')).strip()
    if len(v)<min_len or len(v)>max_len: raise ValueError(f'{key}: tamanho inválido.')
    return v

def require_date(data,key):
    v=str(data.get(key,'')).strip()
    try: date.fromisoformat(v)
    except: raise ValueError(f'{key}: data inválida.')
    return v

def require_value(data,key='valor'):
    try: v=float(data.get(key))
    except: raise ValueError(f'{key}: valor inválido.')
    if v<=0: raise ValueError(f'{key}: deve ser maior que zero.')
    return round(v,2)

def period_bounds(periodo, referencia=None):
    ref = date.fromisoformat(referencia) if referencia else date.today()
    if periodo=='mes':
        start=ref.replace(day=1)
        end=(start.replace(day=28)+timedelta(days=4)).replace(day=1)-timedelta(days=1)
    elif periodo=='trimestre':
        m=((ref.month-1)//3)*3+1; start=date(ref.year,m,1)
        nm=m+3
        end=(date(ref.year+1,1,1) if nm>12 else date(ref.year,nm,1))-timedelta(days=1)
    elif periodo=='ano': start=date(ref.year,1,1); end=date(ref.year,12,31)
    else: raise ValueError('Período deve ser mes, trimestre ou ano.')
    return start.isoformat(),end.isoformat()

# ---------------- HTTP ----------------
class Handler(BaseHTTPRequestHandler):
    server_version='ContabilizaPlus/1.0'
    def log_message(self, fmt, *args): print('[HTTP]', fmt%args)
    def _send(self,status,body=None,ctype='application/json; charset=utf-8'):
        self.send_response(status); self.send_header('Content-Type',ctype); self.send_header('Cache-Control','no-store'); self.end_headers()
        if body is not None:
            if isinstance(body,(dict,list)): body=json.dumps(body,ensure_ascii=False).encode('utf-8')
            elif isinstance(body,str): body=body.encode('utf-8')
            self.wfile.write(body)
    def ok(self,obj): self._send(200,obj)
    def error(self,status,msg): self._send(status,{'erro':msg})
    def auth(self):
        h=self.headers.get('Authorization','')
        if not h.startswith('Bearer '): return None
        tok=h[7:]
        con=db(); row=con.execute('''SELECT u.id usuario_id,u.empresa_id,u.nome,u.email,u.ativo,e.* FROM sessoes s JOIN usuarios u ON u.id=s.usuario_id JOIN empresas e ON e.id=u.empresa_id WHERE s.token=?''',(tok,)).fetchone(); con.close()
        if not row or not row['ativo']: return None
        return dict(row)|{'token':tok}
    def do_OPTIONS(self):
        self.send_response(204); self.send_header('Access-Control-Allow-Origin','*'); self.send_header('Access-Control-Allow-Headers','Content-Type,Authorization'); self.send_header('Access-Control-Allow-Methods','GET,POST,PUT,DELETE,OPTIONS'); self.end_headers()
    def do_GET(self):
        try: self.route('GET')
        except ValueError as e: self.error(400,str(e))
        except Exception as e: print('ERR',repr(e)); self.error(500,'Erro interno do servidor.')
    def do_POST(self):
        try: self.route('POST')
        except ValueError as e: self.error(400,str(e))
        except Exception as e: print('ERR',repr(e)); self.error(500,'Erro interno do servidor.')
    def do_PUT(self):
        try: self.route('PUT')
        except ValueError as e: self.error(400,str(e))
        except Exception as e: print('ERR',repr(e)); self.error(500,'Erro interno do servidor.')
    def do_DELETE(self):
        try: self.route('DELETE')
        except ValueError as e: self.error(400,str(e))
        except Exception as e: print('ERR',repr(e)); self.error(500,'Erro interno do servidor.')
    def route(self, method):
        u=urllib.parse.urlsplit(self.path); path=u.path; q=urllib.parse.parse_qs(u.query)
        if path.startswith('/api/'):
            if path=='/api/login' and method=='POST': return self.login()
            user=self.auth()
            if not user: return self.error(401,'Sessão inválida. Faça login novamente.')
            if path=='/api/logout' and method=='POST':
                con=db(); con.execute('DELETE FROM sessoes WHERE token=?',(user['token'],)); con.commit(); con.close(); return self.ok({'ok':True})
            if path=='/api/me' and method=='GET': return self.ok({'nome':user['nome'],'email':user['email'],'empresa':{k:user[k] for k in ['cnpj','razao_social','nome_fantasia','regime_tributario','endereco','inscricao_estadual','inscricao_municipal','email_contato']}})
            if path=='/api/dashboard' and method=='GET': return self.dashboard(user)
            if path=='/api/pendencias': return self.pendencias(method,user,q)
            if path.startswith('/api/pendencias/'):
                parts=path.split('/'); pid=int(parts[3]); action=parts[4] if len(parts)>4 else None
                return self.pendencia_item(method,user,pid,action)
            if path=='/api/notas': return self.notas(method,user,q)
            if path.startswith('/api/notas/'):
                parts=path.split('/'); nid=int(parts[3]); action=parts[4] if len(parts)>4 else None
                return self.nota_item(method,user,nid,action)
            if path=='/api/lancamentos': return self.lancamentos(method,user,q)
            if path.startswith('/api/lancamentos/'):
                lid=int(path.split('/')[3]); return self.lancamento_item(method,user,lid)
            if path=='/api/indicadores' and method=='GET': return self.indicadores(user,q)
            return self.error(404,'Endpoint não encontrado.')
        return self.static(path)
    def login(self):
        data=json_body(self); email=require_text(data,'email',5,160).lower(); senha=require_text(data,'senha',1,200)
        con=db(); row=con.execute('SELECT * FROM usuarios WHERE lower(email)=?',(email,)).fetchone()
        if not row or not row['ativo'] or not verify_password(senha,row['senha_hash']): con.close(); return self.error(401,'E-mail ou senha inválidos.')
        token=secrets.token_urlsafe(32); con.execute('INSERT INTO sessoes(token,usuario_id,criado_em) VALUES(?,?,?)',(token,row['id'],datetime.now().isoformat(timespec='seconds'))); con.commit(); con.close(); return self.ok({'token':token})
    def dashboard(self,user):
        con=db(); update_overdue(con,user['empresa_id']); con.commit()
        rows=con.execute("SELECT status,valor FROM pendencias WHERE empresa_id=? AND status<>'PAGA'",(user['empresa_id'],)).fetchall()
        notas=con.execute('SELECT numero,data_emissao,valor,status,cliente_tomador FROM notas_fiscais WHERE empresa_id=? ORDER BY data_emissao DESC,id DESC LIMIT 5',(user['empresa_id'],)).fetchall(); con.close()
        return self.ok({'empresa':{k:user[k] for k in ['cnpj','razao_social','nome_fantasia','regime_tributario']},'pendencias':{'quantidade':len(rows),'valor_total':money(sum(r['valor'] for r in rows)),'vencidas':sum(r['status']=='VENCIDA' for r in rows)},'ultimas_notas':[dict(r) for r in notas]})
    def pendencias(self,method,user,q):
        con=db(); update_overdue(con,user['empresa_id']); con.commit()
        if method=='GET':
            status=(q.get('status') or [''])[0]; tipo=(q.get('tipo') or [''])[0]
            sql='SELECT * FROM pendencias WHERE empresa_id=?'; args=[user['empresa_id']]
            if status: sql+=' AND status=?'; args.append(status)
            if tipo: sql+=' AND lower(tipo) LIKE ?'; args.append('%'+tipo.lower()+'%')
            sql+=' ORDER BY data_vencimento ASC,id DESC'; rows=con.execute(sql,args).fetchall(); con.close(); return self.ok([dict(r) for r in rows])
        if method=='POST':
            d=json_body(self); tipo=require_text(d,'tipo',2,80); desc=require_text(d,'descricao',3,300); val=require_value(d); venc=require_date(d,'data_vencimento'); now=datetime.now().isoformat(timespec='seconds'); st='VENCIDA' if venc<date.today().isoformat() else 'PENDENTE'
            cur=con.execute('INSERT INTO pendencias(empresa_id,tipo,descricao,valor,data_vencimento,status,data_pagamento,criado_em,atualizado_em) VALUES(?,?,?,?,?,?,NULL,?,?)',(user['empresa_id'],tipo,desc,val,venc,st,now,now)); con.commit(); row=con.execute('SELECT * FROM pendencias WHERE id=?',(cur.lastrowid,)).fetchone(); con.close(); return self._send(201,dict(row))
        con.close(); return self.error(405,'Método não permitido.')
    def pendencia_item(self,method,user,pid,action):
        con=db(); row=con.execute('SELECT * FROM pendencias WHERE id=? AND empresa_id=?',(pid,user['empresa_id'])).fetchone()
        if not row: con.close(); return self.error(404,'Pendência não encontrada.')
        if action=='pagar' and method=='POST':
            hoje=date.today().isoformat(); now=datetime.now().isoformat(timespec='seconds'); con.execute("UPDATE pendencias SET status='PAGA',data_pagamento=?,atualizado_em=? WHERE id=?",(hoje,now,pid)); con.commit(); r=con.execute('SELECT * FROM pendencias WHERE id=?',(pid,)).fetchone(); con.close(); return self.ok(dict(r))
        if method=='PUT':
            d=json_body(self); tipo=require_text(d,'tipo',2,80); desc=require_text(d,'descricao',3,300); val=require_value(d); venc=require_date(d,'data_vencimento'); now=datetime.now().isoformat(timespec='seconds')
            st=row['status']; pag=row['data_pagamento']
            if st!='PAGA': st='VENCIDA' if venc<date.today().isoformat() else 'PENDENTE'; pag=None
            con.execute('UPDATE pendencias SET tipo=?,descricao=?,valor=?,data_vencimento=?,status=?,data_pagamento=?,atualizado_em=? WHERE id=?',(tipo,desc,val,venc,st,pag,now,pid)); con.commit(); r=con.execute('SELECT * FROM pendencias WHERE id=?',(pid,)).fetchone(); con.close(); return self.ok(dict(r))
        if method=='DELETE': con.execute('DELETE FROM pendencias WHERE id=?',(pid,)); con.commit(); con.close(); return self.ok({'ok':True})
        con.close(); return self.error(405,'Método/ação não permitido.')
    def notas(self,method,user,q):
        con=db()
        if method=='GET':
            sql='SELECT * FROM notas_fiscais WHERE empresa_id=?'; args=[user['empresa_id']]
            di=(q.get('inicio') or [''])[0]; df=(q.get('fim') or [''])[0]; cli=(q.get('cliente') or [''])[0]; st=(q.get('status') or [''])[0]
            if di: date.fromisoformat(di); sql+=' AND data_emissao>=?'; args.append(di)
            if df: date.fromisoformat(df); sql+=' AND data_emissao<=?'; args.append(df)
            if cli: sql+=' AND lower(cliente_tomador) LIKE ?'; args.append('%'+cli.lower()+'%')
            if st: sql+=' AND status=?'; args.append(st)
            sql+=' ORDER BY data_emissao DESC,id DESC'; rows=con.execute(sql,args).fetchall(); con.close(); return self.ok([dict(r) for r in rows])
        if method=='POST':
            d=json_body(self); cli=require_text(d,'cliente_tomador',2,160); desc=require_text(d,'descricao',3,500); val=require_value(d); dt=require_date(d,'data_emissao')
            n=con.execute('SELECT COALESCE(MAX(numero),1000)+1 n FROM notas_fiscais WHERE empresa_id=?',(user['empresa_id'],)).fetchone()['n']
            cur=con.execute("INSERT INTO notas_fiscais(empresa_id,numero,cliente_tomador,descricao,valor,data_emissao,status) VALUES(?,?,?,?,?,?,'EMITIDA')",(user['empresa_id'],n,cli,desc,val,dt)); con.commit(); r=con.execute('SELECT * FROM notas_fiscais WHERE id=?',(cur.lastrowid,)).fetchone(); con.close(); return self._send(201,dict(r))
        con.close(); return self.error(405,'Método não permitido.')
    def nota_item(self,method,user,nid,action):
        con=db(); row=con.execute('SELECT * FROM notas_fiscais WHERE id=? AND empresa_id=?',(nid,user['empresa_id'])).fetchone()
        if not row: con.close(); return self.error(404,'Nota não encontrada.')
        if action=='cancelar' and method=='POST':
            if row['status']=='CANCELADA': con.close(); return self.error(400,'Nota já está cancelada.')
            con.execute("UPDATE notas_fiscais SET status='CANCELADA',data_cancelamento=? WHERE id=?",(date.today().isoformat(),nid)); con.commit(); r=con.execute('SELECT * FROM notas_fiscais WHERE id=?',(nid,)).fetchone(); con.close(); return self.ok(dict(r))
        con.close(); return self.error(405,'Método/ação não permitido.')
    def lancamentos(self,method,user,q):
        con=db()
        if method=='GET':
            sql='SELECT * FROM lancamentos WHERE empresa_id=?'; args=[user['empresa_id']]
            tp=(q.get('tipo') or [''])[0]
            if tp: sql+=' AND tipo=?'; args.append(tp)
            sql+=' ORDER BY data DESC,id DESC'; rows=con.execute(sql,args).fetchall(); con.close(); return self.ok([dict(r) for r in rows])
        if method=='POST':
            d=json_body(self); tp=require_text(d,'tipo',4,5).upper();
            if tp not in ('GANHO','CUSTO'): raise ValueError('tipo deve ser GANHO ou CUSTO.')
            desc=require_text(d,'descricao',3,300); cat=require_text(d,'categoria',2,80); val=require_value(d); dt=require_date(d,'data'); now=datetime.now().isoformat(timespec='seconds')
            cur=con.execute('INSERT INTO lancamentos(empresa_id,tipo,descricao,categoria,valor,data,criado_em) VALUES(?,?,?,?,?,?,?)',(user['empresa_id'],tp,desc,cat,val,dt,now)); con.commit(); r=con.execute('SELECT * FROM lancamentos WHERE id=?',(cur.lastrowid,)).fetchone(); con.close(); return self._send(201,dict(r))
        con.close(); return self.error(405,'Método não permitido.')
    def lancamento_item(self,method,user,lid):
        con=db(); row=con.execute('SELECT * FROM lancamentos WHERE id=? AND empresa_id=?',(lid,user['empresa_id'])).fetchone()
        if not row: con.close(); return self.error(404,'Lançamento não encontrado.')
        if method=='PUT':
            d=json_body(self); tp=require_text(d,'tipo',4,5).upper();
            if tp not in ('GANHO','CUSTO'): raise ValueError('tipo deve ser GANHO ou CUSTO.')
            desc=require_text(d,'descricao',3,300); cat=require_text(d,'categoria',2,80); val=require_value(d); dt=require_date(d,'data')
            con.execute('UPDATE lancamentos SET tipo=?,descricao=?,categoria=?,valor=?,data=? WHERE id=?',(tp,desc,cat,val,dt,lid)); con.commit(); r=con.execute('SELECT * FROM lancamentos WHERE id=?',(lid,)).fetchone(); con.close(); return self.ok(dict(r))
        if method=='DELETE': con.execute('DELETE FROM lancamentos WHERE id=?',(lid,)); con.commit(); con.close(); return self.ok({'ok':True})
        con.close(); return self.error(405,'Método não permitido.')
    def indicadores(self,user,q):
        periodo=(q.get('periodo') or ['mes'])[0]; ref=(q.get('referencia') or [date.today().isoformat()])[0]; start,end=period_bounds(periodo,ref)
        con=db(); rows=con.execute('SELECT tipo,categoria,valor,data FROM lancamentos WHERE empresa_id=? AND data BETWEEN ? AND ? ORDER BY data',(user['empresa_id'],start,end)).fetchall(); con.close()
        ganhos=sum(r['valor'] for r in rows if r['tipo']=='GANHO'); custos=sum(r['valor'] for r in rows if r['tipo']=='CUSTO')
        cats={}; evo={}
        for r in rows:
            if r['tipo']=='CUSTO': cats[r['categoria']]=cats.get(r['categoria'],0)+r['valor']
            key=r['data'][:7] if periodo!='mes' else r['data']
            evo.setdefault(key,{'ganhos':0,'custos':0}); evo[key]['ganhos' if r['tipo']=='GANHO' else 'custos']+=r['valor']
        return self.ok({'periodo':periodo,'inicio':start,'fim':end,'ganhos':money(ganhos),'custos':money(custos),'saldo':money(ganhos-custos),'evolucao':[{'rotulo':k,'ganhos':money(v['ganhos']),'custos':money(v['custos'])} for k,v in sorted(evo.items())],'custos_por_categoria':[{'categoria':k,'valor':money(v)} for k,v in sorted(cats.items(),key=lambda x:-x[1])]})
    def static(self,path):
        rel='index.html' if path in ('','/') else path.lstrip('/')
        fp=(STATIC/rel).resolve()
        if not str(fp).startswith(str(STATIC.resolve())) or not fp.exists() or fp.is_dir(): fp=STATIC/'index.html'
        data=fp.read_bytes(); ctype=mimetypes.guess_type(fp.name)[0] or 'application/octet-stream'
        if ctype.startswith('text/') or ctype in ('application/javascript','application/json'): ctype+='; charset=utf-8'
        self._send(200,data,ctype)


def main():
    reset='--reset' in os.sys.argv
    init_db(reset=reset)
    server=ThreadingHTTPServer((HOST,PORT),Handler)
    print('='*62); print('CONTABILIZA+ - servidor'); print(f'Ouvindo em: {HOST}:{PORT}'); print('Login: financeiro@empresa.com.br'); print('Senha: Contabiliza@2026'); print('Pressione Ctrl+C para encerrar.'); print('='*62)
    is_remoto = 'PORT' in os.environ  # provedores de hospedagem definem PORT
    if '--no-browser' not in os.sys.argv and not is_remoto:
        try: threading.Timer(1.0, lambda: webbrowser.open(f'http://127.0.0.1:{PORT}')).start()
        except Exception: pass
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()

if __name__=='__main__': main()
