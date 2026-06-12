"""Testa agendamento com múltiplos serviços + pré-carga na comanda."""
import os, json, urllib.request, urllib.error, http.cookiejar
from datetime import date
import psycopg2
from dotenv import load_dotenv

load_dotenv()
BASE = 'http://localhost:5002'
cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

def call(m, p, b=None):
    data = json.dumps(b).encode() if b is not None else None
    req = urllib.request.Request(BASE + p, data=data, method=m); req.add_header('Content-Type', 'application/json')
    try: r = op.open(req, timeout=10); return json.loads(r.read().decode())
    except urllib.error.HTTPError as e: return json.loads(e.read().decode())

def db():
    return psycopg2.connect(host=os.getenv('DB_HOST'), port=os.getenv('DB_PORT'), dbname=os.getenv('DB_NAME'),
        user=os.getenv('DB_USER'), password=os.getenv('DB_PASSWORD'))

c = db(); cur = c.cursor()
cur.execute("UPDATE usuarios SET must_change_password=false WHERE email='caixa@barbearia.local'")
c.commit(); cur.close(); c.close()

hoje = date.today()
call('POST', '/api/login', {'email': 'caixa@barbearia.local', 'senha': 'joga123'})
servs = {s['nome']: s for s in call('GET', '/api/servicos')['rows']}
prof = call('GET', '/api/profissionais')['rows'][0]['id']
ids = [servs['Cabelo']['id'], servs['Sobrancelha']['id']]

print("== Agendar Cabelo + Sobrancelha (2 serviços) ==")
ag = call('POST', '/api/agendamentos', {'profissional_id': prof, 'data': hoje.isoformat(), 'hora_inicio': '09:00', 'servicos_ids': ids})
print("   agendamento:", ag)

ja = call('GET', f'/api/agenda?data={hoje.isoformat()}')
alvo = next((a for a in ja['agendamentos'] if a['id'] == ag['id']), None)
print(f"   -> servicos_nomes no card: {alvo['servicos_nomes']} · duracao_slots={alvo['duracao_slots']} (esperado 2 serviços, 2 slots p/ 45min)")

print("== Abrir comanda do agendamento → deve vir com os 2 serviços ==")
com = call('POST', '/api/comandas', {'agendamento_id': ag['id']})
det = call('GET', f"/api/comandas/aberta?id={com['id']}")
print(f"   -> itens pré-carregados: {[i['descricao'] for i in det['itens']]} (esperado Cabelo, Sobrancelha)")
print(f"   -> total comanda: {det['comanda']['valor_total']} (esperado 54.00 = 36 + 18)")

print("== Limpeza ==")
c = db(); cur = c.cursor()
cur.execute("DELETE FROM comanda_itens WHERE comanda_id=%s", (com['id'],))
cur.execute("DELETE FROM comandas WHERE id=%s", (com['id'],))
cur.execute("DELETE FROM agendamentos WHERE id=%s", (ag['id'],))
cur.execute("UPDATE usuarios SET must_change_password=true")
c.commit(); cur.close(); c.close()
print("   [OK] limpo.")
