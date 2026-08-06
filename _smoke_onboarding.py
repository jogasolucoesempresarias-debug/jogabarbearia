"""Onboarding ponta a ponta numa instância VIRGEM: cria um banco descartável, sobe um servidor
só pra ele, e roda o caminho real — gerar link → cliente preenche a ficha → JOGA aplica →
a barbearia nasce populada e o dono já consegue logar.

Não toca no banco de desenvolvimento. Rode: python -X utf8 _smoke_onboarding.py
"""
import os, sys, json, time, socket, subprocess, urllib.request, urllib.error, http.cookiejar
import psycopg2
from psycopg2 import sql
from dotenv import load_dotenv

load_dotenv()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_TESTE = 'joga_barbearia_smoke'


def porta_livre():
    """Porta escolhida pelo SO. Porta fixa faz o teste conversar com um servidor de dev que já
    esteja de pé (o nosso nem sobe, e o smoke valida a instância errada em silêncio)."""
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


def conn_teste():
    return psycopg2.connect(host=os.getenv('DB_HOST'), port=os.getenv('DB_PORT'), dbname=DB_TESTE,
                            user=os.getenv('DB_USER'), password=os.getenv('DB_PASSWORD'))


def derrubar_banco():
    c = conn_admin(); cur = c.cursor()
    cur.execute("""SELECT pg_terminate_backend(pid) FROM pg_stat_activity
                   WHERE datname=%s AND pid<>pg_backend_pid()""", (DB_TESTE,))
    cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(DB_TESTE)))
    cur.close(); c.close()


cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))


def call(m, p, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + p, data=data, method=m)
    req.add_header('Content-Type', 'application/json')
    try:
        return json.loads(op.open(req, timeout=15).read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {'ok': False, 'error': f'HTTP {e.code}'}


# ── Instância virgem ──────────────────────────────────────────────────
print("== Criando instância virgem ==")
derrubar_banco()
c = conn_admin(); cur = c.cursor()
cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DB_TESTE)))
cur.close(); c.close()

env = {**os.environ, 'DB_NAME': DB_TESTE, 'PORT': PORTA, 'SEED_SENHA_INICIAL': 'joga123'}
r = subprocess.run([sys.executable, '-X', 'utf8', os.path.join(BASE_DIR, 'init_db.py')],
                   env=env, capture_output=True, text=True)
check('schema criado no banco novo', r.returncode == 0, r.stdout.strip().splitlines()[-1] if r.stdout else r.stderr[-200:])

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
    check('servidor da instância nova respondendo', pronto)
    if not pronto:
        print(srv.stderr.read().decode(errors='ignore')[-800:])
        raise SystemExit(1)

    # Sem usuário no banco novo, o login master do .env é a única porta. Usa o de suporte.
    print("== Login master (suporte JOGA) ==")
    sup_email = os.getenv('SUPORTE_EMAIL', '').strip()
    if not sup_email:
        # Sem SUPORTE_* configurado, cria um dono provisório direto no banco (é o que a JOGA faria).
        from werkzeug.security import generate_password_hash
        c = conn_teste(); cur = c.cursor()
        cur.execute("""INSERT INTO usuarios (nome, email, password_hash, role, must_change_password)
                       VALUES ('Suporte JOGA','suporte@joga.local',%s,'dono',false)""",
                    (generate_password_hash('joga123'),))
        c.commit(); cur.close(); c.close()
        sup_email, sup_senha = 'suporte@joga.local', 'joga123'
    else:
        sup_senha = os.getenv('SUPORTE_SENHA', '')
    j = call('POST', '/api/login', {'email': sup_email, 'senha': sup_senha})
    check('login master', j.get('ok'), j.get('error'))

    print("== 1. Gerar o link da ficha ==")
    j = call('POST', '/api/setup/link')
    token = j.get('token')
    check('token gerado', bool(token))
    check('ficha sem token é 404', call('GET', '/api/coleta').get('ok') is False)
    check('ficha com token errado é 404', call('GET', '/api/coleta?t=errado').get('ok') is False)

    print("== 2. O cliente abre a ficha (preset pré-preenchido) ==")
    j = call('GET', f'/api/coleta?t={token}')
    dados = j.get('dados') or {}
    check('ficha abre com o preset', j.get('ok') and len(dados.get('servicos') or []) >= 5,
          f"{len(dados.get('servicos') or [])} serviços sugeridos")

    print("== 3. O cliente preenche e envia ==")
    dados['barbearia'] = {'nome': 'Barbearia do Zé', 'endereco': 'Rua A, 100', 'whatsapp': '(34) 99999-0000'}
    dados['barbeiros'] = [
        {'nome': 'José Silva', 'dono': True, 'comissao_pct': 45, 'login': True},
        {'nome': 'Pedro Alves', 'dono': False, 'comissao_pct': 40, 'login': True},
        {'nome': 'Tiago Sem Login', 'dono': False, 'comissao_pct': 45, 'login': False},
    ]
    dados['servicos'] = [
        {'nome': 'Cabelo', 'preco': 45, 'duracao_min': 30, 'usa': True},
        {'nome': 'Barba', 'preco': 35, 'duracao_min': 30, 'usa': True},
        {'nome': 'Sobrancelha', 'preco': 20, 'duracao_min': 15, 'usa': False},   # não faz
    ]
    dados['vende_produto'] = True
    dados['produtos'] = [{'nome': 'Pomada', 'preco': 40, 'usa': True},
                         {'nome': 'Minoxidil', 'preco': 70, 'usa': False}]
    dados['vende_plano'] = True
    dados['planos'] = [{'nome': 'Plano Cabelo', 'valor_mensal': 120, 'servicos': ['Cabelo'],
                        'dias': [2, 3, 4], 'regra': 'fixo', 'valor_fixo': 18}]
    dados['formas_pagamento'] = ['Dinheiro', 'Pix', 'Cartão']
    dados['horarios'] = {'0': None, '1': {'abre': '09:00', 'fecha': '20:00'},
                         '2': {'abre': '09:00', 'fecha': '20:00'}, '3': {'abre': '09:00', 'fecha': '20:00'},
                         '4': {'abre': '09:00', 'fecha': '20:00'}, '5': {'abre': '09:00', 'fecha': '20:00'},
                         '6': {'abre': '08:00', 'fecha': '17:00'}}
    check('salva e envia a ficha', call('PUT', '/api/coleta', {'t': token, 'dados': dados, 'enviar': True}).get('ok'))
    j = call('GET', '/api/setup')
    check('a JOGA vê como enviada', j.get('status') == 'enviada', j.get('status'))
    check('instância ainda virgem', not j.get('em_operacao'))

    print("== 4. A JOGA aplica ==")
    j = call('POST', '/api/setup/aplicar', {'dados': dados})
    check('aplicou', j.get('ok'), j.get('error'))
    cr = j.get('criados') or {}
    check('3 barbeiros', cr.get('profissionais') == 3, cr.get('profissionais'))
    check('2 logins (o Tiago não pediu acesso)', cr.get('usuarios') == 2, cr.get('usuarios'))
    check('2 serviços (Sobrancelha ficou de fora)', cr.get('servicos') == 2, cr.get('servicos'))
    check('1 produto (Minoxidil desmarcado)', cr.get('produtos') == 1, cr.get('produtos'))
    check('1 plano', cr.get('planos') == 1, cr.get('planos'))
    logins = {l['email']: l for l in (j.get('logins') or [])}
    check('login do dono gerado', 'jose@barbearia.local' in logins, list(logins))
    check('dono entrou como role dono', logins.get('jose@barbearia.local', {}).get('papel') == 'dono')

    print("== 5. Conferindo o que ficou no banco ==")
    c = conn_teste(); cur = c.cursor()
    cur.execute("SELECT nome, comissao_pct, recebe_comissao, cor_agenda FROM profissionais ORDER BY id")
    profs = cur.fetchall()
    dono = [p for p in profs if p[0] == 'José Silva'][0]
    check('dono NÃO recebe comissão', dono[2] is False)
    check('comissão individual respeitada (Pedro 40%)',
          float([p for p in profs if p[0] == 'Pedro Alves'][0][1]) == 40)
    check('cores de agenda diferentes entre barbeiros', len({p[3] for p in profs}) == 3,
          {p[3] for p in profs})
    cur.execute("SELECT nome, valor_mensal, comissao_assinante_regra, comissao_assinante_valor FROM planos")
    pl = cur.fetchone()
    check("plano gravou a regra 'fixo' com o R$", pl[2] == 'fixo' and float(pl[3]) == 18, pl)
    cur.execute("""SELECT s.nome FROM plano_servicos ps JOIN servicos s ON s.id=ps.servico_id""")
    check('serviço do plano ligado', [r[0] for r in cur.fetchall()] == ['Cabelo'])
    cur.execute("SELECT marca_nome, horarios, formas_pagamento FROM configuracoes WHERE id=1")
    cfg_row = cur.fetchone()
    check('marca gravada', cfg_row[0] == 'Barbearia do Zé', cfg_row[0])
    check('horário do cliente gravado (seg 09:00)', cfg_row[1].get('1', {}).get('abre') == '09:00')
    cur.execute("SELECT status, token FROM setup_coleta WHERE id=1")
    st = cur.fetchone()
    check('ficha marcada como aplicada', st[0] == 'aplicada', st[0])
    check('token invalidado depois de aplicar', st[1] is None)
    cur.close(); c.close()

    print("== 6. Portas fechadas depois da entrega ==")
    check('ficha pública fechou', call('GET', f'/api/coleta?t={token}').get('ok') is False)
    j = call('POST', '/api/setup/aplicar', {'dados': dados})
    check('aplicar de novo é bloqueado', j.get('ok') is False, j.get('error'))

    print("== 7. O dono consegue entrar ==")
    cj2 = http.cookiejar.CookieJar()
    op2 = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj2))
    req = urllib.request.Request(BASE + '/api/login', method='POST',
                                 data=json.dumps({'email': 'jose@barbearia.local', 'senha': 'joga123'}).encode())
    req.add_header('Content-Type', 'application/json')
    j = json.loads(op2.open(req, timeout=10).read().decode())
    check('dono loga com a senha inicial', j.get('ok'), j.get('error'))
    check('e é obrigado a trocar a senha', j.get('redirect') == '/trocar-senha', j.get('redirect'))

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
