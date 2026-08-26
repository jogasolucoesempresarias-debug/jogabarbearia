"""Instância de DEMONSTRAÇÃO: valida a trava do seed, o reset e o que o prospect vê.

O ponto mais importante aqui é a trava: seed_demo.py APAGA TODOS OS DADOS. Se um dia ele rodar
por engano na instância de um cliente, a barbearia perde o histórico inteiro. Este smoke prova
que ele se recusa a rodar sem MODO_DEMO e que o reset não encosta nas fichas de coleta.

Rode: python -X utf8 _smoke_demo.py
"""
import os, sys, json, time, socket, subprocess, urllib.request, urllib.error, http.cookiejar
import psycopg2
from psycopg2 import sql
from dotenv import load_dotenv

load_dotenv()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_TESTE = 'joga_demo_smoke'
OK = 0
FALHA = 0


def check(rotulo, cond, extra=''):
    global OK, FALHA
    print(f"  [{'OK' if cond else 'FALHA'}] {rotulo}" + (f"  → {extra}" if extra else ''))
    OK += bool(cond); FALHA += (not cond)


def porta_livre():
    s = socket.socket(); s.bind(('127.0.0.1', 0)); p = s.getsockname()[1]; s.close()
    return str(p)


def conn_admin():
    c = psycopg2.connect(host=os.getenv('DB_HOST'), port=os.getenv('DB_PORT'), dbname='postgres',
                         user=os.getenv('DB_USER'), password=os.getenv('DB_PASSWORD'))
    c.autocommit = True
    return c


def liga():
    return psycopg2.connect(host=os.getenv('DB_HOST'), port=os.getenv('DB_PORT'), dbname=DB_TESTE,
                            user=os.getenv('DB_USER'), password=os.getenv('DB_PASSWORD'))


def derrubar_banco():
    c = conn_admin(); cur = c.cursor()
    cur.execute("""SELECT pg_terminate_backend(pid) FROM pg_stat_activity
                   WHERE datname=%s AND pid<>pg_backend_pid()""", (DB_TESTE,))
    cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(DB_TESTE)))
    cur.close(); c.close()


def roda(script, env_extra=None):
    # Sem encurtar DEMO_DIAS: o gráfico de 6 meses e o status "dormente" (quem sumiu há 40+ dias)
    # só existem na janela real de 180. Testar num atalho testaria outra coisa.
    return subprocess.run([sys.executable, '-X', 'utf8', os.path.join(BASE_DIR, script)],
                          env={**os.environ, 'DB_NAME': DB_TESTE, **(env_extra or {})},
                          capture_output=True, text=True, cwd=BASE_DIR)


def conta(tabela):
    c = liga(); cur = c.cursor()
    cur.execute(f"SELECT COUNT(*) FROM {tabela}")
    n = cur.fetchone()[0]
    cur.close(); c.close()
    return n


derrubar_banco()
c = conn_admin(); cur = c.cursor()
cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DB_TESTE)))
cur.close(); c.close()
roda('init_db.py')

print("== A trava (o que impede o desastre) ==")
r = roda('seed_demo.py', {'MODO_DEMO': ''})
check('recusa rodar sem MODO_DEMO', r.returncode != 0, (r.stdout + r.stderr).strip().splitlines()[0][:70])
check('e não apagou nem criou nada', conta('profissionais') == 0)
r = roda('seed_demo.py', {'MODO_DEMO': 'talvez'})
check('recusa valor que não é 1/true/sim', r.returncode != 0)

print("== Popular ==")
r = roda('seed_demo.py', {'MODO_DEMO': '1'})
check('seed rodou', r.returncode == 0, (r.stderr or '')[-160:] if r.returncode else '')
check('3 barbeiros', conta('profissionais') == 3, conta('profissionais'))
check('2 logins', conta('usuarios') == 2, conta('usuarios'))
check('45 clientes', conta('clientes') == 45, conta('clientes'))
check('2 planos', conta('planos') == 2)
check('10 assinantes', conta('assinaturas') == 10)
comandas_1 = conta('comandas')
check('gerou histórico de comandas', comandas_1 > 100, comandas_1)
check('gerou receita no caixa', conta('movimentos') > 100, conta('movimentos'))
check('agenda dos próximos dias', conta('agendamentos') > 0, conta('agendamentos'))

c = liga(); cur = c.cursor()
cur.execute("SELECT COUNT(*) FROM comanda_itens WHERE coberto_plano")
check('tem visita de assinante coberta', cur.fetchone()[0] > 0)
cur.execute("SELECT comissao_assinante_regra FROM planos ORDER BY id")
regras = [r[0] for r in cur.fetchall()]
check("planos com regras diferentes (bolo e fixo)", set(regras) == {'bolo', 'fixo'}, regras)
cur.execute("SELECT COUNT(*) FROM comissoes_pagas")
check('quinzena anterior já fechada', cur.fetchone()[0] > 0)
cur.execute("SELECT COUNT(*) FROM movimentos WHERE origem='assinatura' AND status='pago' "
            "AND date_trunc('month', data) = date_trunc('month', CURRENT_DATE)")
check('tem mensalidade paga no mês corrente (senão o bolo zera na tela)', cur.fetchone()[0] > 0)
cur.execute("SELECT COUNT(*) FROM profissionais WHERE NOT recebe_comissao")
check('o dono não recebe comissão', cur.fetchone()[0] == 1)
cur.close(); c.close()

print("== Reset: roda de novo sem duplicar e sem levar as fichas junto ==")
# Simula uma ficha de coleta viva no banco — o reset NÃO pode encostar nela.
c = liga(); cur = c.cursor()
cur.execute("UPDATE setup_coleta SET nome='prospect que nao pode sumir', token='tok-teste', "
            "status='enviada' WHERE id=1")
c.commit(); cur.close(); c.close()

r = roda('seed_demo.py', {'MODO_DEMO': '1'})
check('reset rodou', r.returncode == 0)
check('não duplicou barbeiro', conta('profissionais') == 3, conta('profissionais'))
check('não duplicou cliente', conta('clientes') == 45, conta('clientes'))
c = liga(); cur = c.cursor()
cur.execute("SELECT nome, token, status FROM setup_coleta WHERE id=1")
ficha = cur.fetchone()
check('A FICHA DE COLETA SOBREVIVEU AO RESET', ficha == ('prospect que nao pode sumir', 'tok-teste', 'enviada'), ficha)
cur.close(); c.close()

print("== O que o prospect vê ==")
PORTA = porta_livre()
BASE = f'http://localhost:{PORTA}'
env = {**os.environ, 'DB_NAME': DB_TESTE, 'PORT': PORTA, 'MODO_DEMO': '1', 'MODO_COLETA': ''}
srv = subprocess.Popen([sys.executable, '-X', 'utf8', os.path.join(BASE_DIR, 'server.py')],
                       env=env, cwd=BASE_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
try:
    for _ in range(30):
        time.sleep(0.5)
        try:
            urllib.request.urlopen(BASE + '/health', timeout=2); break
        except Exception:
            pass

    ctx = json.loads(urllib.request.urlopen(BASE + '/api/cadastro/contexto', timeout=10).read().decode())
    check('a instância se declara demo (mostra a senha no login)', ctx.get('modo_demo') is True)
    check('marca da barbearia fictícia', ctx.get('marca_nome') == 'Barbearia do Zé', ctx.get('marca_nome'))

    def sessao(email, senha):
        cj = http.cookiejar.CookieJar()
        o = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        req = urllib.request.Request(BASE + '/api/login', method='POST',
                                     data=json.dumps({'email': email, 'senha': senha}).encode())
        req.add_header('Content-Type', 'application/json')
        return o, json.loads(o.open(req, timeout=10).read().decode())

    def pega(o, p):
        try:
            return json.loads(o.open(urllib.request.Request(BASE + p), timeout=20).read().decode())
        except urllib.error.HTTPError as e:
            return {'ok': False, 'status': e.code}

    dono, j = sessao('ze@barbearia.local', 'demo')
    check('dono entra com a senha publicada', j.get('ok'))
    check('e NÃO cai na troca de senha', j.get('redirect') != '/trocar-senha', j.get('redirect'))

    # /setup devolve HTML (redirect pra agenda), então não dá pra ler como JSON: olha o status cru.
    class _SemRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    sem_redirect = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(dono.handlers[0].cookiejar
                                           if hasattr(dono.handlers[0], 'cookiejar') else http.cookiejar.CookieJar()),
        _SemRedirect)
    try:
        st_setup, para = sem_redirect.open(BASE + '/setup', timeout=10).status, None
    except urllib.error.HTTPError as e:
        st_setup, para = e.code, e.headers.get('Location')
    check('entregar instância fica fora da demo (/setup redireciona)', st_setup in (301, 302),
          f"{st_setup} → {para}")

    hoje = __import__('datetime').date.today()
    ini = hoje.replace(day=1).isoformat()
    dre = pega(dono, f'/api/relatorios/dre?de={ini}&ate={hoje.isoformat()}')
    check('DRE responde', dre.get('ok'))
    serie = pega(dono, '/api/relatorios/dre-serie')
    meses_com_receita = sum(1 for m in (serie.get('serie') or []) if m.get('receita', 0) > 0)
    check('gráfico de 6 meses com histórico em todos', meses_com_receita >= 5, meses_com_receita)
    uso = pega(dono, f'/api/relatorios/assinantes-uso?de={ini}&ate={hoje.isoformat()}')
    res = uso.get('resumo', {})
    check('Uso dos Planos com assinantes', res.get('assinantes') == 10, res.get('assinantes'))
    check('mostra dormente e quem nunca veio', res.get('dormentes', 0) > 0 and res.get('nunca', 0) > 0,
          f"dormentes={res.get('dormentes')} nunca={res.get('nunca')}")
    ag = pega(dono, f'/api/agenda?data={hoje.isoformat()}')
    check('agenda de hoje não está vazia', len(ag.get('agendamentos') or []) > 0,
          len(ag.get('agendamentos') or []))
    # O prospect navega pra trás. Se a agenda de ontem estiver vazia enquanto o Resultado mostra
    # milhares de atendimentos, a demo se contradiz sozinha.
    ontem = pega(dono, f'/api/agenda?data={(hoje - __import__("datetime").timedelta(days=1)).isoformat()}')
    check('agenda de ontem tem histórico', len(ontem.get('agendamentos') or []) > 0,
          len(ontem.get('agendamentos') or []))
    semana = pega(dono, f'/api/agenda?data={(hoje - __import__("datetime").timedelta(days=7)).isoformat()}')
    ags = semana.get('agendamentos') or []
    check('agenda de 7 dias atrás tem histórico', len(ags) > 0, len(ags))
    check('e tudo lá está como atendido', all(a.get('status') == 'atendido' for a in ags))

    # ── Telas novas: taxa da maquininha e agendamento online ──
    # Na demo elas precisam nascer POPULADAS: tela vazia num tour de vendas parece defeito.
    tx = pega(dono, f'/api/relatorios/taxas?de={ini}&ate={hoje.isoformat()}')
    check('relatório de taxas responde', tx.get('ok'))
    check('taxa de cartão tem valor no período', tx.get('total_taxa', 0) > 0, tx.get('total_taxa'))
    check('líquido = bruto − taxa',
          abs(tx.get('total_liquido', 0) - (tx.get('total_bruto', 0) - tx.get('total_taxa', 0))) < 0.01)
    formas_tx = {r['forma']: r['taxa'] for r in tx.get('rows', [])}
    check('crédito custa mais que débito (taxas diferentes de verdade)',
          formas_tx.get('Crédito', 0) > formas_tx.get('Débito', 0), formas_tx)
    check('dinheiro não tem taxa', formas_tx.get('Dinheiro', 0) == 0, formas_tx.get('Dinheiro'))

    fp = pega(dono, '/api/formas-pagamento')
    check('formas de pagamento cadastradas com taxa',
          any(f['nome'] == 'Crédito' and f['taxa_pct'] > 0 for f in fp.get('rows', [])))

    ctx = pega(dono, '/api/agendar/contexto')
    check('agendamento online está ligado na demo', ctx.get('ok'))
    check('link mostra serviços e barbeiros',
          len(ctx.get('servicos') or []) > 0 and len(ctx.get('profissionais') or []) > 0)

    pend = [a for a in (ag.get('agendamentos') or []) if a.get('status') == 'pendente']
    amanha_ag = pega(dono, f'/api/agenda?data={(hoje + __import__("datetime").timedelta(days=1)).isoformat()}')
    pend += [a for a in (amanha_ag.get('agendamentos') or []) if a.get('status') == 'pendente']
    check('tem pedido online esperando aceite', len(pend) > 0, len(pend))

    msg = pega(dono, '/api/mensagens/pendentes')
    check('fila de mensagens responde', msg.get('ok'))
    check('fila tem algo pra enviar', msg.get('total', 0) > 0, msg.get('total'))
    todos = (msg.get('aceites') or []) + (msg.get('lembretes') or [])
    com_link = [m for m in todos if m.get('wa_url')]
    check('mensagens trazem link do WhatsApp', len(com_link) > 0, len(com_link))
    check('link wa.me com número completo (55+DDD+9 dígitos)',
          all(len(m['wa_url'].split('/')[-1].split('?')[0]) == 13 for m in com_link),
          [m['wa_url'].split('/')[-1].split('?')[0] for m in com_link[:2]])
    check('texto traz o nome da barbearia da demo',
          all('Barbearia do Zé' in m['texto'] for m in todos))

    barb, jb = sessao('rafael@barbearia.local', 'demo')
    check('barbeiro entra', jb.get('ok'))
    mc = pega(barb, '/api/relatorios/minha-comissao')
    check('barbeiro vê a própria comissão', mc.get('ok'))
    check('barbeiro NÃO vê o financeiro',
          pega(barb, f'/api/relatorios/dre?de={ini}&ate={hoje.isoformat()}').get('status') == 403)

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
