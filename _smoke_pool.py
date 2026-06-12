"""Valida o comissionamento POOL: arrecadação→bolo 45%, rateio por produção, avulso 45%.
Insere dados controlados direto no banco (evita depender do dia da semana p/ cobertura)."""
import os, json, urllib.request, urllib.error, http.cookiejar
from datetime import date
import psycopg2
from dotenv import load_dotenv

load_dotenv()
BASE = 'http://localhost:5002'
cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
hoje = date.today()

def call(m, p):
    req = urllib.request.Request(BASE + p, method=m); req.add_header('Content-Type', 'application/json')
    try: r = op.open(req, timeout=10); return json.loads(r.read().decode())
    except urllib.error.HTTPError as e: return json.loads(e.read().decode())

def db():
    return psycopg2.connect(host=os.getenv('DB_HOST'), port=os.getenv('DB_PORT'), dbname=os.getenv('DB_NAME'),
        user=os.getenv('DB_USER'), password=os.getenv('DB_PASSWORD'))

c = db(); cur = c.cursor()
cur.execute("UPDATE usuarios SET must_change_password=false WHERE email='caixa@barbearia.local'")
# pega 2 barbeiros
cur.execute("SELECT id, nome FROM profissionais ORDER BY id LIMIT 2")
(A, nomeA), (B, nomeB) = cur.fetchall()

# limpa resíduo anterior
cur.execute("DELETE FROM comanda_itens WHERE descricao LIKE '[SP]%%'")
cur.execute("DELETE FROM comandas WHERE id IN (SELECT id FROM comandas WHERE criado_por IS NULL AND valor_total=999999)")
cur.execute("DELETE FROM movimentos WHERE descricao='[SP] arrecadacao'")

# Arrecadação dos planos no período = R$1000 (pago) → bolo 45% = R$450
cur.execute("""INSERT INTO movimentos (tipo, origem, descricao, valor, data, status)
               VALUES ('receita','assinatura','[SP] arrecadacao',1000,%s,'pago')""", (hoje,))

# Comanda do barbeiro A: 3 serviços cobertos + 1 avulso (Barba 29)
cur.execute("INSERT INTO comandas (profissional_id, status, valor_total, fechada_em) VALUES (%s,'fechada',999999,NOW()) RETURNING id", (A,))
comA = cur.fetchone()[0]
for _ in range(3):
    cur.execute("""INSERT INTO comanda_itens (comanda_id, tipo, descricao, profissional_id, preco_unit, qtd, subtotal, preco_tabela, coberto_plano)
                   VALUES (%s,'servico','[SP] Cabelo coberto',%s,0,1,0,36,true)""", (comA, A))
cur.execute("""INSERT INTO comanda_itens (comanda_id, tipo, descricao, profissional_id, preco_unit, qtd, subtotal, coberto_plano)
               VALUES (%s,'servico','[SP] Barba avulso',%s,29,1,29,false)""", (comA, A))

# Comanda do barbeiro B: 1 serviço coberto + 1 avulso (Cabelo 36)
cur.execute("INSERT INTO comandas (profissional_id, status, valor_total, fechada_em) VALUES (%s,'fechada',999999,NOW()) RETURNING id", (B,))
comB = cur.fetchone()[0]
cur.execute("""INSERT INTO comanda_itens (comanda_id, tipo, descricao, profissional_id, preco_unit, qtd, subtotal, preco_tabela, coberto_plano)
               VALUES (%s,'servico','[SP] Cabelo coberto',%s,0,1,0,36,true)""", (comB, B))
cur.execute("""INSERT INTO comanda_itens (comanda_id, tipo, descricao, profissional_id, preco_unit, qtd, subtotal, coberto_plano)
               VALUES (%s,'servico','[SP] Cabelo avulso',%s,36,1,36,false)""", (comB, B))
c.commit(); cur.close(); c.close()

print("== Login ==")
req = urllib.request.Request(BASE + '/api/login', data=json.dumps({'email':'caixa@barbearia.local','senha':'joga123'}).encode(), method='POST')
req.add_header('Content-Type','application/json'); op.open(req)
planos = call('GET', '/api/planos')['rows']
print("  planos:", [(p['nome'], p['valor_mensal'], [s['nome'] for s in p['servicos']]) for p in planos])

print("== Relatório de comissão (hoje) ==")
j = call('GET', f'/api/relatorios/comissao?de={hoje.isoformat()}&ate={hoje.isoformat()}')
print(f"  arrecadacao={j['plan_revenue']} bolo={j['pool']} (esperado 1000 / 450)")
print(f"  total_atend={j['total_atend']} (esperado 4)")
for r in j['rows']:
    print(f"   - {r['profissional']:8} avulso={r['avulso']:>7} bolo={r['pool_share']:>7} ({r['participacao_pct']}% · {r['atend']} atend) total={r['total']}")
print(f"  TOTAL geral={j['total']}")
print("  Esperado:  A(3 atend)=avulso 13.05 + bolo 337.50 = 350.55 | B(1)=16.20 + 112.50 = 128.70")

print("== Limpeza ==")
c = db(); cur = c.cursor()
cur.execute("DELETE FROM comanda_itens WHERE descricao LIKE '[SP]%%'")
cur.execute("DELETE FROM comandas WHERE valor_total=999999")
cur.execute("DELETE FROM movimentos WHERE descricao='[SP] arrecadacao'")
cur.execute("UPDATE usuarios SET must_change_password=true")
c.commit(); cur.close(); c.close()
print("  [OK] limpo.")
