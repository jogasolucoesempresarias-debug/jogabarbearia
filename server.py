"""
JOGA Barbearia — Backend (Flask + Waitress)
Rode: python -X utf8 server.py   ·   Acesse: http://localhost:5000
"""
import os
import math
import calendar
import functools
from datetime import date, datetime, time

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


def rate_limit_ok(ip, maximo=5, janela_seg=600):
    import time
    agora = time.time()
    hist = [t for t in _RATE.get(ip, []) if agora - t < janela_seg]
    if len(hist) >= maximo:
        _RATE[ip] = hist
        return False
    hist.append(agora)
    _RATE[ip] = hist
    return True


def cobertura_plano(cliente_id, servico_id, d: date):
    """Retorna (coberto:bool, assinatura_id|None) p/ um serviço de um cliente numa data."""
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
    return jsonify({'ok': True, 'marca_nome': c.get('marca_nome') or 'Barbearia',
                    'marca_cor': c.get('marca_cor') or '#38bdf8'})


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


@app.route('/<path:rota>')
@app.route('/', endpoint='root')
@login_required
def pagina(rota=''):
    arquivo = PAGINAS.get('/' + rota if rota else '/')
    if not arquivo:
        return jsonify({'ok': False, 'error': 'Página não encontrada'}), 404
    return send_from_directory(BASE_DIR, arquivo)


# ── Auth API ──────────────────────────────────────────────────────────
@app.route('/api/login', methods=['POST'])
def login_post():
    import hmac
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
                    'marca': {'nome': c.get('marca_nome'), 'cor': c.get('marca_cor')}})


# ══════════════════════════════════════════════════════════════════════
# Cadastros / Config
# ══════════════════════════════════════════════════════════════════════
@app.route('/api/profissionais', methods=['GET'])
@login_required
def prof_list():
    return jsonify({'ok': True, 'rows': q_all("SELECT * FROM profissionais WHERE ativo ORDER BY nome")})


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
                  (d['nome'].strip(), d.get('comissao_pct') or 45, d.get('cor_agenda') or '#38bdf8', recebe), returning=True)
    return jsonify({'ok': True, 'row': row})


@app.route('/api/profissionais/<int:pid>', methods=['PUT'])
@login_required
@role_required('dono')
def prof_update(pid):
    d = request.get_json() or {}
    execute("""UPDATE profissionais SET nome=COALESCE(%s,nome), comissao_pct=COALESCE(%s,comissao_pct),
               cor_agenda=COALESCE(%s,cor_agenda), recebe_comissao=COALESCE(%s,recebe_comissao) WHERE id=%s""",
            (d.get('nome'), d.get('comissao_pct'), d.get('cor_agenda'), d.get('recebe_comissao'), pid))
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
    if busca:
        rows = q_all("""SELECT c.*, p.nome AS prof_nome FROM clientes c
                        LEFT JOIN profissionais p ON p.id=c.profissional_fixo_id
                        WHERE c.ativo AND c.status='aprovado' AND (c.nome ILIKE %s OR c.telefone ILIKE %s)
                        ORDER BY c.nome LIMIT 50""",
                     (f"%{busca}%", f"%{busca}%"))
    else:
        rows = q_all("""SELECT c.*, p.nome AS prof_nome FROM clientes c
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


@app.route('/api/planos', methods=['POST'])
@login_required
@role_required('dono')
def plano_create():
    d = request.get_json() or {}
    if not (d.get('nome') or '').strip():
        return jsonify({'ok': False, 'error': 'Nome obrigatório'}), 400
    row = execute("""INSERT INTO planos (nome, valor_mensal, limite_uso, dias_inclusos,
                     comissao_assinante_regra, comissao_assinante_valor)
                     VALUES (%s,%s,%s,%s,%s,%s) RETURNING *""",
                  (d['nome'].strip(), d.get('valor_mensal') or 0, d.get('limite_uso') or None,
                   Json(d.get('dias_inclusos') or [1, 2, 3]),
                   d.get('comissao_assinante_regra') or 'tabela', d.get('comissao_assinante_valor') or None),
                  returning=True)
    for sid in (d.get('servicos') or []):
        execute("INSERT INTO plano_servicos (plano_id, servico_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (row['id'], sid))
    return jsonify({'ok': True, 'row': row})


@app.route('/api/planos/<int:pid>', methods=['PUT'])
@login_required
@role_required('dono')
def plano_update(pid):
    d = request.get_json() or {}
    execute("""UPDATE planos SET nome=COALESCE(%s,nome), valor_mensal=COALESCE(%s,valor_mensal),
               limite_uso=%s, dias_inclusos=COALESCE(%s,dias_inclusos),
               comissao_assinante_regra=COALESCE(%s,comissao_assinante_regra),
               comissao_assinante_valor=%s WHERE id=%s""",
            (d.get('nome'), d.get('valor_mensal'), d.get('limite_uso') or None,
             Json(d['dias_inclusos']) if d.get('dias_inclusos') is not None else None,
             d.get('comissao_assinante_regra'), d.get('comissao_assinante_valor') or None, pid))
    if d.get('servicos') is not None:
        execute("DELETE FROM plano_servicos WHERE plano_id=%s", (pid,))
        for sid in d['servicos']:
            execute("INSERT INTO plano_servicos (plano_id, servico_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (pid, sid))
    return jsonify({'ok': True})


@app.route('/api/config', methods=['GET'])
@login_required
def config_get():
    return jsonify({'ok': True, 'config': cfg()})


@app.route('/api/config', methods=['PUT'])
@login_required
@role_required('dono')
def config_update():
    d = request.get_json() or {}
    execute("""UPDATE configuracoes SET
                 slot_min=COALESCE(%s,slot_min), comissao_padrao=COALESCE(%s,comissao_padrao),
                 formas_pagamento=COALESCE(%s,formas_pagamento), horarios=COALESCE(%s,horarios),
                 categorias_despesa=COALESCE(%s,categorias_despesa),
                 marca_nome=COALESCE(%s,marca_nome), marca_cor=COALESCE(%s,marca_cor), marca_logo_url=%s
               WHERE id=1""",
            (d.get('slot_min'), d.get('comissao_padrao'),
             Json(d['formas_pagamento']) if d.get('formas_pagamento') is not None else None,
             Json(d['horarios']) if d.get('horarios') is not None else None,
             Json(d['categorias_despesa']) if d.get('categorias_despesa') is not None else None,
             d.get('marca_nome'), d.get('marca_cor'), d.get('marca_logo_url')))
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
    conflito = scalar("""SELECT COUNT(*) FROM agendamentos WHERE profissional_id=%s AND data=%s
                         AND hora_inicio=%s AND status NOT IN ('cancelado')""", (prof, data, hora))
    if conflito:
        return jsonify({'ok': False, 'error': 'Já existe agendamento nesse horário'}), 409
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
    coberto, assin_id = cobertura_plano(com['cliente_id'], s['id'], date.today())
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
    return jsonify({'ok': True, 'comanda': com, 'itens': itens})


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
        execute("""INSERT INTO movimentos (tipo, origem, ref_id, descricao, valor, forma_pagamento, status, criado_por)
                   VALUES ('receita','comanda',%s,%s,%s,%s,'pago',%s)""",
                (cid, f"Comanda #{cid}", total, forma, session['user_id']))
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
        execute("""INSERT INTO movimentos (tipo, origem, ref_id, descricao, valor, forma_pagamento, data, status, criado_por)
                   VALUES ('receita','assinatura',%s,%s,%s,%s,CURRENT_DATE,'pago',%s)""",
                (row['id'], f"Assinatura {cli['nome'] if cli else ''} — 1ª mensalidade",
                 pl['valor_mensal'], d.get('forma_pagamento'), session['user_id']))
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
    execute("""INSERT INTO movimentos (tipo, origem, descricao, categoria, valor, forma_pagamento, data, status, criado_por)
               VALUES (%s,'manual',%s,%s,%s,%s,%s,'pago',%s)""",
            (d['tipo'], d['descricao'].strip(), d.get('categoria'), d.get('valor') or 0, d.get('forma_pagamento'),
             parse_date(d.get('data'), date.today()), session['user_id']))
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
    execute("DELETE FROM comissoes_pagas WHERE movimento_id=%s", (mid,))
    execute("DELETE FROM movimentos WHERE id=%s", (mid,))
    return jsonify({'ok': True})


@app.route('/api/movimentos/<int:mid>/pagar', methods=['POST'])
@login_required
def mov_pagar(mid):
    d = request.get_json() or {}
    execute("UPDATE movimentos SET status='pago', forma_pagamento=COALESCE(%s,forma_pagamento), data=CURRENT_DATE WHERE id=%s",
            (d.get('forma_pagamento'), mid))
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
    return jsonify({'ok': True, 'data': d.isoformat(), 'por_forma': por_forma,
                    'total_receita': round(total_receita, 2), 'total_despesa': round(total_despesa, 2),
                    'saldo': round(total_receita - total_despesa, 2), 'atendimentos': atendimentos})


@app.route('/api/relatorios/comissao')
@login_required
def rel_comissao():
    """Comissão por barbeiro no período:
       - AVULSO: 45% (comissao_pct do barbeiro) sobre o valor dos serviços não cobertos.
       - ASSINANTE (POOL): bolo = 45% (config.comissao_padrao) da arrecadação dos planos paga no
         período, rateado entre os barbeiros pela fatia de atendimentos cobertos de cada um."""
    de = parse_date(request.args.get('de'), date.today().replace(day=1))
    ate = parse_date(request.args.get('ate'), date.today())

    pool_pct = float(cfg().get('comissao_padrao') or 45)
    plan_revenue = scalar("""SELECT COALESCE(SUM(valor),0) FROM movimentos
        WHERE origem='assinatura' AND status='pago' AND data BETWEEN %s AND %s""", (de, ate)) or 0
    pool = plan_revenue * pool_pct / 100.0

    avulso_rows = q_all("""
        SELECT ci.profissional_id, pr.nome AS prof_nome, pr.comissao_pct, pr.recebe_comissao,
               COALESCE(SUM(ci.subtotal),0) AS base
        FROM comanda_itens ci JOIN comandas c ON c.id=ci.comanda_id AND c.status='fechada'
        LEFT JOIN profissionais pr ON pr.id=ci.profissional_id
        WHERE ci.tipo='servico' AND ci.coberto_plano=false AND c.fechada_em::date BETWEEN %s AND %s
        GROUP BY ci.profissional_id, pr.nome, pr.comissao_pct, pr.recebe_comissao
    """, (de, ate))
    # Produção = VISITAS de assinante (cada comanda com >=1 serviço coberto = 1 atendimento)
    atend_rows = q_all("""
        SELECT c.profissional_id, pr.nome AS prof_nome, pr.recebe_comissao, COUNT(DISTINCT c.id) AS atend
        FROM comandas c
        LEFT JOIN profissionais pr ON pr.id=c.profissional_id
        WHERE c.status='fechada' AND c.fechada_em::date BETWEEN %s AND %s
          AND EXISTS (SELECT 1 FROM comanda_itens ci WHERE ci.comanda_id=c.id AND ci.coberto_plano=true)
        GROUP BY c.profissional_id, pr.nome, pr.recebe_comissao
    """, (de, ate))

    # total_atend inclui TODOS (até a dona) → a fração dela não é distribuída (fica com a casa)
    total_atend = sum(r['atend'] for r in atend_rows) or 0

    # já pagos (fechamentos) deste período exato
    pagos = {r['profissional_id'] for r in q_all(
        "SELECT profissional_id FROM comissoes_pagas WHERE periodo_de=%s AND periodo_ate=%s", (de, ate))}

    barb = {}  # só quem RECEBE comissão entra no resultado
    for r in avulso_rows:
        if r['profissional_id'] is None or not r['recebe_comissao']:
            continue
        b = barb.setdefault(r['profissional_id'], {'id': r['profissional_id'], 'profissional': r['prof_nome'], 'avulso': 0.0, 'atend': 0})
        b['avulso'] = float(r['base']) * float(r['comissao_pct'] or 0) / 100.0
    for r in atend_rows:
        if r['profissional_id'] is None or not r['recebe_comissao']:
            continue
        b = barb.setdefault(r['profissional_id'], {'id': r['profissional_id'], 'profissional': r['prof_nome'], 'avulso': 0.0, 'atend': 0})
        b['atend'] = r['atend']

    rows = []
    for b in barb.values():
        share = (b['atend'] / total_atend) if total_atend else 0
        pool_share = pool * share
        rows.append({'profissional_id': b['id'], 'profissional': b['profissional'], 'avulso': round(b['avulso'], 2),
                     'atend': b['atend'], 'participacao_pct': round(share * 100, 1),
                     'pool_share': round(pool_share, 2), 'total': round(b['avulso'] + pool_share, 2),
                     'pago': b['id'] in pagos})
    rows.sort(key=lambda x: x['total'], reverse=True)
    return jsonify({'ok': True, 'de': de.isoformat(), 'ate': ate.isoformat(),
                    'plan_revenue': round(plan_revenue, 2), 'pool': round(pool, 2),
                    'pool_pct': pool_pct, 'total_atend': total_atend, 'rows': rows,
                    'total': round(sum(r['total'] for r in rows), 2)})


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

    pct = float(scalar("SELECT comissao_pct FROM profissionais WHERE id=%s", (pid,)) or 0)
    avulso_base = scalar("""SELECT COALESCE(SUM(ci.subtotal),0) FROM comanda_itens ci
        JOIN comandas c ON c.id=ci.comanda_id AND c.status='fechada'
        WHERE ci.tipo='servico' AND ci.coberto_plano=false AND ci.profissional_id=%s
          AND c.fechada_em::date BETWEEN %s AND %s""", (pid, de, hoje)) or 0
    avulso = avulso_base * pct / 100.0

    pool_pct = float(cfg().get('comissao_padrao') or 45)
    plan_revenue = scalar("""SELECT COALESCE(SUM(valor),0) FROM movimentos
        WHERE origem='assinatura' AND status='pago' AND data BETWEEN %s AND %s""", (de, hoje)) or 0
    pool = plan_revenue * pool_pct / 100.0
    # Produção por VISITA (comanda com >=1 serviço coberto)
    meu_atend = scalar("""SELECT COUNT(DISTINCT c.id) FROM comandas c
        WHERE c.status='fechada' AND c.profissional_id=%s AND c.fechada_em::date BETWEEN %s AND %s
          AND EXISTS (SELECT 1 FROM comanda_itens ci WHERE ci.comanda_id=c.id AND ci.coberto_plano=true)""",
        (pid, de, hoje)) or 0
    total_atend = scalar("""SELECT COUNT(DISTINCT c.id) FROM comandas c
        WHERE c.status='fechada' AND c.fechada_em::date BETWEEN %s AND %s
          AND EXISTS (SELECT 1 FROM comanda_itens ci WHERE ci.comanda_id=c.id AND ci.coberto_plano=true)""",
        (de, hoje)) or 0
    pool_share = pool * (meu_atend / total_atend) if total_atend else 0

    return jsonify({'ok': True, 'comissao': round(avulso + pool_share, 2),
                    'avulso': round(avulso, 2), 'pool_share': round(pool_share, 2),
                    'atendimentos': meu_atend, 'periodo': f"{de.isoformat()} a {hoje.isoformat()}"})


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
    prof = q_one("SELECT nome FROM profissionais WHERE id=%s", (pid,))
    nome = prof['nome'] if prof else f"barbeiro {pid}"
    mov = execute("""INSERT INTO movimentos (tipo, origem, descricao, categoria, valor, data, status, criado_por)
                     VALUES ('despesa','manual',%s,'Comissões',%s,CURRENT_DATE,'pago',%s) RETURNING id""",
                  (f"Comissão {nome} ({de.strftime('%d/%m')}–{ate.strftime('%d/%m')})", valor, session['user_id']), returning=True)
    execute("""INSERT INTO comissoes_pagas (profissional_id, periodo_de, periodo_ate, valor, movimento_id, criado_por)
               VALUES (%s,%s,%s,%s,%s,%s)""", (pid, de, ate, valor, mov['id'], session['user_id']))
    return jsonify({'ok': True})


@app.route('/api/relatorios/dre')
@login_required
def rel_dre():
    """Resultado (DRE) do período — base caixa (status=pago / comandas fechadas)."""
    de = parse_date(request.args.get('de'), date.today().replace(day=1))
    ate = parse_date(request.args.get('ate'), date.today())

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
    total_rec = rec_serv + rec_prod + rec_assin + rec_outras

    despesas = q_all("""SELECT COALESCE(categoria,'Sem categoria') AS categoria, COALESCE(SUM(valor),0) AS total
        FROM movimentos WHERE tipo='despesa' AND status='pago' AND data BETWEEN %s AND %s
        GROUP BY categoria ORDER BY total DESC""", (de, ate))
    total_desp = sum(float(x['total']) for x in despesas)

    return jsonify({'ok': True, 'de': de.isoformat(), 'ate': ate.isoformat(),
                    'receitas': {'servicos': round(rec_serv, 2), 'produtos': round(rec_prod, 2),
                                 'assinaturas': round(rec_assin, 2), 'outras': round(rec_outras, 2),
                                 'total': round(total_rec, 2)},
                    'despesas': despesas, 'despesas_total': round(total_desp, 2),
                    'resultado': round(total_rec - total_desp, 2)})


if __name__ == '__main__':
    porta = int(os.getenv('PORT', '5000'))
    if os.getenv('FLASK_ENV') == 'development' or os.getenv('DEV'):
        app.run(host='0.0.0.0', port=porta, debug=True)
    else:
        from waitress import serve
        print(f"[JOGA Barbearia] http://0.0.0.0:{porta}")
        serve(app, host='0.0.0.0', port=porta, threads=8)
