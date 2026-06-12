"""Popula a instância com os dados reais da barbearia (entrega pré-configurada).
Rode após init_db.py: python -X utf8 seed_barbearia.py
Idempotente: pula seções já populadas.

Convenção de dias da semana: 0=domingo .. 6=sábado (igual JS getDay)."""
import os
import json
import psycopg2
from werkzeug.security import generate_password_hash
from dotenv import load_dotenv

load_dotenv()
SENHA = os.getenv('SEED_SENHA_INICIAL', 'joga123')

conn = psycopg2.connect(
    host=os.getenv('DB_HOST', 'localhost'), port=os.getenv('DB_PORT', '5432'),
    dbname=os.getenv('DB_NAME', 'joga_barbearia'),
    user=os.getenv('DB_USER', 'postgres'), password=os.getenv('DB_PASSWORD', ''),
)
cur = conn.cursor()


def vazia(tabela):
    cur.execute(f"SELECT COUNT(*) FROM {tabela}")
    return cur.fetchone()[0] == 0


# ── Profissionais ─────────────────────────────────────────────────────
# Regiane está sozinha (dona E barbeira). João Victor é o barbeiro de teste/treino.
if vazia('profissionais'):
    barbeiros = [('Regiane Vieira Zava', '#38bdf8'), ('João Victor', '#34d399')]
    for nome, cor in barbeiros:
        cur.execute("INSERT INTO profissionais (nome, comissao_pct, cor_agenda) VALUES (%s,45,%s)", (nome, cor))
    print(f"[OK] {len(barbeiros)} barbeiros criados (Regiane, João Victor).")

# ── Usuários ──────────────────────────────────────────────────────────
if vazia('usuarios'):
    def prof_id(nome):
        cur.execute("SELECT id FROM profissionais WHERE nome=%s", (nome,))
        r = cur.fetchone()
        return r[0] if r else None

    # Regiane: DONA (acesso total) e também BARBEIRA (vinculada ao perfil dela)
    cur.execute("""INSERT INTO usuarios (nome, email, password_hash, role, profissional_id)
                   VALUES (%s,%s,%s,'dono',%s)""",
                ('Regiane Vieira Zava', 'regiane@barbearia.local', generate_password_hash(SENHA),
                 prof_id('Regiane Vieira Zava')))
    # João Victor: barbeiro
    cur.execute("""INSERT INTO usuarios (nome, email, password_hash, role, profissional_id)
                   VALUES (%s,%s,%s,'barbeiro',%s)""",
                ('João Victor', 'joaovictor@barbearia.local', generate_password_hash(SENHA),
                 prof_id('João Victor')))
    print("[OK] Usuarios: regiane@barbearia.local (dona+barbeira) / joaovictor@barbearia.local (barbeiro) — senha:", SENHA)

# ── Serviços ──────────────────────────────────────────────────────────
if vazia('servicos'):
    servicos = [
        ('Cabelo', 36.00, 30),
        ('Barba', 29.00, 30),
        ('Cabelo + Barba', 65.00, 60),
        ('Infantil', 40.00, 30),
        ('Acabamento', 25.00, 15),
        ('Sobrancelha', 18.00, 15),
    ]
    cur.executemany("INSERT INTO servicos (nome, preco, duracao_min) VALUES (%s,%s,%s)", servicos)
    print(f"[OK] {len(servicos)} servicos criados.")

# ── Produtos ──────────────────────────────────────────────────────────
if vazia('produtos'):
    produtos = [
        ('Pomada', 35.00),
        ('Pomada fixação forte', 40.00),
        ('Balm', 45.00),
        ('Leave-in', 48.00),
        ('Shampoo', 50.00),
        ('Sabonete íntimo masculino', 48.00),
        ('Minoxidil', 70.00),
    ]
    cur.executemany("INSERT INTO produtos (nome, preco) VALUES (%s,%s)", produtos)
    print(f"[OK] {len(produtos)} produtos criados.")

# ── Planos de assinatura (3 planos, ilimitado seg–qua) ────────────────
if vazia('planos'):
    def serv_id(nome):
        cur.execute("SELECT id FROM servicos WHERE nome = %s LIMIT 1", (nome,))
        r = cur.fetchone()
        return r[0] if r else None

    DIAS = json.dumps([1, 2, 3])  # seg, ter, qua
    planos = [
        ('Plano Cabelo', 100.00, ['Cabelo']),
        ('Plano Barba', 100.00, ['Barba']),
        ('Plano Cabelo + Barba', 150.00, ['Cabelo', 'Barba']),
    ]
    for nome, valor, servicos in planos:
        cur.execute("""INSERT INTO planos (nome, valor_mensal, limite_uso, dias_inclusos, comissao_assinante_regra)
                       VALUES (%s,%s,NULL,%s,'tabela') RETURNING id""", (nome, valor, DIAS))
        pid = cur.fetchone()[0]
        for sn in servicos:
            sid = serv_id(sn)
            if sid:
                cur.execute("INSERT INTO plano_servicos (plano_id, servico_id) VALUES (%s,%s)", (pid, sid))
    print("[OK] 3 planos criados (Cabelo 100, Barba 100, Cabelo+Barba 150) — ilimitado seg-qua.")

# ── Horários de funcionamento (config) ────────────────────────────────
horarios = {
    "0": None,                                  # domingo: fechado
    "1": {"abre": "08:00", "fecha": "19:00"},   # seg
    "2": {"abre": "08:00", "fecha": "19:00"},   # ter
    "3": {"abre": "08:00", "fecha": "19:00"},   # qua
    "4": {"abre": "08:00", "fecha": "19:00"},   # qui
    "5": {"abre": "08:00", "fecha": "19:00"},   # sex
    "6": {"abre": "07:30", "fecha": "16:00"},   # sáb
}
cur.execute("UPDATE configuracoes SET horarios = %s WHERE id = 1", (json.dumps(horarios),))
print("[OK] Horarios configurados (Seg-Sex 8-19, Sab 7:30-16, Dom fechado).")

conn.commit()
cur.close()
conn.close()
print("[OK] Seed concluido. Rode: python -X utf8 server.py")
