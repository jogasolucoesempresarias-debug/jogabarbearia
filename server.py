"""
JOGA Barbearia — Backend (Flask + Waitress)
Rode: python -X utf8 server.py   ·   Acesse: http://localhost:5000
"""
import os
import json
import hmac
import math
import secrets
import calendar
import threading
import functools
import urllib.request
import urllib.error
import urllib.parse
from datetime import date, datetime, time, timedelta

import psycopg2
from psycopg2.extras import RealDictCursor, Json
from flask import Flask, jsonify, send_from_directory, request, session, redirect
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=None)
app.secret_key = os.getenv('SECRET_KEY', 'dev-secret-change-me')
CORS(app, supports_credentials=True)


# ── Banco ─────────────────────────────────────────────────────────────
def get_db():
    return psycopg2.connect(
        host=os.getenv('DB_HOST', 'localhost'), port=os.getenv('DB_PORT', '5432'),
        dbname=os.getenv('DB_NAME', 'joga_barbearia'),
        user=os.getenv('DB_USER', 'postgres'), password=os.getenv('DB_PASSWORD', ''),
        options='-c timezone=America/Sao_Paulo',   # NOW()/CURRENT_DATE/::date em horário de Brasília
    )


def _ser(v):
    from decimal import Decimal
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, time):
        return v.strftime('%H:%M')
    return v


def _clean(row):
    return None if row is None else {k: _ser(v) for k, v in row.items()}


def q_all(sql, params=None):
    conn = get_db(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(sql, params or ()); rows = [_clean(r) for r in cur.fetchall()]
    cur.close(); conn.close(); return rows


def q_one(sql, params=None):
    conn = get_db(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(sql, params or ()); row = cur.fetchone()
    cur.close(); conn.close(); return _clean(row)


def execute(sql, params=None, returning=False):
    conn = get_db(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(sql, params or ()); out = _clean(cur.fetchone()) if returning else None
    conn.commit(); cur.close(); conn.close(); return out


def scalar(sql, params=None):
    from decimal import Decimal
    conn = get_db(); cur = conn.cursor()
    cur.execute(sql, params or ()); row = cur.fetchone()
    cur.close(); conn.close()
    v = row[0] if row else None
    return float(v) if isinstance(v, Decimal) else v


# ── Helpers ───────────────────────────────────────────────────────────
def parse_date(s, default=None):
    try:
        return date.fromisoformat(str(s)[:10])
    except (ValueError, TypeError):
        return default


def js_weekday(d: date) -> int:
    """0=domingo .. 6=sábado (igual JS getDay)."""
    return (d.weekday() + 1) % 7


def cfg():
    return q_one("SELECT * FROM configuracoes WHERE id=1") or {}


# ── Formas de pagamento e a taxa da maquininha ────────────────────────
# A tabela `formas_pagamento` é a fonte da verdade (nome + taxa). O `configuracoes.formas_pagamento`
# (JSONB) virou vestigial — o /api/config segue devolvendo a lista de NOMES derivada daqui porque
# seis telas consomem aquele formato.
TAXA_CATEGORIA = 'Taxas de cartão'


def formas_ativas():
    """Nomes das formas ativas, na ordem — o contrato que as telas esperam (array de strings)."""
    return [r['nome'] for r in q_all(
        "SELECT nome FROM formas_pagamento WHERE ativo ORDER BY ordem, nome")]


def taxa_da_forma(forma):
    """% da maquininha para uma forma. Desconhecida, inativa ou sem taxa → 0."""
    if not forma:
        return 0.0
    row = q_one("SELECT taxa_pct FROM formas_pagamento WHERE nome=%s", (forma,))
    return float(row['taxa_pct'] or 0) if row else 0.0


def registrar_taxa(mov_id, valor_bruto, forma, data_mov=None):
    """Lança a taxa da maquininha como despesa AMARRADA ao movimento de receita (ref_id=mov_id).

    A receita continua BRUTA de propósito: a taxa é custo, não desconto no faturamento. Se fosse
    abatida da receita, a comissão do barbeiro — que sai de comanda_itens.subtotal — mudaria
    conforme a forma de pagamento que o CLIENTE escolheu. Ninguém decidiu isso.

    Dinheiro/Pix (taxa 0) não geram lançamento nenhum. Idempotente: refazer substitui.
    """
    if not mov_id:
        return None
    execute("DELETE FROM movimentos WHERE origem='taxa' AND ref_id=%s", (mov_id,))
    valor = round(float(valor_bruto or 0) * taxa_da_forma(forma) / 100.0, 2)
    if valor <= 0:
        return None
    row = execute("""INSERT INTO movimentos (tipo, origem, ref_id, descricao, categoria, valor,
                       forma_pagamento, data, status, criado_por)
                     VALUES ('despesa','taxa',%s,%s,%s,%s,%s,COALESCE(%s::date,CURRENT_DATE),'pago',%s)
                     RETURNING id""",
                  (mov_id, f"Taxa {forma}", TAXA_CATEGORIA, valor, forma, data_mov,
                   session.get('user_id')), returning=True)
    return row['id']


def remover_taxas_de(mov_ids):
    """Apaga as taxas filhas destes movimentos. Chamar SEMPRE antes de apagar os pais."""
    ids = [i for i in (mov_ids or []) if i]
    if ids:
        execute("DELETE FROM movimentos WHERE origem='taxa' AND ref_id = ANY(%s)", (ids,))


def telefone_existe(telefone, excluir_id=None):
    """True se já há cliente ativo com o mesmo telefone (comparando só os dígitos).
    excluir_id ignora o próprio cliente (usado na edição)."""
    digitos = ''.join(ch for ch in str(telefone or '') if ch.isdigit())
    if not digitos:
        return False
    if excluir_id:
        return bool(scalar("""SELECT 1 FROM clientes WHERE ativo
            AND regexp_replace(telefone, '\\D', '', 'g') = %s AND id <> %s LIMIT 1""", (digitos, excluir_id)))
    return bool(scalar("""SELECT 1 FROM clientes WHERE ativo
        AND regexp_replace(telefone, '\\D', '', 'g') = %s LIMIT 1""", (digitos,)))


# Rate-limit simples em memória (instância única por cliente): {ip: [timestamps]}.
# Reseta em redeploy — suficiente p/ conter abuso do cadastro público sem captcha.
_RATE = {}


def rate_limit_ok(ip, maximo=5, janela_seg=600, consumir=True):
    """consumir=False só CONSULTA a cota, sem gastar.

    Gastar cota em tentativa que falhou pune quem errou o próprio telefone: oito typos e o
    cliente real fica trancado 10 minutos. Onde a validação é longa, consulte primeiro e
    consuma só quando a coisa foi de fato criada.
    """
    import time
    agora = time.time()
    hist = [t for t in _RATE.get(ip, []) if agora - t < janela_seg]
    if len(hist) >= maximo:
        _RATE[ip] = hist
        return False
    if consumir:
        hist.append(agora)
    _RATE[ip] = hist
    return True


# ── Alerta de WhatsApp (UazAPI) ───────────────────────────────────────
# Mesmo contrato do DanfeZap / joga-diagnostico-api (POST /send/text, header `token`), mas com
# urllib da stdlib: é UM post, não vale puxar httpx pra dentro da imagem.
# ALERTAS_ATIVO é a trava mestra — desligado, nada é enviado de verdade (protege dev e os smokes,
# que criam e "enviam" fichas o tempo todo).
UAZAPI_URL = os.getenv('UAZAPI_URL', 'https://free.uazapi.com').rstrip('/')
UAZAPI_TOKEN = os.getenv('UAZAPI_TOKEN', '').strip()
ALERTAS_ATIVO = os.getenv('ALERTAS_ATIVO', '').strip().lower() in ('1', 'true', 'sim')
ALERTA_WHATSAPP = [n.strip() for n in os.getenv('ALERTA_WHATSAPP', '').split(',') if n.strip()]


def normalizar_telefone_br(telefone):
    """Canônico da UazAPI: 55 + DDD + 8 dígitos (sem o "9 extra" do celular)."""
    numero = ''.join(filter(str.isdigit, str(telefone or '')))
    if not numero.startswith('55'):
        numero = '55' + numero
    if len(numero) == 13 and numero[4] == '9':
        numero = numero[:4] + numero[5:]
    return numero


def _uazapi_enviar(numero, texto):
    req = urllib.request.Request(
        f"{UAZAPI_URL}/send/text",
        data=json.dumps({'number': normalizar_telefone_br(numero), 'text': texto}).encode(),
        headers={'Content-Type': 'application/json', 'token': UAZAPI_TOKEN},
        method='POST')
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return 200 <= r.status < 300, f"status {r.status}"
    except urllib.error.HTTPError as e:
        return False, f"status {e.code}: {e.read().decode(errors='ignore')[:200]}"
    except Exception as e:                                   # rede caiu, DNS, timeout…
        return False, str(e)


def alerta_whatsapp(texto):
    """Dispara em BACKGROUND: o dono acabou de clicar 'enviar' e não pode esperar rede.
    Falha de WhatsApp nunca derruba o salvamento da ficha — no máximo vira log."""
    if not ALERTAS_ATIVO:
        print(f"[alerta] desligado (ALERTAS_ATIVO=0). Enviaria:\n{texto}")
        return
    if not (UAZAPI_TOKEN and ALERTA_WHATSAPP):
        print("[alerta] ligado, mas falta UAZAPI_TOKEN ou ALERTA_WHATSAPP — nada enviado.")
        return

    def _worker():
        for numero in ALERTA_WHATSAPP:
            ok, detalhe = _uazapi_enviar(numero, texto)
            print(f"[alerta] {numero}: {'enviado' if ok else 'FALHOU'} ({detalhe})")

    threading.Thread(target=_worker, daemon=True).start()


def visita_coberta_no_dia(cliente_id, d, exclude_comanda_id=None):
    """Nº de comandas (não canceladas) do cliente com item coberto no dia d.
    exclude_comanda_id ignora a própria comanda (p/ não bloquear 2 serviços na MESMA visita)."""
    if not cliente_id:
        return 0
    return scalar("""SELECT COUNT(DISTINCT c.id) FROM comandas c
        JOIN comanda_itens ci ON ci.comanda_id = c.id
        WHERE c.cliente_id = %s AND ci.coberto_plano AND c.status <> 'cancelada'
          AND COALESCE(c.fechada_em::date, c.aberta_em::date) = %s
          AND (%s::int IS NULL OR c.id <> %s::int)""",
        (cliente_id, d, exclude_comanda_id, exclude_comanda_id)) or 0


def _hm(t):
    """'HH:MM[:SS]' (ou time já serializado) → minutos desde a meia-noite."""
    h, m = str(t)[:5].split(':')
    return int(h) * 60 + int(m)


def slot_livre(prof_id, d: date, hora_inicio, slots, slot_min=None, excluir_id=None):
    """(ok, motivo) para um agendamento de `slots` slots começando em hora_inicio.

    Confere as três coisas que só a UI checava e o backend deixava passar: horário de
    funcionamento, SOBREPOSIÇÃO de intervalo (não apenas o mesmo minuto de início) e bloqueio
    do barbeiro. Comparar só `hora_inicio` deixa um serviço de 60min às 9h não impedir alguém
    de marcar 9h30 — com uma porta pública, quem posta é um desconhecido e a agenda dobra.
    """
    c = cfg()
    slot_min = slot_min or (c.get('slot_min') or 30)
    ini = _hm(hora_inicio)
    fim = ini + max(1, int(slots or 1)) * slot_min

    dia_cfg = (c.get('horarios') or {}).get(str(js_weekday(d)))
    if not dia_cfg or not dia_cfg.get('abre') or not dia_cfg.get('fecha'):
        return (False, 'A barbearia não abre nesse dia.')
    if ini < _hm(dia_cfg['abre']) or fim > _hm(dia_cfg['fecha']):
        return (False, f"Fora do horário de funcionamento ({dia_cfg['abre']}–{dia_cfg['fecha']}).")

    ags = q_all("""SELECT id, hora_inicio, duracao_slots FROM agendamentos
                   WHERE profissional_id=%s AND data=%s AND status NOT IN ('cancelado')""", (prof_id, d))
    bloqs = q_all("SELECT hora_inicio, hora_fim FROM bloqueios WHERE profissional_id=%s AND data=%s",
                  (prof_id, d))
    return _checa_intervalo(ini, fim, ags, bloqs, slot_min, excluir_id)


def _checa_intervalo(ini, fim, ags, bloqs, slot_min, excluir_id=None):
    """Núcleo da checagem, com as listas do dia JÁ carregadas — a grade de disponibilidade
    percorre dezenas de slots e não pode bater no banco a cada um."""
    for a in ags:
        if excluir_id and a['id'] == excluir_id:
            continue
        a_ini = _hm(a['hora_inicio'])
        a_fim = a_ini + max(1, a['duracao_slots'] or 1) * slot_min
        if ini < a_fim and a_ini < fim:                      # [ini,fim) cruza [a_ini,a_fim)
            return (False, 'Esse horário já está ocupado.')
    for b in bloqs:
        if ini < _hm(b['hora_fim']) and _hm(b['hora_inicio']) < fim:
            return (False, 'O barbeiro não atende nesse horário.')
    return (True, None)


def cobertura_plano(cliente_id, servico_id, d: date, exclude_comanda_id=None):
    """Retorna (coberto:bool, assinatura_id|None) p/ um serviço de um cliente numa data.
    Regra de 1 visita coberta por dia: se já há OUTRA comanda coberta do cliente no dia, não cobre
    de novo (o benefício do plano já foi usado) — evita dobrar visita/comissão."""
    if not cliente_id:
        return (False, None)
    row = q_one("""
        SELECT a.id, p.dias_inclusos, p.limite_uso
        FROM assinaturas a
        JOIN planos p ON p.id = a.plano_id
        JOIN plano_servicos ps ON ps.plano_id = p.id AND ps.servico_id = %s
        WHERE a.cliente_id = %s AND a.status = 'ativa'
          AND a.data_inicio <= %s AND (a.data_fim IS NULL OR a.data_fim >= %s)
        LIMIT 1
    """, (servico_id, cliente_id, d, d))
    if not row:
        return (False, None)
    dias = row['dias_inclusos'] or []
    if js_weekday(d) not in dias:
        return (False, None)
    if visita_coberta_no_dia(cliente_id, d, exclude_comanda_id) > 0:
        return (False, None)
    if row['limite_uso'] is not None:
        usados = scalar("""
            SELECT COUNT(*) FROM comanda_itens ci JOIN comandas c ON c.id = ci.comanda_id
            WHERE ci.assinatura_id = %s AND ci.coberto_plano
              AND date_trunc('month', c.aberta_em) = date_trunc('month', %s::timestamp)
        """, (row['id'], d)) or 0
        if usados >= row['limite_uso']:
            return (False, None)
    return (True, row['id'])


# ── Auth ──────────────────────────────────────────────────────────────
def login_required(f):
    @functools.wraps(f)
    def dec(*a, **k):
        if 'user_id' not in session:
            if request.path.startswith('/api/'):
                return jsonify({'ok': False, 'error': 'Não autenticado'}), 401
            return redirect('/login')
        if session.get('must_change_password') and request.path not in ('/trocar-senha', '/api/trocar-senha'):
            if request.path.startswith('/api/'):
                return jsonify({'ok': False, 'error': 'Troque a senha', 'redirect': '/trocar-senha'}), 403
            return redirect('/trocar-senha')
        return f(*a, **k)
    return dec


def role_required(*roles):
    def wrap(f):
        @functools.wraps(f)
        def dec(*a, **k):
            if session.get('role') not in roles:
                return jsonify({'ok': False, 'error': 'Acesso negado'}), 403
            return f(*a, **k)
        return dec
    return wrap


# ── RBAC do barbeiro: só a própria agenda + a própria comissão ────────
# Tudo o mais (caixa, despesas, comandas, clientes, comissões, DRE, config) é bloqueado,
# mesmo se ele digitar a URL direto. Defesa de verdade, não só esconder no menu.
BARBEIRO_PERMITIDO = {
    '/', '/barbeiro', '/login', '/trocar-senha', '/logout', '/health', '/manifest.json', '/sw.js',
    '/api/me', '/api/login', '/api/trocar-senha', '/api/agenda',
    '/api/relatorios/minha-comissao', '/api/servicos',
}


@app.before_request
def _rbac_barbeiro():
    if session.get('role') != 'barbeiro':
        return
    p = request.path
    if p in BARBEIRO_PERMITIDO or p.startswith('/static/'):
        return
    if p.startswith('/api/'):
        return jsonify({'ok': False, 'error': 'Acesso restrito ao caixa/dono'}), 403
    return redirect('/')


# ── Páginas ───────────────────────────────────────────────────────────
PAGINAS = {
    '/':            'agenda.html',
    '/comanda':     'comanda.html',
    '/clientes':    'clientes.html',
    '/assinaturas': 'assinaturas.html',
    '/caixa':       'caixa.html',
    '/despesas':    'despesas.html',
    '/relatorios':  'relatorios.html',
    '/taxas':       'taxas.html',
    '/uso':         'uso.html',
    '/dre':         'dre.html',
    '/config':      'config.html',
    '/barbeiro':    'barbeiro.html',
}


@app.route('/static/<path:filename>')
def static_assets(filename):
    return send_from_directory(os.path.join(BASE_DIR, 'static'), filename)


@app.route('/manifest.json')
def manifest():
    return send_from_directory(BASE_DIR, 'manifest.json')


@app.route('/sw.js')
def service_worker():
    return send_from_directory(BASE_DIR, 'sw.js')


@app.route('/health')
def health():
    return jsonify({'ok': True, 'service': 'joga-barbearia'}), 200


@app.route('/login', methods=['GET'])
def login_page():
    if 'user_id' in session and not session.get('must_change_password'):
        return redirect('/')
    return send_from_directory(BASE_DIR, 'login.html')


@app.route('/trocar-senha', methods=['GET'])
def trocar_senha_page():
    if 'user_id' not in session:
        return redirect('/login')
    return send_from_directory(BASE_DIR, 'trocar-senha.html')


# ── Autocadastro público (QR) — SEM login, a única porta pública do sistema ──
@app.route('/cadastro', methods=['GET'])
def cadastro_page():
    return send_from_directory(BASE_DIR, 'cadastro.html')


@app.route('/api/cadastro/contexto')
def cadastro_contexto():
    """Marca da barbearia p/ a página pública ficar com a identidade da instância. Read-only."""
    c = cfg()
    # modo_demo alimenta a caixa de credenciais na tela de login — a demo é pública e o prospect
    # não pode ter que pedir senha pra ninguém.
    return jsonify({'ok': True, 'marca_nome': c.get('marca_nome') or 'Barbearia',
                    'modo_demo': MODO_DEMO})


@app.route('/api/cadastro/publico', methods=['POST'])
def cadastro_publico():
    """Cliente se cadastra sozinho via QR. Entra como 'pendente' até a dona aprovar."""
    d = request.get_json() or {}
    if (d.get('empresa') or '').strip():          # honeypot: bot preencheu → finge sucesso e ignora
        return jsonify({'ok': True})
    if not rate_limit_ok(request.remote_addr or 'desconhecido'):
        return jsonify({'ok': False, 'error': 'Muitos cadastros. Tente de novo em alguns minutos.'}), 429
    nome = (d.get('nome') or '').strip().upper()
    telefone = (d.get('telefone') or '').strip()
    if not nome:
        return jsonify({'ok': False, 'error': 'Informe seu nome'}), 400
    if not telefone:
        return jsonify({'ok': False, 'error': 'Informe seu telefone'}), 400
    if telefone_existe(telefone):
        return jsonify({'ok': False, 'error': 'Este telefone já está cadastrado'}), 409
    execute("INSERT INTO clientes (nome, telefone, status, origem) VALUES (%s,%s,'pendente','qr')",
            (nome, telefone))
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════════
# Agendamento online — a segunda porta pública do sistema
# ══════════════════════════════════════════════════════════════════════
# O cliente marca sozinho por um link, sem app e sem conta: nome e telefone bastam. Depois ele
# volta na mesma página, entra com nome + telefone e vê/cancela os próprios horários.
#
# REGRA DE OURO destas rotas: nunca devolver dado de OUTRO cliente. A disponibilidade responde
# só livre/ocupado — o /api/agenda (que devolve nome de todo mundo do dia) é de uso interno e
# NÃO pode ser reaproveitado aqui.
def _norm_nome(s):
    """MAIÚSCULO sem acento, p/ casar o nome digitado com o cadastrado sem implicância."""
    import unicodedata
    s = unicodedata.normalize('NFKD', str(s or '').strip().upper())
    return ''.join(ch for ch in s if not unicodedata.combining(ch))


def so_digitos(s):
    return ''.join(ch for ch in str(s or '') if ch.isdigit())


def agendamento_cfg():
    c = cfg()
    return {
        'ligado': bool(c.get('agendamento_online')),
        'confirmar_manual': bool(c.get('agendamento_confirmar_manual')),
        'antecedencia_horas': int(c.get('agendamento_antecedencia_horas') or 0),
        'janela_dias': int(c.get('agendamento_janela_dias') or 30),
        'slot_min': c.get('slot_min') or 30,
        'horarios': c.get('horarios') or {},
        'marca_nome': c.get('marca_nome') or 'Barbearia',
        'marca_endereco': c.get('marca_endereco') or '',
    }


def cliente_por_telefone(telefone):
    d = so_digitos(telefone)
    if not d:
        return None
    return q_one("""SELECT * FROM clientes WHERE ativo
                    AND regexp_replace(telefone, '\\D', '', 'g') = %s ORDER BY id LIMIT 1""", (d,))


def grade_do_dia(d: date, ac):
    """Lista de horários ('HH:MM') que o dia oferece, do abre ao fecha. [] = fechado."""
    dia = (ac['horarios'] or {}).get(str(js_weekday(d)))
    if not dia or not dia.get('abre') or not dia.get('fecha'):
        return []
    ini, fim, step = _hm(dia['abre']), _hm(dia['fecha']), ac['slot_min']
    return [f"{m // 60:02d}:{m % 60:02d}" for m in range(ini, fim, step)]


def _servicos_validos(ids):
    ids = [int(x) for x in (ids or []) if str(x).isdigit()]
    return q_all("SELECT id, nome, preco, duracao_min FROM servicos WHERE ativo AND id = ANY(%s)",
                 (ids,)) if ids else []


@app.route('/agendamento', methods=['GET'])
def agendar_page():
    if not agendamento_cfg()['ligado'] or MODO_COLETA:
        return jsonify({'ok': False, 'error': 'Agendamento online indisponível'}), 404
    return send_from_directory(BASE_DIR, 'agendar.html')


@app.route('/api/agendar/contexto')
def agendar_contexto():
    """O que a página pública precisa pra montar o fluxo. Read-only, sem dado de cliente."""
    ac = agendamento_cfg()
    if not ac['ligado'] or MODO_COLETA:
        return jsonify({'ok': False, 'error': 'Agendamento online indisponível'}), 404
    hoje_ = date.today()
    return jsonify({
        'ok': True, 'marca_nome': ac['marca_nome'], 'endereco': ac['marca_endereco'],
        'servicos': q_all("SELECT id, nome, preco, duracao_min FROM servicos WHERE ativo ORDER BY nome"),
        'profissionais': q_all("SELECT id, nome FROM profissionais WHERE ativo AND aceita_online ORDER BY nome"),
        'confirmar_manual': ac['confirmar_manual'],
        'primeiro_dia': hoje_.isoformat(),
        'ultimo_dia': (hoje_ + timedelta(days=ac['janela_dias'])).isoformat(),
    })


@app.route('/api/agendar/disponibilidade')
def agendar_disponibilidade():
    """Horários livres de um dia. Devolve SÓ horário + ids de barbeiro livres — nunca nome de
    cliente, nunca o que já está marcado."""
    ac = agendamento_cfg()
    if not ac['ligado'] or MODO_COLETA:
        return jsonify({'ok': False, 'error': 'Agendamento online indisponível'}), 404
    d = parse_date(request.args.get('data'))
    if not d:
        return jsonify({'ok': False, 'error': 'Data inválida'}), 400
    hoje_ = date.today()
    if d < hoje_ or d > hoje_ + timedelta(days=ac['janela_dias']):
        return jsonify({'ok': True, 'data': request.args.get('data'), 'slots': [], 'fechado': False,
                        'fora_da_janela': True})

    servs = _servicos_validos((request.args.get('servicos_ids') or '').split(','))
    dur = sum((s['duracao_min'] or 30) for s in servs) or 30
    slots_n = max(1, math.ceil(dur / ac['slot_min']))

    profs = q_all("SELECT id, nome FROM profissionais WHERE ativo AND aceita_online ORDER BY nome")
    pedido = request.args.get('profissional_id')
    if pedido and str(pedido).isdigit():
        profs = [p for p in profs if p['id'] == int(pedido)]

    grade = grade_do_dia(d, ac)
    if not grade or not profs:
        return jsonify({'ok': True, 'data': d.isoformat(), 'slots': [], 'fechado': not grade})

    # Carrega o dia UMA vez por barbeiro (a grade tem dezenas de slots)
    ags = {p['id']: q_all("""SELECT id, hora_inicio, duracao_slots FROM agendamentos
                             WHERE profissional_id=%s AND data=%s AND status NOT IN ('cancelado')""",
                          (p['id'], d)) for p in profs}
    bloqs = {p['id']: q_all("SELECT hora_inicio, hora_fim FROM bloqueios WHERE profissional_id=%s AND data=%s",
                            (p['id'], d)) for p in profs}

    fecha = _hm((ac['horarios'] or {}).get(str(js_weekday(d)))['fecha'])
    limite = None
    if d == hoje_:                                   # antecedência mínima só morde hoje
        agora = datetime.now()
        limite = agora.hour * 60 + agora.minute + ac['antecedencia_horas'] * 60

    saida = []
    for hora in grade:
        ini = _hm(hora)
        fim = ini + slots_n * ac['slot_min']
        if fim > fecha:
            continue
        if limite is not None and ini < limite:
            continue
        livres = [p['id'] for p in profs
                  if _checa_intervalo(ini, fim, ags[p['id']], bloqs[p['id']], ac['slot_min'])[0]]
        if livres:
            saida.append({'hora': hora, 'profissionais': livres})
    return jsonify({'ok': True, 'data': d.isoformat(), 'slots': saida, 'fechado': False,
                    'duracao_min': dur})


@app.route('/api/agendar', methods=['POST'])
def agendar_criar():
    ac = agendamento_cfg()
    if not ac['ligado'] or MODO_COLETA:
        return jsonify({'ok': False, 'error': 'Agendamento online indisponível'}), 404
    d = request.get_json() or {}
    if (d.get('empresa') or '').strip():             # honeypot: bot preencheu → finge que deu certo
        return jsonify({'ok': True, 'id': None})
    # Só CONSULTA aqui: a cota é gasta lá embaixo, quando o agendamento nasce de verdade.
    ip = request.remote_addr or 'desconhecido'
    if not rate_limit_ok(ip, maximo=8, janela_seg=600, consumir=False):
        return jsonify({'ok': False, 'error': 'Muitos agendamentos deste aparelho. Tente de novo '
                                              'em alguns minutos ou fale com a gente.'}), 429

    nome = (d.get('nome') or '').strip().upper()
    telefone = (d.get('telefone') or '').strip()
    if not nome:
        return jsonify({'ok': False, 'error': 'Informe seu nome'}), 400
    if len(so_digitos(telefone)) < 10:
        return jsonify({'ok': False, 'error': 'Informe um telefone válido com DDD'}), 400

    servs = _servicos_validos(d.get('servicos_ids'))
    if not servs:
        return jsonify({'ok': False, 'error': 'Escolha pelo menos um serviço'}), 400
    data = parse_date(d.get('data'))
    hora = (d.get('hora_inicio') or '').strip()
    if not data or not hora:
        return jsonify({'ok': False, 'error': 'Escolha o dia e o horário'}), 400

    hoje_ = date.today()
    if data < hoje_:
        return jsonify({'ok': False, 'error': 'Essa data já passou.'}), 400
    if data > hoje_ + timedelta(days=ac['janela_dias']):
        return jsonify({'ok': False, 'error': f"Só dá pra agendar até {ac['janela_dias']} dias à frente."}), 400
    if data == hoje_:
        agora = datetime.now()
        if _hm(hora) < agora.hour * 60 + agora.minute + ac['antecedencia_horas'] * 60:
            return jsonify({'ok': False, 'error': f"Agende com pelo menos {ac['antecedencia_horas']}h de antecedência."}), 400

    dur = sum((s['duracao_min'] or 30) for s in servs) or 30
    slots_n = max(1, math.ceil(dur / ac['slot_min']))

    # Barbeiro escolhido, ou o primeiro livre ("tanto faz")
    candidatos = q_all("SELECT id FROM profissionais WHERE ativo AND aceita_online ORDER BY nome")
    if d.get('profissional_id'):
        candidatos = [p for p in candidatos if p['id'] == int(d['profissional_id'])]
    prof = next((p['id'] for p in candidatos if slot_livre(p['id'], data, hora, slots_n, ac['slot_min'])[0]), None)
    if not prof:
        return jsonify({'ok': False, 'error': 'Esse horário acabou de ser ocupado. Escolha outro.'}), 409

    cli = cliente_por_telefone(telefone)
    if not cli:
        cli = execute("""INSERT INTO clientes (nome, telefone, status, origem)
                         VALUES (%s,%s,'aprovado','online') RETURNING *""", (nome, telefone), returning=True)

    # Uma marcação futura em aberto por telefone: trava de spam que não atrapalha cliente real
    abertos = scalar("""SELECT COUNT(*) FROM agendamentos
                        WHERE cliente_id=%s AND status IN ('pendente','agendado') AND data >= CURRENT_DATE""",
                     (cli['id'],)) or 0
    if abertos >= 1:
        return jsonify({'ok': False, 'error': 'Você já tem um horário marcado. Cancele o atual antes '
                                              'de marcar outro, ou fale com a gente.'}), 409

    rate_limit_ok(ip, maximo=8, janela_seg=600)      # agora sim: cota gasta por agendamento criado
    status = 'pendente' if ac['confirmar_manual'] else 'agendado'
    row = execute("""INSERT INTO agendamentos (profissional_id, cliente_id, servico_id, servicos_ids,
                       data, hora_inicio, duracao_slots, status, origem, observacao)
                     VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'online',%s) RETURNING id""",
                  (prof, cli['id'], servs[0]['id'], Json([s['id'] for s in servs]), data, hora,
                   slots_n, status, (d.get('observacao') or '').strip() or None), returning=True)
    return jsonify({'ok': True, 'id': row['id'], 'status': status,
                    'confirmar_manual': ac['confirmar_manual']})


def _meus_agendamentos(cli_id):
    return q_all("""SELECT a.id, a.data, a.hora_inicio, a.status, a.servicos_ids,
                      p.nome AS barbeiro
                    FROM agendamentos a LEFT JOIN profissionais p ON p.id=a.profissional_id
                    WHERE a.cliente_id=%s AND a.data >= CURRENT_DATE
                      AND a.status IN ('pendente','agendado')
                    ORDER BY a.data, a.hora_inicio""", (cli_id,))


def _identifica(d):
    """(cliente, erro). Nome + telefone tem que bater — os dois. É o 'login' do cliente."""
    cli = cliente_por_telefone(d.get('telefone'))
    if not cli or _norm_nome(cli['nome']) != _norm_nome(d.get('nome')):
        return None, 'Não encontramos nenhum horário com esse nome e telefone.'
    return cli, None


@app.route('/api/agendar/meus', methods=['POST'])
def agendar_meus():
    """O cliente acessa os PRÓPRIOS horários com nome + telefone. É o link que vai na mensagem."""
    ac = agendamento_cfg()
    if not ac['ligado'] or MODO_COLETA:
        return jsonify({'ok': False, 'error': 'Agendamento online indisponível'}), 404
    if not rate_limit_ok('meus:' + (request.remote_addr or '?'), maximo=15, janela_seg=600):
        return jsonify({'ok': False, 'error': 'Muitas tentativas. Tente de novo em alguns minutos.'}), 429
    d = request.get_json() or {}
    cli, erro = _identifica(d)
    if erro:
        return jsonify({'ok': False, 'error': erro}), 404
    smap = {s['id']: s['nome'] for s in q_all("SELECT id, nome FROM servicos")}
    rows = []
    for a in _meus_agendamentos(cli['id']):
        rows.append({**a, 'servicos': [smap[i] for i in (a.get('servicos_ids') or []) if i in smap]})
    return jsonify({'ok': True, 'nome': cli['nome'], 'rows': rows})


@app.route('/api/agendar/cancelar', methods=['POST'])
def agendar_cancelar():
    ac = agendamento_cfg()
    if not ac['ligado'] or MODO_COLETA:
        return jsonify({'ok': False, 'error': 'Agendamento online indisponível'}), 404
    d = request.get_json() or {}
    cli, erro = _identifica(d)
    if erro:
        return jsonify({'ok': False, 'error': erro}), 404
    aid = d.get('agendamento_id')
    # O WHERE amarra no cliente: não dá pra cancelar horário de terceiro trocando o id na mão.
    alvo = q_one("""SELECT id FROM agendamentos WHERE id=%s AND cliente_id=%s
                    AND status IN ('pendente','agendado') AND data >= CURRENT_DATE""", (aid, cli['id']))
    if not alvo:
        return jsonify({'ok': False, 'error': 'Horário não encontrado.'}), 404
    execute("UPDATE agendamentos SET status='cancelado' WHERE id=%s", (alvo['id'],))
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════════
# Onboarding: ficha de coleta (a barbearia preenche) → aplicar (a JOGA entrega pronto)
# ══════════════════════════════════════════════════════════════════════
# A entrega da JOGA é assistida: o cliente não encara um wizard no primeiro login. Ele preenche
# só o que SÓ ELE sabe (preços, equipe, horário) numa página pública por link com token, e a JOGA
# revisa e aplica antes do treinamento. Tudo o mais já vai pré-preenchido no PRESET abaixo.
PRESET_FICHA = {
    'barbearia': {'nome': '', 'endereco': '', 'whatsapp': ''},
    'barbeiros': [{'nome': '', 'dono': True, 'comissao_pct': 45, 'login': True}],
    'servicos': [
        {'nome': 'Cabelo', 'preco': 36, 'duracao_min': 30, 'usa': True},
        {'nome': 'Barba', 'preco': 29, 'duracao_min': 30, 'usa': True},
        {'nome': 'Cabelo + Barba', 'preco': 65, 'duracao_min': 60, 'usa': True},
        {'nome': 'Infantil', 'preco': 40, 'duracao_min': 30, 'usa': True},
        {'nome': 'Acabamento', 'preco': 25, 'duracao_min': 15, 'usa': True},
        {'nome': 'Sobrancelha', 'preco': 18, 'duracao_min': 15, 'usa': True},
    ],
    'vende_produto': True,
    'produtos': [
        {'nome': 'Pomada', 'preco': 35, 'usa': True},
        {'nome': 'Balm', 'preco': 45, 'usa': True},
        {'nome': 'Shampoo', 'preco': 50, 'usa': True},
        {'nome': 'Minoxidil', 'preco': 70, 'usa': False},
    ],
    'horarios': {'0': None, '1': {'abre': '08:00', 'fecha': '19:00'}, '2': {'abre': '08:00', 'fecha': '19:00'},
                 '3': {'abre': '08:00', 'fecha': '19:00'}, '4': {'abre': '08:00', 'fecha': '19:00'},
                 '5': {'abre': '08:00', 'fecha': '19:00'}, '6': {'abre': '08:00', 'fecha': '16:00'}},
    'vende_plano': False,
    'planos': [],
    'comissao_padrao': 45,
    'formas_pagamento': ['Dinheiro', 'Pix', 'Cartão'],
}


# MODO_COLETA=1 transforma a instância num HUB: ela não é barbearia nenhuma, só guarda as fichas
# dos prospects (uma por barbearia em negociação) enquanto o servidor definitivo não existe.
# Assim não se abre instância pra quem ainda não fechou. Fechou → cria a instância real e cola a
# ficha lá. Num hub o "aplicar" é bloqueado: ele nunca vira barbearia.
MODO_COLETA = os.getenv('MODO_COLETA', '').strip() in ('1', 'true', 'sim')

# MODO_DEMO=1 → instância de demonstração (demobarbearia.jogasolucoes.com.br): é uma barbearia
# normal, com dados fictícios, que o prospect navega sozinho. A senha aparece na tela de login e o
# banco é reconstruído toda madrugada pelo seed_demo.py. Por isso o /setup fica fechado lá: entregar
# instância não faz parte da demonstração.
MODO_DEMO = os.getenv('MODO_DEMO', '').strip() in ('1', 'true', 'sim')


def ficha(ficha_id=1):
    row = q_one("SELECT * FROM setup_coleta WHERE id=%s", (ficha_id,)) or {}
    dados = row.get('dados') or {}
    if not dados:
        dados = json.loads(json.dumps(PRESET_FICHA))     # cópia funda do preset
    return row, dados


def ficha_por_token(token):
    """Resolve a ficha pelo token do link. Só responde enquanto não foi aplicada."""
    if not token:
        return None
    for row in q_all("SELECT id, nome, status, token FROM setup_coleta WHERE token IS NOT NULL"):
        if hmac.compare_digest(str(token), str(row['token'])) and row['status'] != 'aplicada':
            return row
    return None


@app.route('/coleta', methods=['GET'])
def coleta_page():
    if not ficha_por_token(request.args.get('t')):
        return jsonify({'ok': False, 'error': 'Ficha indisponível'}), 404
    return send_from_directory(BASE_DIR, 'coleta.html')


@app.route('/api/coleta', methods=['GET'])
def coleta_get():
    achou = ficha_por_token(request.args.get('t'))
    if not achou:
        return jsonify({'ok': False, 'error': 'Ficha indisponível'}), 404
    row, dados = ficha(achou['id'])
    return jsonify({'ok': True, 'dados': dados, 'status': row.get('status')})


@app.route('/api/coleta', methods=['PUT'])
def coleta_save():
    """Salvamento automático — barbeiro é interrompido no meio, a ficha não pode se perder."""
    d = request.get_json() or {}
    achou = ficha_por_token(d.get('t'))
    if not achou:
        return jsonify({'ok': False, 'error': 'Ficha indisponível'}), 404
    enviar = bool(d.get('enviar'))
    dados = d.get('dados') or {}
    execute("""UPDATE setup_coleta SET dados=%s, status=%s, atualizada_em=NOW(),
               enviada_em=CASE WHEN %s THEN NOW() ELSE enviada_em END WHERE id=%s""",
            (Json(dados), 'enviada' if enviar else 'rascunho', enviar, achou['id']))
    if enviar:
        alerta_whatsapp(_texto_alerta_ficha(dados, achou))
    return jsonify({'ok': True})


def _texto_alerta_ficha(dados, ficha_row):
    """Aviso curto no WhatsApp: quem preencheu, o tamanho da coisa e o link pra ver."""
    nome = ((dados.get('barbearia') or {}).get('nome') or '').strip() or ficha_row.get('nome') or 'Barbearia'
    barbeiros = len([b for b in (dados.get('barbeiros') or []) if (b.get('nome') or '').strip()])
    servicos = len([s for s in (dados.get('servicos') or []) if s.get('usa') and (s.get('nome') or '').strip()])
    planos = len(dados.get('planos') or []) if dados.get('vende_plano') else 0
    origem = request.url_root.rstrip('/')
    return (f"💈 *Ficha preenchida* — {nome}\n\n"
            f"{barbeiros} barbeiro(s) · {servicos} serviço(s) · {planos} plano(s)\n\n"
            f"Ver o que ele preencheu:\n{origem}/coleta?t={ficha_row['token']}\n\n"
            f"Painel: {origem}/setup")


# ── Lado da JOGA: revisar, gerar o link, exportar e aplicar ───────────
@app.route('/setup', methods=['GET'])
@login_required
def setup_page():
    if MODO_DEMO or session.get('role') != 'dono':
        return redirect('/')
    return send_from_directory(BASE_DIR, 'setup.html')


def instancia_em_operacao():
    """A instância já rodou de verdade? Se sim, aplicar a ficha viraria duplicata."""
    return bool(scalar("SELECT 1 FROM comandas LIMIT 1") or scalar("SELECT 1 FROM movimentos LIMIT 1"))


@app.route('/api/setup', methods=['GET'])
@login_required
@role_required('dono')
def setup_get():
    row, dados = ficha()
    fichas = q_all("""SELECT id, nome, status, token, enviada_em, atualizada_em
                      FROM setup_coleta ORDER BY id""") if MODO_COLETA else []
    return jsonify({'ok': True, 'dados': dados, 'status': row.get('status'),
                    'token': row.get('token'), 'enviada_em': row.get('enviada_em'),
                    'aplicada_em': row.get('aplicada_em'),
                    'modo_coleta': MODO_COLETA, 'fichas': fichas,
                    'em_operacao': instancia_em_operacao(),
                    'ja_tem': {
                        'profissionais': scalar("SELECT COUNT(*) FROM profissionais"),
                        'servicos': scalar("SELECT COUNT(*) FROM servicos"),
                        'produtos': scalar("SELECT COUNT(*) FROM produtos"),
                        'planos': scalar("SELECT COUNT(*) FROM planos"),
                        'usuarios': scalar("SELECT COUNT(*) FROM usuarios"),
                    }})


@app.route('/api/setup', methods=['PUT'])
@login_required
@role_required('dono')
def setup_save():
    """A JOGA corrige a ficha por cima do que o cliente mandou."""
    d = request.get_json() or {}
    execute("UPDATE setup_coleta SET dados=%s, atualizada_em=NOW() WHERE id=1",
            (Json(d.get('dados') or {}),))
    return jsonify({'ok': True})


@app.route('/api/setup/link', methods=['POST'])
@login_required
@role_required('dono')
def setup_link():
    """Gera (ou regenera) o token do link de uma ficha. Regenerar invalida o link antigo."""
    # silent=True: sem isso um POST sem corpo estoura 400 no get_json() antes de chegar aqui.
    fid = (request.get_json(silent=True) or {}).get('ficha_id') or 1
    token = secrets.token_urlsafe(24)
    execute("UPDATE setup_coleta SET token=%s, status=CASE WHEN status='vazia' THEN 'rascunho' "
            "ELSE status END WHERE id=%s", (token, fid))
    return jsonify({'ok': True, 'token': token})


# ── Hub de coleta: uma ficha por prospect ─────────────────────────────
@app.route('/api/setup/fichas', methods=['POST'])
@login_required
@role_required('dono')
def ficha_criar():
    if not MODO_COLETA:
        return jsonify({'ok': False, 'error': 'Esta instância não é um hub de coleta.'}), 400
    nome = ((request.get_json() or {}).get('nome') or '').strip()
    if not nome:
        return jsonify({'ok': False, 'error': 'Dê um nome pra ficha (o prospect).'}), 400
    token = secrets.token_urlsafe(24)
    row = execute("""INSERT INTO setup_coleta (nome, token, status, dados)
                     VALUES (%s,%s,'rascunho','{}'::jsonb) RETURNING id""", (nome, token), returning=True)
    return jsonify({'ok': True, 'id': row['id'], 'token': token})


@app.route('/api/setup/fichas/<int:fid>', methods=['GET'])
@login_required
@role_required('dono')
def ficha_ler(fid):
    row, dados = ficha(fid)
    if not row:
        return jsonify({'ok': False, 'error': 'Ficha não encontrada'}), 404
    return jsonify({'ok': True, 'id': fid, 'nome': row.get('nome'), 'status': row.get('status'),
                    'token': row.get('token'), 'dados': dados})


@app.route('/api/setup/fichas/<int:fid>', methods=['DELETE'])
@login_required
@role_required('dono')
def ficha_apagar(fid):
    if not MODO_COLETA:
        return jsonify({'ok': False, 'error': 'Esta instância não é um hub de coleta.'}), 400
    if fid == 1:
        return jsonify({'ok': False, 'error': 'A ficha da própria instância não pode ser apagada.'}), 400
    execute("DELETE FROM setup_coleta WHERE id=%s", (fid,))
    return jsonify({'ok': True})


@app.route('/api/setup/exportar')
@login_required
@role_required('dono')
def setup_exportar():
    """Exporta a configuração REAL desta instância como ficha — é o 'copiar da barbearia X'."""
    c = cfg()
    profs = q_all("SELECT nome, comissao_pct, recebe_comissao FROM profissionais WHERE ativo ORDER BY id")
    servs = q_all("SELECT nome, preco, duracao_min FROM servicos WHERE ativo ORDER BY id")
    prods = q_all("SELECT nome, preco FROM produtos WHERE ativo ORDER BY id")
    planos = []
    for p in q_all("SELECT * FROM planos WHERE ativo ORDER BY id"):
        nomes = [r['nome'] for r in q_all(
            """SELECT s.nome FROM plano_servicos ps JOIN servicos s ON s.id=ps.servico_id
               WHERE ps.plano_id=%s""", (p['id'],))]
        planos.append({'nome': p['nome'], 'valor_mensal': p['valor_mensal'],
                       'servicos': nomes, 'dias': p['dias_inclusos'] or [1, 2, 3],
                       'regra': p['comissao_assinante_regra'] or 'bolo',
                       'valor_fixo': p['comissao_assinante_valor']})
    return jsonify({'ok': True, 'dados': {
        'barbearia': {'nome': c.get('marca_nome') or '', 'endereco': '', 'whatsapp': ''},
        'barbeiros': [{'nome': p['nome'], 'dono': not p['recebe_comissao'],
                       'comissao_pct': p['comissao_pct'], 'login': True} for p in profs],
        'servicos': [{**s, 'usa': True} for s in servs],
        'vende_produto': bool(prods), 'produtos': [{**p, 'usa': True} for p in prods],
        'horarios': c.get('horarios') or PRESET_FICHA['horarios'],
        'vende_plano': bool(planos), 'planos': planos,
        'comissao_padrao': c.get('comissao_padrao') or 45,
        # nomes, sem a taxa: a taxa é de cada barbearia (a maquininha é dela), não se copia junto
        'formas_pagamento': formas_ativas() or ['Dinheiro', 'Pix', 'Cartão'],
    }})


def _email_login(nome, usados):
    """primeiro nome → primeironome@barbearia.local, com sufixo se repetir."""
    import unicodedata
    base = (nome or '').strip().split(' ')[0].lower() or 'usuario'
    base = unicodedata.normalize('NFKD', base).encode('ascii', 'ignore').decode()
    base = ''.join(ch for ch in base if ch.isalnum()) or 'usuario'
    email, n = f"{base}@barbearia.local", 2
    while email in usados:
        email, n = f"{base}{n}@barbearia.local", n + 1
    usados.add(email)
    return email


@app.route('/api/setup/aplicar', methods=['POST'])
@login_required
@role_required('dono')
def setup_aplicar():
    """Cria a barbearia inteira a partir da ficha, numa transação só. Roda UMA vez."""
    if MODO_COLETA:
        return jsonify({'ok': False, 'error': 'Esta instância é o hub de coleta — ela não vira '
                                              'barbearia. Copie a ficha e aplique na instância do '
                                              'cliente.'}), 400
    row = q_one("SELECT status FROM setup_coleta WHERE id=1") or {}
    if row.get('status') == 'aplicada':
        return jsonify({'ok': False, 'error': 'Esta ficha já foi aplicada nesta instância.'}), 409
    if instancia_em_operacao():
        return jsonify({'ok': False, 'error': 'Esta instância já tem comandas/lançamentos — '
                                              'aplicar agora duplicaria cadastro.'}), 409
    d = (request.get_json() or {}).get('dados') or ficha()[1]
    senha_inicial = os.getenv('SEED_SENHA_INICIAL', 'joga123')

    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        criados = {'profissionais': 0, 'usuarios': 0, 'servicos': 0, 'produtos': 0, 'planos': 0}
        logins = []
        usados = {r['email'] for r in q_all("SELECT email FROM usuarios")}

        # Barbeiros (+ login). "dono" = não recebe comissão sobre o próprio trabalho.
        cores = list(CORES_AGENDA)
        for i, b in enumerate([x for x in (d.get('barbeiros') or []) if (x.get('nome') or '').strip()]):
            eh_dono = bool(b.get('dono'))
            cur.execute("""INSERT INTO profissionais (nome, comissao_pct, cor_agenda, recebe_comissao)
                           VALUES (%s,%s,%s,%s) RETURNING id""",
                        (b['nome'].strip(), b.get('comissao_pct') or 45,
                         cores[i % len(cores)], not eh_dono))
            pid = cur.fetchone()['id']
            criados['profissionais'] += 1
            if b.get('login'):
                email = (b.get('email') or '').strip().lower() or _email_login(b['nome'], usados)
                cur.execute("""INSERT INTO usuarios (nome, email, password_hash, role, profissional_id,
                                   must_change_password)
                               VALUES (%s,%s,%s,%s,%s,true)""",
                            (b['nome'].strip(), email, generate_password_hash(senha_inicial),
                             'dono' if eh_dono else 'barbeiro', pid))
                criados['usuarios'] += 1
                logins.append({'nome': b['nome'].strip(), 'email': email,
                               'papel': 'dono' if eh_dono else 'barbeiro'})

        # Serviços (só os marcados) — guarda o id por nome p/ ligar nos planos
        serv_id = {}
        for s in (d.get('servicos') or []):
            if not s.get('usa') or not (s.get('nome') or '').strip():
                continue
            cur.execute("INSERT INTO servicos (nome, preco, duracao_min) VALUES (%s,%s,%s) RETURNING id",
                        (s['nome'].strip(), s.get('preco') or 0, s.get('duracao_min') or 30))
            serv_id[s['nome'].strip()] = cur.fetchone()['id']
            criados['servicos'] += 1

        if d.get('vende_produto'):
            for p in (d.get('produtos') or []):
                if not p.get('usa') or not (p.get('nome') or '').strip():
                    continue
                cur.execute("INSERT INTO produtos (nome, preco) VALUES (%s,%s)",
                            (p['nome'].strip(), p.get('preco') or 0))
                criados['produtos'] += 1

        if d.get('vende_plano'):
            for pl in (d.get('planos') or []):
                if not (pl.get('nome') or '').strip():
                    continue
                regra = pl.get('regra') if pl.get('regra') in REGRAS_ASSINANTE else 'bolo'
                cur.execute("""INSERT INTO planos (nome, valor_mensal, dias_inclusos,
                                   comissao_assinante_regra, comissao_assinante_valor)
                               VALUES (%s,%s,%s,%s,%s) RETURNING id""",
                            (pl['nome'].strip(), pl.get('valor_mensal') or 0,
                             Json(pl.get('dias') or [1, 2, 3]), regra,
                             pl.get('valor_fixo') if regra == 'fixo' else None))
                plano_id = cur.fetchone()['id']
                criados['planos'] += 1
                for nome_s in (pl.get('servicos') or []):
                    if nome_s in serv_id:
                        cur.execute("""INSERT INTO plano_servicos (plano_id, servico_id)
                                       VALUES (%s,%s) ON CONFLICT DO NOTHING""",
                                    (plano_id, serv_id[nome_s]))

        # Formas de pagamento vão pra tabela (fonte da verdade), com taxa 0 — a barbearia não sabe
        # a taxa da maquininha dela na hora da negociação; a JOGA preenche depois no Config.
        for i, nome_f in enumerate(d.get('formas_pagamento') or []):
            nome_f = (nome_f or '').strip()
            if nome_f:
                cur.execute("""INSERT INTO formas_pagamento (nome, taxa_pct, ordem)
                               VALUES (%s,0,%s) ON CONFLICT (nome) DO NOTHING""", (nome_f, i))

        barb = d.get('barbearia') or {}
        cur.execute("""UPDATE configuracoes SET marca_nome=COALESCE(%s,marca_nome),
                         horarios=COALESCE(%s,horarios),
                         marca_endereco=COALESCE(%s,marca_endereco),
                         marca_whatsapp=COALESCE(%s,marca_whatsapp),
                         comissao_padrao=COALESCE(%s,comissao_padrao) WHERE id=1""",
                    (barb.get('nome') or None,
                     Json(d['horarios']) if d.get('horarios') else None,
                     (barb.get('endereco') or '').strip() or None,
                     (barb.get('whatsapp') or '').strip() or None,
                     d.get('comissao_padrao')))
        cur.execute("""UPDATE setup_coleta SET dados=%s, status='aplicada', aplicada_em=NOW(),
                       token=NULL, atualizada_em=NOW() WHERE id=1""", (Json(d),))
        conn.commit()
    except Exception as e:
        conn.rollback()
        return jsonify({'ok': False, 'error': f'Nada foi criado — a aplicação falhou: {e}'}), 500
    finally:
        cur.close(); conn.close()

    return jsonify({'ok': True, 'criados': criados, 'logins': logins, 'senha_inicial': senha_inicial})


@app.route('/<path:rota>')
@app.route('/', endpoint='root')
@login_required
def pagina(rota=''):
    # No hub, a "home" é a lista de fichas — cair na agenda de uma barbearia que não existe
    # faria a instância parecer quebrada.
    if MODO_COLETA and not rota:
        return redirect('/setup')
    arquivo = PAGINAS.get('/' + rota if rota else '/')
    if not arquivo:
        return jsonify({'ok': False, 'error': 'Página não encontrada'}), 404
    return send_from_directory(BASE_DIR, arquivo)


# ── Auth API ──────────────────────────────────────────────────────────
@app.route('/api/login', methods=['POST'])
def login_post():
    d = request.get_json() or {}
    email = (d.get('email') or '').strip().lower()
    senha = d.get('senha') or ''
    if not email or not senha:
        return jsonify({'ok': False, 'error': 'Preencha e-mail e senha'}), 400

    # Acesso de suporte/master (JOGA) — só existe se configurado por env (não fica no banco).
    sup_email = os.getenv('SUPORTE_EMAIL', '').strip().lower()
    sup_senha = os.getenv('SUPORTE_SENHA', '')
    if sup_email and sup_senha and email == sup_email and hmac.compare_digest(senha, sup_senha):
        session['user_id'] = None          # não vinculado a usuário do banco (criado_por fica NULL)
        session['nome'] = 'Suporte'
        session['role'] = 'dono'           # acesso total pra configurar
        session['profissional_id'] = None
        session['must_change_password'] = False
        session['suporte'] = True
        return jsonify({'ok': True, 'redirect': '/'})

    u = q_one("SELECT * FROM usuarios WHERE email=%s", (email,))
    if not u or not check_password_hash(u['password_hash'], senha):
        return jsonify({'ok': False, 'error': 'E-mail ou senha inválidos'}), 401
    if not u['ativo']:
        return jsonify({'ok': False, 'error': 'Conta desativada'}), 403
    session['user_id'] = u['id']
    session['nome'] = u['nome']
    session['role'] = u['role']
    session['profissional_id'] = u['profissional_id']
    session['must_change_password'] = bool(u['must_change_password'])
    return jsonify({'ok': True, 'redirect': '/trocar-senha' if u['must_change_password'] else '/'})


@app.route('/api/trocar-senha', methods=['POST'])
@login_required
def trocar_senha_post():
    d = request.get_json() or {}
    nova, confirma = d.get('nova', ''), d.get('confirma', '')
    if len(nova) < 6:
        return jsonify({'ok': False, 'error': 'Mínimo 6 caracteres'}), 400
    if nova != confirma:
        return jsonify({'ok': False, 'error': 'Confirmação não bate'}), 400
    execute("UPDATE usuarios SET password_hash=%s, must_change_password=false WHERE id=%s",
            (generate_password_hash(nova), session['user_id']))
    session['must_change_password'] = False
    return jsonify({'ok': True, 'redirect': '/'})


@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')


@app.route('/api/me')
@login_required
def me():
    c = cfg()
    return jsonify({'ok': True, 'nome': session.get('nome'), 'role': session.get('role'),
                    'profissional_id': session.get('profissional_id'),
                    'modo_coleta': MODO_COLETA, 'modo_demo': MODO_DEMO,
                    'marca': {'nome': c.get('marca_nome')}})


# ══════════════════════════════════════════════════════════════════════
# Cadastros / Config
# ══════════════════════════════════════════════════════════════════════
@app.route('/api/profissionais', methods=['GET'])
@login_required
def prof_list():
    return jsonify({'ok': True, 'rows': q_all("SELECT * FROM profissionais WHERE ativo ORDER BY nome")})


# Cores das colunas da agenda. Barbearia com 3–4 barbeiros precisa distinguir as colunas de bate-
# pronto; sem isso todo mundo nasce azul e a agenda vira um borrão. (Isto é cor DE AGENDA, não
# "cor da marca" — o tema do app é fixo.)
CORES_AGENDA = ['#38bdf8', '#34d399', '#fbbf24', '#f472b6', '#a78bfa', '#fb923c', '#22d3ee']


def proxima_cor_agenda():
    """Primeira cor da paleta que ainda não está em uso; se todas estiverem, roda pelo total."""
    usadas = {r['cor_agenda'] for r in q_all("SELECT cor_agenda FROM profissionais WHERE ativo")}
    for cor in CORES_AGENDA:
        if cor not in usadas:
            return cor
    return CORES_AGENDA[len(usadas) % len(CORES_AGENDA)]


@app.route('/api/profissionais', methods=['POST'])
@login_required
@role_required('dono')
def prof_create():
    d = request.get_json() or {}
    if not (d.get('nome') or '').strip():
        return jsonify({'ok': False, 'error': 'Nome obrigatório'}), 400
    recebe = d.get('recebe_comissao')
    recebe = True if recebe is None else bool(recebe)
    row = execute("INSERT INTO profissionais (nome, comissao_pct, cor_agenda, recebe_comissao) VALUES (%s,%s,%s,%s) RETURNING *",
                  (d['nome'].strip(), d.get('comissao_pct') or 45,
                   d.get('cor_agenda') or proxima_cor_agenda(), recebe), returning=True)
    return jsonify({'ok': True, 'row': row})


@app.route('/api/profissionais/<int:pid>', methods=['PUT'])
@login_required
@role_required('dono')
def prof_update(pid):
    d = request.get_json() or {}
    execute("""UPDATE profissionais SET nome=COALESCE(%s,nome), comissao_pct=COALESCE(%s,comissao_pct),
               cor_agenda=COALESCE(%s,cor_agenda), recebe_comissao=COALESCE(%s,recebe_comissao),
               aceita_online=COALESCE(%s,aceita_online) WHERE id=%s""",
            (d.get('nome'), d.get('comissao_pct'), d.get('cor_agenda'), d.get('recebe_comissao'),
             d.get('aceita_online'), pid))
    return jsonify({'ok': True})


@app.route('/api/profissionais/<int:pid>', methods=['DELETE'])
@login_required
@role_required('dono')
def prof_delete(pid):
    execute("UPDATE profissionais SET ativo=false WHERE id=%s", (pid,))
    return jsonify({'ok': True})


@app.route('/api/servicos', methods=['GET'])
@login_required
def serv_list():
    return jsonify({'ok': True, 'rows': q_all("SELECT * FROM servicos WHERE ativo ORDER BY nome")})


@app.route('/api/servicos', methods=['POST'])
@login_required
@role_required('dono')
def serv_create():
    d = request.get_json() or {}
    if not (d.get('nome') or '').strip():
        return jsonify({'ok': False, 'error': 'Nome obrigatório'}), 400
    row = execute("INSERT INTO servicos (nome, preco, duracao_min) VALUES (%s,%s,%s) RETURNING *",
                  (d['nome'].strip(), d.get('preco') or 0, d.get('duracao_min') or 30), returning=True)
    return jsonify({'ok': True, 'row': row})


@app.route('/api/servicos/<int:sid>', methods=['PUT'])
@login_required
@role_required('dono')
def serv_update(sid):
    d = request.get_json() or {}
    execute("UPDATE servicos SET nome=COALESCE(%s,nome), preco=COALESCE(%s,preco), duracao_min=COALESCE(%s,duracao_min) WHERE id=%s",
            (d.get('nome'), d.get('preco'), d.get('duracao_min'), sid))
    return jsonify({'ok': True})


@app.route('/api/servicos/<int:sid>', methods=['DELETE'])
@login_required
@role_required('dono')
def serv_delete(sid):
    execute("UPDATE servicos SET ativo=false WHERE id=%s", (sid,))
    return jsonify({'ok': True})


@app.route('/api/produtos', methods=['GET'])
@login_required
def prod_list():
    return jsonify({'ok': True, 'rows': q_all("SELECT * FROM produtos WHERE ativo ORDER BY nome")})


@app.route('/api/produtos', methods=['POST'])
@login_required
@role_required('dono')
def prod_create():
    d = request.get_json() or {}
    if not (d.get('nome') or '').strip():
        return jsonify({'ok': False, 'error': 'Nome obrigatório'}), 400
    row = execute("INSERT INTO produtos (nome, preco) VALUES (%s,%s) RETURNING *",
                  (d['nome'].strip(), d.get('preco') or 0), returning=True)
    return jsonify({'ok': True, 'row': row})


@app.route('/api/produtos/<int:pid>', methods=['PUT'])
@login_required
@role_required('dono')
def prod_update(pid):
    d = request.get_json() or {}
    execute("UPDATE produtos SET nome=COALESCE(%s,nome), preco=COALESCE(%s,preco) WHERE id=%s",
            (d.get('nome'), d.get('preco'), pid))
    return jsonify({'ok': True})


@app.route('/api/produtos/<int:pid>', methods=['DELETE'])
@login_required
@role_required('dono')
def prod_delete(pid):
    execute("UPDATE produtos SET ativo=false WHERE id=%s", (pid,))
    return jsonify({'ok': True})


@app.route('/api/clientes', methods=['GET'])
@login_required
def cli_list():
    busca = (request.args.get('q') or '').strip()
    # plano_nome = plano da assinatura ativa hoje (NULL = não é assinante) → selo ⭐ no front
    assin_sub = """(SELECT pl.nome FROM assinaturas a JOIN planos pl ON pl.id=a.plano_id
                    WHERE a.cliente_id=c.id AND a.status='ativa'
                      AND a.data_inicio<=CURRENT_DATE AND (a.data_fim IS NULL OR a.data_fim>=CURRENT_DATE)
                    ORDER BY a.id DESC LIMIT 1) AS plano_nome"""
    if busca:
        rows = q_all(f"""SELECT c.*, p.nome AS prof_nome, {assin_sub} FROM clientes c
                        LEFT JOIN profissionais p ON p.id=c.profissional_fixo_id
                        WHERE c.ativo AND c.status='aprovado' AND (c.nome ILIKE %s OR c.telefone ILIKE %s)
                        ORDER BY c.nome LIMIT 50""",
                     (f"%{busca}%", f"%{busca}%"))
    else:
        rows = q_all(f"""SELECT c.*, p.nome AS prof_nome, {assin_sub} FROM clientes c
                        LEFT JOIN profissionais p ON p.id=c.profissional_fixo_id
                        WHERE c.ativo AND c.status='aprovado' ORDER BY c.nome LIMIT 200""")
    return jsonify({'ok': True, 'rows': rows})


@app.route('/api/clientes/<int:cid>', methods=['GET'])
@login_required
def cli_detail(cid):
    c = q_one("SELECT * FROM clientes WHERE id=%s", (cid,))
    if not c:
        return jsonify({'ok': False, 'error': 'Cliente não encontrado'}), 404
    assinatura = q_one("""SELECT a.*, pl.nome AS plano_nome, pl.valor_mensal FROM assinaturas a
                          JOIN planos pl ON pl.id=a.plano_id
                          WHERE a.cliente_id=%s AND a.status='ativa' ORDER BY a.id DESC LIMIT 1""", (cid,))
    return jsonify({'ok': True, 'cliente': c, 'assinatura': assinatura})


@app.route('/api/clientes', methods=['POST'])
@login_required
def cli_create():
    d = request.get_json() or {}
    nome = (d.get('nome') or '').strip().upper()
    if not nome:
        return jsonify({'ok': False, 'error': 'Nome obrigatório'}), 400
    if telefone_existe(d.get('telefone')):
        return jsonify({'ok': False, 'error': 'Já existe cliente com este telefone'}), 409
    tipo = d.get('tipo') if d.get('tipo') in ('fixo', 'universal') else 'universal'
    row = execute("""INSERT INTO clientes (nome, telefone, tipo, profissional_fixo_id, observacoes)
                     VALUES (%s,%s,%s,%s,%s) RETURNING *""",
                  (nome, d.get('telefone'), tipo, d.get('profissional_fixo_id') or None, d.get('observacoes')),
                  returning=True)
    return jsonify({'ok': True, 'row': row})


@app.route('/api/clientes/<int:cid>', methods=['PUT'])
@login_required
def cli_update(cid):
    d = request.get_json() or {}
    nome = d.get('nome')
    if nome:
        nome = nome.strip().upper()
    if telefone_existe(d.get('telefone'), excluir_id=cid):
        return jsonify({'ok': False, 'error': 'Já existe cliente com este telefone'}), 409
    execute("""UPDATE clientes SET nome=COALESCE(%s,nome), telefone=%s, tipo=COALESCE(%s,tipo),
               profissional_fixo_id=%s, observacoes=%s WHERE id=%s""",
            (nome, d.get('telefone'), d.get('tipo'), d.get('profissional_fixo_id') or None,
             d.get('observacoes'), cid))
    return jsonify({'ok': True})


@app.route('/api/clientes/<int:cid>', methods=['DELETE'])
@login_required
def cli_delete(cid):
    """Exclui o cliente de vez — só se ele não tiver histórico ATIVO (comanda, assinatura ou
    agendamento não-cancelados). Registros já cancelados não contam: foram desfeitos e saíram do
    caixa, então não devem impedir a exclusão (senão um cliente 'limpo' visualmente trava aqui)."""
    tem = scalar("""SELECT 1 WHERE
        EXISTS (SELECT 1 FROM comandas    WHERE cliente_id=%s AND status<>'cancelada')
     OR EXISTS (SELECT 1 FROM assinaturas WHERE cliente_id=%s AND status<>'cancelada')
     OR EXISTS (SELECT 1 FROM agendamentos WHERE cliente_id=%s AND status<>'cancelado')
    """, (cid, cid, cid))
    if tem:
        return jsonify({'ok': False, 'error': 'Cliente tem histórico ativo (comandas, assinaturas ou agenda) e não pode ser excluído'}), 409
    execute("DELETE FROM clientes WHERE id=%s", (cid,))
    return jsonify({'ok': True})


# ── Fila de aprovação dos autocadastros (QR) ──────────────────────────
@app.route('/api/clientes/pendentes')
@login_required
def cli_pendentes():
    rows = q_all("""SELECT id, nome, telefone, criado_em FROM clientes
                    WHERE status='pendente' AND ativo ORDER BY criado_em DESC""")
    return jsonify({'ok': True, 'rows': rows})


@app.route('/api/clientes/<int:cid>/aprovar', methods=['POST'])
@login_required
def cli_aprovar(cid):
    execute("UPDATE clientes SET status='aprovado' WHERE id=%s AND status='pendente'", (cid,))
    return jsonify({'ok': True})


@app.route('/api/clientes/<int:cid>/recusar', methods=['POST'])
@login_required
def cli_recusar(cid):
    execute("UPDATE clientes SET ativo=false WHERE id=%s AND status='pendente'", (cid,))
    return jsonify({'ok': True})


@app.route('/api/planos', methods=['GET'])
@login_required
def plano_list():
    rows = q_all("SELECT * FROM planos WHERE ativo ORDER BY nome")
    for p in rows:
        p['servicos'] = q_all("""SELECT s.id, s.nome FROM plano_servicos ps
                                 JOIN servicos s ON s.id=ps.servico_id WHERE ps.plano_id=%s""", (p['id'],))
    return jsonify({'ok': True, 'rows': rows})


REGRAS_ASSINANTE = ('bolo', 'tabela', 'fixo', 'zero')


def valida_regra_assinante(d):
    """(regra, valor, erro) — regra de comissão do assinante vinda do front. 'fixo' exige o R$."""
    regra = d.get('comissao_assinante_regra')
    if regra is not None and regra not in REGRAS_ASSINANTE:
        return None, None, f"Regra de comissão inválida: {regra}"
    try:
        valor = float(d['comissao_assinante_valor']) if d.get('comissao_assinante_valor') else None
    except (TypeError, ValueError):
        return None, None, 'Valor da comissão do assinante inválido'
    if regra == 'fixo' and not valor:
        return None, None, "Na regra 'fixo' informe o R$ por atendimento do assinante."
    return regra, valor, None


@app.route('/api/planos', methods=['POST'])
@login_required
@role_required('dono')
def plano_create():
    d = request.get_json() or {}
    if not (d.get('nome') or '').strip():
        return jsonify({'ok': False, 'error': 'Nome obrigatório'}), 400
    regra, valor_regra, erro = valida_regra_assinante(d)
    if erro:
        return jsonify({'ok': False, 'error': erro}), 400
    row = execute("""INSERT INTO planos (nome, valor_mensal, limite_uso, dias_inclusos,
                     comissao_assinante_regra, comissao_assinante_valor)
                     VALUES (%s,%s,%s,%s,%s,%s) RETURNING *""",
                  (d['nome'].strip(), d.get('valor_mensal') or 0, d.get('limite_uso') or None,
                   Json(d.get('dias_inclusos') or [1, 2, 3]), regra or 'bolo', valor_regra),
                  returning=True)
    for sid in (d.get('servicos') or []):
        execute("INSERT INTO plano_servicos (plano_id, servico_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (row['id'], sid))
    return jsonify({'ok': True, 'row': row})


@app.route('/api/planos/<int:pid>', methods=['PUT'])
@login_required
@role_required('dono')
def plano_update(pid):
    d = request.get_json() or {}
    regra, valor_regra, erro = valida_regra_assinante(d)
    if erro:
        return jsonify({'ok': False, 'error': erro}), 400
    # comissao_assinante_valor só é sobrescrito quando a regra vem junto — senão um PUT parcial
    # (só o nome, por exemplo) apagaria o R$ do plano 'fixo'.
    execute("""UPDATE planos SET nome=COALESCE(%s,nome), valor_mensal=COALESCE(%s,valor_mensal),
               limite_uso=%s, dias_inclusos=COALESCE(%s,dias_inclusos),
               comissao_assinante_regra=COALESCE(%s,comissao_assinante_regra),
               comissao_assinante_valor=CASE WHEN %s THEN %s ELSE comissao_assinante_valor END
               WHERE id=%s""",
            (d.get('nome'), d.get('valor_mensal'), d.get('limite_uso') or None,
             Json(d['dias_inclusos']) if d.get('dias_inclusos') is not None else None,
             regra, regra is not None, valor_regra, pid))
    if d.get('servicos') is not None:
        execute("DELETE FROM plano_servicos WHERE plano_id=%s", (pid,))
        for sid in d['servicos']:
            execute("INSERT INTO plano_servicos (plano_id, servico_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (pid, sid))
    return jsonify({'ok': True})


@app.route('/api/config', methods=['GET'])
@login_required
def config_get():
    # CONTRATO: formas_pagamento sai daqui como ARRAY DE NOMES, igual sempre foi — comanda,
    # despesas, assinaturas, config e a ficha de coleta consomem esse formato. A fonte da verdade
    # passou a ser a tabela formas_pagamento (que tem a taxa); o JSONB da configuracoes ficou
    # vestigial. Quem precisa da taxa usa /api/formas-pagamento.
    c = cfg()
    c['formas_pagamento'] = formas_ativas()
    return jsonify({'ok': True, 'config': c})


# ── Formas de pagamento (nome + taxa da maquininha) ───────────────────
@app.route('/api/formas-pagamento', methods=['GET'])
@login_required
def forma_list():
    return jsonify({'ok': True, 'rows': q_all(
        "SELECT * FROM formas_pagamento ORDER BY ordem, nome")})


@app.route('/api/formas-pagamento', methods=['POST'])
@login_required
@role_required('dono')
def forma_create():
    d = request.get_json() or {}
    nome = (d.get('nome') or '').strip()
    if not nome:
        return jsonify({'ok': False, 'error': 'Nome obrigatório'}), 400
    if scalar("SELECT 1 FROM formas_pagamento WHERE lower(nome)=lower(%s)", (nome,)):
        return jsonify({'ok': False, 'error': 'Já existe uma forma com esse nome'}), 409
    ordem = d.get('ordem')
    if ordem is None:
        ordem = (scalar("SELECT COALESCE(MAX(ordem),0)+1 FROM formas_pagamento") or 0)
    row = execute("INSERT INTO formas_pagamento (nome, taxa_pct, ordem) VALUES (%s,%s,%s) RETURNING *",
                  (nome, d.get('taxa_pct') or 0, ordem), returning=True)
    return jsonify({'ok': True, 'row': row})


@app.route('/api/formas-pagamento/<int:fid>', methods=['PUT'])
@login_required
@role_required('dono')
def forma_update(fid):
    """Só taxa/ativo/ordem. O NOME não muda: ele é a chave gravada no histórico de movimentos e
    comandas — renomear aqui deixaria os lançamentos antigos órfãos da taxa deles."""
    d = request.get_json() or {}
    execute("""UPDATE formas_pagamento SET taxa_pct=COALESCE(%s,taxa_pct),
               ativo=COALESCE(%s,ativo), ordem=COALESCE(%s,ordem) WHERE id=%s""",
            (d.get('taxa_pct'), d.get('ativo'), d.get('ordem'), fid))
    return jsonify({'ok': True})


@app.route('/api/formas-pagamento/<int:fid>', methods=['DELETE'])
@login_required
@role_required('dono')
def forma_delete(fid):
    """Desativa (não apaga): o nome segue vivo no histórico de movimentos e comandas."""
    execute("UPDATE formas_pagamento SET ativo=false WHERE id=%s", (fid,))
    return jsonify({'ok': True})


@app.route('/api/config', methods=['PUT'])
@login_required
@role_required('dono')
def config_update():
    d = request.get_json() or {}
    # formas_pagamento NÃO é gravada aqui: a tabela formas_pagamento é a fonte da verdade e tem a
    # taxa. Duas fontes pra mesma coisa seria bug garantido — use /api/formas-pagamento.
    execute("""UPDATE configuracoes SET
                 slot_min=COALESCE(%s,slot_min), comissao_padrao=COALESCE(%s,comissao_padrao),
                 horarios=COALESCE(%s,horarios),
                 categorias_despesa=COALESCE(%s,categorias_despesa),
                 marca_nome=COALESCE(%s,marca_nome), marca_logo_url=%s,
                 marca_endereco=COALESCE(%s,marca_endereco), marca_whatsapp=COALESCE(%s,marca_whatsapp),
                 agendamento_online=COALESCE(%s,agendamento_online),
                 agendamento_confirmar_manual=COALESCE(%s,agendamento_confirmar_manual),
                 agendamento_antecedencia_horas=COALESCE(%s,agendamento_antecedencia_horas),
                 agendamento_janela_dias=COALESCE(%s,agendamento_janela_dias)
               WHERE id=1""",
            (d.get('slot_min'), d.get('comissao_padrao'),
             Json(d['horarios']) if d.get('horarios') is not None else None,
             Json(d['categorias_despesa']) if d.get('categorias_despesa') is not None else None,
             d.get('marca_nome'), d.get('marca_logo_url'),
             d.get('marca_endereco'), d.get('marca_whatsapp'),
             d.get('agendamento_online'), d.get('agendamento_confirmar_manual'),
             d.get('agendamento_antecedencia_horas'), d.get('agendamento_janela_dias')))
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════════
# Agenda
# ══════════════════════════════════════════════════════════════════════
@app.route('/api/agenda')
@login_required
def agenda():
    d = parse_date(request.args.get('data'), date.today())
    c = cfg()
    slot_min = c.get('slot_min') or 30
    horarios = c.get('horarios') or {}
    dia_cfg = horarios.get(str(js_weekday(d)))

    # barbeiro só vê a própria agenda
    if session.get('role') == 'barbeiro' and session.get('profissional_id'):
        profs = q_all("SELECT * FROM profissionais WHERE ativo AND id=%s", (session['profissional_id'],))
    else:
        profs = q_all("SELECT * FROM profissionais WHERE ativo ORDER BY nome")

    agendamentos = q_all("""
        SELECT a.*, c.nome AS cliente_nome, c.tipo AS cliente_tipo, s.nome AS servico_nome, s.preco
        FROM agendamentos a
        LEFT JOIN clientes c ON c.id=a.cliente_id
        LEFT JOIN servicos s ON s.id=a.servico_id
        WHERE a.data=%s AND a.status NOT IN ('cancelado')
    """, (d,))
    smap = {s['id']: s['nome'] for s in q_all("SELECT id, nome FROM servicos")}
    for a in agendamentos:
        ids = a.get('servicos_ids') or ([a['servico_id']] if a.get('servico_id') else [])
        a['servicos_nomes'] = [smap[i] for i in ids if i in smap]

    # Marca assinantes (selo na agenda) e se o plano cobre o dia exibido (aviso de cobertura)
    cli_ids = list({a['cliente_id'] for a in agendamentos if a.get('cliente_id')})
    assin_map = {}
    if cli_ids:
        for r in q_all("""SELECT DISTINCT ON (a.cliente_id) a.cliente_id, pl.nome AS plano_nome, pl.dias_inclusos
                          FROM assinaturas a JOIN planos pl ON pl.id=a.plano_id
                          WHERE a.cliente_id = ANY(%s) AND a.status='ativa'
                            AND a.data_inicio<=%s AND (a.data_fim IS NULL OR a.data_fim>=%s)
                          ORDER BY a.cliente_id, a.id DESC""", (cli_ids, d, d)):
            assin_map[r['cliente_id']] = r
    wd = js_weekday(d)
    for a in agendamentos:
        info = assin_map.get(a.get('cliente_id'))
        a['assinante'] = bool(info)
        a['plano_nome'] = info['plano_nome'] if info else None
        a['plano_cobre_dia'] = bool(info and wd in (info['dias_inclusos'] or []))

    bloqueios = q_all("SELECT * FROM bloqueios WHERE data=%s", (d,))

    return jsonify({'ok': True, 'data': d.isoformat(), 'slot_min': slot_min,
                    'horario_dia': dia_cfg, 'profissionais': profs,
                    'agendamentos': agendamentos, 'bloqueios': bloqueios})


@app.route('/api/agendamentos', methods=['POST'])
@login_required
def agend_create():
    d = request.get_json() or {}
    prof = d.get('profissional_id')
    # aceita lista de serviços (vários) ou um único (compat)
    serv_ids = d.get('servicos_ids') or ([d['servico_id']] if d.get('servico_id') else [])
    serv_ids = [int(x) for x in serv_ids if x]
    data = parse_date(d.get('data'))
    hora = d.get('hora_inicio')  # 'HH:MM'
    if not (prof and data and hora):
        return jsonify({'ok': False, 'error': 'Profissional, data e hora obrigatórios'}), 400
    slot_min = (cfg().get('slot_min') or 30)
    dur = 30
    if serv_ids:
        rows = q_all("SELECT id, duracao_min FROM servicos WHERE id = ANY(%s)", (serv_ids,))
        dur = sum((r['duracao_min'] or 30) for r in rows) or 30
    slots = max(1, math.ceil(dur / slot_min))
    principal = serv_ids[0] if serv_ids else None
    ok, motivo = slot_livre(prof, data, hora, slots, slot_min)
    if not ok:
        return jsonify({'ok': False, 'error': motivo}), 409
    row = execute("""INSERT INTO agendamentos (profissional_id, cliente_id, servico_id, servicos_ids,
                     data, hora_inicio, duracao_slots, origem, observacao, criado_por)
                     VALUES (%s,%s,%s,%s,%s,%s,%s,'agenda',%s,%s) RETURNING id""",
                  (prof, d.get('cliente_id') or None, principal, Json(serv_ids), data, hora, slots,
                   d.get('observacao'), session['user_id']), returning=True)
    return jsonify({'ok': True, 'id': row['id']})


@app.route('/api/agendamentos/<int:aid>', methods=['PUT'])
@login_required
def agend_update(aid):
    d = request.get_json() or {}
    if d.get('status') in ('agendado', 'atendido', 'cancelado', 'falta'):
        execute("UPDATE agendamentos SET status=%s WHERE id=%s", (d['status'], aid))
    return jsonify({'ok': True})


@app.route('/api/agendamentos/<int:aid>', methods=['DELETE'])
@login_required
def agend_delete(aid):
    execute("UPDATE agendamentos SET status='cancelado' WHERE id=%s", (aid,))
    return jsonify({'ok': True})


@app.route('/api/bloqueios', methods=['POST'])
@login_required
def bloqueio_create():
    d = request.get_json() or {}
    data = parse_date(d.get('data'))
    if not (d.get('profissional_id') and data and d.get('hora_inicio') and d.get('hora_fim')):
        return jsonify({'ok': False, 'error': 'Dados incompletos'}), 400
    execute("""INSERT INTO bloqueios (profissional_id, data, hora_inicio, hora_fim, motivo)
               VALUES (%s,%s,%s,%s,%s)""",
            (d['profissional_id'], data, d['hora_inicio'], d['hora_fim'], d.get('motivo')))
    return jsonify({'ok': True})


@app.route('/api/bloqueios/<int:bid>', methods=['DELETE'])
@login_required
def bloqueio_delete(bid):
    execute("DELETE FROM bloqueios WHERE id=%s", (bid,))
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════════
# Fila de mensagens de WhatsApp (envio MANUAL, pelo aparelho da barbearia)
# ══════════════════════════════════════════════════════════════════════
# Nada é disparado automaticamente. O sistema monta o texto e devolve um link wa.me; a barbearia
# toca, o WhatsApp DELA abre com a mensagem pronta e ela envia. Assim:
#   · a mensagem sai do número real da barbearia (o cliente reconhece e responde ali)
#   · não existe instância de WhatsApp por cliente pra conectar, pagar e reconectar
#   · não há risco de bloqueio por disparo em massa
#
# A FILA É UMA CONSULTA (as duas colunas de timestamp nulas) — por isso não há cron nem agendador.
#
# O canal UazAPI (alerta_whatsapp) NÃO é usado aqui: aquilo é o aviso interno da JOGA.
MENSAGEM_TIPOS = ('aceite', 'lembrete')


def wa_numero(telefone):
    """Número para link wa.me: 55 + DDD + número CHEIO (com o 9).

    NÃO usar normalizar_telefone_br: ela REMOVE o 9º dígito porque esse é o canônico da UazAPI.
    Aqui o 9 tem que ficar, senão o link abre uma conversa que não existe.
    """
    n = so_digitos(telefone)
    if len(n) < 10:
        return None
    if not n.startswith('55'):
        n = '55' + n
    return n


def _primeiro_nome(nome):
    return (str(nome or '').strip().split(' ')[0] or 'tudo bem').title()


def _texto_mensagem(tipo, ag, marca, endereco, base_url):
    d = parse_date(ag['data'])
    dia = f"{DIAS_SEMANA_PT[js_weekday(d)]}, {d.strftime('%d/%m')}" if d else ''
    hora = str(ag['hora_inicio'])[:5]
    servs = ', '.join(ag.get('servicos') or []) or 'atendimento'
    link = f"{base_url}/agendamento"
    # SEM EMOJI de propósito. O link wa.me sai em UTF-8 correto, mas quando o Windows repassa a
    # URL pro WhatsApp Desktop ele converte pra codepage ANSI: acento sobrevive (existe em
    # Latin-1), emoji não — chega como "?" no cliente da barbearia. Não dá pra controlar o
    # aplicativo do outro lado, então a mensagem tem que ser legível em texto puro.
    if tipo == 'aceite':
        corpo = (f"Oi, {_primeiro_nome(ag['cliente_nome'])}! Seu horário na *{marca}* está confirmado.\n\n"
                 f"*{dia.capitalize()}, às {hora}*\n{servs}\nCom {ag.get('barbeiro') or 'a equipe'}\n")
        if endereco:
            corpo += f"{endereco}\n"
        return corpo + f"\nPrecisando cancelar ou remarcar, é por aqui:\n{link}"
    return (f"Oi, {_primeiro_nome(ag['cliente_nome'])}! Passando pra confirmar seu horário "
            f"de amanhã na *{marca}*.\n\n*{dia.capitalize()}, às {hora}*\n{servs}\n\n"
            f"Consegue confirmar pra mim? Se precisar remarcar:\n{link}")


DIAS_SEMANA_PT = ['domingo', 'segunda', 'terça', 'quarta', 'quinta', 'sexta', 'sábado']


@app.route('/api/mensagens/pendentes')
@login_required
def mensagens_pendentes():
    """Duas filas: avisar quem foi aceito, e confirmar quem vem amanhã.

    `aceite` só vale pra agendamento online — é quem está esperando resposta. O `lembrete` vale
    pra todo mundo de amanhã, tenha vindo do link ou do balcão.
    """
    c = cfg()
    marca = c.get('marca_nome') or 'Barbearia'
    endereco = c.get('marca_endereco') or ''
    base = request.url_root.rstrip('/')
    smap = {s['id']: s['nome'] for s in q_all("SELECT id, nome FROM servicos")}

    def montar(tipo, rows):
        out = []
        for a in rows:
            a = dict(a)
            a['servicos'] = [smap[i] for i in (a.get('servicos_ids') or []) if i in smap]
            numero = wa_numero(a.get('telefone'))
            texto = _texto_mensagem(tipo, a, marca, endereco, base)
            out.append({
                'tipo': tipo, 'agendamento_id': a['id'], 'cliente_nome': a['cliente_nome'],
                'telefone': a.get('telefone'), 'data': a['data'], 'hora': str(a['hora_inicio'])[:5],
                'barbeiro': a.get('barbeiro'), 'servicos': a['servicos'], 'texto': texto,
                # safe='' força codificar a '/' também: o texto tem o link /agendamento dentro e
                # deixar barra crua no query string é pedir pra algum cliente truncar a mensagem.
                'wa_url': (f"https://wa.me/{numero}?text={urllib.parse.quote(texto, safe='')}"
                           if numero else None),
                'sem_telefone': numero is None,
            })
        return out

    aceites = q_all("""SELECT a.id, a.data, a.hora_inicio, a.servicos_ids, c.nome AS cliente_nome,
                         c.telefone, p.nome AS barbeiro
                       FROM agendamentos a
                       JOIN clientes c ON c.id=a.cliente_id
                       LEFT JOIN profissionais p ON p.id=a.profissional_id
                       WHERE a.origem='online' AND a.status='agendado'
                         AND a.confirmacao_enviada_em IS NULL AND a.data >= CURRENT_DATE
                       ORDER BY a.data, a.hora_inicio""")
    lembretes = q_all("""SELECT a.id, a.data, a.hora_inicio, a.servicos_ids, c.nome AS cliente_nome,
                           c.telefone, p.nome AS barbeiro
                         FROM agendamentos a
                         JOIN clientes c ON c.id=a.cliente_id
                         LEFT JOIN profissionais p ON p.id=a.profissional_id
                         WHERE a.status='agendado' AND a.lembrete_enviado_em IS NULL
                           AND a.data = CURRENT_DATE + 1
                         ORDER BY a.hora_inicio""")
    a_rows, l_rows = montar('aceite', aceites), montar('lembrete', lembretes)
    return jsonify({'ok': True, 'aceites': a_rows, 'lembretes': l_rows,
                    'total': len(a_rows) + len(l_rows)})


@app.route('/api/mensagens/<tipo>/<int:aid>/enviado', methods=['POST', 'DELETE'])
@login_required
def mensagem_marcar(tipo, aid):
    """Carimba (ou desfaz) o envio.

    O sistema NÃO sabe se ela apertou enviar no WhatsApp — o wa.me não devolve nada. Marcamos no
    clique e deixamos o desfazer à mão. Melhor assumir isso do que exibir um "entregue" falso.
    """
    if tipo not in MENSAGEM_TIPOS:
        return jsonify({'ok': False, 'error': 'Tipo inválido'}), 400
    col = 'confirmacao_enviada_em' if tipo == 'aceite' else 'lembrete_enviado_em'
    valor = 'NOW()' if request.method == 'POST' else 'NULL'
    execute(f"UPDATE agendamentos SET {col} = {valor} WHERE id=%s", (aid,))
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════════
# Comanda (o pivô)
# ══════════════════════════════════════════════════════════════════════
def _recalc_comanda(comanda_id):
    total = scalar("SELECT COALESCE(SUM(subtotal),0) FROM comanda_itens WHERE comanda_id=%s", (comanda_id,)) or 0
    execute("UPDATE comandas SET valor_total=%s WHERE id=%s", (total, comanda_id))
    return round(total, 2)


def _add_servico_item(com, servico_id, executor, qtd=1):
    """Insere um item de serviço na comanda, aplicando cobertura do plano. Reuso."""
    s = q_one("SELECT * FROM servicos WHERE id=%s", (servico_id,))
    if not s:
        return False
    coberto, assin_id = cobertura_plano(com['cliente_id'], s['id'], date.today(), exclude_comanda_id=com['id'])
    preco_unit = 0 if coberto else float(s['preco'])
    execute("""INSERT INTO comanda_itens (comanda_id, tipo, ref_id, descricao, profissional_id,
               preco_unit, qtd, subtotal, preco_tabela, coberto_plano, assinatura_id)
               VALUES (%s,'servico',%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (com['id'], s['id'], s['nome'], executor, preco_unit, qtd, preco_unit * qtd,
             float(s['preco']), coberto, assin_id))
    return True


@app.route('/api/comandas', methods=['POST'])
@login_required
def comanda_abrir():
    """Abre comanda (de um agendamento ou walk-in)."""
    d = request.get_json() or {}
    agendamento_id = d.get('agendamento_id') or None
    cliente_id = d.get('cliente_id') or None
    prof = d.get('profissional_id') or None
    if agendamento_id:
        ag = q_one("SELECT * FROM agendamentos WHERE id=%s", (agendamento_id,))
        if ag:
            cliente_id = cliente_id or ag['cliente_id']
            prof = prof or ag['profissional_id']
    if not prof:
        return jsonify({'ok': False, 'error': 'Profissional obrigatório'}), 400
    row = execute("""INSERT INTO comandas (cliente_id, profissional_id, agendamento_id, criado_por)
                     VALUES (%s,%s,%s,%s) RETURNING id""",
                  (cliente_id, prof, agendamento_id, session['user_id']), returning=True)
    cid = row['id']
    # Pré-carrega os serviços que estavam no agendamento (vários serviços)
    if agendamento_id:
        ag = q_one("SELECT servico_id, servicos_ids FROM agendamentos WHERE id=%s", (agendamento_id,))
        ids = (ag.get('servicos_ids') if ag else None) or ([] if not (ag and ag.get('servico_id')) else [ag['servico_id']])
        com = {'id': cid, 'cliente_id': cliente_id}
        for sid in ids:
            _add_servico_item(com, sid, prof)
        if ids:
            _recalc_comanda(cid)
    return jsonify({'ok': True, 'id': cid})


@app.route('/api/comandas/aberta')
@login_required
def comanda_aberta():
    """Retorna a comanda aberta (por id ou por agendamento) com itens."""
    cid = request.args.get('id')
    if cid:
        com = q_one("""SELECT co.*, c.nome AS cliente_nome, p.nome AS prof_nome
                       FROM comandas co LEFT JOIN clientes c ON c.id=co.cliente_id
                       LEFT JOIN profissionais p ON p.id=co.profissional_id WHERE co.id=%s""", (cid,))
    else:
        return jsonify({'ok': False, 'error': 'Informe o id'}), 400
    if not com:
        return jsonify({'ok': False, 'error': 'Comanda não encontrada'}), 404
    itens = q_all("SELECT * FROM comanda_itens WHERE comanda_id=%s ORDER BY id", (com['id'],))
    # assinatura ativa do cliente → cabeçalho "⭐ assinante" + status de cobertura hoje
    assinatura = None
    if com.get('cliente_id'):
        assinatura = q_one("""SELECT pl.nome AS plano_nome, pl.dias_inclusos FROM assinaturas a
            JOIN planos pl ON pl.id=a.plano_id WHERE a.cliente_id=%s AND a.status='ativa'
              AND a.data_inicio<=CURRENT_DATE AND (a.data_fim IS NULL OR a.data_fim>=CURRENT_DATE)
            ORDER BY a.id DESC LIMIT 1""", (com['cliente_id'],))
    return jsonify({'ok': True, 'comanda': com, 'itens': itens, 'assinatura': assinatura})


@app.route('/api/comandas/abertas')
@login_required
def comandas_abertas():
    rows = q_all("""SELECT co.*, c.nome AS cliente_nome, p.nome AS prof_nome
                    FROM comandas co LEFT JOIN clientes c ON c.id=co.cliente_id
                    LEFT JOIN profissionais p ON p.id=co.profissional_id
                    WHERE co.status='aberta' ORDER BY co.aberta_em DESC""")
    return jsonify({'ok': True, 'rows': rows})


@app.route('/api/comandas/fechadas')
@login_required
def comandas_fechadas():
    """Atendimentos fechados de um dia (default hoje) — para o 'cancelar atendimento'."""
    d = parse_date(request.args.get('data'), date.today())
    rows = q_all("""SELECT co.*, c.nome AS cliente_nome, p.nome AS prof_nome
                    FROM comandas co LEFT JOIN clientes c ON c.id=co.cliente_id
                    LEFT JOIN profissionais p ON p.id=co.profissional_id
                    WHERE co.status='fechada' AND co.fechada_em::date=%s ORDER BY co.fechada_em DESC""", (d,))
    return jsonify({'ok': True, 'rows': rows})


@app.route('/api/comandas/<int:cid>/itens', methods=['POST'])
@login_required
def comanda_add_item(cid):
    d = request.get_json() or {}
    com = q_one("SELECT * FROM comandas WHERE id=%s", (cid,))
    if not com or com['status'] != 'aberta':
        return jsonify({'ok': False, 'error': 'Comanda inválida'}), 400
    tipo = d.get('tipo')
    ref_id = d.get('ref_id')
    qtd = max(1, int(d.get('qtd') or 1))
    hoje = date.today()

    if tipo == 'servico':
        executor = d.get('profissional_id') or com['profissional_id']
        if not _add_servico_item(com, ref_id, executor, qtd):
            return jsonify({'ok': False, 'error': 'Serviço inválido'}), 400
    elif tipo == 'produto':
        p = q_one("SELECT * FROM produtos WHERE id=%s", (ref_id,))
        if not p:
            return jsonify({'ok': False, 'error': 'Produto inválido'}), 400
        preco_unit = float(p['preco'])
        execute("""INSERT INTO comanda_itens (comanda_id, tipo, ref_id, descricao, profissional_id,
                   preco_unit, qtd, subtotal, coberto_plano)
                   VALUES (%s,'produto',%s,%s,%s,%s,%s,%s,false)""",
                (cid, p['id'], p['nome'], com['profissional_id'], preco_unit, qtd, preco_unit * qtd))
    else:
        return jsonify({'ok': False, 'error': 'Tipo inválido'}), 400

    total = _recalc_comanda(cid)
    return jsonify({'ok': True, 'valor_total': total})


@app.route('/api/comandas/itens/<int:item_id>', methods=['DELETE'])
@login_required
def comanda_del_item(item_id):
    item = q_one("SELECT comanda_id FROM comanda_itens WHERE id=%s", (item_id,))
    if not item:
        return jsonify({'ok': False, 'error': 'Item não encontrado'}), 404
    execute("DELETE FROM comanda_itens WHERE id=%s", (item_id,))
    total = _recalc_comanda(item['comanda_id'])
    return jsonify({'ok': True, 'valor_total': total})


@app.route('/api/comandas/<int:cid>/fechar', methods=['POST'])
@login_required
def comanda_fechar(cid):
    d = request.get_json() or {}
    com = q_one("SELECT * FROM comandas WHERE id=%s", (cid,))
    if not com or com['status'] != 'aberta':
        return jsonify({'ok': False, 'error': 'Comanda inválida'}), 400
    forma = d.get('forma_pagamento')
    total = _recalc_comanda(cid)
    execute("UPDATE comandas SET status='fechada', forma_pagamento=%s, fechada_em=NOW() WHERE id=%s", (forma, cid))
    if total > 0:
        mov = execute("""INSERT INTO movimentos (tipo, origem, ref_id, descricao, valor, forma_pagamento, status, criado_por)
                   VALUES ('receita','comanda',%s,%s,%s,%s,'pago',%s) RETURNING id""",
                      (cid, f"Comanda #{cid}", total, forma, session['user_id']), returning=True)
        registrar_taxa(mov['id'], total, forma)
    if com['agendamento_id']:
        execute("UPDATE agendamentos SET status='atendido' WHERE id=%s", (com['agendamento_id'],))
    return jsonify({'ok': True, 'valor_total': total})


@app.route('/api/comandas/<int:cid>/cancelar', methods=['POST'])
@login_required
def comanda_cancelar(cid):
    """Cancela a comanda. Se ela já estava FECHADA (atendimento desfeito), também tira a receita
    do caixa e devolve o agendamento para 'agendado' (pra poder refazer pela Agenda)."""
    com = q_one("SELECT status, agendamento_id FROM comandas WHERE id=%s", (cid,))
    if not com or com['status'] == 'cancelada':
        return jsonify({'ok': True})
    if com['status'] == 'fechada':
        # A taxa é filha do movimento de receita: sai antes, senão vira despesa órfã no caixa.
        remover_taxas_de([r['id'] for r in q_all(
            "SELECT id FROM movimentos WHERE origem='comanda' AND ref_id=%s", (cid,))])
        execute("DELETE FROM movimentos WHERE origem='comanda' AND ref_id=%s", (cid,))
        if com['agendamento_id']:
            execute("UPDATE agendamentos SET status='agendado' WHERE id=%s", (com['agendamento_id'],))
    execute("UPDATE comandas SET status='cancelada' WHERE id=%s", (cid,))
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════════
# Assinaturas
# ══════════════════════════════════════════════════════════════════════
@app.route('/api/assinaturas', methods=['GET'])
@login_required
def assin_list():
    rows = q_all("""SELECT a.*, c.nome AS cliente_nome, c.telefone, pl.nome AS plano_nome, pl.valor_mensal
                    FROM assinaturas a JOIN clientes c ON c.id=a.cliente_id
                    JOIN planos pl ON pl.id=a.plano_id
                    WHERE a.status<>'cancelada' ORDER BY c.nome""")
    return jsonify({'ok': True, 'rows': rows})


def _add_um_mes(dt):
    m = dt.month % 12 + 1
    y = dt.year + (1 if dt.month == 12 else 0)
    return date(y, m, min(dt.day, calendar.monthrange(y, m)[1]))


def _dia_mes_seguinte(base, dia):
    """Dia (10/30) do mês SEGUINTE ao da data base (~1 mês à frente)."""
    m = base.month % 12 + 1
    y = base.year + (1 if base.month == 12 else 0)
    return date(y, m, min(dia, calendar.monthrange(y, m)[1]))


@app.route('/api/assinaturas', methods=['POST'])
@login_required
def assin_create():
    d = request.get_json() or {}
    if not (d.get('cliente_id') and d.get('plano_id')):
        return jsonify({'ok': False, 'error': 'Cliente e plano obrigatórios'}), 400
    venc = int(d.get('dia_vencimento') or 10)
    if venc not in (10, 30):
        venc = 10
    pl = q_one("SELECT valor_mensal FROM planos WHERE id=%s", (d['plano_id'],))
    cli = q_one("SELECT nome FROM clientes WHERE id=%s", (d['cliente_id'],))
    hoje = date.today()
    proxima = _dia_mes_seguinte(hoje, venc)   # 1ª paga hoje → próxima no dia escolhido do mês seguinte
    row = execute("""INSERT INTO assinaturas (cliente_id, plano_id, dia_vencimento, proxima_cobranca)
                     VALUES (%s,%s,%s,%s) RETURNING id""",
                  (d['cliente_id'], d['plano_id'], venc, proxima), returning=True)
    # 1ª mensalidade cobrada na hora → cai no caixa hoje
    if d.get('receber_agora', True) and pl:
        mov = execute("""INSERT INTO movimentos (tipo, origem, ref_id, descricao, valor, forma_pagamento, data, status, criado_por)
                   VALUES ('receita','assinatura',%s,%s,%s,%s,CURRENT_DATE,'pago',%s) RETURNING id""",
                      (row['id'], f"Assinatura {cli['nome'] if cli else ''} — 1ª mensalidade",
                       pl['valor_mensal'], d.get('forma_pagamento'), session['user_id']), returning=True)
        registrar_taxa(mov['id'], pl['valor_mensal'], d.get('forma_pagamento'))
    return jsonify({'ok': True, 'id': row['id'], 'proxima_cobranca': proxima.isoformat()})


@app.route('/api/assinaturas/<int:aid>', methods=['PUT'])
@login_required
def assin_update(aid):
    d = request.get_json() or {}
    if d.get('status') in ('ativa', 'pausada', 'cancelada'):
        fim = ", data_fim=CURRENT_DATE" if d['status'] == 'cancelada' else ""
        execute(f"UPDATE assinaturas SET status=%s{fim} WHERE id=%s", (d['status'], aid))
        # Cancelar a assinatura desfaz os lançamentos dela no caixa (a 1ª mensalidade já recebida
        # + as cobranças futuras previstas). O "desfazer" da assinatura mora aqui, na origem —
        # no Caixa só se exclui o que é manual.
        if d['status'] == 'cancelada':
            remover_taxas_de([r['id'] for r in q_all(
                "SELECT id FROM movimentos WHERE origem='assinatura' AND ref_id=%s", (aid,))])
            execute("DELETE FROM movimentos WHERE origem='assinatura' AND ref_id=%s", (aid,))
    return jsonify({'ok': True})


@app.route('/api/assinaturas/gerar-cobrancas', methods=['POST'])
@login_required
def assin_gerar_cobrancas():
    """Gera as cobranças (previsto) das assinaturas conforme a próxima_cobranca, até o fim do mês
    selecionado. Avança a próxima_cobranca de mês em mês. Idempotente."""
    d = request.get_json() or {}
    comp = d.get('competencia')  # 'YYYY-MM'
    hoje = date.today()
    try:
        ano, mes = (int(comp[:4]), int(comp[5:7])) if comp else (hoje.year, hoje.month)
    except (ValueError, IndexError):
        return jsonify({'ok': False, 'error': 'Competência inválida'}), 400
    ref = date(ano, mes, calendar.monthrange(ano, mes)[1])   # gera o que vence até o fim do mês
    # assinatura_id opcional: gera só a desse assinante (botão por linha); sem ele, gera de todas
    aid = d.get('assinatura_id')
    filtro, params = ("AND a.id=%s", (aid,)) if aid else ("", ())
    ativas = q_all(f"""SELECT a.id, a.dia_vencimento, a.proxima_cobranca, pl.valor_mensal, c.nome AS cliente_nome
                      FROM assinaturas a JOIN planos pl ON pl.id=a.plano_id JOIN clientes c ON c.id=a.cliente_id
                      WHERE a.status='ativa' {filtro}""", params)
    gerados = 0
    for a in ativas:
        px = parse_date(a['proxima_cobranca'])
        if not px:
            px = _dia_mes_seguinte(hoje, a['dia_vencimento'])  # legado sem proxima_cobranca
        while px <= ref:
            existe = scalar("SELECT COUNT(*) FROM movimentos WHERE origem='assinatura' AND ref_id=%s AND vencimento=%s", (a['id'], px))
            if not existe:
                execute("""INSERT INTO movimentos (tipo, origem, ref_id, descricao, valor, data, vencimento, status, criado_por)
                           VALUES ('receita','assinatura',%s,%s,%s,%s,%s,'previsto',%s)""",
                        (a['id'], f"Mensalidade — {a['cliente_nome']}", a['valor_mensal'], px, px, session['user_id']))
                gerados += 1
            px = _add_um_mes(px)
        execute("UPDATE assinaturas SET proxima_cobranca=%s WHERE id=%s", (px, a['id']))
    return jsonify({'ok': True, 'gerados': gerados})


# ══════════════════════════════════════════════════════════════════════
# Visitas de assinante (documentação de uso do plano)
# ══════════════════════════════════════════════════════════════════════
@app.route('/api/assinantes/ativos')
@login_required
def assinantes_ativos():
    """Assinantes ativos + serviços cobertos do plano — alimenta a tela de registrar visita."""
    rows = q_all("""SELECT a.id AS assinatura_id, a.cliente_id, c.nome AS cliente_nome, c.telefone,
                      a.plano_id, pl.nome AS plano_nome, pl.valor_mensal, pl.dias_inclusos
                    FROM assinaturas a JOIN clientes c ON c.id=a.cliente_id
                    JOIN planos pl ON pl.id=a.plano_id
                    WHERE a.status='ativa' ORDER BY c.nome""")
    for r in rows:
        r['servicos'] = q_all("""SELECT s.id, s.nome FROM plano_servicos ps JOIN servicos s ON s.id=ps.servico_id
                                 WHERE ps.plano_id=%s ORDER BY s.nome""", (r['plano_id'],))
    return jsonify({'ok': True, 'rows': rows})


@app.route('/api/visitas/registrar', methods=['POST'])
@login_required
def visita_registrar():
    """Registra uma visita de assinante em 1 passo: cria e fecha uma comanda R$0 com os serviços
    cobertos, atribuindo o barbeiro executor (que entra no bolo de comissão). Aceita data passada."""
    d = request.get_json() or {}
    cliente_id = d.get('cliente_id')
    prof = d.get('profissional_id')
    dia = parse_date(d.get('data'), date.today())
    if not (cliente_id and prof):
        return jsonify({'ok': False, 'error': 'Cliente e barbeiro são obrigatórios'}), 400
    if dia > date.today():
        return jsonify({'ok': False, 'error': 'A data não pode ser futura'}), 400
    assin = q_one("""SELECT a.id, a.plano_id, pl.limite_uso FROM assinaturas a JOIN planos pl ON pl.id=a.plano_id
                     WHERE a.cliente_id=%s AND a.status='ativa'
                       AND a.data_inicio<=%s AND (a.data_fim IS NULL OR a.data_fim>=%s)
                     ORDER BY a.id DESC LIMIT 1""", (cliente_id, dia, dia))
    if not assin:
        return jsonify({'ok': False, 'error': 'Cliente não tem assinatura ativa nessa data'}), 400
    # Trava firme: 1 visita coberta por dia — evita dobrar visita/comissão (comanda + registrar)
    if visita_coberta_no_dia(cliente_id, dia) > 0:
        return jsonify({'ok': False, 'error': 'Este assinante já tem uma visita registrada nesse dia'}), 409
    serv_ids = [int(x) for x in (d.get('servicos_ids') or []) if x]
    if not serv_ids:
        serv_ids = [r['servico_id'] for r in q_all("SELECT servico_id FROM plano_servicos WHERE plano_id=%s", (assin['plano_id'],))]
    if not serv_ids:
        return jsonify({'ok': False, 'error': 'O plano não tem serviços configurados'}), 400
    if assin['limite_uso'] is not None:
        usados = scalar("""SELECT COUNT(DISTINCT c.id) FROM comandas c JOIN comanda_itens ci ON ci.comanda_id=c.id
            WHERE ci.assinatura_id=%s AND ci.coberto_plano AND c.status<>'cancelada'
              AND date_trunc('month', c.fechada_em)=date_trunc('month', %s::timestamp)""", (assin['id'], dia)) or 0
        if usados >= assin['limite_uso']:
            return jsonify({'ok': False, 'error': f"Limite do plano no mês já atingido ({assin['limite_uso']} visitas)"}), 409
    fechada = f"{dia.isoformat()} 12:00:00"   # meio-dia p/ o dia bater com o filtro por data (fechada_em::date)
    row = execute("""INSERT INTO comandas (cliente_id, profissional_id, status, valor_total, fechada_em, criado_por)
                     VALUES (%s,%s,'fechada',0,%s,%s) RETURNING id""",
                  (cliente_id, prof, fechada, session['user_id']), returning=True)
    cid = row['id']
    for sid in serv_ids:
        s = q_one("SELECT id, nome, preco FROM servicos WHERE id=%s", (sid,))
        if not s:
            continue
        execute("""INSERT INTO comanda_itens (comanda_id, tipo, ref_id, descricao, profissional_id,
                   preco_unit, qtd, subtotal, preco_tabela, coberto_plano, assinatura_id)
                   VALUES (%s,'servico',%s,%s,%s,0,1,0,%s,true,%s)""",
                (cid, s['id'], s['nome'], prof, float(s['preco']), assin['id']))
    return jsonify({'ok': True, 'comanda_id': cid})


@app.route('/api/assinantes/<int:cliente_id>/visitas')
@login_required
def assinante_visitas(cliente_id):
    """Histórico de visitas cobertas de um assinante (para o detalhe/modal)."""
    rows = q_all("""SELECT c.id, c.fechada_em::date AS dia, pr.nome AS barbeiro,
                      (SELECT string_agg(ci.descricao, ', ' ORDER BY ci.descricao) FROM comanda_itens ci
                         WHERE ci.comanda_id=c.id AND ci.coberto_plano) AS servicos
                    FROM comandas c LEFT JOIN profissionais pr ON pr.id=c.profissional_id
                    WHERE c.status='fechada' AND c.cliente_id=%s
                      AND EXISTS (SELECT 1 FROM comanda_itens ci WHERE ci.comanda_id=c.id AND ci.coberto_plano)
                    ORDER BY c.fechada_em DESC LIMIT 60""", (cliente_id,))
    return jsonify({'ok': True, 'rows': rows})


@app.route('/api/relatorios/assinantes-uso')
@login_required
def rel_assinantes_uso():
    """Uso dos planos: por assinante ativo — visitas no período, última visita, frequência média,
    custo por visita, status de uso e distribuição por barbeiro. Mais agregados p/ decisão."""
    de = parse_date(request.args.get('de'), date.today().replace(day=1))
    ate = parse_date(request.args.get('ate'), date.today())
    hoje = date.today()

    ativos = q_all("""SELECT a.cliente_id, c.nome AS cliente_nome, pl.nome AS plano_nome,
                        pl.valor_mensal, a.dia_vencimento
                      FROM assinaturas a JOIN clientes c ON c.id=a.cliente_id
                      JOIN planos pl ON pl.id=a.plano_id WHERE a.status='ativa' ORDER BY c.nome""")

    visitas_periodo = q_all("""SELECT c.id, c.cliente_id, pr.nome AS prof_nome
        FROM comandas c LEFT JOIN profissionais pr ON pr.id=c.profissional_id
        WHERE c.status='fechada' AND c.fechada_em::date BETWEEN %s AND %s
          AND EXISTS (SELECT 1 FROM comanda_itens ci WHERE ci.comanda_id=c.id AND ci.coberto_plano)""", (de, ate))
    alltime = q_all("""SELECT c.cliente_id, MAX(c.fechada_em::date) AS ultima, COUNT(DISTINCT c.id) AS total
        FROM comandas c WHERE c.status='fechada'
          AND EXISTS (SELECT 1 FROM comanda_itens ci WHERE ci.comanda_id=c.id AND ci.coberto_plano)
        GROUP BY c.cliente_id""")
    ult90 = q_all("""SELECT c.cliente_id, c.fechada_em::date AS dia FROM comandas c
        WHERE c.status='fechada' AND c.fechada_em::date >= %s
          AND EXISTS (SELECT 1 FROM comanda_itens ci WHERE ci.comanda_id=c.id AND ci.coberto_plano)""",
        (hoje - timedelta(days=90),))

    at_idx = {r['cliente_id']: r for r in alltime}
    per_idx = {}
    for v in visitas_periodo:
        e = per_idx.setdefault(v['cliente_id'], {'ids': set(), 'barb': {}})
        e['ids'].add(v['id'])
        nome = v['prof_nome'] or '—'
        e['barb'][nome] = e['barb'].get(nome, 0) + 1
    freq_idx = {}
    for r in ult90:
        freq_idx.setdefault(r['cliente_id'], set()).add(parse_date(r['dia']))

    def freq_media(cid):
        ds = sorted(d for d in freq_idx.get(cid, set()) if d)
        if len(ds) < 2:
            return None
        gaps = [(ds[i] - ds[i - 1]).days for i in range(1, len(ds))]
        return round(sum(gaps) / len(gaps), 1)

    rows = []
    for a in ativos:
        cid = a['cliente_id']
        per = per_idx.get(cid, {'ids': set(), 'barb': {}})
        vis = len(per['ids'])
        at = at_idx.get(cid)
        ultima = at['ultima'] if at else None
        total = at['total'] if at else 0
        dias_sem = (hoje - parse_date(ultima)).days if ultima else None
        mensal = float(a['valor_mensal'] or 0)
        custo_visita = round(mensal / vis, 2) if vis else None
        if total == 0:
            status = 'nunca'
        elif dias_sem is not None and dias_sem > 30:
            status = 'dormente'
        elif vis >= 4:
            status = 'intenso'
        else:
            status = 'regular'
        barb = sorted(({'nome': k, 'qtd': v} for k, v in per['barb'].items()), key=lambda x: -x['qtd'])
        rows.append({'cliente_id': cid, 'cliente_nome': a['cliente_nome'], 'plano_nome': a['plano_nome'],
                     'mensalidade': mensal, 'visitas': vis, 'ultima_visita': ultima, 'dias_sem_visita': dias_sem,
                     'total_visitas': total, 'frequencia_media': freq_media(cid), 'custo_por_visita': custo_visita,
                     'status': status, 'barbeiros': barb, 'dia_vencimento': a['dia_vencimento']})
    rows.sort(key=lambda x: (-x['visitas'], x['cliente_nome'] or ''))

    # Custo de comissão dos assinantes: vem do MESMO motor do fechamento (o bolo dos planos 'bolo'
    # + a comissão direta dos planos 'tabela'/'fixo'), senão a margem mentiria em quem não usa bolo.
    c = calc_comissao(de, ate)
    arrecad = c['plan_revenue']
    bolo = round(c['pool'] + sum(r['assinante_direto'] for r in c['rows']), 2)
    pool_pct = c['pool_pct']
    barb_agg = {}
    for v in visitas_periodo:
        nome = v['prof_nome'] or '—'
        barb_agg[nome] = barb_agg.get(nome, 0) + 1
    por_barbeiro = sorted(({'nome': k, 'qtd': v} for k, v in barb_agg.items()), key=lambda x: -x['qtd'])

    return jsonify({'ok': True, 'de': de.isoformat(), 'ate': ate.isoformat(), 'rows': rows,
                    'resumo': {'assinantes': len(rows), 'visitas': sum(r['visitas'] for r in rows),
                               'dormentes': sum(1 for r in rows if r['status'] == 'dormente'),
                               'nunca': sum(1 for r in rows if r['status'] == 'nunca'),
                               'arrecadacao': round(arrecad, 2), 'bolo': round(bolo, 2),
                               'margem': round(arrecad - bolo, 2), 'pool_pct': pool_pct,
                               'por_barbeiro': por_barbeiro}})


# ══════════════════════════════════════════════════════════════════════
# Caixa / Movimentos / Relatórios
# ══════════════════════════════════════════════════════════════════════
@app.route('/api/movimentos', methods=['GET'])
@login_required
def mov_list():
    de = parse_date(request.args.get('de'), date.today())
    ate = parse_date(request.args.get('ate'), date.today())
    where = ["data>=%s AND data<=%s"]
    params = [de, ate]
    if request.args.get('tipo') in ('receita', 'despesa'):
        where.append("tipo=%s"); params.append(request.args.get('tipo'))
    if request.args.get('categoria'):
        where.append("categoria=%s"); params.append(request.args.get('categoria'))
    rows = q_all(f"SELECT * FROM movimentos WHERE {' AND '.join(where)} ORDER BY data DESC, id DESC LIMIT 800", params)
    return jsonify({'ok': True, 'rows': rows})


@app.route('/api/movimentos', methods=['POST'])
@login_required
def mov_create():
    d = request.get_json() or {}
    if d.get('tipo') not in ('receita', 'despesa') or not (d.get('descricao') or '').strip():
        return jsonify({'ok': False, 'error': 'Tipo e descrição obrigatórios'}), 400
    data_mov = parse_date(d.get('data'), date.today())
    mov = execute("""INSERT INTO movimentos (tipo, origem, descricao, categoria, valor, forma_pagamento, data, status, criado_por)
               VALUES (%s,'manual',%s,%s,%s,%s,%s,'pago',%s) RETURNING id""",
                  (d['tipo'], d['descricao'].strip(), d.get('categoria'), d.get('valor') or 0,
                   d.get('forma_pagamento'), data_mov, session['user_id']), returning=True)
    # Só receita paga taxa — despesa lançada "no cartão" é dinheiro saindo, não entrando.
    if d['tipo'] == 'receita':
        registrar_taxa(mov['id'], d.get('valor') or 0, d.get('forma_pagamento'), data_mov)
    return jsonify({'ok': True})


@app.route('/api/movimentos/<int:mid>', methods=['DELETE'])
@login_required
def mov_delete(mid):
    """Exclui um lançamento do caixa, mantendo as origens consistentes:
       - se veio de uma comanda fechada → cancela a comanda (DRE/comissão leem comanda_itens das
         comandas fechadas; sem isso o valor sairia do caixa mas continuaria no DRE);
       - se é a despesa de uma comissão fechada → desfaz o registro pago (volta a ficar pendente)."""
    m = q_one("SELECT origem, ref_id FROM movimentos WHERE id=%s", (mid,))
    if not m:
        return jsonify({'ok': True})
    if m['origem'] == 'comanda' and m['ref_id']:
        execute("UPDATE comandas SET status='cancelada' WHERE id=%s", (m['ref_id'],))
    remover_taxas_de([mid])                     # a taxa da maquininha sai junto com a receita
    execute("DELETE FROM comissoes_pagas WHERE movimento_id=%s", (mid,))
    execute("DELETE FROM movimentos WHERE id=%s", (mid,))
    return jsonify({'ok': True})


@app.route('/api/movimentos/<int:mid>/pagar', methods=['POST'])
@login_required
def mov_pagar(mid):
    """Marca um 'previsto' como pago. É AQUI que a mensalidade da assinatura ganha forma de
    pagamento — e portanto onde a taxa da maquininha nasce pra ela."""
    d = request.get_json() or {}
    execute("UPDATE movimentos SET status='pago', forma_pagamento=COALESCE(%s,forma_pagamento), data=CURRENT_DATE WHERE id=%s",
            (d.get('forma_pagamento'), mid))
    m = q_one("SELECT tipo, valor, forma_pagamento, data FROM movimentos WHERE id=%s", (mid,))
    if m and m['tipo'] == 'receita':
        registrar_taxa(mid, m['valor'], m['forma_pagamento'], m['data'])
    return jsonify({'ok': True})


# ── Despesas fixas (recorrentes) + resumo ─────────────────────────────
@app.route('/api/despesas-fixas', methods=['GET'])
@login_required
def df_list():
    return jsonify({'ok': True, 'rows': q_all("SELECT * FROM despesas_fixas WHERE ativo ORDER BY descricao")})


@app.route('/api/despesas-fixas', methods=['POST'])
@login_required
def df_create():
    d = request.get_json() or {}
    if not (d.get('descricao') or '').strip():
        return jsonify({'ok': False, 'error': 'Descrição obrigatória'}), 400
    dia = max(1, min(int(d.get('dia') or 5), 28))
    row = execute("""INSERT INTO despesas_fixas (descricao, categoria, valor, dia)
                     VALUES (%s,%s,%s,%s) RETURNING *""",
                  (d['descricao'].strip(), d.get('categoria'), d.get('valor') or 0, dia), returning=True)
    return jsonify({'ok': True, 'row': row})


@app.route('/api/despesas-fixas/<int:fid>', methods=['PUT'])
@login_required
def df_update(fid):
    d = request.get_json() or {}
    execute("""UPDATE despesas_fixas SET descricao=COALESCE(%s,descricao), categoria=%s,
               valor=COALESCE(%s,valor), dia=COALESCE(%s,dia) WHERE id=%s""",
            (d.get('descricao'), d.get('categoria'), d.get('valor'), d.get('dia'), fid))
    return jsonify({'ok': True})


@app.route('/api/despesas-fixas/<int:fid>', methods=['DELETE'])
@login_required
def df_delete(fid):
    execute("UPDATE despesas_fixas SET ativo=false WHERE id=%s", (fid,))
    return jsonify({'ok': True})


@app.route('/api/despesas-fixas/gerar', methods=['POST'])
@login_required
def df_gerar():
    """Gera as despesas fixas do mês (já pagas). Idempotente: pula as já geradas no mês."""
    d = request.get_json() or {}
    comp = d.get('competencia')
    hoje = date.today()
    try:
        ano, mes = (int(comp[:4]), int(comp[5:7])) if comp else (hoje.year, hoje.month)
    except (ValueError, IndexError):
        return jsonify({'ok': False, 'error': 'Competência inválida'}), 400
    import calendar
    ult = calendar.monthrange(ano, mes)[1]
    fixas = q_all("SELECT * FROM despesas_fixas WHERE ativo")
    gerados = 0
    for f in fixas:
        existe = scalar("""SELECT COUNT(*) FROM movimentos WHERE despesa_fixa_id=%s
                           AND date_trunc('month', data) = date_trunc('month', %s::date)""",
                        (f['id'], date(ano, mes, 1)))
        if existe:
            continue
        dia = min(int(f['dia'] or 5), ult)
        execute("""INSERT INTO movimentos (tipo, origem, descricao, categoria, valor, data, status, despesa_fixa_id, criado_por)
                   VALUES ('despesa','manual',%s,%s,%s,%s,'pago',%s,%s)""",
                (f['descricao'], f['categoria'], f['valor'], date(ano, mes, dia), f['id'], session['user_id']))
        gerados += 1
    return jsonify({'ok': True, 'gerados': gerados, 'competencia': f"{mes:02d}/{ano}"})


@app.route('/api/despesas/resumo')
@login_required
def despesas_resumo():
    de = parse_date(request.args.get('de'), date.today().replace(day=1))
    ate = parse_date(request.args.get('ate'), date.today())
    por_cat = q_all("""SELECT COALESCE(categoria,'Sem categoria') AS categoria, COALESCE(SUM(valor),0) AS total, COUNT(*) AS qtd
                       FROM movimentos WHERE tipo='despesa' AND status='pago' AND data BETWEEN %s AND %s
                       GROUP BY categoria ORDER BY total DESC""", (de, ate))
    total = scalar("SELECT COALESCE(SUM(valor),0) FROM movimentos WHERE tipo='despesa' AND status='pago' AND data BETWEEN %s AND %s", (de, ate)) or 0
    return jsonify({'ok': True, 'de': de.isoformat(), 'ate': ate.isoformat(),
                    'total': round(total, 2), 'por_categoria': por_cat})


@app.route('/api/caixa/fechamento')
@login_required
def caixa_fechamento():
    d = parse_date(request.args.get('data'), date.today())
    por_forma = q_all("""SELECT COALESCE(forma_pagamento,'—') AS forma, COALESCE(SUM(valor),0) AS total, COUNT(*) AS qtd
                         FROM movimentos WHERE tipo='receita' AND status='pago' AND data=%s
                         GROUP BY forma_pagamento ORDER BY total DESC""", (d,))
    total_receita = scalar("SELECT COALESCE(SUM(valor),0) FROM movimentos WHERE tipo='receita' AND status='pago' AND data=%s", (d,)) or 0
    total_despesa = scalar("SELECT COALESCE(SUM(valor),0) FROM movimentos WHERE tipo='despesa' AND status='pago' AND data=%s", (d,)) or 0
    atendimentos = scalar("SELECT COUNT(*) FROM comandas WHERE status='fechada' AND fechada_em::date=%s", (d,)) or 0

    # Taxa da maquininha do dia. Já está dentro de total_despesa (é despesa paga) — o que falta é
    # ela aparecer separada, senão o custo do cartão fica diluído no meio do aluguel.
    taxa_por_forma = {r['forma']: float(r['total']) for r in q_all(
        """SELECT COALESCE(forma_pagamento,'—') AS forma, COALESCE(SUM(valor),0) AS total
           FROM movimentos WHERE origem='taxa' AND status='pago' AND data=%s
           GROUP BY forma_pagamento""", (d,))}
    for f in por_forma:
        f['taxa'] = round(taxa_por_forma.get(f['forma'], 0), 2)
        f['liquido'] = round(float(f['total']) - f['taxa'], 2)
    total_taxas = round(sum(taxa_por_forma.values()), 2)

    return jsonify({'ok': True, 'data': d.isoformat(), 'por_forma': por_forma,
                    'total_receita': round(total_receita, 2), 'total_despesa': round(total_despesa, 2),
                    'total_taxas': total_taxas, 'liquido': round(total_receita - total_taxas, 2),
                    'saldo': round(total_receita - total_despesa, 2), 'atendimentos': atendimentos})


@app.route('/api/relatorios/taxas')
@login_required
def rel_taxas():
    """Custo da maquininha no período, por forma de pagamento: bruto, taxa e líquido.

    O % efetivo é calculado do realizado (taxa/bruto), não da configuração — se a taxa mudou no
    meio do período, o que vale é o que foi lançado na época, não o número de hoje.
    """
    de = parse_date(request.args.get('de'), date.today().replace(day=1))
    ate = parse_date(request.args.get('ate'), date.today())

    bruto = {r['forma']: r for r in q_all(
        """SELECT COALESCE(forma_pagamento,'—') AS forma, COALESCE(SUM(valor),0) AS total, COUNT(*) AS qtd
           FROM movimentos WHERE tipo='receita' AND status='pago' AND data BETWEEN %s AND %s
           GROUP BY forma_pagamento""", (de, ate))}
    taxas = {r['forma']: float(r['total']) for r in q_all(
        """SELECT COALESCE(forma_pagamento,'—') AS forma, COALESCE(SUM(valor),0) AS total
           FROM movimentos WHERE origem='taxa' AND status='pago' AND data BETWEEN %s AND %s
           GROUP BY forma_pagamento""", (de, ate))}
    configurada = {r['nome']: float(r['taxa_pct'] or 0) for r in q_all("SELECT nome, taxa_pct FROM formas_pagamento")}

    rows = []
    for forma in sorted(set(bruto) | set(taxas)):
        b = float(bruto.get(forma, {}).get('total') or 0)
        t = round(taxas.get(forma, 0), 2)
        rows.append({'forma': forma, 'bruto': round(b, 2), 'qtd': bruto.get(forma, {}).get('qtd') or 0,
                     'taxa': t, 'liquido': round(b - t, 2),
                     'taxa_pct_efetiva': round(t / b * 100, 2) if b else 0,
                     'taxa_pct_config': configurada.get(forma)})
    rows.sort(key=lambda x: x['taxa'], reverse=True)

    tot_bruto = round(sum(r['bruto'] for r in rows), 2)
    tot_taxa = round(sum(r['taxa'] for r in rows), 2)
    return jsonify({'ok': True, 'de': de.isoformat(), 'ate': ate.isoformat(), 'rows': rows,
                    'total_bruto': tot_bruto, 'total_taxa': tot_taxa,
                    'total_liquido': round(tot_bruto - tot_taxa, 2),
                    'taxa_pct_media': round(tot_taxa / tot_bruto * 100, 2) if tot_bruto else 0})


def calc_comissao(de, ate, profissional_id=None):
    """Motor único de comissão do período — usado pelo fechamento (dono) e pela tela do barbeiro.

    AVULSO (serviço não coberto por plano): comissao_pct do barbeiro sobre o que o cliente pagou,
    creditado a quem EXECUTOU o item (comanda_itens.profissional_id).

    ASSINANTE: a regra é do PLANO (planos.comissao_assinante_regra), porque cada barbearia vende
    o plano de um jeito. Um plano nunca cai em duas regras:
      - 'bolo'   → % (config.comissao_padrao) da arrecadação DOS PLANOS 'bolo' no período, rateado
                   entre os barbeiros pela produção. Atribuição por COMANDA (c.profissional_id),
                   como sempre foi. A fatia de quem não recebe comissão (dona) fica com a casa.
      - 'tabela' → comissao_pct do barbeiro sobre o PREÇO CHEIO do serviço coberto, direto pra quem
                   executou. O assinante paga R$0, mas o barbeiro ganha como se fosse avulso.
      - 'fixo'   → R$ comissao_assinante_valor por visita coberta, direto pra quem executou. Se dois
                   barbeiros atendem a mesma visita, cada um conta a sua (é atendimento de cada um).
      - 'zero'   → assinante não gera comissão.

    Planos apagados/órfãos caem em 'bolo' (COALESCE) para não sumir do cálculo antigo.
    """
    pool_pct = float(cfg().get('comissao_padrao') or 45)

    # Arrecadação: total (p/ exibir) e a parcela que alimenta o bolo (só planos com regra 'bolo')
    plan_revenue = scalar("""SELECT COALESCE(SUM(valor),0) FROM movimentos
        WHERE origem='assinatura' AND status='pago' AND data BETWEEN %s AND %s""", (de, ate)) or 0
    pool_revenue = scalar("""SELECT COALESCE(SUM(m.valor),0) FROM movimentos m
        LEFT JOIN assinaturas a ON a.id=m.ref_id
        LEFT JOIN planos p ON p.id=a.plano_id
        WHERE m.origem='assinatura' AND m.status='pago' AND m.data BETWEEN %s AND %s
          AND COALESCE(p.comissao_assinante_regra,'bolo')='bolo'""", (de, ate)) or 0
    pool = pool_revenue * pool_pct / 100.0

    avulso_rows = q_all("""
        SELECT ci.profissional_id, pr.nome AS prof_nome, pr.comissao_pct, pr.recebe_comissao,
               COALESCE(SUM(ci.subtotal),0) AS base
        FROM comanda_itens ci JOIN comandas c ON c.id=ci.comanda_id AND c.status='fechada'
        LEFT JOIN profissionais pr ON pr.id=ci.profissional_id
        WHERE ci.tipo='servico' AND ci.coberto_plano=false AND c.fechada_em::date BETWEEN %s AND %s
        GROUP BY ci.profissional_id, pr.nome, pr.comissao_pct, pr.recebe_comissao
    """, (de, ate))

    # Comissão DIRETA do assinante ('tabela' e 'fixo') — por executor do item
    direto_rows = q_all("""
        SELECT ci.profissional_id, pr.nome AS prof_nome, pr.comissao_pct, pr.recebe_comissao,
               COALESCE(p.comissao_assinante_regra,'bolo') AS regra,
               COALESCE(p.comissao_assinante_valor,0) AS valor_fixo,
               COALESCE(SUM(ci.preco_tabela * ci.qtd),0) AS base_tabela,
               COUNT(DISTINCT ci.comanda_id) AS visitas
        FROM comanda_itens ci JOIN comandas c ON c.id=ci.comanda_id AND c.status='fechada'
        LEFT JOIN profissionais pr ON pr.id=ci.profissional_id
        LEFT JOIN assinaturas a ON a.id=ci.assinatura_id
        LEFT JOIN planos p ON p.id=a.plano_id
        WHERE ci.coberto_plano AND c.fechada_em::date BETWEEN %s AND %s
          AND COALESCE(p.comissao_assinante_regra,'bolo') IN ('tabela','fixo')
        GROUP BY ci.profissional_id, pr.nome, pr.comissao_pct, pr.recebe_comissao,
                 p.comissao_assinante_regra, p.comissao_assinante_valor
    """, (de, ate))

    # Produção do BOLO = visitas cobertas por planos com regra 'bolo' (1 comanda = 1 atendimento)
    atend_rows = q_all("""
        SELECT c.profissional_id, pr.nome AS prof_nome, pr.recebe_comissao, COUNT(DISTINCT c.id) AS atend
        FROM comandas c
        LEFT JOIN profissionais pr ON pr.id=c.profissional_id
        WHERE c.status='fechada' AND c.fechada_em::date BETWEEN %s AND %s
          AND EXISTS (SELECT 1 FROM comanda_itens ci
                      LEFT JOIN assinaturas a ON a.id=ci.assinatura_id
                      LEFT JOIN planos p ON p.id=a.plano_id
                      WHERE ci.comanda_id=c.id AND ci.coberto_plano
                        AND COALESCE(p.comissao_assinante_regra,'bolo')='bolo')
        GROUP BY c.profissional_id, pr.nome, pr.recebe_comissao
    """, (de, ate))

    # total_atend inclui TODOS (até a dona) → a fração dela não é distribuída (fica com a casa)
    total_atend = sum(r['atend'] for r in atend_rows) or 0

    pagos = {r['profissional_id']: r for r in q_all(
        """SELECT profissional_id, valor, valor_calculado, ajuste_motivo FROM comissoes_pagas
           WHERE periodo_de=%s AND periodo_ate=%s""", (de, ate))}

    def slot(pid, nome):
        return barb.setdefault(pid, {'id': pid, 'profissional': nome, 'avulso': 0.0,
                                     'assinante_direto': 0.0, 'atend': 0})

    barb = {}  # só quem RECEBE comissão entra no resultado
    for r in avulso_rows:
        if r['profissional_id'] is None or not r['recebe_comissao']:
            continue
        slot(r['profissional_id'], r['prof_nome'])['avulso'] = \
            float(r['base']) * float(r['comissao_pct'] or 0) / 100.0
    for r in direto_rows:
        if r['profissional_id'] is None or not r['recebe_comissao']:
            continue
        b = slot(r['profissional_id'], r['prof_nome'])
        if r['regra'] == 'tabela':
            b['assinante_direto'] += float(r['base_tabela']) * float(r['comissao_pct'] or 0) / 100.0
        else:                                    # 'fixo': R$ por visita coberta
            b['assinante_direto'] += float(r['valor_fixo']) * int(r['visitas'])
    for r in atend_rows:
        if r['profissional_id'] is None or not r['recebe_comissao']:
            continue
        slot(r['profissional_id'], r['prof_nome'])['atend'] = r['atend']

    rows = []
    for b in barb.values():
        share = (b['atend'] / total_atend) if total_atend else 0
        pool_share = pool * share
        total = b['avulso'] + b['assinante_direto'] + pool_share
        fechado = pagos.get(b['id'])
        rows.append({'profissional_id': b['id'], 'profissional': b['profissional'],
                     'avulso': round(b['avulso'], 2), 'assinante_direto': round(b['assinante_direto'], 2),
                     'atend': b['atend'], 'participacao_pct': round(share * 100, 1),
                     'pool_share': round(pool_share, 2), 'total': round(total, 2),
                     'pago': fechado is not None,
                     'valor_pago': fechado['valor'] if fechado else None,
                     'ajuste_motivo': fechado.get('ajuste_motivo') if fechado else None})
    rows.sort(key=lambda x: x['total'], reverse=True)
    if profissional_id is not None:
        rows = [r for r in rows if r['profissional_id'] == profissional_id]

    distribuido = sum(r['pool_share'] for r in rows)
    return {'de': de.isoformat(), 'ate': ate.isoformat(),
            'plan_revenue': round(plan_revenue, 2), 'pool_revenue': round(pool_revenue, 2),
            'pool': round(pool, 2), 'pool_distribuido': round(distribuido, 2),
            'pool_retido': round(pool - distribuido, 2), 'pool_pct': pool_pct,
            'total_atend': total_atend, 'rows': rows,
            'total': round(sum(r['total'] for r in rows), 2)}


@app.route('/api/relatorios/comissao')
@login_required
def rel_comissao():
    """Comissão por barbeiro no período (avulso + regra do plano do assinante). Ver calc_comissao."""
    de = parse_date(request.args.get('de'), date.today().replace(day=1))
    ate = parse_date(request.args.get('ate'), date.today())
    return jsonify({'ok': True, **calc_comissao(de, ate)})


@app.route('/api/relatorios/minha-comissao')
@login_required
def rel_minha_comissao():
    """Comissão do barbeiro logado na quinzena atual (avulso + fatia do bolo dos planos)."""
    pid = session.get('profissional_id')
    if not pid:
        return jsonify({'ok': True, 'comissao': 0, 'atendimentos': 0})
    recebe = scalar("SELECT recebe_comissao FROM profissionais WHERE id=%s", (pid,))
    if not recebe:
        return jsonify({'ok': True, 'comissao': 0, 'avulso': 0, 'pool_share': 0, 'atendimentos': 0,
                        'sem_comissao': True})
    hoje = date.today()
    de = hoje.replace(day=1) if hoje.day <= 15 else hoje.replace(day=16)

    # Mesmo motor do fechamento do dono — o barbeiro vê exatamente o que vai receber.
    r = calc_comissao(de, hoje, profissional_id=pid)
    linha = r['rows'][0] if r['rows'] else {'avulso': 0, 'assinante_direto': 0, 'pool_share': 0,
                                            'total': 0, 'atend': 0}
    return jsonify({'ok': True, 'comissao': linha['total'], 'avulso': linha['avulso'],
                    'assinante_direto': linha['assinante_direto'], 'pool_share': linha['pool_share'],
                    'atendimentos': linha['atend'], 'periodo': f"{de.isoformat()} a {hoje.isoformat()}"})


@app.route('/api/comissoes/fechar', methods=['POST'])
@login_required
def comissao_fechar():
    """Fecha/paga a comissão de um barbeiro no período → vira despesa (sai do caixa) + registro pago."""
    d = request.get_json() or {}
    pid = d.get('profissional_id')
    de = parse_date(d.get('de'))
    ate = parse_date(d.get('ate'))
    try:
        valor = float(d.get('valor'))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'Valor inválido'}), 400
    if not (pid and de and ate):
        return jsonify({'ok': False, 'error': 'Dados incompletos'}), 400
    if scalar("SELECT COUNT(*) FROM comissoes_pagas WHERE profissional_id=%s AND periodo_de=%s AND periodo_ate=%s", (pid, de, ate)):
        return jsonify({'ok': False, 'error': 'Comissão deste período já foi fechada'}), 409

    # O calculado vem do motor, NUNCA do front — senão o "ajuste" não teria contra o que ser medido.
    calc = calc_comissao(de, ate, profissional_id=pid)
    calculado = calc['rows'][0]['total'] if calc['rows'] else 0.0
    motivo = (d.get('ajuste_motivo') or '').strip() or None
    if abs(valor - calculado) >= 0.01 and not motivo:
        return jsonify({'ok': False, 'error': f'Valor difere do calculado ({calculado:.2f}). '
                                              'Escreva o motivo do ajuste.'}), 400

    prof = q_one("SELECT nome FROM profissionais WHERE id=%s", (pid,))
    nome = prof['nome'] if prof else f"barbeiro {pid}"
    desc = f"Comissão {nome} ({de.strftime('%d/%m')}–{ate.strftime('%d/%m')})"
    if motivo:
        desc += " · ajustada"
    mov = execute("""INSERT INTO movimentos (tipo, origem, descricao, categoria, valor, data, status, criado_por)
                     VALUES ('despesa','manual',%s,'Comissões',%s,CURRENT_DATE,'pago',%s) RETURNING id""",
                  (desc, valor, session['user_id']), returning=True)
    execute("""INSERT INTO comissoes_pagas (profissional_id, periodo_de, periodo_ate, valor,
                   valor_calculado, ajuste_motivo, movimento_id, criado_por)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (pid, de, ate, valor, calculado, motivo, mov['id'], session['user_id']))
    return jsonify({'ok': True, 'valor_calculado': round(calculado, 2), 'ajustado': bool(motivo)})


def _dre_totais(de, ate):
    """Receita/despesa/resultado de um período (base caixa). Reuso no comparativo."""
    rec_serv = scalar("""SELECT COALESCE(SUM(ci.subtotal),0) FROM comanda_itens ci
        JOIN comandas c ON c.id=ci.comanda_id AND c.status='fechada'
        WHERE ci.tipo='servico' AND c.fechada_em::date BETWEEN %s AND %s""", (de, ate)) or 0
    rec_prod = scalar("""SELECT COALESCE(SUM(ci.subtotal),0) FROM comanda_itens ci
        JOIN comandas c ON c.id=ci.comanda_id AND c.status='fechada'
        WHERE ci.tipo='produto' AND c.fechada_em::date BETWEEN %s AND %s""", (de, ate)) or 0
    rec_assin = scalar("""SELECT COALESCE(SUM(valor),0) FROM movimentos
        WHERE origem='assinatura' AND status='pago' AND data BETWEEN %s AND %s""", (de, ate)) or 0
    rec_outras = scalar("""SELECT COALESCE(SUM(valor),0) FROM movimentos
        WHERE origem='manual' AND tipo='receita' AND status='pago' AND data BETWEEN %s AND %s""", (de, ate)) or 0
    total_desp = scalar("""SELECT COALESCE(SUM(valor),0) FROM movimentos
        WHERE tipo='despesa' AND status='pago' AND data BETWEEN %s AND %s""", (de, ate)) or 0
    total_rec = rec_serv + rec_prod + rec_assin + rec_outras
    return {'servicos': rec_serv, 'produtos': rec_prod, 'assinaturas': rec_assin, 'outras': rec_outras,
            'receita_total': total_rec, 'despesa_total': total_desp, 'resultado': total_rec - total_desp}


@app.route('/api/relatorios/dre')
@login_required
def rel_dre():
    """Resultado (DRE) do período — base caixa (status=pago / comandas fechadas).
    Inclui o detalhamento por serviço/produto, atendimentos, ticket médio e comparativo
    com o período anterior de mesmo tamanho."""
    de = parse_date(request.args.get('de'), date.today().replace(day=1))
    ate = parse_date(request.args.get('ate'), date.today())
    t = _dre_totais(de, ate)

    # Detalhamento (só itens PAGOS: coberto_plano=false → composição real do faturamento)
    serv_det = q_all("""SELECT ci.descricao AS nome, SUM(ci.qtd) AS qtd, COALESCE(SUM(ci.subtotal),0) AS total
        FROM comanda_itens ci JOIN comandas c ON c.id=ci.comanda_id AND c.status='fechada'
        WHERE ci.tipo='servico' AND ci.coberto_plano=false AND c.fechada_em::date BETWEEN %s AND %s
        GROUP BY ci.descricao ORDER BY total DESC""", (de, ate))
    prod_det = q_all("""SELECT ci.descricao AS nome, SUM(ci.qtd) AS qtd, COALESCE(SUM(ci.subtotal),0) AS total
        FROM comanda_itens ci JOIN comandas c ON c.id=ci.comanda_id AND c.status='fechada'
        WHERE ci.tipo='produto' AND c.fechada_em::date BETWEEN %s AND %s
        GROUP BY ci.descricao ORDER BY total DESC""", (de, ate))

    despesas = q_all("""SELECT COALESCE(categoria,'Sem categoria') AS categoria, COALESCE(SUM(valor),0) AS total
        FROM movimentos WHERE tipo='despesa' AND status='pago' AND data BETWEEN %s AND %s
        GROUP BY categoria ORDER BY total DESC""", (de, ate))

    # Atendimentos pagos (comandas fechadas com valor > 0) → ticket médio de serviços+produtos
    atend = scalar("""SELECT COUNT(*) FROM comandas WHERE status='fechada' AND valor_total>0
        AND fechada_em::date BETWEEN %s AND %s""", (de, ate)) or 0
    visitas_plano = scalar("""SELECT COUNT(DISTINCT c.id) FROM comandas c
        WHERE c.status='fechada' AND c.fechada_em::date BETWEEN %s AND %s
          AND EXISTS (SELECT 1 FROM comanda_itens ci WHERE ci.comanda_id=c.id AND ci.coberto_plano)""", (de, ate)) or 0
    ticket = round((t['servicos'] + t['produtos']) / atend, 2) if atend else 0

    # Comparativo: período anterior imediatamente antes, de mesmo tamanho
    dias = (ate - de).days
    prev_ate = de - timedelta(days=1)
    prev_de = prev_ate - timedelta(days=dias)
    p = _dre_totais(prev_de, prev_ate)

    return jsonify({'ok': True, 'de': de.isoformat(), 'ate': ate.isoformat(),
                    'receitas': {'servicos': round(t['servicos'], 2), 'produtos': round(t['produtos'], 2),
                                 'assinaturas': round(t['assinaturas'], 2), 'outras': round(t['outras'], 2),
                                 'total': round(t['receita_total'], 2),
                                 'servicos_detalhe': serv_det, 'produtos_detalhe': prod_det},
                    'despesas': despesas, 'despesas_total': round(t['despesa_total'], 2),
                    'resultado': round(t['resultado'], 2),
                    'atendimentos': atend, 'visitas_plano': visitas_plano, 'ticket_medio': ticket,
                    'comparativo': {'de': prev_de.isoformat(), 'ate': prev_ate.isoformat(),
                                    'receita': round(p['receita_total'], 2), 'despesa': round(p['despesa_total'], 2),
                                    'resultado': round(p['resultado'], 2)}})


_MES_ABR = ['', 'jan', 'fev', 'mar', 'abr', 'mai', 'jun', 'jul', 'ago', 'set', 'out', 'nov', 'dez']


@app.route('/api/relatorios/dre-serie')
@login_required
def rel_dre_serie():
    """Série mensal de receita/despesa/resultado dos últimos N meses (evolução no DRE)."""
    try:
        meses = min(max(int(request.args.get('meses') or 6), 1), 24)
    except (ValueError, TypeError):
        meses = 6
    ref = parse_date(request.args.get('ate'), date.today())
    lista = []
    y, m = ref.year, ref.month
    for _ in range(meses):
        lista.append((y, m))
        m -= 1
        if m == 0:
            m = 12; y -= 1
    lista.reverse()
    start = date(lista[0][0], lista[0][1], 1)
    end = date(ref.year, ref.month, calendar.monthrange(ref.year, ref.month)[1])

    rec_comanda = {r['ym']: float(r['total']) for r in q_all("""
        SELECT to_char(date_trunc('month', c.fechada_em),'YYYY-MM') AS ym, COALESCE(SUM(ci.subtotal),0) AS total
        FROM comanda_itens ci JOIN comandas c ON c.id=ci.comanda_id AND c.status='fechada'
        WHERE c.fechada_em::date BETWEEN %s AND %s GROUP BY 1""", (start, end))}
    rec_mov = {r['ym']: float(r['total']) for r in q_all("""
        SELECT to_char(date_trunc('month', data),'YYYY-MM') AS ym, COALESCE(SUM(valor),0) AS total
        FROM movimentos WHERE tipo='receita' AND status='pago' AND origem IN ('assinatura','manual')
          AND data BETWEEN %s AND %s GROUP BY 1""", (start, end))}
    desp = {r['ym']: float(r['total']) for r in q_all("""
        SELECT to_char(date_trunc('month', data),'YYYY-MM') AS ym, COALESCE(SUM(valor),0) AS total
        FROM movimentos WHERE tipo='despesa' AND status='pago' AND data BETWEEN %s AND %s GROUP BY 1""", (start, end))}

    serie = []
    for (yy, mm) in lista:
        ym = f"{yy}-{mm:02d}"
        r = rec_comanda.get(ym, 0) + rec_mov.get(ym, 0)
        dd = desp.get(ym, 0)
        serie.append({'competencia': ym, 'label': f"{_MES_ABR[mm]}/{str(yy)[2:]}",
                      'receita': round(r, 2), 'despesa': round(dd, 2), 'resultado': round(r - dd, 2)})
    return jsonify({'ok': True, 'serie': serie})


if __name__ == '__main__':
    porta = int(os.getenv('PORT', '5000'))
    if os.getenv('FLASK_ENV') == 'development' or os.getenv('DEV'):
        app.run(host='0.0.0.0', port=porta, debug=True)
    else:
        from waitress import serve
        print(f"[JOGA Barbearia] http://0.0.0.0:{porta}")
        serve(app, host='0.0.0.0', port=porta, threads=8)
