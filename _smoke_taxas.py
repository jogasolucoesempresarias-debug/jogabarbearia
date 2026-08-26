"""Smoke das taxas de maquininha: cadastro com taxa, os 4 pontos onde a taxa nasce,
os 3 caminhos de desfazer, o líquido no caixa do dia e o relatório do período.

Regra que este smoke protege: a RECEITA CONTINUA BRUTA. A taxa é despesa amarrada ao movimento
de receita (origem='taxa', ref_id=id da receita). Se alguém um dia abater a taxa da receita,
a comissão do barbeiro muda sozinha conforme o cliente paga — e este teste cai.
"""
import os, json, urllib.request, urllib.error, http.cookiejar
from datetime import date
import psycopg2
from dotenv import load_dotenv

load_dotenv()
BASE = 'http://localhost:5002'
cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
hoje = date.today()
HOJE = hoje.isoformat()
MES_INI = hoje.replace(day=1).isoformat()

OK = 0; FAIL = 0
def check(label, cond, extra=''):
    global OK, FAIL
    print(f"  [{'PASS' if cond else 'FALHA'}] {label}" + (f"  → {extra}" if extra else '')); OK += bool(cond); FAIL += (not cond)

def call(m, p, b=None):
    data = json.dumps(b).encode() if b is not None else None
    req = urllib.request.Request(BASE + p, data=data, method=m); req.add_header('Content-Type', 'application/json')
    try: r = op.open(req, timeout=15); return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try: return json.loads(e.read().decode())
        except Exception: return {'ok': False, 'status': e.code}

def db():
    return psycopg2.connect(host=os.getenv('DB_HOST'), port=os.getenv('DB_PORT'), dbname=os.getenv('DB_NAME'),
        user=os.getenv('DB_USER'), password=os.getenv('DB_PASSWORD'))

def limpar():
    c = db(); cur = c.cursor()
    cur.execute("""DELETE FROM movimentos WHERE ref_id IN (
                     SELECT id FROM movimentos WHERE descricao LIKE '[ST]%%') AND origem='taxa'""")
    cur.execute("DELETE FROM movimentos WHERE descricao LIKE '[ST]%%'")
    cur.execute("""DELETE FROM movimentos WHERE origem='taxa' AND ref_id IN (
                     SELECT id FROM movimentos WHERE origem='comanda' AND ref_id IN (
                       SELECT id FROM comandas WHERE cliente_id IN (SELECT id FROM clientes WHERE nome LIKE '[ST]%%')))""")
    cur.execute("""DELETE FROM movimentos WHERE origem='comanda' AND ref_id IN (
                     SELECT id FROM comandas WHERE cliente_id IN (SELECT id FROM clientes WHERE nome LIKE '[ST]%%'))""")
    cur.execute("""DELETE FROM movimentos WHERE origem IN ('assinatura','taxa') AND ref_id IN (
                     SELECT id FROM assinaturas WHERE cliente_id IN (SELECT id FROM clientes WHERE nome LIKE '[ST]%%'))""")
    cur.execute("""DELETE FROM comanda_itens WHERE comanda_id IN (
                     SELECT id FROM comandas WHERE cliente_id IN (SELECT id FROM clientes WHERE nome LIKE '[ST]%%'))""")
    cur.execute("DELETE FROM comandas WHERE cliente_id IN (SELECT id FROM clientes WHERE nome LIKE '[ST]%%')")
    cur.execute("DELETE FROM assinaturas WHERE cliente_id IN (SELECT id FROM clientes WHERE nome LIKE '[ST]%%')")
    cur.execute("DELETE FROM clientes WHERE nome LIKE '[ST]%%'")
    c.commit(); cur.close(); c.close()

c = db(); cur = c.cursor()
cur.execute("UPDATE usuarios SET must_change_password=false WHERE email='regiane@barbearia.local'")
c.commit(); cur.close(); c.close()
limpar()

call('POST', '/api/login', {'email': 'regiane@barbearia.local', 'senha': 'joga123'})

# ── Cadastro das formas com taxa ──────────────────────────────────────
print("== Formas de pagamento com taxa ==")
formas = {f['nome']: f for f in call('GET', '/api/formas-pagamento')['rows']}
check('Débito e Crédito existem após a migração', 'Débito' in formas and 'Crédito' in formas, list(formas))
call('PUT', f"/api/formas-pagamento/{formas['Débito']['id']}", {'taxa_pct': 1.5})
call('PUT', f"/api/formas-pagamento/{formas['Crédito']['id']}", {'taxa_pct': 3.2})
formas = {f['nome']: f for f in call('GET', '/api/formas-pagamento')['rows']}
check('taxa do Débito gravada (1,5%)', abs(formas['Débito']['taxa_pct'] - 1.5) < 0.001, formas['Débito']['taxa_pct'])
check('taxa do Crédito gravada (3,2%)', abs(formas['Crédito']['taxa_pct'] - 3.2) < 0.001, formas['Crédito']['taxa_pct'])

print("== Contrato: /api/config segue devolvendo array de NOMES ==")
cfg = call('GET', '/api/config')['config']
fp = cfg.get('formas_pagamento')
check('formas_pagamento é lista de strings', isinstance(fp, list) and all(isinstance(x, str) for x in fp), fp)
check('inclui Crédito', 'Crédito' in fp)

# ── Comanda no Crédito ────────────────────────────────────────────────
print("== Comanda de R$ 100 no Crédito → taxa de R$ 3,20 ==")
prof = call('GET', '/api/profissionais')['rows'][0]
serv = call('POST', '/api/servicos', {'nome': '[ST] Corte teste', 'preco': 100, 'duracao_min': 30})['row']
cli = call('POST', '/api/clientes', {'nome': '[ST] CLIENTE TAXA', 'telefone': '(34) 91111-1111'})['row']

def nova_comanda(forma, valor_serv_id):
    j = call('POST', '/api/comandas', {'profissional_id': prof['id'], 'cliente_id': cli['id']})
    call('POST', f"/api/comandas/{j['id']}/itens", {'tipo': 'servico', 'ref_id': valor_serv_id, 'profissional_id': prof['id']})
    call('POST', f"/api/comandas/{j['id']}/fechar", {'forma_pagamento': forma})
    return j['id']

def movs(tipo=None):
    p = f'/api/movimentos?de={HOJE}&ate={HOJE}' + (f'&tipo={tipo}' if tipo else '')
    return call('GET', p)['rows']

com_cred = nova_comanda('Crédito', serv['id'])
taxas = [m for m in movs('despesa') if m['origem'] == 'taxa']
check('gerou 1 lançamento de taxa', len(taxas) == 1, f"{len(taxas)} lançamento(s)")
check('valor da taxa = 3,20', taxas and abs(taxas[0]['valor'] - 3.20) < 0.01, taxas[0]['valor'] if taxas else None)
check('categoria = Taxas de cartão', taxas and taxas[0]['categoria'] == 'Taxas de cartão', taxas[0]['categoria'] if taxas else None)
rec = [m for m in movs('receita') if m['origem'] == 'comanda']
check('RECEITA CONTINUA BRUTA (100, não 96,80)', rec and abs(rec[0]['valor'] - 100) < 0.01, rec[0]['valor'] if rec else None)
check('taxa aponta pro movimento de receita (ref_id)', taxas and rec and taxas[0]['ref_id'] == rec[0]['id'])

print("== Comanda no Dinheiro → nenhuma taxa ==")
com_din = nova_comanda('Dinheiro', serv['id'])
taxas = [m for m in movs('despesa') if m['origem'] == 'taxa']
check('continua com 1 taxa só (Dinheiro não gera)', len(taxas) == 1, f"{len(taxas)} lançamento(s)")

# ── Caixa do dia ──────────────────────────────────────────────────────
print("== Caixa do dia: bruto, taxas e líquido ==")
fx = call('GET', f'/api/caixa/fechamento?data={HOJE}')
check('total_taxas = 3,20', abs(fx['total_taxas'] - 3.20) < 0.01, fx['total_taxas'])
check('líquido = receita - taxas', abs(fx['liquido'] - (fx['total_receita'] - fx['total_taxas'])) < 0.01,
      f"{fx['liquido']} vs {fx['total_receita']}-{fx['total_taxas']}")
linha_cred = next((f for f in fx['por_forma'] if f['forma'] == 'Crédito'), None)
check('linha do Crédito traz taxa e líquido', linha_cred and abs(linha_cred['taxa'] - 3.20) < 0.01, linha_cred)

# ── Desfazer: cancelar a comanda ──────────────────────────────────────
print("== Cancelar a comanda leva a taxa junto (nada de despesa órfã) ==")
call('POST', f'/api/comandas/{com_cred}/cancelar')
taxas = [m for m in movs('despesa') if m['origem'] == 'taxa']
check('taxa sumiu com o cancelamento', len(taxas) == 0, f"{len(taxas)} sobrando")

# ── Desfazer: excluir o lançamento do caixa ───────────────────────────
print("== Excluir a receita pelo caixa leva a taxa junto ==")
com2 = nova_comanda('Crédito', serv['id'])
rec2 = next(m for m in movs('receita') if m['origem'] == 'comanda' and m['ref_id'] == com2)
check('taxa criada de novo', len([m for m in movs('despesa') if m['origem'] == 'taxa']) == 1)
call('DELETE', f"/api/movimentos/{rec2['id']}")
check('taxa sumiu junto com a receita excluída', len([m for m in movs('despesa') if m['origem'] == 'taxa']) == 0)

# ── Assinatura no Crédito ─────────────────────────────────────────────
print("== 1ª mensalidade no Crédito também paga taxa ==")
planos = call('GET', '/api/planos')['rows']
plano = planos[0]
assin = call('POST', '/api/assinaturas', {'cliente_id': cli['id'], 'plano_id': plano['id'],
                                          'forma_pagamento': 'Crédito', 'dia_vencimento': 10})
esperado = round(float(plano['valor_mensal']) * 3.2 / 100, 2)
taxas = [m for m in movs('despesa') if m['origem'] == 'taxa']
check('mensalidade gerou taxa', len(taxas) == 1, f"{len(taxas)} lançamento(s)")
check(f'valor bate ({esperado})', taxas and abs(taxas[0]['valor'] - esperado) < 0.01, taxas[0]['valor'] if taxas else None)

print("== Cancelar a assinatura leva a taxa dela junto ==")
call('PUT', f"/api/assinaturas/{assin['id']}", {'status': 'cancelada'})
check('taxa da assinatura sumiu', len([m for m in movs('despesa') if m['origem'] == 'taxa']) == 0)

# ── mov_pagar: o previsto que vira pago ───────────────────────────────
print("== Cobrança 'previsto' marcada como paga no Débito gera taxa ==")
c = db(); cur = c.cursor()
cur.execute("""INSERT INTO movimentos (tipo, origem, descricao, valor, data, vencimento, status)
               VALUES ('receita','manual','[ST] Mensalidade prevista',200,%s,%s,'previsto') RETURNING id""", (hoje, hoje))
prev_id = cur.fetchone()[0]; c.commit(); cur.close(); c.close()
call('POST', f'/api/movimentos/{prev_id}/pagar', {'forma_pagamento': 'Débito'})
taxas = [m for m in movs('despesa') if m['origem'] == 'taxa']
check('pagar o previsto gerou taxa de 3,00 (200 × 1,5%)', len(taxas) == 1 and abs(taxas[0]['valor'] - 3.00) < 0.01,
      taxas[0]['valor'] if taxas else f"{len(taxas)} lançamento(s)")
call('POST', f'/api/movimentos/{prev_id}/pagar', {'forma_pagamento': 'Débito'})
check('pagar de novo NÃO duplica a taxa (idempotente)',
      len([m for m in movs('despesa') if m['origem'] == 'taxa']) == 1)

# ── Receita manual ────────────────────────────────────────────────────
print("== Receita manual no Crédito paga taxa; despesa manual no Crédito NÃO ==")
call('POST', '/api/movimentos', {'tipo': 'receita', 'descricao': '[ST] Venda avulsa', 'valor': 50,
                                 'forma_pagamento': 'Crédito', 'data': HOJE})
check('receita manual gerou taxa (1,60)',
      any(abs(m['valor'] - 1.60) < 0.01 for m in movs('despesa') if m['origem'] == 'taxa'))
antes = len([m for m in movs('despesa') if m['origem'] == 'taxa'])
call('POST', '/api/movimentos', {'tipo': 'despesa', 'descricao': '[ST] Compra no cartão', 'valor': 300,
                                 'categoria': 'Insumos/Produtos', 'forma_pagamento': 'Crédito', 'data': HOJE})
check('despesa no cartão não gera taxa', len([m for m in movs('despesa') if m['origem'] == 'taxa']) == antes)

# ── Relatório do período ──────────────────────────────────────────────
print("== Relatório de taxas do período ==")
rel = call('GET', f'/api/relatorios/taxas?de={MES_INI}&ate={HOJE}')
check('relatório responde', rel.get('ok') is True)
check('total_liquido = total_bruto - total_taxa',
      abs(rel['total_liquido'] - (rel['total_bruto'] - rel['total_taxa'])) < 0.01,
      f"{rel['total_liquido']} vs {rel['total_bruto']}-{rel['total_taxa']}")
lin_cred = next((r for r in rel['rows'] if r['forma'] == 'Crédito'), None)
check('% efetivo do Crédito ≈ 3,2', lin_cred and abs(lin_cred['taxa_pct_efetiva'] - 3.2) < 0.15,
      lin_cred['taxa_pct_efetiva'] if lin_cred else None)

print("== DRE enxerga a categoria Taxas de cartão ==")
dre = call('GET', f'/api/relatorios/dre?de={MES_INI}&ate={HOJE}')
cats = {x['categoria']: x['total'] for x in dre['despesas']}
check('Taxas de cartão aparece nas despesas do DRE', 'Taxas de cartão' in cats, list(cats))

print("== Limpeza ==")
limpar()
c = db(); cur = c.cursor()
cur.execute("DELETE FROM plano_servicos WHERE servico_id IN (SELECT id FROM servicos WHERE nome LIKE '[ST]%%')")
cur.execute("DELETE FROM comanda_itens WHERE descricao LIKE '[ST]%%'")
cur.execute("DELETE FROM servicos WHERE nome LIKE '[ST]%%'")
cur.execute("UPDATE formas_pagamento SET taxa_pct=0 WHERE nome IN ('Débito','Crédito')")
cur.execute("UPDATE usuarios SET must_change_password=true")
c.commit(); cur.close(); c.close()
print(f"\n========== RESULTADO: {OK} PASS · {FAIL} FALHA ==========")
