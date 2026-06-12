"""Smoke test end-to-end da JOGA Barbearia. Limpa o resíduo no fim."""
import os, json, urllib.request, urllib.error, http.cookiejar
from datetime import date
import psycopg2
from dotenv import load_dotenv

load_dotenv()
BASE = 'http://localhost:5000'
cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))


def call(method, path, body=None, expect_ok=True):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header('Content-Type', 'application/json')
    try:
        r = op.open(req, timeout=10); raw = r.read().decode(); st = r.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode(); st = e.code
    j = json.loads(raw)
    flag = 'OK ' if j.get('ok') else ('OK ' if not expect_ok else 'ERR')
    print(f"  [{flag}] {method:4} {path:46} -> {st}")
    return j


def db():
    return psycopg2.connect(host=os.getenv('DB_HOST'), port=os.getenv('DB_PORT'),
        dbname=os.getenv('DB_NAME'), user=os.getenv('DB_USER'), password=os.getenv('DB_PASSWORD'))


# libera troca de senha do caixa pra testar
c = db(); cur = c.cursor()
cur.execute("UPDATE usuarios SET must_change_password=false WHERE email IN ('caixa@barbearia.local','dono@barbearia.local')")
c.commit(); cur.close(); c.close()

hoje = date.today()

print("== Auth (caixa) ==")
call('POST', '/api/login', {'email': 'caixa@barbearia.local', 'senha': 'joga123'})
call('GET', '/api/me')

print("== Cadastro de cliente (universal) ==")
cli = call('POST', '/api/clientes', {'nome': '[SMOKE] João Teste', 'telefone': '27999990000', 'tipo': 'universal'})
cli_id = cli['row']['id']

print("== Dados base ==")
profs = call('GET', '/api/profissionais')['rows']
servs = {s['nome']: s for s in call('GET', '/api/servicos')['rows']}
prof_id = profs[0]['id']
cabelo = servs['Cabelo']; barba = servs['Barba']

print("== Comanda walk-in: Cabelo + Barba, fecha no Pix ==")
com = call('POST', '/api/comandas', {'profissional_id': prof_id, 'cliente_id': cli_id})
com_id = com['id']
call('POST', f'/api/comandas/{com_id}/itens', {'tipo': 'servico', 'ref_id': cabelo['id']})
call('POST', f'/api/comandas/{com_id}/itens', {'tipo': 'servico', 'ref_id': barba['id']})
prod = call('GET', '/api/produtos')['rows'][0]
call('POST', f'/api/comandas/{com_id}/itens', {'tipo': 'produto', 'ref_id': prod['id']})
fech = call('POST', f'/api/comandas/{com_id}/fechar', {'forma_pagamento': 'Pix'})
esperado = cabelo['preco'] + barba['preco'] + prod['preco']
print(f"     -> total={fech['valor_total']} (esperado {esperado})")

print("== Assinante: cria assinatura no plano (Cabelo seg-qua) e testa cobertura ==")
plano = call('GET', '/api/planos')['rows'][0]
assin = call('POST', '/api/assinaturas', {'cliente_id': cli_id, 'plano_id': plano['id'], 'dia_vencimento': 10})
com2 = call('POST', '/api/comandas', {'profissional_id': prof_id, 'cliente_id': cli_id})['id']
add = call('POST', f'/api/comandas/{com2}/itens', {'tipo': 'servico', 'ref_id': cabelo['id']})
det = call('GET', f'/api/comandas/aberta?id={com2}')
item = det['itens'][0]
dia_semana = (hoje.weekday() + 1) % 7
coberto_esperado = dia_semana in [1, 2, 3]
print(f"     -> hoje js_weekday={dia_semana} coberto={item['coberto_plano']} (esperado {coberto_esperado}); preco_unit={item['preco_unit']} tabela={item['preco_tabela']}")
call('POST', f'/api/comandas/{com2}/fechar', {'forma_pagamento': 'Dinheiro'})

print("== Agenda do dia ==")
ag = call('GET', f'/api/agenda?data={hoje.isoformat()}')
print(f"     -> profissionais={len(ag['profissionais'])} horario_dia={ag['horario_dia']}")

print("== Caixa: fechamento do dia ==")
fx = call('GET', f'/api/caixa/fechamento?data={hoje.isoformat()}')
print(f"     -> receita={fx['total_receita']} atendimentos={fx['atendimentos']} formas={[(p['forma'],p['total']) for p in fx['por_forma']]}")

print("== Cobranças de assinatura (previsto) ==")
gc = call('POST', '/api/assinaturas/gerar-cobrancas', {'competencia': f'{hoje.year}-{hoje.month:02d}'})
print(f"     -> cobrancas geradas={gc['gerados']}")

print("== Relatório de comissão (quinzena atual) ==")
de = hoje.replace(day=1) if hoje.day <= 15 else hoje.replace(day=16)
rc = call('GET', f'/api/relatorios/comissao?de={de.isoformat()}&ate={hoje.isoformat()}')
print(f"     -> total comissao={rc['total']} por barbeiro={[(r['profissional'],r['comissao']) for r in rc['rows']]}")

print("== Limpeza ==")
c = db(); cur = c.cursor()
cur.execute("DELETE FROM comanda_itens WHERE comanda_id IN (SELECT id FROM comandas WHERE cliente_id=%s)", (cli_id,))
cur.execute("DELETE FROM movimentos WHERE descricao LIKE '%%João Teste%%' OR ref_id IN (SELECT id FROM comandas WHERE cliente_id=%s)", (cli_id,))
cur.execute("DELETE FROM comandas WHERE cliente_id=%s", (cli_id,))
cur.execute("DELETE FROM assinaturas WHERE cliente_id=%s", (cli_id,))
cur.execute("DELETE FROM clientes WHERE id=%s", (cli_id,))
cur.execute("UPDATE usuarios SET must_change_password=true")
c.commit(); cur.close(); c.close()
print("     [OK] resíduo removido.")
print("\n== SMOKE CONCLUÍDO ==")
