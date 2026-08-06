"""Alerta de WhatsApp quando o prospect ENVIA a ficha.

Não manda mensagem de verdade: sobe um servidor apontando a UAZAPI_URL pra um servidor HTTP
local de mentira, que grava o que recebeu. Assim dá pra conferir número normalizado, header do
token, texto e o disparo pros DOIS destinatários — sem gastar mensagem nem depender de internet.

Rode: python -X utf8 _smoke_alerta.py
"""
import os, sys, json, time, socket, threading, subprocess, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
import psycopg2
from psycopg2 import sql
from werkzeug.security import generate_password_hash
from dotenv import load_dotenv

load_dotenv()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_TESTE = 'joga_barbearia_alerta_smoke'
OK = 0
FALHA = 0
RECEBIDAS = []


def check(rotulo, cond, extra=''):
    global OK, FALHA
    print(f"  [{'OK' if cond else 'FALHA'}] {rotulo}" + (f"  → {extra}" if extra else ''))
    OK += bool(cond); FALHA += (not cond)


def porta_livre():
    s = socket.socket(); s.bind(('127.0.0.1', 0)); p = s.getsockname()[1]; s.close()
    return p


# ── UazAPI de mentira ─────────────────────────────────────────────────
class FakeUaz(BaseHTTPRequestHandler):
    def do_POST(self):
        corpo = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        RECEBIDAS.append({'path': self.path, 'token': self.headers.get('token'),
                          'body': json.loads(corpo.decode())})
        self.send_response(200); self.send_header('Content-Type', 'application/json')
        self.end_headers(); self.wfile.write(b'{"ok":true}')

    def log_message(self, *a):
        pass


PORTA_UAZ = porta_livre()
fake = HTTPServer(('127.0.0.1', PORTA_UAZ), FakeUaz)
threading.Thread(target=fake.serve_forever, daemon=True).start()
print(f"== UazAPI de mentira na {PORTA_UAZ} ==")


def conn_admin():
    c = psycopg2.connect(host=os.getenv('DB_HOST'), port=os.getenv('DB_PORT'), dbname='postgres',
                         user=os.getenv('DB_USER'), password=os.getenv('DB_PASSWORD'))
    c.autocommit = True
    return c


def derrubar_banco():
    c = conn_admin(); cur = c.cursor()
    cur.execute("""SELECT pg_terminate_backend(pid) FROM pg_stat_activity
                   WHERE datname=%s AND pid<>pg_backend_pid()""", (DB_TESTE,))
    cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(DB_TESTE)))
    cur.close(); c.close()


def call(base, m, p, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + p, data=data, method=m)
    req.add_header('Content-Type', 'application/json')
    try:
        return json.loads(urllib.request.urlopen(req, timeout=15).read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {'ok': False, 'error': f'HTTP {e.code}'}


derrubar_banco()
c = conn_admin(); cur = c.cursor()
cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DB_TESTE)))
cur.close(); c.close()

PORTA = str(porta_livre())
BASE = f'http://localhost:{PORTA}'
env = {**os.environ, 'DB_NAME': DB_TESTE, 'PORT': PORTA, 'MODO_COLETA': '1',
       'ALERTAS_ATIVO': '1', 'UAZAPI_URL': f'http://127.0.0.1:{PORTA_UAZ}',
       'UAZAPI_TOKEN': 'token-de-teste',
       'ALERTA_WHATSAPP': '34999434613,28999850221'}
subprocess.run([sys.executable, '-X', 'utf8', os.path.join(BASE_DIR, 'init_db.py')],
               env=env, capture_output=True, text=True)

c = psycopg2.connect(host=os.getenv('DB_HOST'), port=os.getenv('DB_PORT'), dbname=DB_TESTE,
                     user=os.getenv('DB_USER'), password=os.getenv('DB_PASSWORD'))
cur = c.cursor()
cur.execute("""INSERT INTO usuarios (nome, email, password_hash, role, must_change_password)
               VALUES ('Suporte','suporte@joga.local',%s,'dono',false)""",
            (generate_password_hash('joga123'),))
c.commit(); cur.close(); c.close()

srv = subprocess.Popen([sys.executable, '-X', 'utf8', os.path.join(BASE_DIR, 'server.py')],
                       env=env, cwd=BASE_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
try:
    for _ in range(30):
        time.sleep(0.5)
        try:
            urllib.request.urlopen(BASE + '/health', timeout=2); break
        except Exception:
            pass

    import http.cookiejar
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    req = urllib.request.Request(BASE + '/api/login', method='POST',
                                 data=json.dumps({'email': 'suporte@joga.local', 'senha': 'joga123'}).encode())
    req.add_header('Content-Type', 'application/json')
    op.open(req)
    r = json.loads(op.open(urllib.request.Request(
        BASE + '/api/setup/fichas', method='POST',
        data=json.dumps({'nome': 'Zé — teste de alerta'}).encode(),
        headers={'Content-Type': 'application/json'})).read().decode())
    token = r['token']

    print("== Rascunho NÃO deve alertar ==")
    dados = call(BASE, 'GET', f'/api/coleta?t={token}')['dados']
    dados['barbearia'] = {'nome': 'Barbearia do Zé', 'endereco': '', 'whatsapp': ''}
    dados['barbeiros'] = [{'nome': 'Zé', 'dono': True, 'comissao_pct': 45, 'login': True},
                          {'nome': 'Pedro', 'dono': False, 'comissao_pct': 40, 'login': True}]
    dados['vende_plano'] = True
    dados['planos'] = [{'nome': 'Plano Cabelo', 'valor_mensal': 120, 'servicos': ['Cabelo'],
                        'dias': [1, 2], 'regra': 'tabela', 'valor_fixo': None}]
    call(BASE, 'PUT', '/api/coleta', {'t': token, 'dados': dados, 'enviar': False})
    time.sleep(1.5)
    check('salvar rascunho não dispara WhatsApp', len(RECEBIDAS) == 0, len(RECEBIDAS))

    print("== Enviar dispara pros dois números ==")
    call(BASE, 'PUT', '/api/coleta', {'t': token, 'dados': dados, 'enviar': True})
    for _ in range(20):                          # o envio é em thread: espera chegar
        if len(RECEBIDAS) >= 2:
            break
        time.sleep(0.3)
    check('2 mensagens (Gabriel e João)', len(RECEBIDAS) == 2, len(RECEBIDAS))
    if RECEBIDAS:
        numeros = sorted(m['body']['number'] for m in RECEBIDAS)
        check('número normalizado (sem o 9 extra)', numeros == ['552899850221', '553499434613'], numeros)
        check('token vai no header', all(m['token'] == 'token-de-teste' for m in RECEBIDAS))
        check('endpoint /send/text', all(m['path'] == '/send/text' for m in RECEBIDAS),
              RECEBIDAS[0]['path'])
        texto = RECEBIDAS[0]['body']['text']
        print("  ── mensagem enviada ──")
        for linha in texto.split('\n'):
            print(f"  | {linha}")
        check('traz o nome da barbearia', 'Barbearia do Zé' in texto)
        check('traz o resumo', '2 barbeiro(s)' in texto and '1 plano(s)' in texto)
        check('traz o link com o token', f'/coleta?t={token}' in texto)
        check('traz o painel', '/setup' in texto)

    print("== Trava mestra desligada ==")
    RECEBIDAS.clear()
    srv.terminate(); srv.wait(timeout=10)
    env_off = {**env, 'ALERTAS_ATIVO': ''}
    srv = subprocess.Popen([sys.executable, '-X', 'utf8', os.path.join(BASE_DIR, 'server.py')],
                           env=env_off, cwd=BASE_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    for _ in range(30):
        time.sleep(0.5)
        try:
            urllib.request.urlopen(BASE + '/health', timeout=2); break
        except Exception:
            pass
    call(BASE, 'PUT', '/api/coleta', {'t': token, 'dados': dados, 'enviar': True})
    time.sleep(1.5)
    check('ALERTAS_ATIVO vazio não envia nada', len(RECEBIDAS) == 0, len(RECEBIDAS))

finally:
    print("== Limpeza ==")
    srv.terminate()
    try:
        srv.wait(timeout=10)
    except subprocess.TimeoutExpired:
        srv.kill()
    fake.shutdown()
    time.sleep(0.5)
    derrubar_banco()
    print(f"  [OK] banco {DB_TESTE} removido.")

print(f"\n========== RESULTADO: {OK} OK · {FALHA} FALHA ==========")
raise SystemExit(1 if FALHA else 0)
