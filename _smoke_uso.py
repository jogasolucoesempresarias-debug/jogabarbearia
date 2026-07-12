"""Smoke test das novas rotas: registrar visita de assinante, Uso dos Planos e DRE aprimorado.
Usa o login master (env) p/ pular a troca de senha. Limpa o resíduo no fim.
Rode: python -X utf8 _smoke_uso.py   (precisa do schema+seed já aplicados)"""
import os
os.environ['SUPORTE_EMAIL'] = 'smoke@joga.local'
os.environ['SUPORTE_SENHA'] = 'smoke-secret'

import server  # noqa: E402

app = server.app.test_client()
FALHAS = []


def ok(cond, msg):
    print(('  [OK] ' if cond else '  [FALHA] ') + msg)
    if not cond:
        FALHAS.append(msg)


def j(resp):
    return resp.get_json()


print("== login master ==")
r = app.post('/api/login', json={'email': 'smoke@joga.local', 'senha': 'smoke-secret'})
ok(r.status_code == 200 and j(r)['ok'], "login master")

from datetime import date, timedelta

print("== dados base ==")
planos = j(app.get('/api/planos'))['rows']
ok(len(planos) >= 1, f"planos no seed ({len(planos)})")
# Barbeiro de teste próprio (ativo, recebe comissão) — independe do estado do seed
barb = j(app.post('/api/profissionais', json={'nome': 'Barbeiro Smoke Uso', 'recebe_comissao': True}))['row']
ok(barb and barb.get('id'), "barbeiro de teste criado (ativo, recebe comissão)")

print("== cria assinante ==")
cli = j(app.post('/api/clientes', json={'nome': 'Cliente Smoke Uso', 'telefone': '(11) 90000-0011'}))['row']
cid = cli['id']
assin = j(app.post('/api/assinaturas', json={'cliente_id': cid, 'plano_id': planos[0]['id'],
                                             'dia_vencimento': 10, 'receber_agora': True, 'forma_pagamento': 'Pix'}))
ok(assin['ok'], "assinatura criada (1ª mensalidade no caixa)")
# retroage o início p/ testar visita de ontem (assinatura já existia antes)
asid = next((a['id'] for a in j(app.get('/api/assinaturas'))['rows'] if a['cliente_id'] == cid), None)
server.execute("UPDATE assinaturas SET data_inicio=%s WHERE id=%s", (date.today() - timedelta(days=7), asid))

print("== assinantes ativos (tela registrar visita) ==")
ativos = j(app.get('/api/assinantes/ativos'))['rows']
meu = next((a for a in ativos if a['cliente_id'] == cid), None)
ok(meu is not None, "assinante aparece na lista de ativos")
ok(len(meu['servicos']) >= 1, f"serviços do plano expostos ({len(meu['servicos'])})")

print("== registra 2 visitas (hoje e retroativa) ==")
r1 = j(app.post('/api/visitas/registrar', json={'cliente_id': cid, 'profissional_id': barb['id']}))
ok(r1['ok'], "visita hoje registrada")
ontem = (date.today() - timedelta(days=1)).isoformat()
r2 = j(app.post('/api/visitas/registrar', json={'cliente_id': cid, 'profissional_id': barb['id'], 'data': ontem}))
ok(r2['ok'], "visita retroativa (ontem) registrada")
# futura deve falhar
rf = app.post('/api/visitas/registrar', json={'cliente_id': cid, 'profissional_id': barb['id'],
                                              'data': (date.today() + timedelta(days=2)).isoformat()})
ok(rf.status_code == 400, "visita futura bloqueada")

print("== relatório de uso dos assinantes ==")
uso = j(app.get('/api/relatorios/assinantes-uso'))
linha = next((x for x in uso['rows'] if x['cliente_id'] == cid), None)
ok(linha is not None, "assinante aparece no uso")
ok(linha and linha['visitas'] == 2, f"2 visitas no mês (got {linha['visitas'] if linha else '?'})")
ok(linha and linha['total_visitas'] == 2, "total histórico = 2")
ok(linha and linha['custo_por_visita'] is not None, f"custo/visita calculado ({linha['custo_por_visita'] if linha else '?'})")
ok(linha and linha['barbeiros'] and linha['barbeiros'][0]['qtd'] == 2, "distribuição por barbeiro = 2")
ok(uso['resumo']['visitas'] >= 2, "resumo total de visitas")
ok(uso['resumo']['margem'] == round(uso['resumo']['arrecadacao'] - uso['resumo']['bolo'], 2), "margem = arrecadação − bolo")

print("== histórico de visitas do assinante ==")
hist = j(app.get(f'/api/assinantes/{cid}/visitas'))['rows']
ok(len(hist) == 2, f"histórico com 2 visitas (got {len(hist)})")

print("== comissão: barbeiro leva o bolo pelas visitas ==")
com = j(app.get('/api/relatorios/comissao'))
brow = next((x for x in com['rows'] if x['profissional_id'] == barb['id']), None)
ok(brow is not None and brow['atend'] >= 2, f"barbeiro com >=2 atendimentos de plano no bolo (got {brow['atend'] if brow else '?'})")

print("== DRE aprimorado ==")
dre = j(app.get('/api/relatorios/dre'))
ok('servicos_detalhe' in dre['receitas'], "DRE traz detalhe de serviços")
ok('produtos_detalhe' in dre['receitas'], "DRE traz detalhe de produtos")
ok(dre['visitas_plano'] >= 2, f"DRE conta visitas de plano ({dre['visitas_plano']})")
ok('comparativo' in dre and 'resultado' in dre['comparativo'], "DRE traz comparativo")
ok('ticket_medio' in dre and 'atendimentos' in dre, "DRE traz ticket médio e atendimentos")
# assinatura entra na receita (1ª mensalidade); visitas R$0 não inflam serviços
ok(dre['receitas']['assinaturas'] >= float(planos[0]['valor_mensal']), "receita de assinatura no DRE")

print("== série mensal (evolução) ==")
serie = j(app.get('/api/relatorios/dre-serie?meses=6'))['serie']
ok(len(serie) == 6, f"série com 6 meses (got {len(serie)})")
ok(all('receita' in s and 'despesa' in s and 'resultado' in s for s in serie), "série com receita/despesa/resultado")

print("== limpeza do resíduo ==")
# visitas de hoje e de ontem -> cancelar as comandas; assinatura -> cancelar (remove movimento)
for dia_ref in (date.today(), date.today() - timedelta(days=1)):
    for c in j(app.get(f'/api/comandas/fechadas?data={dia_ref.isoformat()}'))['rows']:
        if c['cliente_id'] == cid:
            app.post(f"/api/comandas/{c['id']}/cancelar")
if asid:
    app.put(f'/api/assinaturas/{asid}', json={'status': 'cancelada'})
delr = app.delete(f'/api/clientes/{cid}')
ok(j(delr).get('ok'), "cliente de teste removido")
# remove o barbeiro de teste de vez (FKs em comandas/itens são ON DELETE SET NULL)
server.execute("DELETE FROM profissionais WHERE id=%s", (barb['id'],))
ok(True, "barbeiro de teste removido")

print()
if FALHAS:
    print(f"XX {len(FALHAS)} FALHA(S):")
    for f in FALHAS:
        print("   -", f)
    raise SystemExit(1)
print("OK — todos os checks passaram.")
