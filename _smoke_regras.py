"""Valida a REGRA DE COMISSÃO POR PLANO (bolo / tabela / fixo / zero) e o ajuste manual
no fechamento. Monta o cenário direto no banco (não depende do dia da semana p/ cobertura)
e confere os valores contra o esperado. Limpa todo o resíduo no fim.

Rode com o server de pé na 5002: python -X utf8 _smoke_regras.py
"""
import os, json, urllib.request, urllib.error, http.cookiejar
from datetime import date
import psycopg2
from dotenv import load_dotenv

load_dotenv()
BASE = 'http://localhost:5002'
MARCA = '[SR]'                     # marcador do resíduo deste smoke
cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
hoje = date.today()
falhas = []


def call(m, p, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + p, data=data, method=m)
    req.add_header('Content-Type', 'application/json')
    try:
        return json.loads(op.open(req, timeout=10).read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode())


def db():
    return psycopg2.connect(host=os.getenv('DB_HOST'), port=os.getenv('DB_PORT'),
                            dbname=os.getenv('DB_NAME'), user=os.getenv('DB_USER'),
                            password=os.getenv('DB_PASSWORD'))


def confere(rotulo, obtido, esperado, tol=0.01):
    ok = abs(float(obtido) - float(esperado)) <= tol
    print(f"  {'[OK]' if ok else '[FALHA]'} {rotulo}: {obtido} (esperado {esperado})")
    if not ok:
        falhas.append(rotulo)


def limpar(cur):
    cur.execute(f"DELETE FROM comanda_itens WHERE descricao LIKE '{MARCA}%%'")
    cur.execute(f"DELETE FROM comandas WHERE valor_total = 888888")
    cur.execute(f"DELETE FROM comissoes_pagas WHERE profissional_id IN "
                f"(SELECT id FROM profissionais WHERE nome LIKE '{MARCA}%%')")
    cur.execute(f"DELETE FROM movimentos WHERE descricao LIKE '{MARCA}%%'")
    cur.execute(f"DELETE FROM assinaturas WHERE cliente_id IN "
                f"(SELECT id FROM clientes WHERE nome LIKE '{MARCA}%%')")
    cur.execute(f"DELETE FROM clientes WHERE nome LIKE '{MARCA}%%'")
    cur.execute(f"DELETE FROM plano_servicos WHERE plano_id IN "
                f"(SELECT id FROM planos WHERE nome LIKE '{MARCA}%%')")
    cur.execute(f"DELETE FROM planos WHERE nome LIKE '{MARCA}%%'")
    cur.execute(f"DELETE FROM profissionais WHERE nome LIKE '{MARCA}%%'")


# ── Cenário ───────────────────────────────────────────────────────────
c = db(); cur = c.cursor()
limpar(cur)
cur.execute("UPDATE usuarios SET must_change_password=false WHERE email='regiane@barbearia.local'")

# 2 barbeiros que RECEBEM comissão, 45% cada (isola do seed: a dona não recebe)
cur.execute(f"INSERT INTO profissionais (nome, comissao_pct, recebe_comissao) VALUES ('{MARCA} A',45,true) RETURNING id")
A = cur.fetchone()[0]
cur.execute(f"INSERT INTO profissionais (nome, comissao_pct, recebe_comissao) VALUES ('{MARCA} B',45,true) RETURNING id")
B = cur.fetchone()[0]

planos = {}
for chave, regra, valor_fixo, mensal in [('bolo', 'bolo', None, 100), ('tabela', 'tabela', None, 200),
                                         ('fixo', 'fixo', 15, 150), ('zero', 'zero', None, 100)]:
    cur.execute("""INSERT INTO planos (nome, valor_mensal, comissao_assinante_regra, comissao_assinante_valor)
                   VALUES (%s,%s,%s,%s) RETURNING id""",
                (f"{MARCA} {chave}", mensal, regra, valor_fixo))
    planos[chave] = cur.fetchone()[0]

# Um assinante por plano + a mensalidade PAGA no período (é ela que forma o bolo)
assin = {}
arrecad = {'bolo': 1000, 'tabela': 200, 'fixo': 150, 'zero': 100}
for chave, pid in planos.items():
    cur.execute(f"INSERT INTO clientes (nome) VALUES ('{MARCA} cli {chave}') RETURNING id")
    cli = cur.fetchone()[0]
    cur.execute("INSERT INTO assinaturas (cliente_id, plano_id) VALUES (%s,%s) RETURNING id", (cli, pid))
    assin[chave] = cur.fetchone()[0]
    cur.execute("""INSERT INTO movimentos (tipo, origem, ref_id, descricao, valor, data, status)
                   VALUES ('receita','assinatura',%s,%s,%s,%s,'pago')""",
                (assin[chave], f"{MARCA} mensalidade {chave}", arrecad[chave], hoje))


def visita(prof):
    cur.execute("""INSERT INTO comandas (profissional_id, status, valor_total, fechada_em)
                   VALUES (%s,'fechada',888888,NOW()) RETURNING id""", (prof,))
    return cur.fetchone()[0]


def coberto(com, prof, chave, preco_tabela=36):
    cur.execute("""INSERT INTO comanda_itens (comanda_id, tipo, descricao, profissional_id,
                       preco_unit, qtd, subtotal, preco_tabela, coberto_plano, assinatura_id)
                   VALUES (%s,'servico',%s,%s,0,1,0,%s,true,%s)""",
                (com, f"{MARCA} coberto {chave}", prof, preco_tabela, assin[chave]))


def avulso(com, prof, valor):
    cur.execute("""INSERT INTO comanda_itens (comanda_id, tipo, descricao, profissional_id,
                       preco_unit, qtd, subtotal, coberto_plano)
                   VALUES (%s,'servico',%s,%s,%s,1,%s,false)""",
                (com, f"{MARCA} avulso", prof, valor, valor))


# Barbeiro A: 2 visitas do plano BOLO, 1 do TABELA (serviço de 36), 1 do FIXO, 1 avulso de 100
v = visita(A); coberto(v, A, 'bolo')
v = visita(A); coberto(v, A, 'bolo')
v = visita(A); coberto(v, A, 'tabela', 36)
v = visita(A); coberto(v, A, 'fixo')
v = visita(A); avulso(v, A, 100)
# Barbeiro B: 1 visita do BOLO e 1 do ZERO (a do zero não pode virar dinheiro nenhum)
v = visita(B); coberto(v, B, 'bolo')
v = visita(B); coberto(v, B, 'zero')
c.commit(); cur.close(); c.close()

# ── Conferência ───────────────────────────────────────────────────────
print("== Login ==")
call('POST', '/api/login', {'email': 'regiane@barbearia.local', 'senha': 'joga123'})

print("== Relatório de comissão ==")
j = call('GET', f'/api/relatorios/comissao?de={hoje.isoformat()}&ate={hoje.isoformat()}')
confere('arrecadação total dos planos', j['plan_revenue'], 1450)      # 1000+200+150+100
confere("arrecadação que forma o bolo (só planos 'bolo')", j['pool_revenue'], 1000)
confere('bolo (45% de 1000)', j['pool'], 450)
confere("visitas que entram no rateio (só do plano 'bolo')", j['total_atend'], 3)

linhas = {r['profissional']: r for r in j['rows']}
a, b = linhas.get(f'{MARCA} A', {}), linhas.get(f'{MARCA} B', {})
print(f"  A: {a}")
confere('A · avulso (100 × 45%)', a.get('avulso', 0), 45)
confere('A · assinante direto (tabela 36×45% + fixo 15)', a.get('assinante_direto', 0), 31.20)
confere('A · bolo (2 de 3 visitas)', a.get('pool_share', 0), 300)
confere('A · total', a.get('total', 0), 376.20)
print(f"  B: {b}")
confere("B · assinante direto (plano 'zero' não paga nada)", b.get('assinante_direto', 0), 0)
confere('B · bolo (1 de 3 visitas)', b.get('pool_share', 0), 150)
confere('B · total', b.get('total', 0), 150)
confere('bolo distribuído (ninguém sem comissão no cenário)', j['pool_distribuido'], 450)
confere('bolo retido pela casa', j['pool_retido'], 0)

print("== Ajuste manual no fechamento ==")
r = call('POST', '/api/comissoes/fechar', {'profissional_id': a.get('profissional_id'),
                                           'de': hoje.isoformat(), 'ate': hoje.isoformat(), 'valor': 300})
print(f"  sem motivo → recusado? {'[OK]' if not r.get('ok') else '[FALHA]'} ({r.get('error')})")
if r.get('ok'):
    falhas.append('ajuste sem motivo deveria ser recusado')

r = call('POST', '/api/comissoes/fechar', {'profissional_id': a.get('profissional_id'),
                                           'de': hoje.isoformat(), 'ate': hoje.isoformat(),
                                           'valor': 300, 'ajuste_motivo': 'vale adiantado'})
print(f"  com motivo → aceito? {'[OK]' if r.get('ok') else '[FALHA]'}")
if not r.get('ok'):
    falhas.append('ajuste com motivo deveria passar')
else:
    confere('valor_calculado guardado no fechamento', r['valor_calculado'], 376.20)

c = db(); cur = c.cursor()
cur.execute("SELECT valor, valor_calculado, ajuste_motivo FROM comissoes_pagas WHERE profissional_id=%s", (A,))
linha = cur.fetchone()
print(f"  comissoes_pagas: pago={linha[0]} calculado={linha[1]} motivo={linha[2]!r}")
cur.execute("SELECT valor, descricao FROM movimentos WHERE tipo='despesa' AND categoria='Comissões' "
            "AND descricao LIKE %s", (f"%{MARCA} A%",))
desp = cur.fetchone()
print(f"  despesa no caixa: {desp}")
if not desp or float(desp[0]) != 300:
    falhas.append('despesa da comissão deveria sair pelo valor ajustado (300)')

print("== Limpeza ==")
limpar(cur)
cur.execute("UPDATE usuarios SET must_change_password=true")
c.commit(); cur.close(); c.close()
print("  [OK] limpo.")

print()
print(f"RESULTADO: {'TUDO VERDE' if not falhas else str(len(falhas)) + ' FALHA(S): ' + '; '.join(falhas)}")
raise SystemExit(1 if falhas else 0)
