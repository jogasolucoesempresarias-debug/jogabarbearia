"""BATERIA ROBUSTA DE VALIDAÇÃO — via HTTP no servidor real (login regiane/240316).
Simula um dia da barbearia com lançamentos reais e valida ponta a ponta:
  identidade do assinante · cobertura R$0 · trava de 1 visita/dia · registrar visita ·
  comissão (avulso + bolo) · Uso dos Planos · DRE (drill-down/série/comparativo) · caixa.
Cria um barbeiro/plano/clientes próprios (plano cobre todos os dias → independe do dia da semana)
e LIMPA todo o resíduo no fim (bloco finally). Rode com o server no ar (porta 5002):
  PORT=5002 ... server.py   →   python -X utf8 _valida_completo.py
"""
import os, json, urllib.request, urllib.error, http.cookiejar
from datetime import date, timedelta
import psycopg2
from dotenv import load_dotenv

load_dotenv()
BASE = os.getenv('VALIDA_BASE', 'http://localhost:5002')
SENHA = os.getenv('VALIDA_SENHA', '240316')
cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

PASS, FALHAS = 0, []


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header('Content-Type', 'application/json')
    try:
        r = op.open(req, timeout=15); return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read().decode())
        except Exception: return e.code, {}


def ck(cond, msg):
    global PASS
    if cond: PASS += 1; print(f"  [OK] {msg}")
    else: FALHAS.append(msg); print(f"  [FALHA] {msg}")


def db():
    return psycopg2.connect(host=os.getenv('DB_HOST'), port=os.getenv('DB_PORT'), dbname=os.getenv('DB_NAME'),
                            user=os.getenv('DB_USER'), password=os.getenv('DB_PASSWORD'))


hoje = date.today()
ontem = hoje - timedelta(days=1)
ids = {'barb': None, 'plano': None, 'assin': None, 'avulso': None}


def limpar():
    cids = [v for v in (ids['assin'], ids['avulso']) if v]
    c = db(); cur = c.cursor()
    try:
        if cids:
            cur.execute("DELETE FROM comanda_itens WHERE comanda_id IN (SELECT id FROM comandas WHERE cliente_id = ANY(%s))", (cids,))
            cur.execute("DELETE FROM movimentos WHERE origem='comanda' AND ref_id IN (SELECT id FROM comandas WHERE cliente_id = ANY(%s))", (cids,))
            cur.execute("DELETE FROM comandas WHERE cliente_id = ANY(%s)", (cids,))
            cur.execute("DELETE FROM agendamentos WHERE cliente_id = ANY(%s)", (cids,))
        if ids['assin']:
            cur.execute("DELETE FROM movimentos WHERE origem='assinatura' AND ref_id IN (SELECT id FROM assinaturas WHERE cliente_id=%s)", (ids['assin'],))
            cur.execute("DELETE FROM assinaturas WHERE cliente_id=%s", (ids['assin'],))
        if cids:
            cur.execute("DELETE FROM clientes WHERE id = ANY(%s)", (cids,))
        if ids['plano']:
            cur.execute("DELETE FROM plano_servicos WHERE plano_id=%s", (ids['plano'],))
            cur.execute("DELETE FROM planos WHERE id=%s", (ids['plano'],))
        if ids['barb']:
            cur.execute("DELETE FROM profissionais WHERE id=%s", (ids['barb'],))
        c.commit()
    finally:
        cur.close(); c.close()


try:
    print("== 0. LOGIN (regiane, senha real) ==")
    st, j = call('POST', '/api/login', {'email': 'regiane@barbearia.local', 'senha': SENHA})
    ck(st == 200 and j.get('ok'), f"login regiane ({st})")
    if j.get('redirect') == '/trocar-senha':
        print("  !! must_change_password ativo — abortando"); raise SystemExit(1)
    ck(call('GET', '/api/me')[1].get('role') == 'dono', "sessão com papel dono")

    print("== 1. SETUP (barbeiro, plano 7 dias, 2 clientes) ==")
    servs = call('GET', '/api/servicos')[1]['rows']
    prods = call('GET', '/api/produtos')[1]['rows']
    s_cab = next((s for s in servs if 'Cabelo' == s['nome']), servs[0])
    s_bar = next((s for s in servs if 'Barba' == s['nome']), servs[1])
    prod = prods[0]
    ids['barb'] = call('POST', '/api/profissionais', {'nome': '[VAL] Barbeiro', 'recebe_comissao': True})[1]['row']['id']
    ids['plano'] = call('POST', '/api/planos', {'nome': '[VAL] Plano 7d', 'valor_mensal': 120,
                        'dias_inclusos': [0, 1, 2, 3, 4, 5, 6], 'servicos': [s_cab['id'], s_bar['id']]})[1]['row']['id']
    ids['assin'] = call('POST', '/api/clientes', {'nome': '[VAL] ASSINANTE', 'telefone': '(11) 90000-1111'})[1]['row']['id']
    ids['avulso'] = call('POST', '/api/clientes', {'nome': '[VAL] AVULSO', 'telefone': '(11) 90000-2222'})[1]['row']['id']
    call('POST', '/api/assinaturas', {'cliente_id': ids['assin'], 'plano_id': ids['plano'],
                                      'receber_agora': True, 'forma_pagamento': 'Pix'})
    asid = next(a['id'] for a in call('GET', '/api/assinaturas')[1]['rows'] if a['cliente_id'] == ids['assin'])
    c = db(); cur = c.cursor()
    cur.execute("UPDATE assinaturas SET data_inicio=%s WHERE id=%s", (hoje - timedelta(days=7), asid))
    c.commit(); cur.close(); c.close()
    ck(all(ids.values()), "barbeiro/plano/clientes/assinatura criados")

    print("== 2. IDENTIDADE DO ASSINANTE ==")
    crow = next((x for x in call('GET', '/api/clientes?q=[VAL]')[1]['rows'] if x['id'] == ids['assin']), None)
    ck(crow and crow.get('plano_nome') == '[VAL] Plano 7d', "cliente aparece como assinante (plano_nome)")
    st, jag = call('POST', '/api/agendamentos', {'profissional_id': ids['barb'], 'data': hoje.isoformat(),
                    'hora_inicio': '10:00', 'cliente_id': ids['assin'], 'servicos_ids': [s_cab['id'], s_bar['id']]})
    ck(st == 200 and jag.get('ok'), "agendamento do assinante criado")
    ag = next((a for a in call('GET', f'/api/agenda?data={hoje.isoformat()}')[1]['agendamentos'] if a['cliente_id'] == ids['assin']), None)
    ck(ag and ag.get('assinante') is True, "agenda marca o slot como ⭐ assinante")
    ck(ag and ag.get('plano_cobre_dia') is True, "agenda indica plano cobre o dia")

    print("== 3. ATENDIMENTO DO ASSINANTE (comanda cobre R$0) ==")
    com1 = call('POST', '/api/comandas', {'agendamento_id': ag['id']})[1]['id']
    det = call('GET', f'/api/comandas/aberta?id={com1}')[1]
    ck(det.get('assinatura', {}) and det['assinatura'].get('plano_nome') == '[VAL] Plano 7d', "cabeçalho da comanda traz a assinatura")
    ck(len(det['itens']) == 2 and all(i['coberto_plano'] for i in det['itens']), "2 serviços da visita cobertos R$0")
    ck(float(det['comanda']['valor_total']) == 0.0, "total da visita = R$0")
    st, jf = call('POST', f'/api/comandas/{com1}/fechar', {'forma_pagamento': 'Dinheiro'})
    ck(st == 200 and float(jf['valor_total']) == 0.0, "comanda fechada em R$0")

    print("== 4. TRAVA DE 1 VISITA/DIA ==")
    st, _ = call('POST', '/api/visitas/registrar', {'cliente_id': ids['assin'], 'profissional_id': ids['barb']})
    ck(st == 409, f"registrar 2ª visita no mesmo dia é bloqueado ({st})")
    com2 = call('POST', '/api/comandas', {'profissional_id': ids['barb'], 'cliente_id': ids['assin']})[1]['id']
    call('POST', f'/api/comandas/{com2}/itens', {'tipo': 'servico', 'ref_id': s_cab['id']})
    it2 = call('GET', f'/api/comandas/aberta?id={com2}')[1]['itens'][0]
    ck(it2['coberto_plano'] is False and float(it2['subtotal']) == float(s_cab['preco']),
       "2ª comanda no dia cobra o serviço (benefício já usado)")
    call('POST', f'/api/comandas/{com2}/cancelar')  # desfaz p/ não sujar números

    print("== 5. ATENDIMENTO AVULSO (serviço + produto) ==")
    com3 = call('POST', '/api/comandas', {'profissional_id': ids['barb'], 'cliente_id': ids['avulso']})[1]['id']
    call('POST', f'/api/comandas/{com3}/itens', {'tipo': 'servico', 'ref_id': s_cab['id']})
    call('POST', f'/api/comandas/{com3}/itens', {'tipo': 'produto', 'ref_id': prod['id']})
    st, jf3 = call('POST', f'/api/comandas/{com3}/fechar', {'forma_pagamento': 'Pix'})
    esperado = float(s_cab['preco']) + float(prod['preco'])
    ck(st == 200 and float(jf3['valor_total']) == esperado, f"comanda avulsa fecha em {esperado}")

    print("== 6. REGISTRAR VISITA RETROATIVA (ontem) ==")
    st, jr = call('POST', '/api/visitas/registrar', {'cliente_id': ids['assin'], 'profissional_id': ids['barb'], 'data': ontem.isoformat()})
    ck(st == 200 and jr.get('ok'), f"visita de ontem registrada ({st})")

    print("== 7. COMISSÃO (avulso + bolo, barbeiro exclusivo do teste) ==")
    de_mes = hoje.replace(day=1).isoformat()
    jc = call('GET', f'/api/relatorios/comissao?de={de_mes}&ate={hoje.isoformat()}')[1]
    brow = next((r for r in jc['rows'] if r['profissional_id'] == ids['barb']), None)
    ck(brow is not None, "barbeiro aparece na comissão")
    ck(brow and brow['atend'] == 2, f"2 visitas de plano no bolo (hoje + ontem) (got {brow['atend'] if brow else '?'})")
    ck(brow and round(brow['avulso'], 2) == round(float(s_cab['preco']) * 0.45, 2),
       f"avulso = 45% do serviço avulso (got {brow['avulso'] if brow else '?'})")
    ck(brow and brow['pool_share'] > 0, "barbeiro recebe fatia do bolo")

    print("== 8. USO DOS PLANOS ==")
    ju = call('GET', f'/api/relatorios/assinantes-uso?de={de_mes}&ate={hoje.isoformat()}')[1]
    urow = next((r for r in ju['rows'] if r['cliente_id'] == ids['assin']), None)
    ck(urow is not None, "assinante aparece no Uso dos Planos")
    ck(urow and urow['visitas'] == 2, f"2 visitas no mês (comanda hoje + visita ontem) (got {urow['visitas'] if urow else '?'})")
    ck(urow and urow['custo_por_visita'] == round(120.0 / 2, 2), f"custo/visita = 120/2 = 60 (got {urow['custo_por_visita'] if urow else '?'})")
    ck(urow and urow['barbeiros'] and urow['barbeiros'][0]['qtd'] == 2, "distribuição por barbeiro = 2")
    ck(ju['resumo']['margem'] == round(ju['resumo']['arrecadacao'] - ju['resumo']['bolo'], 2), "margem = arrecadação − bolo")

    print("== 9. HISTÓRICO DE VISITAS ==")
    jh = call('GET', f"/api/assinantes/{ids['assin']}/visitas")[1]['rows']
    ck(len(jh) == 2, f"histórico com 2 visitas (got {len(jh)})")

    print("== 10. DRE APRIMORADO ==")
    jd = call('GET', f'/api/relatorios/dre?de={de_mes}&ate={hoje.isoformat()}')[1]
    ck('servicos_detalhe' in jd['receitas'] and 'produtos_detalhe' in jd['receitas'], "DRE tem drill-down de serviços/produtos")
    ck(jd['receitas']['assinaturas'] >= 120, "receita de assinatura no DRE (1ª mensalidade)")
    ck(jd['atendimentos'] >= 1 and jd['ticket_medio'] > 0, f"atendimentos pagos + ticket médio (atend={jd['atendimentos']}, ticket={jd['ticket_medio']})")
    ck(jd['visitas_plano'] >= 2, f"DRE conta visitas de plano ({jd['visitas_plano']})")
    ck('comparativo' in jd and 'resultado' in jd['comparativo'], "DRE traz comparativo com período anterior")
    cab_det = next((d for d in jd['receitas']['produtos_detalhe'] if d['nome'] == prod['nome']), None)
    ck(cab_det is not None and float(cab_det['total']) >= float(prod['preco']), "produto vendido aparece no detalhamento")

    print("== 11. SÉRIE MENSAL (evolução) ==")
    js = call('GET', '/api/relatorios/dre-serie?meses=6')[1]['serie']
    ck(len(js) == 6 and all('resultado' in s for s in js), "série de 6 meses com resultado")

    print("== 12. CAIXA DO DIA ==")
    jx = call('GET', f'/api/caixa/fechamento?data={hoje.isoformat()}')[1]
    ck(jx['total_receita'] >= 120 + esperado, f"caixa soma mensalidade + avulso (receita={jx['total_receita']})")
    ck(jx['atendimentos'] >= 2, f"atendimentos do dia contam as comandas fechadas ({jx['atendimentos']})")

finally:
    print("== LIMPEZA ==")
    try:
        limpar(); print("  [OK] resíduo [VAL] removido")
    except Exception as e:
        print("  [FALHA] limpeza:", e)

print()
print(f"========== RESULTADO: {PASS} PASS · {len(FALHAS)} FALHA ==========")
if FALHAS:
    for f in FALHAS:
        print("   - ", f)
    raise SystemExit(1)
