"""Smoke das fases 'assinante ponta a ponta':
  1) Identidade: plano_nome em /api/clientes, assinante em /api/agenda, assinatura em /api/comandas/aberta
  2) Cobertura: comanda de assinante cobre R$0; 2 serviços na MESMA visita ficam cobertos
  3) Trava diária: 2ª comanda coberta no dia é cobrada; registrar visita no dia é bloqueado (409)
Usa login master + plano de teste que cobre todos os dias (independe do dia da semana). Limpa tudo no fim.
Rode: python -X utf8 _smoke_assinante.py"""
import os
os.environ['SUPORTE_EMAIL'] = 'smoke@joga.local'
os.environ['SUPORTE_SENHA'] = 'smoke-secret'

from datetime import date, timedelta
import server  # noqa: E402

app = server.app.test_client()
FALHAS = []


def ok(cond, msg):
    print(('  [OK] ' if cond else '  [FALHA] ') + msg)
    if not cond:
        FALHAS.append(msg)


def j(r):
    return r.get_json()


print("== setup ==")
app.post('/api/login', json={'email': 'smoke@joga.local', 'senha': 'smoke-secret'})
servs = j(app.get('/api/servicos'))['rows']
s1, s2 = servs[0], servs[1]
barb = j(app.post('/api/profissionais', json={'nome': 'Barbeiro Smoke Assin', 'recebe_comissao': True}))['row']
# plano de teste cobrindo TODOS os dias (0..6), com 2 serviços
plano = j(app.post('/api/planos', json={'nome': 'Plano Smoke 7d', 'valor_mensal': 120,
          'dias_inclusos': [0, 1, 2, 3, 4, 5, 6], 'servicos': [s1['id'], s2['id']]}))['row']
cli = j(app.post('/api/clientes', json={'nome': 'Cliente Smoke Assin', 'telefone': '(11) 90000-0022'}))['row']
cid = cli['id']
app.post('/api/assinaturas', json={'cliente_id': cid, 'plano_id': plano['id'], 'receber_agora': False})
asid = next(a['id'] for a in j(app.get('/api/assinaturas'))['rows'] if a['cliente_id'] == cid)
server.execute("UPDATE assinaturas SET data_inicio=%s WHERE id=%s", (date.today() - timedelta(days=7), asid))
ok(True, "assinante de teste criado (plano cobre todos os dias)")

print("== FASE 1: identidade ==")
crow = next((c for c in j(app.get('/api/clientes?q=Smoke Assin'))['rows'] if c['id'] == cid), None)
ok(crow and crow.get('plano_nome') == 'Plano Smoke 7d', f"/api/clientes traz plano_nome (got {crow.get('plano_nome') if crow else '?'})")

hoje = date.today().isoformat()
app.post('/api/agendamentos', json={'profissional_id': barb['id'], 'data': hoje, 'hora_inicio': '09:00',
                                     'cliente_id': cid, 'servicos_ids': [s1['id'], s2['id']]})
ag = next((a for a in j(app.get(f'/api/agenda?data={hoje}'))['agendamentos'] if a['cliente_id'] == cid), None)
ok(ag and ag.get('assinante') is True, "agendamento marcado como assinante na agenda")
ok(ag and ag.get('plano_cobre_dia') is True, "agenda indica que o plano cobre o dia")

print("== FASE 2: comanda cobre R$0 (2 serviços = 1 visita) ==")
com1 = j(app.post('/api/comandas', json={'agendamento_id': ag['id']}))['id']
det = j(app.get(f'/api/comandas/aberta?id={com1}'))
ok(det.get('assinatura') and det['assinatura'].get('plano_nome') == 'Plano Smoke 7d', "comanda/aberta traz a assinatura (cabeçalho)")
itens = det['itens']
cobertos = [i for i in itens if i['coberto_plano']]
ok(len(itens) == 2, f"2 serviços pré-carregados do agendamento (got {len(itens)})")
ok(len(cobertos) == 2, f"os 2 serviços da MESMA visita ficam cobertos R$0 (got {len(cobertos)})")
ok(det['comanda']['valor_total'] in (0, 0.0), f"total da visita = R$0 (got {det['comanda']['valor_total']})")

print("== FASE 3: trava diária ==")
# registrar visita no mesmo dia deve ser bloqueado (já há comanda coberta hoje)
rv = app.post('/api/visitas/registrar', json={'cliente_id': cid, 'profissional_id': barb['id']})
ok(rv.status_code == 409, f"registrar visita no dia é bloqueado 409 (got {rv.status_code})")
# 2ª comanda (walk-in) no mesmo dia: o serviço coberto agora é COBRADO (benefício já usado)
com2 = j(app.post('/api/comandas', json={'profissional_id': barb['id'], 'cliente_id': cid}))['id']
add = j(app.post(f'/api/comandas/{com2}/itens', json={'tipo': 'servico', 'ref_id': s1['id']}))
it2 = j(app.get(f'/api/comandas/aberta?id={com2}'))['itens'][0]
ok(it2['coberto_plano'] is False, "2ª comanda no dia: serviço NÃO coberto (trava de 1 visita/dia)")
ok(float(it2['subtotal']) == float(s1['preco']), f"2ª comanda cobra o preço cheio ({it2['subtotal']} == {s1['preco']})")

print("== limpeza ==")
server.execute("DELETE FROM comanda_itens WHERE comanda_id IN (SELECT id FROM comandas WHERE cliente_id=%s)", (cid,))
server.execute("DELETE FROM movimentos WHERE ref_id IN (SELECT id FROM comandas WHERE cliente_id=%s) AND origem='comanda'", (cid,))
server.execute("DELETE FROM comandas WHERE cliente_id=%s", (cid,))
server.execute("DELETE FROM agendamentos WHERE cliente_id=%s", (cid,))
server.execute("DELETE FROM movimentos WHERE origem='assinatura' AND ref_id=%s", (asid,))
server.execute("DELETE FROM assinaturas WHERE cliente_id=%s", (cid,))
server.execute("DELETE FROM clientes WHERE id=%s", (cid,))
server.execute("DELETE FROM plano_servicos WHERE plano_id=%s", (plano['id'],))
server.execute("DELETE FROM planos WHERE id=%s", (plano['id'],))
server.execute("DELETE FROM profissionais WHERE id=%s", (barb['id'],))
ok(True, "resíduo removido")

print()
if FALHAS:
    print(f"XX {len(FALHAS)} FALHA(S):")
    for f in FALHAS:
        print("   -", f)
    raise SystemExit(1)
print("OK — todas as fases passaram.")
