"""Smoke: timezone, dona fora do rateio, fechar comissão (vira despesa) e DRE."""
import os, json, urllib.request, urllib.error, http.cookiejar
from datetime import date
import psycopg2
from dotenv import load_dotenv

load_dotenv()
BASE = 'http://localhost:5002'
cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
hoje = date.today()
de = hoje.replace(day=1).isoformat()
ate = hoje.isoformat()

OK = 0; FAIL = 0
def check(label, cond, extra=''):
    global OK, FAIL
    print(f"  [{'PASS' if cond else 'FALHA'}] {label}" + (f"  → {extra}" if extra else '')); OK += cond; FAIL += (not cond)

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

c = db(); cur = c.cursor()
cur.execute("UPDATE usuarios SET must_change_password=false WHERE email='regiane@barbearia.local'")
# limpa resíduo
cur.execute("DELETE FROM comanda_itens WHERE descricao LIKE '[SG]%%'")
cur.execute("DELETE FROM comissoes_pagas")
cur.execute("DELETE FROM movimentos WHERE descricao LIKE '%%[SG]%%' OR categoria='Comissões'")
c.commit()

print("== 1. Timezone (conexão em America/Sao_Paulo) ==")
cur.execute("SHOW timezone"); tz = cur.fetchone()[0]
cur.execute("SELECT CURRENT_DATE"); cdate = cur.fetchone()[0]
check('timezone da sessão = America/Sao_Paulo', tz == 'America/Sao_Paulo', tz)
check('CURRENT_DATE = hoje (BRT)', cdate == hoje, f"{cdate} vs {hoje}")

# barbeiros: Regiane (dona, sem comissão) e João (recebe)
cur.execute("SELECT id, nome, recebe_comissao FROM profissionais ORDER BY id")
profs = cur.fetchall()
regiane = next(p[0] for p in profs if 'Regiane' in p[1]); joao = next(p[0] for p in profs if 'João' in p[1] or 'Joao' in p[1])
check('Regiane NÃO recebe comissão', not next(p[2] for p in profs if p[0]==regiane))
check('João recebe comissão', next(p[2] for p in profs if p[0]==joao))

# cliente assinante
cur.execute("INSERT INTO clientes (nome, tipo) VALUES ('[SG] ASSIN', 'fixo') RETURNING id"); cli = cur.fetchone()[0]
cur.execute("SELECT id FROM planos WHERE nome ILIKE 'Plano Cabelo' LIMIT 1"); plano = cur.fetchone()[0]
cur.execute("INSERT INTO assinaturas (cliente_id, plano_id) VALUES (%s,%s)", (cli, plano))
# arrecadação: 1 mensalidade paga (100) → bolo 45
cur.execute("INSERT INTO movimentos (tipo,origem,descricao,valor,data,status) VALUES ('receita','assinatura','[SG] mensalidade',100,CURRENT_DATE,'pago')")

def comanda(prof):
    cur.execute("INSERT INTO comandas (profissional_id,status,valor_total,fechada_em) VALUES (%s,'fechada',0,NOW()) RETURNING id",(prof,)); return cur.fetchone()[0]
def coberto(com, prof):
    cur.execute("INSERT INTO comanda_itens (comanda_id,tipo,descricao,profissional_id,preco_unit,qtd,subtotal,preco_tabela,coberto_plano) VALUES (%s,'servico','[SG] cob',%s,0,1,0,36,true)",(com,prof))
def avulso(com, prof, v):
    cur.execute("INSERT INTO comanda_itens (comanda_id,tipo,descricao,profissional_id,preco_unit,qtd,subtotal,coberto_plano) VALUES (%s,'servico','[SG] avulso',%s,%s,1,%s,false)",(com,prof,v,v))
def produto(com, prof, v):
    cur.execute("INSERT INTO comanda_itens (comanda_id,tipo,descricao,profissional_id,preco_unit,qtd,subtotal,coberto_plano) VALUES (%s,'produto','[SG] prod',%s,%s,1,%s,false)",(com,prof,v,v))

# Regiane: 2 visitas de assinante (cobertas) + 1 avulso 36 ; João: 1 visita assinante + 1 avulso 29 + 1 produto 35
cR1=comanda(regiane); coberto(cR1,regiane); avulso(cR1,regiane,36)
cR2=comanda(regiane); coberto(cR2,regiane)
cJ=comanda(joao); coberto(cJ,joao); avulso(cJ,joao,29); produto(cJ,joao,35)
c.commit(); cur.close(); c.close()

call('POST', '/api/login', {'email': 'regiane@barbearia.local', 'senha': 'joga123'})

print("== 2. Comissão: dona fora do rateio, bolo proporcional (parte da dona fica na casa) ==")
rc = call('GET', f'/api/relatorios/comissao?de={de}&ate={ate}')
nomes = [r['profissional'] for r in rc['rows']]
check('Regiane NÃO aparece no relatório', not any('Regiane' in n for n in nomes), nomes)
check('total_atend = 3 visitas (incl. dona)', rc['total_atend'] == 3, rc['total_atend'])
joao_row = next((r for r in rc['rows'] if 'Jo' in r['profissional']), None)
# João: 1 de 3 visitas → bolo = 45 * 1/3 = 15.00 ; avulso = 29*0.45 = 13.05 ; total 28.05
check('João bolo = 15.00 (1/3 do bolo, resto fica com a casa)', joao_row and abs(joao_row['pool_share'] - 15.0) < 0.01, joao_row and joao_row['pool_share'])
check('João avulso = 13.05', joao_row and abs(joao_row['avulso'] - 13.05) < 0.01, joao_row and joao_row['avulso'])
check('João total = 28.05', joao_row and abs(joao_row['total'] - 28.05) < 0.01, joao_row and joao_row['total'])
check('João pendente (ainda não fechado)', joao_row and joao_row['pago'] == False)

print("== 3. Fechar/pagar comissão do João → vira despesa no caixa ==")
fc = call('POST', '/api/comissoes/fechar', {'profissional_id': joao, 'de': de, 'ate': ate, 'valor': joao_row['total']})
check('fechar comissão ok', fc.get('ok'))
fc2 = call('POST', '/api/comissoes/fechar', {'profissional_id': joao, 'de': de, 'ate': ate, 'valor': joao_row['total']})
check('refechar bloqueado (idempotente)', fc2.get('ok') is False, fc2.get('error'))
rc2 = call('GET', f'/api/relatorios/comissao?de={de}&ate={ate}')
check('João agora aparece como PAGO', next(r['pago'] for r in rc2['rows'] if 'Jo' in r['profissional']))
movs = call('GET', f'/api/movimentos?tipo=despesa&de={de}&ate={ate}')['rows']
check('despesa "Comissões" lançada no caixa', any(m['categoria'] == 'Comissões' and abs(float(m['valor']) - 28.05) < 0.01 for m in movs))

print("== 4. DRE / Resultado ==")
dre = call('GET', f'/api/relatorios/dre?de={de}&ate={ate}')
r = dre['receitas']
# receita serviços = avulsos (36+29) = 65 (cobertos=0); produtos=35; assinaturas=100
check('receita serviços = 65', abs(r['servicos'] - 65) < 0.01, r['servicos'])
check('receita produtos = 35', abs(r['produtos'] - 35) < 0.01, r['produtos'])
check('receita assinaturas = 100', abs(r['assinaturas'] - 100) < 0.01, r['assinaturas'])
check('total receitas = 200', abs(r['total'] - 200) < 0.01, r['total'])
check('despesa Comissões = 28.05 no DRE', any(x['categoria'] == 'Comissões' and abs(float(x['total']) - 28.05) < 0.01 for x in dre['despesas']))
check('resultado = 171.95 (200 - 28.05)', abs(dre['resultado'] - 171.95) < 0.01, dre['resultado'])

print("== Limpeza ==")
c = db(); cur = c.cursor()
cur.execute("DELETE FROM comissoes_pagas")
cur.execute("DELETE FROM comanda_itens WHERE descricao LIKE '[SG]%%'")
cur.execute("DELETE FROM comandas WHERE valor_total=0 AND id IN (SELECT comanda_id FROM comanda_itens) OR id IN (%s,%s,%s)", (cR1,cR2,cJ))
cur.execute("DELETE FROM comandas WHERE id IN (%s,%s,%s)", (cR1,cR2,cJ))
cur.execute("DELETE FROM movimentos WHERE descricao LIKE '%%[SG]%%' OR categoria='Comissões'")
cur.execute("DELETE FROM assinaturas WHERE cliente_id=%s", (cli,))
cur.execute("DELETE FROM clientes WHERE id=%s", (cli,))
cur.execute("UPDATE usuarios SET must_change_password=true")
c.commit(); cur.close(); c.close()
print(f"\n========== RESULTADO: {OK} PASS · {FAIL} FALHA ==========")
