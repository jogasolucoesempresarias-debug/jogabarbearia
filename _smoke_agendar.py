"""Agendamento online ponta a ponta, num banco DESCARTÁVEL próprio (não toca no dev).

Cobre as três fases de uma vez:
  Fase 0 — a trava: sobreposição de intervalo, bloqueio e horário de funcionamento
  Fase 1 — a porta pública: disponibilidade sem vazar cliente, agendar, "meus horários", cancelar
  Fase 2 — a fila de mensagens: aceite, lembrete D-1, marcar enviado e desfazer

Rode: python -X utf8 _smoke_agendar.py
"""
import os, sys, json, time, socket, subprocess, urllib.request, urllib.error, http.cookiejar
from datetime import date, timedelta
import psycopg2
from psycopg2 import sql
from dotenv import load_dotenv

load_dotenv()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_TESTE = 'joga_barbearia_agsmoke'


def porta_livre():
    s = socket.socket(); s.bind(('127.0.0.1', 0)); p = s.getsockname()[1]; s.close(); return str(p)


PORTA = porta_livre()
BASE = f'http://localhost:{PORTA}'
OK = 0; FALHA = 0


def check(rotulo, cond, extra=''):
    global OK, FALHA
    print(f"  [{'OK' if cond else 'FALHA'}] {rotulo}" + (f"  → {extra}" if extra else ''))
    OK += bool(cond); FALHA += (not cond)


def conn_admin():
    c = psycopg2.connect(host=os.getenv('DB_HOST'), port=os.getenv('DB_PORT'), dbname='postgres',
                         user=os.getenv('DB_USER'), password=os.getenv('DB_PASSWORD'))
    c.autocommit = True; return c


def conn_teste():
    return psycopg2.connect(host=os.getenv('DB_HOST'), port=os.getenv('DB_PORT'), dbname=DB_TESTE,
                            user=os.getenv('DB_USER'), password=os.getenv('DB_PASSWORD'))


def sql_exec(q, params=None):
    c = conn_teste(); cur = c.cursor(); cur.execute(q, params or ()); c.commit(); cur.close(); c.close()


def sql_one(q, params=None):
    c = conn_teste(); cur = c.cursor(); cur.execute(q, params or ()); r = cur.fetchone(); cur.close(); c.close(); return r


def derrubar_banco():
    c = conn_admin(); cur = c.cursor()
    cur.execute("""SELECT pg_terminate_backend(pid) FROM pg_stat_activity
                   WHERE datname=%s AND pid<>pg_backend_pid()""", (DB_TESTE,))
    cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(DB_TESTE)))
    cur.close(); c.close()


# Sessão do PAINEL (a barbearia) e sessão ANÔNIMA (o cliente no link público) são separadas
# de propósito: o público não pode depender de cookie de login pra funcionar.
cj = http.cookiejar.CookieJar()
painel = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
anon = urllib.request.build_opener()


def _call(opener, m, p, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + p, data=data, method=m)
    req.add_header('Content-Type', 'application/json')
    try:
        return json.loads(opener.open(req, timeout=15).read().decode())
    except urllib.error.HTTPError as e:
        try:
            j = json.loads(e.read().decode()); j['_status'] = e.code; return j
        except Exception:
            return {'ok': False, 'error': f'HTTP {e.code}', '_status': e.code}


def call(m, p, b=None):   return _call(painel, m, p, b)
def pub(m, p, b=None):    return _call(anon, m, p, b)


# ── Instância virgem ──────────────────────────────────────────────────
print("== Instância virgem ==")
derrubar_banco()
c = conn_admin(); cur = c.cursor()
cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DB_TESTE)))
cur.close(); c.close()

env = {**os.environ, 'DB_NAME': DB_TESTE, 'PORT': PORTA, 'SEED_SENHA_INICIAL': 'joga123',
       'MODO_COLETA': '', 'MODO_DEMO': '', 'ALERTAS_ATIVO': ''}
for script in ('init_db.py', 'seed_barbearia.py'):
    r = subprocess.run([sys.executable, '-X', 'utf8', os.path.join(BASE_DIR, script)],
                       env=env, capture_output=True, text=True)
    check(f'{script} rodou', r.returncode == 0, (r.stderr or '')[-200:])

# Todos os dias abertos 08:00–20:00 — deixa o teste determinístico (hoje sempre é dia útil).
HORARIOS = {str(i): {'abre': '08:00', 'fecha': '20:00'} for i in range(7)}
sql_exec("UPDATE configuracoes SET horarios=%s WHERE id=1", (json.dumps(HORARIOS),))

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
    check('servidor de pé', pronto)
    if not pronto:
        print(srv.stderr.read().decode(errors='ignore')[-800:]); raise SystemExit(1)

    call('POST', '/api/login', {'email': 'regiane@barbearia.local', 'senha': 'joga123'})
    call('POST', '/api/trocar-senha', {'nova': 'joga123', 'confirma': 'joga123'})
    profs = call('GET', '/api/profissionais')['rows']
    servs = {s['nome']: s for s in call('GET', '/api/servicos')['rows']}
    P1, P2 = profs[0]['id'], profs[1]['id']
    CABELO, COMBO = servs['Cabelo']['id'], servs['Cabelo + Barba']['id']   # 30min e 60min

    hoje = date.today()
    D1 = (hoje + timedelta(days=2)).isoformat()      # dia de trabalho do teste
    D2 = (hoje + timedelta(days=3)).isoformat()
    AMANHA = (hoje + timedelta(days=1)).isoformat()

    # ══════════════════════════════════════════════════════════════════
    print("\n########## FASE 0 — a trava de conflito ##########")
    # ══════════════════════════════════════════════════════════════════
    print("== Sobreposição de intervalo (o furo que a UI escondia) ==")
    j = call('POST', '/api/agendamentos', {'profissional_id': P1, 'data': D1,
                                           'hora_inicio': '09:00', 'servicos_ids': [COMBO]})
    check('60min às 09:00 aceito', j.get('ok'), j.get('error'))
    j = call('POST', '/api/agendamentos', {'profissional_id': P1, 'data': D1,
                                           'hora_inicio': '09:30', 'servicos_ids': [CABELO]})
    check('09:30 RECUSADO (cai dentro do de 60min)', j.get('ok') is False, j.get('error'))
    j = call('POST', '/api/agendamentos', {'profissional_id': P1, 'data': D1,
                                           'hora_inicio': '08:30', 'servicos_ids': [COMBO]})
    check('08:30 de 60min RECUSADO (encosta no de 09:00)', j.get('ok') is False, j.get('error'))
    j = call('POST', '/api/agendamentos', {'profissional_id': P1, 'data': D1,
                                           'hora_inicio': '10:00', 'servicos_ids': [CABELO]})
    check('10:00 aceito (logo após o fim)', j.get('ok'), j.get('error'))
    j = call('POST', '/api/agendamentos', {'profissional_id': P2, 'data': D1,
                                           'hora_inicio': '09:00', 'servicos_ids': [CABELO]})
    check('mesmo horário em OUTRO barbeiro é aceito', j.get('ok'), j.get('error'))

    print("== Bloqueio e horário de funcionamento ==")
    call('POST', '/api/bloqueios', {'profissional_id': P1, 'data': D1,
                                    'hora_inicio': '14:00', 'hora_fim': '15:00', 'motivo': 'almoço'})
    j = call('POST', '/api/agendamentos', {'profissional_id': P1, 'data': D1,
                                           'hora_inicio': '14:00', 'servicos_ids': [CABELO]})
    check('agendar em cima do bloqueio RECUSADO', j.get('ok') is False, j.get('error'))
    j = call('POST', '/api/agendamentos', {'profissional_id': P1, 'data': D1,
                                           'hora_inicio': '13:30', 'servicos_ids': [COMBO]})
    check('60min às 13:30 RECUSADO (invade o bloqueio)', j.get('ok') is False, j.get('error'))
    j = call('POST', '/api/agendamentos', {'profissional_id': P1, 'data': D1,
                                           'hora_inicio': '21:00', 'servicos_ids': [CABELO]})
    check('21:00 RECUSADO (fora do funcionamento)', j.get('ok') is False, j.get('error'))
    j = call('POST', '/api/agendamentos', {'profissional_id': P1, 'data': D1,
                                           'hora_inicio': '19:30', 'servicos_ids': [COMBO]})
    check('60min às 19:30 RECUSADO (terminaria depois do fecha)', j.get('ok') is False, j.get('error'))

    # ══════════════════════════════════════════════════════════════════
    print("\n########## FASE 1 — a porta pública ##########")
    # ══════════════════════════════════════════════════════════════════
    print("== Desligado, a porta não existe ==")
    check('/agendar responde 404', pub('GET', '/agendar').get('_status') == 404)
    check('contexto 404', pub('GET', '/api/agendar/contexto').get('_status') == 404)
    check('disponibilidade 404', pub('GET', f'/api/agendar/disponibilidade?data={D1}').get('_status') == 404)
    check('POST agendar 404', pub('POST', '/api/agendar', {'nome': 'X'}).get('_status') == 404)

    print("== Ligando pelo Config ==")
    j = call('PUT', '/api/config', {'agendamento_online': True, 'agendamento_confirmar_manual': True,
                                    'agendamento_antecedencia_horas': 2, 'agendamento_janela_dias': 30,
                                    'marca_endereco': 'Rua das Tesouras, 100'})
    check('config salva', j.get('ok'))
    ctx = pub('GET', '/api/agendar/contexto')
    check('contexto abre sem login', ctx.get('ok'))
    check('traz serviços', len(ctx.get('servicos') or []) >= 5)
    check('traz barbeiros', len(ctx.get('profissionais') or []) == 2)
    check('traz endereço', ctx.get('endereco') == 'Rua das Tesouras, 100')
    check('confirmar_manual ligado', ctx.get('confirmar_manual') is True)

    print("== Disponibilidade NÃO vaza dado de cliente ==")
    disp = pub('GET', f'/api/agendar/disponibilidade?data={D1}&servicos_ids={CABELO}')
    bruto = json.dumps(disp, ensure_ascii=False).lower()
    check('resposta não contém nome de cliente', 'regiane' not in bruto and 'cliente_nome' not in bruto)
    check('resposta só tem hora + ids', all(set(s.keys()) == {'hora', 'profissionais'} for s in disp['slots']))
    horas = [s['hora'] for s in disp['slots']]
    check('09:00 sumiu (os dois barbeiros ocupados)', '09:00' not in horas, horas[:6])
    s0930 = next((s for s in disp['slots'] if s['hora'] == '09:30'), None)
    check('09:30 aparece oferecendo só o P2 (P1 preso no combo de 60min)',
          s0930 and s0930['profissionais'] == [P2], s0930)
    check('14:00 sumiu do P1 (bloqueio)',
          all(P1 not in s['profissionais'] for s in disp['slots'] if s['hora'] == '14:00'))

    print("== Serviço de 60min encolhe a grade ==")
    d60 = pub('GET', f'/api/agendar/disponibilidade?data={D1}&servicos_ids={COMBO}&profissional_id={P1}')
    h60 = [s['hora'] for s in d60['slots']]
    check('19:30 não aparece p/ 60min (fecha 20:00)', '19:30' not in h60, h60[-3:])
    check('duracao_min devolvida = 60', d60.get('duracao_min') == 60)

    print("== Dia fechado ==")
    fechado = {**HORARIOS, str((date.fromisoformat(D2).weekday() + 1) % 7): None}
    sql_exec("UPDATE configuracoes SET horarios=%s WHERE id=1", (json.dumps(fechado),))
    j = pub('GET', f'/api/agendar/disponibilidade?data={D2}&servicos_ids={CABELO}')
    check('dia fechado devolve fechado=true e 0 slots', j.get('fechado') is True and not j['slots'])
    sql_exec("UPDATE configuracoes SET horarios=%s WHERE id=1", (json.dumps(HORARIOS),))

    print("== Cliente novo agenda pelo link ==")
    j = pub('POST', '/api/agendar', {'nome': 'joão da silva', 'telefone': '(34) 98888-7777',
                                     'servicos_ids': [CABELO], 'data': D1, 'hora_inicio': '11:00',
                                     'profissional_id': P1})
    check('agendou', j.get('ok'), j.get('error'))
    check('status = pendente (modo eu confirmo)', j.get('status') == 'pendente', j.get('status'))
    AG_JOAO = j.get('id')
    r = sql_one("SELECT nome, origem, status FROM clientes WHERE telefone LIKE %s", ('%98888%',))
    check('cliente criado com nome MAIÚSCULO', r and r[0] == 'JOÃO DA SILVA', r)
    check("cliente marcado origem='online'", r and r[1] == 'online', r)
    check("cliente entra aprovado (o agendamento é o portão)", r and r[2] == 'aprovado', r)

    print("== Travas de abuso ==")
    j = pub('POST', '/api/agendar', {'nome': 'JOÃO DA SILVA', 'telefone': '(34) 98888-7777',
                                     'servicos_ids': [CABELO], 'data': D1, 'hora_inicio': '15:00'})
    check('2º horário em aberto pro mesmo telefone RECUSADO', j.get('ok') is False, j.get('error'))
    j = pub('POST', '/api/agendar', {'nome': 'BOT', 'telefone': '(34) 97777-6666', 'empresa': 'spam ltda',
                                     'servicos_ids': [CABELO], 'data': D1, 'hora_inicio': '16:00'})
    check('honeypot: finge sucesso e não cria nada', j.get('ok') and j.get('id') is None)
    check('honeypot não criou cliente', sql_one("SELECT 1 FROM clientes WHERE telefone LIKE %s", ('%97777%',)) is None)
    j = pub('POST', '/api/agendar', {'nome': 'MARIA', 'telefone': '(34) 96666-5555',
                                     'servicos_ids': [CABELO], 'data': (hoje + timedelta(days=90)).isoformat(),
                                     'hora_inicio': '10:00'})
    check('fora da janela de 30 dias RECUSADO', j.get('ok') is False, j.get('error'))
    j = pub('POST', '/api/agendar', {'nome': 'MARIA', 'telefone': '(34) 96666-5555',
                                     'servicos_ids': [CABELO], 'data': (hoje - timedelta(days=1)).isoformat(),
                                     'hora_inicio': '10:00'})
    check('data no passado RECUSADA', j.get('ok') is False, j.get('error'))
    j = pub('POST', '/api/agendar', {'nome': 'MARIA', 'telefone': '123',
                                     'servicos_ids': [CABELO], 'data': D1, 'hora_inicio': '10:30'})
    check('telefone curto RECUSADO', j.get('ok') is False, j.get('error'))
    j = pub('POST', '/api/agendar', {'nome': '', 'telefone': '(34) 96666-5555',
                                     'servicos_ids': [CABELO], 'data': D1, 'hora_inicio': '10:30'})
    check('sem nome RECUSADO', j.get('ok') is False, j.get('error'))
    j = pub('POST', '/api/agendar', {'nome': 'MARIA', 'telefone': '(34) 96666-5555',
                                     'servicos_ids': [], 'data': D1, 'hora_inicio': '10:30'})
    check('sem serviço RECUSADO', j.get('ok') is False, j.get('error'))
    j = pub('POST', '/api/agendar', {'nome': 'MARIA', 'telefone': '(34) 96666-5555',
                                     'servicos_ids': [99999], 'data': D1, 'hora_inicio': '10:30'})
    check('serviço inexistente RECUSADO', j.get('ok') is False, j.get('error'))

    print("== Antecedência mínima ==")
    call('PUT', '/api/config', {'agendamento_antecedencia_horas': 999})
    j = pub('GET', f'/api/agendar/disponibilidade?data={hoje.isoformat()}&servicos_ids={CABELO}')
    check('com 999h de antecedência, hoje não tem slot', not j['slots'], len(j['slots']))
    j = pub('POST', '/api/agendar', {'nome': 'ZE', 'telefone': '(34) 95555-4444',
                                     'servicos_ids': [CABELO], 'data': hoje.isoformat(), 'hora_inicio': '19:00'})
    check('agendar hoje RECUSADO pela antecedência', j.get('ok') is False, j.get('error'))
    call('PUT', '/api/config', {'agendamento_antecedencia_horas': 2})

    print("== Corrida: o horário some da grade e não aceita segundo ==")
    disp = pub('GET', f'/api/agendar/disponibilidade?data={D1}&servicos_ids={CABELO}&profissional_id={P1}')
    check('11:00 saiu da grade do P1', '11:00' not in [s['hora'] for s in disp['slots']])
    j = pub('POST', '/api/agendar', {'nome': 'OUTRA PESSOA', 'telefone': '(34) 94444-3333',
                                     'servicos_ids': [CABELO], 'data': D1, 'hora_inicio': '11:00',
                                     'profissional_id': P1})
    check('mesmo slot pro P1 RECUSADO', j.get('ok') is False, j.get('error'))

    print("== 'Tanto faz' cai no barbeiro livre ==")
    j = pub('POST', '/api/agendar', {'nome': 'CARLOS', 'telefone': '(34) 93333-2222',
                                     'servicos_ids': [CABELO], 'data': D1, 'hora_inicio': '11:00'})
    check('sem escolher barbeiro, achou o P2 livre', j.get('ok'), j.get('error'))
    AG_CARLOS = j.get('id')
    r = sql_one("SELECT profissional_id FROM agendamentos WHERE id=%s", (AG_CARLOS,))
    check('foi mesmo pro outro barbeiro', r and r[0] == P2, r)

    print("== Meus horários: nome + telefone ==")
    j = pub('POST', '/api/agendar/meus', {'nome': 'JOÃO DA SILVA', 'telefone': '(34) 98888-7777'})
    check('João vê o horário dele', j.get('ok') and len(j['rows']) == 1, j.get('error'))
    check('vem com o serviço', j['ok'] and j['rows'][0]['servicos'] == ['Cabelo'], j.get('rows'))
    check('e com o status pendente', j['ok'] and j['rows'][0]['status'] == 'pendente')
    check('não devolve horário de terceiro', j['ok'] and all(r['id'] != AG_CARLOS for r in j['rows']))
    j = pub('POST', '/api/agendar/meus', {'nome': 'joao da silva', 'telefone': '34988887777'})
    check('acento/caixa/máscara não atrapalham', j.get('ok') and len(j['rows']) == 1, j.get('error'))
    j = pub('POST', '/api/agendar/meus', {'nome': 'OUTRO NOME', 'telefone': '(34) 98888-7777'})
    check('telefone certo + nome errado NÃO abre', j.get('ok') is False, j.get('error'))
    j = pub('POST', '/api/agendar/meus', {'nome': 'JOÃO DA SILVA', 'telefone': '(34) 90000-0000'})
    check('telefone desconhecido NÃO abre', j.get('ok') is False)

    print("== Cancelamento pelo cliente ==")
    j = pub('POST', '/api/agendar/cancelar', {'nome': 'JOÃO DA SILVA', 'telefone': '(34) 98888-7777',
                                              'agendamento_id': AG_CARLOS})
    check('João NÃO cancela o horário do Carlos', j.get('ok') is False, j.get('error'))
    check('o do Carlos segue de pé',
          sql_one("SELECT status FROM agendamentos WHERE id=%s", (AG_CARLOS,))[0] == 'pendente')
    j = pub('POST', '/api/agendar/cancelar', {'nome': 'JOÃO DA SILVA', 'telefone': '(34) 98888-7777',
                                              'agendamento_id': AG_JOAO})
    check('João cancela o próprio', j.get('ok'), j.get('error'))
    check('virou cancelado', sql_one("SELECT status FROM agendamentos WHERE id=%s", (AG_JOAO,))[0] == 'cancelado')
    disp = pub('GET', f'/api/agendar/disponibilidade?data={D1}&servicos_ids={CABELO}&profissional_id={P1}')
    check('11:00 voltou pra grade do P1', '11:00' in [s['hora'] for s in disp['slots']])
    j = pub('POST', '/api/agendar/meus', {'nome': 'JOÃO DA SILVA', 'telefone': '(34) 98888-7777'})
    check('lista do João ficou vazia', j.get('ok') and len(j['rows']) == 0)
    j = pub('POST', '/api/agendar', {'nome': 'JOÃO DA SILVA', 'telefone': '(34) 98888-7777',
                                     'servicos_ids': [CABELO], 'data': D1, 'hora_inicio': '11:00',
                                     'profissional_id': P1})
    check('e ele já consegue marcar de novo', j.get('ok'), j.get('error'))
    AG_JOAO = j.get('id')
    check('não duplicou o cadastro dele',
          sql_one("SELECT COUNT(*) FROM clientes WHERE telefone LIKE %s", ('%98888%',))[0] == 1)

    print("== Barbeiro fora do online some do link ==")
    call('PUT', f'/api/profissionais/{P2}', {'aceita_online': False})
    ctx = pub('GET', '/api/agendar/contexto')
    check('só sobra 1 barbeiro no link', len(ctx['profissionais']) == 1, ctx['profissionais'])
    disp = pub('GET', f'/api/agendar/disponibilidade?data={D1}&servicos_ids={CABELO}')
    check('nenhum slot oferece o P2', all(P2 not in s['profissionais'] for s in disp['slots']))
    call('PUT', f'/api/profissionais/{P2}', {'aceita_online': True})

    print("== Modo automático (sem confirmar manual) ==")
    call('PUT', '/api/config', {'agendamento_confirmar_manual': False})
    j = pub('POST', '/api/agendar', {'nome': 'AUTO CLIENTE', 'telefone': '(34) 92222-1111',
                                     'servicos_ids': [CABELO], 'data': D1, 'hora_inicio': '16:30'})
    check('cai direto como agendado', j.get('ok') and j.get('status') == 'agendado', j.get('status'))
    AG_AUTO = j.get('id')
    call('PUT', '/api/config', {'agendamento_confirmar_manual': True})

    # ══════════════════════════════════════════════════════════════════
    print("\n########## FASE 2 — a fila de mensagens ##########")
    # ══════════════════════════════════════════════════════════════════
    print("== Pendente ainda não gera aviso (ninguém foi aceito) ==")
    m = call('GET', '/api/mensagens/pendentes')
    ids_aceite = [x['agendamento_id'] for x in m['aceites']]
    check('o pendente do João não está na fila', AG_JOAO not in ids_aceite, ids_aceite)
    check('mas o do modo automático está', AG_AUTO in ids_aceite, ids_aceite)

    print("== Aceitar o pedido gera o aviso ==")
    call('PUT', f'/api/agendamentos/{AG_JOAO}', {'status': 'agendado'})
    m = call('GET', '/api/mensagens/pendentes')
    aceite = next((x for x in m['aceites'] if x['agendamento_id'] == AG_JOAO), None)
    check('João entrou na fila de aceite', aceite is not None)
    check('texto traz o nome da barbearia', aceite and 'JOGA Barbearia' in aceite['texto'], aceite['texto'][:60] if aceite else '')
    check('texto traz o endereço', aceite and 'Rua das Tesouras' in aceite['texto'])
    check('texto traz o link /agendar', aceite and '/agendar' in aceite['texto'])
    check('texto traz a hora', aceite and '11:00' in aceite['texto'])

    print("== O link do WhatsApp mantém o 9º dígito ==")
    check('wa.me montado', aceite and aceite['wa_url'].startswith('https://wa.me/'))
    numero = aceite['wa_url'].split('/')[-1].split('?')[0]
    check('número = 55 + DDD + 9 dígitos', numero == '5534988887777', numero)
    check('NÃO usou o canônico da UazAPI (sem o 9)', numero != '553488887777', numero)
    check('texto vai codificado na URL', '?text=' in aceite['wa_url'] and ' ' not in aceite['wa_url'])

    print("== Texto sem caractere que o WhatsApp Desktop quebra ==")
    # O link sai em UTF-8 correto, mas o Windows repassa a URL pro WhatsApp Desktop em codepage
    # ANSI: acento sobrevive (Latin-1), emoji vira "?" na tela do CLIENTE da barbearia.
    # Este check existe pra ninguém reintroduzir emoji "só pra enfeitar" e quebrar de novo.
    for m in [aceite] + m['lembretes']:
        fora = sorted({ch for ch in m['texto'] if ord(ch) > 0xFF})
        check(f"[{m['tipo']}] só caracteres seguros (nada acima de U+00FF)", not fora, fora)

    print("== Marcar enviado e desfazer ==")
    antes = call('GET', '/api/mensagens/pendentes')['total']
    call('POST', f'/api/mensagens/aceite/{AG_JOAO}/enviado')
    m = call('GET', '/api/mensagens/pendentes')
    check('saiu da fila', m['total'] == antes - 1 and AG_JOAO not in [x['agendamento_id'] for x in m['aceites']])
    call('DELETE', f'/api/mensagens/aceite/{AG_JOAO}/enviado')
    m = call('GET', '/api/mensagens/pendentes')
    check('desfazer devolve pra fila', AG_JOAO in [x['agendamento_id'] for x in m['aceites']])
    call('POST', f'/api/mensagens/aceite/{AG_JOAO}/enviado')

    print("== Lembrete D-1 aparece só na véspera ==")
    j = pub('POST', '/api/agendar', {'nome': 'AMANHA CLIENTE', 'telefone': '(34) 91111-0000',
                                     'servicos_ids': [CABELO], 'data': AMANHA, 'hora_inicio': '10:00'})
    ag_amanha = j.get('id')
    check('agendado pra amanhã', j.get('ok'), j.get('error'))
    m = call('GET', '/api/mensagens/pendentes')
    check('pendente NÃO entra no lembrete', ag_amanha not in [x['agendamento_id'] for x in m['lembretes']])
    call('PUT', f'/api/agendamentos/{ag_amanha}', {'status': 'agendado'})
    m = call('GET', '/api/mensagens/pendentes')
    lem = next((x for x in m['lembretes'] if x['agendamento_id'] == ag_amanha), None)
    check('depois de aceito, entra no lembrete de amanhã', lem is not None)
    check('texto do lembrete pede confirmação', lem and 'confirmar' in lem['texto'].lower())
    check('agendamento de D+2 não está no lembrete',
          AG_JOAO not in [x['agendamento_id'] for x in m['lembretes']])

    print("== Cliente sem telefone não quebra a fila ==")
    sql_exec("UPDATE clientes SET telefone=NULL WHERE nome='AMANHA CLIENTE'")
    m = call('GET', '/api/mensagens/pendentes')
    lem = next((x for x in m['lembretes'] if x['agendamento_id'] == ag_amanha), None)
    check('item aparece marcado como sem telefone', lem and lem['sem_telefone'] is True)
    check('e sem wa_url', lem and lem['wa_url'] is None)

    print("== Recusar tira da agenda e da fila ==")
    call('PUT', f'/api/agendamentos/{AG_CARLOS}', {'status': 'cancelado'})
    m = call('GET', '/api/mensagens/pendentes')
    check('recusado não vira mensagem', AG_CARLOS not in [x['agendamento_id'] for x in m['aceites']])
    ag = call('GET', f'/api/agenda?data={D1}')
    check('e sumiu da agenda do dia', all(a['id'] != AG_CARLOS for a in ag['agendamentos']))

    print("== Agenda interna enxerga origem e status do online ==")
    ag = call('GET', f'/api/agenda?data={D1}')
    onl = next((a for a in ag['agendamentos'] if a['id'] == AG_JOAO), None)
    check("agendamento traz origem='online'", onl and onl['origem'] == 'online', onl['origem'] if onl else None)
    check("e status 'agendado' após o aceite", onl and onl['status'] == 'agendado')

    print("== RBAC: barbeiro não mexe em mensagem ==")
    # Sessão separada (o /logout devolve redirect em HTML, não JSON)
    barb_op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    def barb(m, p, b=None): return _call(barb_op, m, p, b)
    barb('POST', '/api/login', {'email': 'joaovictor@barbearia.local', 'senha': 'joga123'})
    barb('POST', '/api/trocar-senha', {'nova': 'joga123', 'confirma': 'joga123'})
    check('barbeiro entra na própria agenda', barb('GET', f'/api/agenda?data={D1}').get('ok') is True)
    check('barbeiro barrado na fila de mensagens',
          barb('GET', '/api/mensagens/pendentes').get('_status') == 403)
    check('barbeiro barrado nas formas de pagamento',
          barb('GET', '/api/formas-pagamento').get('_status') == 403)
    check('barbeiro barrado no relatório de taxas',
          barb('GET', '/api/relatorios/taxas').get('_status') == 403)
    check('barbeiro barrado no caixa', barb('GET', '/api/caixa/fechamento').get('_status') == 403)

finally:
    srv.terminate()
    try: srv.wait(timeout=10)
    except Exception: srv.kill()
    derrubar_banco()

print(f"\n========== RESULTADO: {OK} OK · {FALHA} FALHA ==========")
sys.exit(1 if FALHA else 0)
