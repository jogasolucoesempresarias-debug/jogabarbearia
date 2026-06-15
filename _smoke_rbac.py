"""Valida o RBAC do barbeiro: só agenda própria + comissão própria; resto bloqueado."""
import os, json, urllib.request, urllib.error, http.cookiejar
from datetime import date
import psycopg2
from dotenv import load_dotenv

load_dotenv()
BASE = 'http://localhost:5002'
cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
hoje = date.today().isoformat()
OK = 0; FAIL = 0
def check(label, cond, extra=''):
    global OK, FAIL
    print(f"  [{'PASS' if cond else 'FALHA'}] {label}" + (f"  → {extra}" if extra else '')); OK += cond; FAIL += (not cond)

def status(method, path):
    req = urllib.request.Request(BASE + path, method=method)
    try:
        r = op.open(req, timeout=10); return r.status
    except urllib.error.HTTPError as e:
        return e.code

def db():
    return psycopg2.connect(host=os.getenv('DB_HOST'), port=os.getenv('DB_PORT'), dbname=os.getenv('DB_NAME'),
        user=os.getenv('DB_USER'), password=os.getenv('DB_PASSWORD'))

c = db(); cur = c.cursor()
cur.execute("UPDATE usuarios SET must_change_password=false WHERE email='joaovictor@barbearia.local'")
c.commit(); cur.close(); c.close()

# login como BARBEIRO
req = urllib.request.Request(BASE + '/api/login', data=json.dumps({'email':'joaovictor@barbearia.local','senha':'joga123'}).encode(), method='POST')
req.add_header('Content-Type','application/json'); op.open(req)

print("== Permitido pro barbeiro (200) ==")
for p in ['/api/me', f'/api/agenda?data={hoje}', '/api/relatorios/minha-comissao', '/api/servicos']:
    check(f'GET {p}', status('GET', p) == 200, status('GET', p))

print("== Bloqueado pro barbeiro (403 nas APIs) ==")
for p in ['/api/caixa/fechamento', '/api/despesas/resumo', f'/api/relatorios/comissao?de={hoje}&ate={hoje}',
          f'/api/relatorios/dre?de={hoje}&ate={hoje}', '/api/movimentos', '/api/clientes', '/api/comandas/abertas',
          '/api/assinaturas', '/api/config', '/api/profissionais/1']:
    st = status('GET', p)
    check(f'GET {p} bloqueado', st == 403, st)

print("== Páginas financeiras → redirect (302) pra agenda ==")
for p in ['/caixa', '/despesas', '/dre', '/relatorios', '/config', '/comanda']:
    st = status('GET', p)
    check(f'{p} redireciona', st in (302, 301), st)

print("== Páginas próprias do barbeiro (200) ==")
for p in ['/', '/barbeiro']:
    check(f'{p}', status('GET', p) == 200, status('GET', p))

# restaura
c = db(); cur = c.cursor(); cur.execute("UPDATE usuarios SET must_change_password=true"); c.commit(); cur.close(); c.close()
print(f"\n========== RESULTADO: {OK} PASS · {FAIL} FALHA ==========")
