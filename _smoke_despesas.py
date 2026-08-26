"""Smoke do módulo de Despesas: lançamento manual, despesas fixas, gerar mês, resumo, delete."""
import os, json, urllib.request, urllib.error, http.cookiejar
from datetime import date
import psycopg2
from dotenv import load_dotenv

load_dotenv()
BASE = 'http://localhost:5002'
cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
import calendar
hoje = date.today()
comp = f"{hoje.year}-{hoje.month:02d}"
mes_ini = hoje.replace(day=1).isoformat()
mes_fim = hoje.replace(day=calendar.monthrange(hoje.year, hoje.month)[1]).isoformat()  # mês inteiro (igual a tela)

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

c = db(); cur = c.cursor()
cur.execute("UPDATE usuarios SET must_change_password=false WHERE email='regiane@barbearia.local'")
# limpa resíduo de runs anteriores
cur.execute("DELETE FROM movimentos WHERE descricao LIKE '[SD]%%'")
cur.execute("DELETE FROM despesas_fixas WHERE descricao LIKE '[SD]%%'")
c.commit(); cur.close(); c.close()

call('POST', '/api/login', {'email': 'regiane@barbearia.local', 'senha': 'joga123'})

print("== Categorias de despesa na config ==")
cfg = call('GET', '/api/config')['config']
check('categorias_despesa traz Impostos', 'Impostos' in (cfg.get('categorias_despesa') or []), cfg.get('categorias_despesa'))

print("== Lançar despesa manual (Insumos/Produtos) ==")
call('POST', '/api/movimentos', {'tipo': 'despesa', 'descricao': '[SD] Lâminas e gel', 'categoria': 'Insumos/Produtos', 'valor': 120, 'data': hoje.isoformat(), 'forma_pagamento': 'Pix'})
movs = call('GET', f'/api/movimentos?tipo=despesa&de={mes_ini}&ate={hoje.isoformat()}')['rows']
check('despesa aparece na lista', any(m['descricao'] == '[SD] Lâminas e gel' and m['categoria'] == 'Insumos/Produtos' for m in movs))

print("== Despesas fixas: cadastrar Aluguel 1500 (dia 5) e Impostos 200 (dia 20) ==")
call('POST', '/api/despesas-fixas', {'descricao': '[SD] Aluguel', 'categoria': 'Aluguel', 'valor': 1500, 'dia': 5})
call('POST', '/api/despesas-fixas', {'descricao': '[SD] DAS/Imposto', 'categoria': 'Impostos', 'valor': 200, 'dia': 20})
fx = call('GET', '/api/despesas-fixas')['rows']
check('2 fixas cadastradas', len([f for f in fx if f['descricao'].startswith('[SD]')]) == 2)

print("== Gerar despesas do mês (idempotente) ==")
g1 = call('POST', '/api/despesas-fixas/gerar', {'competencia': comp})
check('gerou 2 despesas fixas', g1.get('gerados') == 2, f"gerados={g1.get('gerados')}")
g2 = call('POST', '/api/despesas-fixas/gerar', {'competencia': comp})
check('rodar de novo gera 0 (idempotente)', g2.get('gerados') == 0, f"gerados={g2.get('gerados')}")

print("== Caixa do dia reflete a despesa de hoje (insumos 120, antes de excluir) ==")
fx_caixa = call('GET', f'/api/caixa/fechamento?data={hoje.isoformat()}')
check('caixa do dia inclui a despesa de hoje', fx_caixa['total_despesa'] >= 120, f"despesa_dia={fx_caixa['total_despesa']}")

print("== Resumo por categoria (mês inteiro, igual a tela) ==")
res = call('GET', f'/api/despesas/resumo?de={mes_ini}&ate={mes_fim}')
porcat = {x['categoria']: x['total'] for x in res['por_categoria']}
check('Aluguel 1500 no resumo', abs(porcat.get('Aluguel', 0) - 1500) < 0.01, porcat.get('Aluguel'))
check('Impostos 200 no resumo (dia 20)', abs(porcat.get('Impostos', 0) - 200) < 0.01, porcat.get('Impostos'))
check('Insumos 120 no resumo', abs(porcat.get('Insumos/Produtos', 0) - 120) < 0.01, porcat.get('Insumos/Produtos'))
check('total = 1820', abs(res['total'] - 1820) < 0.01, res['total'])

print("== Despesa fixa marcada (despesa_fixa_id) ==")
movs2 = call('GET', f'/api/movimentos?tipo=despesa&de={mes_ini}&ate={mes_fim}')['rows']
fixa_gerada = next((m for m in movs2 if m['descricao'] == '[SD] Aluguel'), None)
check('despesa fixa gerada tem despesa_fixa_id', fixa_gerada and fixa_gerada.get('despesa_fixa_id'))

print("== Excluir uma despesa manual (insumos 120) ==")
alvo = next(m for m in movs2 if m['descricao'] == '[SD] Lâminas e gel')
call('DELETE', f"/api/movimentos/{alvo['id']}")
res2 = call('GET', f'/api/despesas/resumo?de={mes_ini}&ate={mes_fim}')
check('após excluir, total cai pra 1700', abs(res2['total'] - 1700) < 0.01, res2['total'])

print("== Limpeza ==")
c = db(); cur = c.cursor()
cur.execute("DELETE FROM movimentos WHERE descricao LIKE '[SD]%%'")
cur.execute("DELETE FROM despesas_fixas WHERE descricao LIKE '[SD]%%'")
cur.execute("UPDATE usuarios SET must_change_password=true")
c.commit(); cur.close(); c.close()
print(f"\n========== RESULTADO: {OK} PASS · {FAIL} FALHA ==========")
