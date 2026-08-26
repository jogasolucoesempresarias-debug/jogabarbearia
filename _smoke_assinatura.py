"""Smoke: 1ª mensalidade cobrada na hora (caixa hoje) + próxima no dia 10/30 do mês seguinte."""
import os, json, urllib.request, urllib.error, http.cookiejar, calendar
from datetime import date
import psycopg2
from dotenv import load_dotenv

load_dotenv()
BASE = 'http://localhost:5002'
cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
hoje = date.today()

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
        user=os.getenv('DB_USER'), password=os.getenv('DB_PASSWORD'), options='-c timezone=America/Sao_Paulo')

def dia_mes_seguinte(base, dia):
    m = base.month % 12 + 1; y = base.year + (1 if base.month == 12 else 0)
    return date(y, m, min(dia, calendar.monthrange(y, m)[1]))

c = db(); cur = c.cursor()
cur.execute("UPDATE usuarios SET must_change_password=false WHERE email='regiane@barbearia.local'")
cur.execute("INSERT INTO clientes (nome, tipo) VALUES ('[SA] CLIENTE', 'fixo') RETURNING id"); cli = cur.fetchone()[0]
cur.execute("SELECT id, valor_mensal FROM planos WHERE nome ILIKE 'Plano Barba' LIMIT 1"); plano_id, plano_val = cur.fetchone()
c.commit(); cur.close(); c.close()
plano_val = float(plano_val)

call('POST', '/api/login', {'email': 'regiane@barbearia.local', 'senha': 'joga123'})

print(f"== Hoje {hoje} · plano R${plano_val} · vencimento dia 30 ==")
print("== 1. Criar assinatura recebendo a 1ª na hora (Pix) ==")
r = call('POST', '/api/assinaturas', {'cliente_id': cli, 'plano_id': plano_id, 'dia_vencimento': 30, 'receber_agora': True, 'forma_pagamento': 'Pix'})
check('assinatura criada', r.get('ok'))
esperado_prox = dia_mes_seguinte(hoje, 30)
check('próxima cobrança = dia 30 do mês seguinte', r.get('proxima_cobranca') == esperado_prox.isoformat(), f"{r.get('proxima_cobranca')} (esperado {esperado_prox})")

print("== 2. 1ª mensalidade caiu no CAIXA de hoje (paga) ==")
movs = call('GET', f'/api/movimentos?de={hoje.isoformat()}&ate={hoje.isoformat()}')['rows']
m1 = next((m for m in movs if m['origem']=='assinatura' and m['ref_id']==r['id'] and m['status']=='pago'), None)
check('receita de assinatura PAGA hoje no caixa', m1 and abs(float(m1['valor'])-plano_val)<0.01 and m1['forma_pagamento']=='Pix', m1 and m1['valor'])

print("== 3. Gerar cobranças do mês ATUAL → não gera (1ª já paga, próxima é mês que vem) ==")
g0 = call('POST', '/api/assinaturas/gerar-cobrancas', {'competencia': f'{hoje.year}-{hoje.month:02d}'})
check('mês atual: 0 geradas', g0.get('gerados') == 0, f"gerados={g0.get('gerados')}")

print("== 4. Gerar cobranças do mês SEGUINTE → gera a previsto no dia 30 ==")
ny, nm = (hoje.year + (1 if hoje.month==12 else 0)), (hoje.month % 12 + 1)
g1 = call('POST', '/api/assinaturas/gerar-cobrancas', {'competencia': f'{ny}-{nm:02d}'})
check('mês seguinte: 1 gerada', g1.get('gerados') == 1, f"gerados={g1.get('gerados')}")
g2 = call('POST', '/api/assinaturas/gerar-cobrancas', {'competencia': f'{ny}-{nm:02d}'})
check('rodar de novo: 0 (idempotente)', g2.get('gerados') == 0, f"gerados={g2.get('gerados')}")
prev = call('GET', f"/api/movimentos?de={ny}-{nm:02d}-01&ate={ny}-{nm:02d}-28")['rows'] + call('GET', f"/api/movimentos?de={ny}-{nm:02d}-29&ate={ny}-{nm:02d}-30")['rows']
prevista = next((m for m in prev if m['origem']=='assinatura' and m['ref_id']==r['id'] and m['status']=='previsto'), None)
check('cobrança do mês seguinte está PREVISTA (a receber)', prevista is not None, prevista and prevista['vencimento'])

print("== Limpeza ==")
c = db(); cur = c.cursor()
cur.execute("DELETE FROM movimentos WHERE ref_id=%s AND origem='assinatura'", (r['id'],))
cur.execute("DELETE FROM assinaturas WHERE cliente_id=%s", (cli,))
cur.execute("DELETE FROM clientes WHERE id=%s", (cli,))
cur.execute("UPDATE usuarios SET must_change_password=true")
c.commit(); cur.close(); c.close()
print(f"\n========== RESULTADO: {OK} PASS · {FAIL} FALHA ==========")
