"""Bateria completa de testes da operação (via API, como um usuário real).
Roda num banco recém-semeado. Deixa dados de exemplo populados pra inspeção."""
import os, json, calendar, urllib.request, urllib.error, http.cookiejar
from datetime import date
import psycopg2
from dotenv import load_dotenv

load_dotenv()
BASE = 'http://localhost:5002'
cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
hoje = date.today()
mes_ini = hoje.replace(day=1).isoformat()
DIAS = ['dom', 'seg', 'ter', 'qua', 'qui', 'sex', 'sab']
js_wd = (hoje.weekday() + 1) % 7

OK = 0; FAIL = 0
def check(label, cond, extra=''):
    global OK, FAIL
    print(f"  [{'PASS' if cond else 'FALHA'}] {label}" + (f"  → {extra}" if extra else ''))
    OK += bool(cond); FAIL += (not cond)

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

# libera login (sem troca de senha) pra testar
c = db(); cur = c.cursor(); cur.execute("UPDATE usuarios SET must_change_password=false"); c.commit(); cur.close(); c.close()

print(f"== Contexto: hoje = {hoje} ({DIAS[js_wd]}), plano cobre seg/ter/qua ==\n")

print("== 1. Login (Regiane / dona) ==")
j = call('POST', '/api/login', {'email': 'regiane@barbearia.local', 'senha': 'joga123'})
check('login dona', j.get('ok'))
me = call('GET', '/api/me'); check('role=dono', me.get('role') == 'dono', me.get('role'))

profs = {p['nome']: p['id'] for p in call('GET', '/api/profissionais')['rows']}
servs = {s['nome']: s for s in call('GET', '/api/servicos')['rows']}
prods = call('GET', '/api/produtos')['rows']
regiane = profs['Regiane Vieira Zava']; joao = profs['João Victor']
cabelo, barba = servs['Cabelo'], servs['Barba']

print("== 2. Cadastro de cliente (nome deve virar MAIÚSCULO) ==")
cli = call('POST', '/api/clientes', {'nome': 'joão da silva', 'telefone': '34999434613', 'tipo': 'universal'})
cli_id = cli['row']['id']
det = call('GET', f'/api/clientes/{cli_id}')
check('nome em maiúsculo', det['cliente']['nome'] == 'JOÃO DA SILVA', det['cliente']['nome'])

print("== 3. Agendamento com 2 serviços (Cabelo + Barba = 60min = 2 slots) ==")
ag = call('POST', '/api/agendamentos', {'profissional_id': regiane, 'data': hoje.isoformat(),
          'hora_inicio': '09:00', 'cliente_id': cli_id, 'servicos_ids': [cabelo['id'], barba['id']]})
agenda = call('GET', f'/api/agenda?data={hoje.isoformat()}')
alvo = next((a for a in agenda['agendamentos'] if a['id'] == ag['id']), {})
check('reservou 2 slots', alvo.get('duracao_slots') == 2, f"slots={alvo.get('duracao_slots')}")
check('2 serviços no card', len(alvo.get('servicos_nomes') or []) == 2, alvo.get('servicos_nomes'))

print("== 4. Abrir comanda do agendamento (deve pré-carregar os 2 serviços) ==")
com = call('POST', '/api/comandas', {'agendamento_id': ag['id']})
d4 = call('GET', f"/api/comandas/aberta?id={com['id']}")
check('2 itens pré-carregados', len(d4['itens']) == 2, [i['descricao'] for i in d4['itens']])

print("== 5. Adicionar produto e fechar no Pix ==")
call('POST', f"/api/comandas/{com['id']}/itens", {'tipo': 'produto', 'ref_id': prods[0]['id']})
fech = call('POST', f"/api/comandas/{com['id']}/fechar", {'forma_pagamento': 'Pix'})
esperado = float(cabelo['preco']) + float(barba['preco']) + float(prods[0]['preco'])
check('total da comanda correto', abs(fech['valor_total'] - esperado) < 0.01, f"{fech['valor_total']} = {esperado}")
movs = call('GET', f'/api/movimentos?de={hoje.isoformat()}&ate={hoje.isoformat()}')['rows']
check('virou receita no caixa (Pix, pago)',
      any(m['origem'] == 'comanda' and m['forma_pagamento'] == 'Pix' and abs(float(m['valor']) - esperado) < 0.01 and m['status'] == 'pago' for m in movs))

print("== 6. Walk-in (João Victor) → 1 serviço → fecha no Dinheiro ==")
wk = call('POST', '/api/comandas', {'profissional_id': joao})
call('POST', f"/api/comandas/{wk['id']}/itens", {'tipo': 'servico', 'ref_id': cabelo['id']})
fwk = call('POST', f"/api/comandas/{wk['id']}/fechar", {'forma_pagamento': 'Dinheiro'})
check('walk-in fechado (Cabelo 36)', abs(fwk['valor_total'] - float(cabelo['preco'])) < 0.01, fwk['valor_total'])

print("== 7. Assinatura + cobrança + receber ==")
cli2 = call('POST', '/api/clientes', {'nome': 'CLIENTE ASSINANTE', 'telefone': '34988887777', 'tipo': 'fixo', 'profissional_fixo_id': regiane})['row']
planos = {p['nome']: p for p in call('GET', '/api/planos')['rows']}
plano_cabelo = planos['Plano Cabelo']
call('POST', '/api/assinaturas', {'cliente_id': cli2['id'], 'plano_id': plano_cabelo['id'], 'dia_vencimento': 10})
# A 1ª mensalidade já caiu no caixa na criação; a PRÓXIMA vence no mês seguinte (regra desde
# a2f2877). Gerar para a competência atual devolve 0 de propósito — quem cobre isso é o
# _smoke_assinatura.py. Aqui a gente pede o mês seguinte, que é onde a cobrança existe.
prox_ano, prox_mes = (hoje.year + 1, 1) if hoje.month == 12 else (hoje.year, hoje.month + 1)
fim_prox = date(prox_ano, prox_mes, calendar.monthrange(prox_ano, prox_mes)[1])
gc = call('POST', '/api/assinaturas/gerar-cobrancas', {'competencia': f'{prox_ano}-{prox_mes:02d}'})
check('cobrança gerada', gc.get('gerados', 0) >= 1, f"gerados={gc.get('gerados')}")
movs = call('GET', f'/api/movimentos?de={mes_ini}&ate={fim_prox.isoformat()}')['rows']
cob = next((m for m in movs if m['origem'] == 'assinatura' and m['status'] == 'previsto'), None)
check('cobrança no caixa como PREVISTO (R$100)', cob and abs(float(cob['valor']) - 100) < 0.01, cob and cob['valor'])
if cob:
    call('POST', f"/api/movimentos/{cob['id']}/pagar", {'forma_pagamento': 'Pix'})
    movs2 = call('GET', f'/api/movimentos?de={mes_ini}&ate={hoje.isoformat()}')['rows']
    check('cobrança virou PAGO ao receber', any(m['id'] == cob['id'] and m['status'] == 'pago' for m in movs2))

print("== 8. Cobertura do plano (assinante atendido hoje) ==")
com_as = call('POST', '/api/comandas', {'profissional_id': regiane, 'cliente_id': cli2['id']})['id']
call('POST', f'/api/comandas/{com_as}/itens', {'tipo': 'servico', 'ref_id': cabelo['id']})  # coberto pelo Plano Cabelo
d8 = call('GET', f'/api/comandas/aberta?id={com_as}')
item = d8['itens'][0]
if js_wd in (1, 2, 3):
    check('Cabelo do assinante saiu R$0 (coberto seg-qua)', item['coberto_plano'] and float(item['preco_unit']) == 0)
else:
    check(f'hoje é {DIAS[js_wd]} (fora do plano) → assinante paga normal', (not item['coberto_plano']) and float(item['preco_unit']) == 36, f"coberto={item['coberto_plano']} preco={item['preco_unit']}")
# Barba NÃO está no Plano Cabelo → sempre paga
call('POST', f'/api/comandas/{com_as}/itens', {'tipo': 'servico', 'ref_id': barba['id']})
d8b = call('GET', f'/api/comandas/aberta?id={com_as}')
barba_item = next(i for i in d8b['itens'] if i['descricao'] == 'Barba')
check('Barba (fora do plano) cobra normal R$29', not barba_item['coberto_plano'] and float(barba_item['preco_unit']) == 29)
call('POST', f'/api/comandas/{com_as}/fechar', {'forma_pagamento': 'Cartão'})

print("== 9. Caixa: fechamento do dia ==")
fx = call('GET', f'/api/caixa/fechamento?data={hoje.isoformat()}')
formas = {p['forma']: p['total'] for p in fx['por_forma']}
check('fechamento tem Pix, Dinheiro e Cartão', all(f in formas for f in ('Pix', 'Dinheiro', 'Cartão')), formas)
check('atendimentos contados', fx['atendimentos'] >= 3, f"atend={fx['atendimentos']}")
print(f"     caixa: receita={fx['total_receita']} despesa={fx['total_despesa']} saldo={fx['saldo']} formas={formas}")

print("== 10. Relatório de comissão (quinzena) ==")
rc = call('GET', f'/api/relatorios/comissao?de={mes_ini}&ate={hoje.isoformat()}')
# Duas mensalidades caem no período: a 1ª cobrada na criação da assinatura (regra a2f2877) e a
# do mês seguinte, que o passo 7 recebeu — o mov_pagar carimba data=hoje.
check('arrecadação = 200 (1ª mensalidade + a recebida)', abs(rc['plan_revenue'] - 200) < 0.01, rc['plan_revenue'])
check('bolo = 90 (45% de 200)', abs(rc['pool'] - 90) < 0.01, rc['pool'])
print(f"     rows: {[(r['profissional'], 'avulso='+str(r['avulso']), 'bolo='+str(r['pool_share']), 'visitas='+str(r['atend']), 'total='+str(r['total'])) for r in rc['rows']]}")

print("== 11. Visão do barbeiro (João Victor) ==")
call('POST', '/api/login', {'email': 'joaovictor@barbearia.local', 'senha': 'joga123'})
mc = call('GET', '/api/relatorios/minha-comissao')
check('barbeiro vê sua comissão (avulso + bolo)', 'avulso' in mc and 'pool_share' in mc, f"comissao={mc.get('comissao')}")
agj = call('GET', f'/api/agenda?data={hoje.isoformat()}')
check('barbeiro vê só a própria agenda', len(agj['profissionais']) == 1 and agj['profissionais'][0]['id'] == joao, [p['nome'] for p in agj['profissionais']])

print(f"\n========== RESULTADO: {OK} PASS · {FAIL} FALHA ==========")
