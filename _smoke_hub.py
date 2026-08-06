"""Hub de coleta (MODO_COLETA=1): a instância que guarda as fichas dos prospects antes de existir
servidor pra eles. Valida que várias fichas convivem, que cada token só abre a sua, e que o hub
NUNCA vira barbearia. Cria e derruba o próprio banco — não toca no de desenvolvimento.

Rode: python -X utf8 _smoke_hub.py
"""
import os, sys, json, time, socket, subprocess, urllib.request, urllib.error, http.cookiejar
import psycopg2
from psycopg2 import sql
from werkzeug.security import generate_password_hash
from dotenv import load_dotenv

load_dotenv()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_TESTE = 'joga_barbearia_hub_smoke'


def porta_livre():
    """Porta escolhida pelo SO — porta fixa faz o teste falar com um servidor de dev já rodando."""
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return str(p)


PORTA = porta_livre()
BASE = f'http://localhost:{PORTA}'
OK = 0
FALHA = 0


def check(rotulo, cond, extra=''):
    global OK, FALHA
    print(f"  [{'OK' if cond else 'FALHA'}] {rotulo}" + (f"  → {extra}" if extra else ''))
    OK += bool(cond); FALHA += (not cond)


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


cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))


def call(m, p, body=None, anon=False):
    """anon=True usa um opener sem cookie — simula o prospect, que não tem login."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + p, data=data, method=m)
    req.add_header('Content-Type', 'application/json')
    cliente = urllib.request.build_opener() if anon else op
    try:
        return json.loads(cliente.open(req, timeout=15).read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {'ok': False, 'error': f'HTTP {e.code}'}


print("== Subindo o hub ==")
derrubar_banco()
c = conn_admin(); cur = c.cursor()
cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DB_TESTE)))
cur.close(); c.close()

env = {**os.environ, 'DB_NAME': DB_TESTE, 'PORT': PORTA, 'MODO_COLETA': '1'}
r = subprocess.run([sys.executable, '-X', 'utf8', os.path.join(BASE_DIR, 'init_db.py')],
                   env=env, capture_output=True, text=True)
check('schema criado', r.returncode == 0, (r.stderr or '')[-200:] if r.returncode else '')

c = psycopg2.connect(host=os.getenv('DB_HOST'), port=os.getenv('DB_PORT'), dbname=DB_TESTE,
                     user=os.getenv('DB_USER'), password=os.getenv('DB_PASSWORD'))
cur = c.cursor()
cur.execute("""INSERT INTO usuarios (nome, email, password_hash, role, must_change_password)
               VALUES ('Suporte JOGA','suporte@joga.local',%s,'dono',false)""",
            (generate_password_hash('joga123'),))
c.commit(); cur.close(); c.close()

srv = subprocess.Popen([sys.executable, '-X', 'utf8', os.path.join(BASE_DIR, 'server.py')],
                       env=env, cwd=BASE_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
try:
    pronto = False
    for _ in range(30):
        time.sleep(0.5)
        try:
            urllib.request.urlopen(BASE + '/health', timeout=2); pronto = True; break
        except Exception:
            pass
    check('hub respondendo', pronto)
    if not pronto:
        print(srv.stderr.read().decode(errors='ignore')[-800:]); raise SystemExit(1)

    call('POST', '/api/login', {'email': 'suporte@joga.local', 'senha': 'joga123'})
    j = call('GET', '/api/setup')
    check('instância se declara hub', j.get('modo_coleta') is True)

    print("== Três prospects ao mesmo tempo ==")
    fichas = {}
    for nome in ['Zé — indicação do João', 'Barbearia Central', 'Corte & Cia']:
        r = call('POST', '/api/setup/fichas', {'nome': nome})
        check(f'ficha criada: {nome}', r.get('ok'), r.get('error'))
        fichas[nome] = r
    check('3 tokens diferentes', len({f['token'] for f in fichas.values()}) == 3)

    j = call('GET', '/api/setup')
    lista = [f for f in j.get('fichas', []) if f['id'] != 1]
    check('as 3 aparecem na lista do painel', len(lista) == 3, len(lista))

    print("== Cada token abre só a sua ficha ==")
    ze = fichas['Zé — indicação do João']
    central = fichas['Barbearia Central']
    j = call('GET', f"/api/coleta?t={ze['token']}", anon=True)
    check('prospect abre a ficha SEM login', j.get('ok'))
    check('vem com o preset', len((j.get('dados') or {}).get('servicos') or []) >= 5)

    dados_ze = j['dados']
    dados_ze['barbearia'] = {'nome': 'Barbearia do Zé', 'endereco': '', 'whatsapp': ''}
    dados_ze['servicos'] = [{'nome': 'Cabelo', 'preco': 55, 'duracao_min': 30, 'usa': True}]
    check('prospect salva sem login',
          call('PUT', '/api/coleta', {'t': ze['token'], 'dados': dados_ze, 'enviar': True}, anon=True).get('ok'))

    j = call('GET', f"/api/coleta?t={central['token']}", anon=True)
    nome_central = (j.get('dados') or {}).get('barbearia', {}).get('nome')
    check('o que o Zé escreveu NÃO vazou pra ficha da Central', not nome_central, repr(nome_central))
    check('token inventado não abre nada', call('GET', '/api/coleta?t=xxxxx', anon=True).get('ok') is False)

    print("== A JOGA pega a ficha pronta ==")
    j = call('GET', f"/api/setup/fichas/{ze['id']}")
    check('ficha do Zé recuperada pelo painel', j.get('ok'))
    check('com o preço que ele digitou', (j['dados']['servicos'][0]['preco']) == 55,
          j['dados']['servicos'][0]['preco'])
    j = call('GET', '/api/setup')
    st = {f['id']: f['status'] for f in j['fichas']}
    check('a do Zé consta como enviada', st.get(ze['id']) == 'enviada', st.get(ze['id']))
    check('a da Central segue em rascunho', st.get(central['id']) == 'rascunho', st.get(central['id']))

    print("== O hub nunca vira barbearia ==")
    j = call('POST', '/api/setup/aplicar', {'dados': dados_ze})
    check('aplicar é bloqueado no hub', j.get('ok') is False, j.get('error'))
    c = psycopg2.connect(host=os.getenv('DB_HOST'), port=os.getenv('DB_PORT'), dbname=DB_TESTE,
                         user=os.getenv('DB_USER'), password=os.getenv('DB_PASSWORD'))
    cur = c.cursor()
    cur.execute("SELECT COUNT(*) FROM profissionais"); n_prof = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM servicos"); n_serv = cur.fetchone()[0]
    cur.close(); c.close()
    check('nenhum barbeiro criado no hub', n_prof == 0, n_prof)
    check('nenhum serviço criado no hub', n_serv == 0, n_serv)

    print("== Apagar ficha ==")
    check('a ficha da própria instância é protegida',
          call('DELETE', '/api/setup/fichas/1').get('ok') is False)
    check('apaga a da Central', call('DELETE', f"/api/setup/fichas/{central['id']}").get('ok'))
    check('link da apagada morre',
          call('GET', f"/api/coleta?t={central['token']}", anon=True).get('ok') is False)
    check('a do Zé continua viva', call('GET', f"/api/coleta?t={ze['token']}", anon=True).get('ok'))

finally:
    print("== Limpeza ==")
    srv.terminate()
    try:
        srv.wait(timeout=10)
    except subprocess.TimeoutExpired:
        srv.kill()
    time.sleep(0.5)
    derrubar_banco()
    print(f"  [OK] banco {DB_TESTE} removido.")

print(f"\n========== RESULTADO: {OK} OK · {FALHA} FALHA ==========")
raise SystemExit(1 if FALHA else 0)
