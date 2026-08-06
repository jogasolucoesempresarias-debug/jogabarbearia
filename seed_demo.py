"""Popula a INSTÂNCIA DE DEMONSTRAÇÃO (demobarbearia.jogasolucoes.com.br) com uma barbearia
fictícia completa: equipe, planos, ~90 dias de atendimento, assinantes com padrões de uso
diferentes, despesas e uma quinzena de comissão já fechada.

Roda também como RESET (é o mesmo script): apaga tudo e refaz. O cron da madrugada chama ele.

    docker exec $(docker ps -q -f name=demobarbearia) python -X utf8 seed_demo.py

TODAS as datas são relativas a HOJE. Data fixa faria a demo parecer abandonada em dois meses —
o prospect abriria o Resultado e veria o mês corrente vazio.

⚠️  ESTE SCRIPT APAGA DADOS. Ele se recusa a rodar se MODO_DEMO não estiver ligado, justamente
    pra nunca ser executado por engano na instância de um cliente.
"""
import os
import random
from datetime import date, datetime, timedelta

import psycopg2
from psycopg2.extras import Json
from werkzeug.security import generate_password_hash
from dotenv import load_dotenv

load_dotenv()

if os.getenv('MODO_DEMO', '').strip().lower() not in ('1', 'true', 'sim'):
    raise SystemExit(
        "[ABORTADO] seed_demo.py apaga todos os dados e só roda em instância de demonstração.\n"
        "           Se esta É a demo, defina MODO_DEMO=1 no ambiente.")

SENHA_DEMO = os.getenv('SENHA_DEMO', 'demo')
# 180 e não 90: o Resultado tem um gráfico de EVOLUÇÃO DE 6 MESES. Com 90 dias, dois meses
# apareciam zerados no gráfico — a tela que deveria impressionar mostrava metade vazia.
DIAS_HISTORICO = int(os.getenv('DEMO_DIAS', '180'))    # DEMO_DIAS menor deixa o smoke rápido
random.seed(20260806)          # mesma barbearia a cada reset
hoje = date.today()

conn = psycopg2.connect(
    host=os.getenv('DB_HOST', 'localhost'), port=os.getenv('DB_PORT', '5432'),
    dbname=os.getenv('DB_NAME', 'barbearia_demo'),
    user=os.getenv('DB_USER', 'postgres'), password=os.getenv('DB_PASSWORD', ''),
    options='-c timezone=America/Sao_Paulo',
)
cur = conn.cursor()


def um(sql, params=None):
    cur.execute(sql, params or ())
    return cur.fetchone()[0]


def meses_atras(d, n):
    """(ano, mes) de n meses atrás. Andar de 30 em 30 dias erra e às vezes repete o mesmo mês."""
    ano, mes = d.year, d.month - n
    while mes <= 0:
        mes += 12
        ano -= 1
    return ano, mes


def proxima_cobranca(venc):
    """Próximo vencimento a partir de hoje — se o dia ainda não passou, é neste mês mesmo."""
    deste_mes = date(hoje.year, hoje.month, min(venc, 28))
    if deste_mes > hoje:
        return deste_mes
    ano, mes = (hoje.year + 1, 1) if hoje.month == 12 else (hoje.year, hoje.month + 1)
    return date(ano, mes, min(venc, 28))


# ── Limpeza ───────────────────────────────────────────────────────────
# setup_coleta e _migracoes ficam DE FORA de propósito: fichas de prospect e o controle de
# migração não são dado de demonstração e nunca devem ser derrubados por um reset.
cur.execute("""
    TRUNCATE comissoes_pagas, movimentos, comanda_itens, comandas, agendamentos, bloqueios,
             assinaturas, plano_servicos, planos, produtos, servicos, clientes, despesas_fixas,
             usuarios, profissionais
    RESTART IDENTITY CASCADE;
""")
print("[OK] base limpa (fichas de coleta preservadas)")

# ── Equipe ────────────────────────────────────────────────────────────
# O dono atende mas NÃO recebe comissão — é a regra que mais gera dúvida na venda, então a demo
# precisa mostrar ela funcionando.
EQUIPE = [
    ('José Carlos Ferreira', 45, '#38bdf8', False),   # dono
    ('Rafael Moura',         45, '#34d399', True),
    ('Diego Nunes',          40, '#fbbf24', True),
]
prof_id = {}
for nome, pct, cor, recebe in EQUIPE:
    prof_id[nome] = um("""INSERT INTO profissionais (nome, comissao_pct, cor_agenda, recebe_comissao)
                          VALUES (%s,%s,%s,%s) RETURNING id""", (nome, pct, cor, recebe))

senha = generate_password_hash(SENHA_DEMO)
cur.execute("""INSERT INTO usuarios (nome, email, password_hash, role, profissional_id, must_change_password)
               VALUES (%s,'ze@barbearia.local',%s,'dono',%s,false)""",
            ('José Carlos Ferreira', senha, prof_id['José Carlos Ferreira']))
cur.execute("""INSERT INTO usuarios (nome, email, password_hash, role, profissional_id, must_change_password)
               VALUES (%s,'rafael@barbearia.local',%s,'barbeiro',%s,false)""",
            ('Rafael Moura', senha, prof_id['Rafael Moura']))
print(f"[OK] 3 barbeiros · logins ze@ (dono) e rafael@ (barbeiro) · senha '{SENHA_DEMO}'")

# ── Serviços e produtos ───────────────────────────────────────────────
SERVICOS = [('Cabelo', 36, 30), ('Barba', 29, 30), ('Cabelo + Barba', 65, 60),
            ('Infantil', 40, 30), ('Acabamento', 25, 15), ('Sobrancelha', 18, 15)]
serv_id, serv_preco, serv_dur = {}, {}, {}
for nome, preco, dur in SERVICOS:
    serv_id[nome] = um("INSERT INTO servicos (nome, preco, duracao_min) VALUES (%s,%s,%s) RETURNING id",
                       (nome, preco, dur))
    serv_preco[nome], serv_dur[nome] = preco, dur

PRODUTOS = [('Pomada', 35), ('Pomada fixação forte', 40), ('Balm', 45),
            ('Shampoo', 50), ('Minoxidil', 70)]
prod_id = {}
for nome, preco in PRODUTOS:
    prod_id[nome] = um("INSERT INTO produtos (nome, preco) VALUES (%s,%s) RETURNING id", (nome, preco))

# ── Planos (regras DIFERENTES de propósito) ───────────────────────────
# É o recurso mais difícil de explicar por telefone e o mais fácil de mostrar na tela.
DIAS_PLANO = [2, 3, 4]                                  # ter, qua, qui
plano_cabelo = um("""INSERT INTO planos (nome, valor_mensal, dias_inclusos, comissao_assinante_regra)
                     VALUES ('Plano Cabelo',120,%s,'bolo') RETURNING id""", (Json(DIAS_PLANO),))
plano_completo = um("""INSERT INTO planos (nome, valor_mensal, dias_inclusos,
                           comissao_assinante_regra, comissao_assinante_valor)
                       VALUES ('Plano Completo',180,%s,'fixo',18) RETURNING id""", (Json(DIAS_PLANO),))
for pid, nomes in [(plano_cabelo, ['Cabelo']), (plano_completo, ['Cabelo', 'Barba'])]:
    for n in nomes:
        cur.execute("INSERT INTO plano_servicos (plano_id, servico_id) VALUES (%s,%s)", (pid, serv_id[n]))
plano_valor = {plano_cabelo: 120, plano_completo: 180}
print("[OK] 6 serviços · 5 produtos · 2 planos (um 'bolo', um 'fixo')")

# ── Configurações da casa ─────────────────────────────────────────────
HORARIOS = {"0": None,
            "1": {"abre": "09:00", "fecha": "20:00"}, "2": {"abre": "09:00", "fecha": "20:00"},
            "3": {"abre": "09:00", "fecha": "20:00"}, "4": {"abre": "09:00", "fecha": "20:00"},
            "5": {"abre": "09:00", "fecha": "20:00"}, "6": {"abre": "08:00", "fecha": "17:00"}}
cur.execute("""UPDATE configuracoes SET marca_nome='Barbearia do Zé', comissao_padrao=45,
               horarios=%s WHERE id=1""", (Json(HORARIOS),))

# ── Clientes ──────────────────────────────────────────────────────────
NOMES = [
    'ANDRÉ LUIZ SANTOS', 'BRUNO CARVALHO', 'CARLOS EDUARDO LIMA', 'DANIEL ROCHA', 'EDUARDO PIRES',
    'FÁBIO MENEZES', 'GABRIEL ANDRADE', 'HENRIQUE BORGES', 'IGOR SAMPAIO', 'JOÃO PEDRO ALVES',
    'KAIO FERNANDES', 'LEONARDO DIAS', 'MARCELO TAVARES', 'NÍCOLAS PRADO', 'OTÁVIO CAMPOS',
    'PAULO RICARDO SILVA', 'QUEZIA MARTINS', 'RAFAEL GOMES', 'SAMUEL OLIVEIRA', 'THIAGO MENDES',
    'ULISSES BARROS', 'VINÍCIUS COSTA', 'WESLEY NOGUEIRA', 'YAN CORREIA', 'ADRIANO PEIXOTO',
    'BENÍCIO RAMOS', 'CAIO VIANA', 'DIOGO FONSECA', 'EMERSON PAIVA', 'FELIPE AZEVEDO',
    'GUSTAVO MOREIRA', 'HUGO SIQUEIRA', 'ÍTALO BATISTA', 'JÚLIO CÉSAR NUNES', 'KEVIN DUARTE',
    'LUCAS TEIXEIRA', 'MATEUS FARIA', 'NELSON QUEIROZ', 'ORLANDO PINHEIRO', 'PEDRO HENRIQUE SÁ',
    'RENATO CAMARGO', 'SÉRGIO BRANDÃO', 'TALES MACHADO', 'VITOR HUGO REIS', 'WALTER ASSIS',
]
cliente_ids = []
for i, nome in enumerate(NOMES):
    tel = f"(34) 9{random.randint(8000, 9999)}-{random.randint(1000, 9999)}"
    cliente_ids.append(um("INSERT INTO clientes (nome, telefone) VALUES (%s,%s) RETURNING id", (nome, tel)))
print(f"[OK] {len(cliente_ids)} clientes")

# ── Assinantes, com padrões de uso DIFERENTES ─────────────────────────
# É o que faz a tela de Uso dos Planos ter o que mostrar: quem usa demais, quem sumiu, e quem
# paga e nunca aparece (o assinante mais lucrativo — e o argumento de venda da tela).
PADROES = [('intenso', 7), ('intenso', 7), ('regular', 15), ('regular', 15),
           ('regular', 18), ('regular', 21), ('dormente', None), ('dormente', None),
           ('nunca', None), ('nunca', None)]
assinantes = []
for i, (padrao, cadencia) in enumerate(PADROES):
    cid = cliente_ids[i]
    plano = plano_completo if i % 3 == 0 else plano_cabelo
    venc = 10 if i % 2 == 0 else 30
    inicio = hoje - timedelta(days=random.randint(120, 210))
    proxima = proxima_cobranca(venc)
    aid = um("""INSERT INTO assinaturas (cliente_id, plano_id, dia_vencimento, data_inicio, proxima_cobranca)
                VALUES (%s,%s,%s,%s,%s) RETURNING id""", (cid, plano, venc, inicio, proxima))
    assinantes.append({'assinatura_id': aid, 'cliente_id': cid, 'plano_id': plano,
                       'padrao': padrao, 'cadencia': cadencia})

    # Mensalidades de 6 meses até a atual. Incluir o MÊS CORRENTE é essencial: sem ela a
    # arrecadação do mês fica zero, o bolo da equipe zera e o Uso dos Planos mostra margem
    # negativa — justamente as telas que a demo existe pra vender.
    for m in range(6, -1, -1):
        ano, mes = meses_atras(hoje, m)
        d = date(ano, mes, min(venc, 28))
        if d < inicio:
            continue
        pago, data_mov = d <= hoje, d
        # Vencimento só pode ser dia 10 ou 30 (regra do produto). Sem isto, do dia 1 ao 9 de
        # todo mês a demo abriria com arrecadação de planos ZERO — e o bolo da equipe, que é o
        # que a gente quer mostrar, apareceria zerado. Parte dos assinantes paga adiantado,
        # que é o que acontece na vida real com quem deixa no automático.
        if not pago and (d.year, d.month) == (hoje.year, hoje.month) and random.random() < 0.55:
            pago, data_mov = True, hoje - timedelta(days=random.randint(0, 4))
        cur.execute("""INSERT INTO movimentos (tipo, origem, ref_id, descricao, valor, data,
                           vencimento, status, forma_pagamento)
                       VALUES ('receita','assinatura',%s,%s,%s,%s,%s,%s,%s)""",
                    (aid, f"Mensalidade — {NOMES[i]}", plano_valor[plano], data_mov, d,
                     'pago' if pago else 'previsto', 'Pix' if pago else None))
print(f"[OK] {len(assinantes)} assinantes (intenso, regular, dormente e os que nunca vêm)")

# ── Motor de atendimento ──────────────────────────────────────────────
PESO_SERVICO = (['Cabelo'] * 45 + ['Cabelo + Barba'] * 25 + ['Barba'] * 15 +
                ['Infantil'] * 7 + ['Acabamento'] * 5 + ['Sobrancelha'] * 3)
PESO_PROF = ([prof_id['José Carlos Ferreira']] * 40 + [prof_id['Rafael Moura']] * 35 +
             [prof_id['Diego Nunes']] * 25)
PESO_PAGTO = ['Pix'] * 45 + ['Cartão'] * 35 + ['Dinheiro'] * 20
# Movimento por dia da semana: segunda é fraca, sexta e sábado lotam. Demo com fluxo chapado
# não parece barbearia de verdade.
POR_DIA = {0: 0, 1: (8, 12), 2: (12, 17), 3: (12, 17), 4: (14, 19), 5: (20, 27), 6: (24, 32)}
pct_prof = {prof_id[n]: p for n, p, _, _ in EQUIPE}


def js_weekday(d):
    return (d.weekday() + 1) % 7


def abre_fecha(d):
    h = HORARIOS[str(js_weekday(d))]
    return (int(h['abre'][:2]), int(h['fecha'][:2])) if h else (None, None)


def slots_do_dia(d):
    """Grade de 30min por barbeiro. Reservar slot evita dois clientes na mesma casinha da agenda."""
    abre, fecha = abre_fecha(d)
    if abre is None:
        return None
    grade = [(h, m) for h in range(abre, fecha) for m in (0, 30)]
    return {pid: list(grade) for pid in prof_id.values()}


def pega_slot(livres, pid):
    if not livres[pid]:
        return None
    return livres[pid].pop(random.randrange(len(livres[pid])))


def nova_comanda(d, pid, cliente, hora, minuto, forma, total, duracao, agendamento_id=None):
    ini = datetime.combine(d, datetime.min.time()).replace(hour=hora, minute=minuto)
    fim = ini + timedelta(minutes=duracao)
    return um("""INSERT INTO comandas (cliente_id, profissional_id, agendamento_id, status,
                     forma_pagamento, valor_total, aberta_em, fechada_em)
                 VALUES (%s,%s,%s,'fechada',%s,%s,%s,%s) RETURNING id""",
              (cliente, pid, agendamento_id, forma, total, ini, fim))


def novo_agendamento(d, pid, cliente, hora, minuto, servico, status, origem='agenda'):
    return um("""INSERT INTO agendamentos (profissional_id, cliente_id, servico_id, data, hora_inicio,
                     duracao_slots, status, origem, servicos_ids)
                 VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
              (pid, cliente, serv_id[servico], d, f"{hora:02d}:{minuto:02d}",
               2 if serv_dur[servico] > 30 else 1, status, origem, Json([serv_id[servico]])))


comandas_criadas = 0
receita_lancada = 0
agendados = 0
LIVRES = {}          # slots ainda livres por dia → reaproveitado pelas visitas de assinante
agora_h = datetime.now().hour

# 1) Atendimento do dia a dia — cada um deixa RASTRO NA AGENDA
# Sem isso o prospect clica em "Ontem" e vê a agenda vazia, enquanto o Resultado diz que a
# barbearia atendeu milhares de pessoas. Parte vem sem hora marcada, como na vida real.
for delta in range(DIAS_HISTORICO, -1, -1):
    d = hoje - timedelta(days=delta)
    faixa = POR_DIA[js_weekday(d)]
    if not faixa:
        continue
    livres = slots_do_dia(d)
    LIVRES[d] = livres
    for _ in range(random.randint(*faixa)):
        pid = random.choice(PESO_PROF)
        slot = pega_slot(livres, pid)
        if not slot:                      # dia lotado pra esse barbeiro
            continue
        hora, minuto = slot
        if d == hoje and hora > agora_h:  # hoje só conta o que já passou
            continue
        cliente = random.choice(cliente_ids)
        servico = random.choice(PESO_SERVICO)
        forma = random.choice(PESO_PAGTO)
        total = serv_preco[servico]
        produto = random.choice(PRODUTOS) if random.random() < 0.12 else None
        if produto:
            total += produto[1]

        # 70% marcou hora; o resto entrou sem hora marcada (walk-in)
        ag = None
        if random.random() < 0.70:
            ag = novo_agendamento(d, pid, cliente, hora, minuto, servico, 'atendido')
            agendados += 1

        com = nova_comanda(d, pid, cliente, hora, minuto, forma, total, serv_dur[servico], ag)
        cur.execute("""INSERT INTO comanda_itens (comanda_id, tipo, ref_id, descricao, profissional_id,
                           preco_unit, qtd, subtotal, preco_tabela, coberto_plano)
                       VALUES (%s,'servico',%s,%s,%s,%s,1,%s,%s,false)""",
                    (com, serv_id[servico], servico, pid, serv_preco[servico],
                     serv_preco[servico], serv_preco[servico]))
        if produto:
            cur.execute("""INSERT INTO comanda_itens (comanda_id, tipo, ref_id, descricao,
                               profissional_id, preco_unit, qtd, subtotal, coberto_plano)
                           VALUES (%s,'produto',%s,%s,%s,%s,1,%s,false)""",
                        (com, prod_id[produto[0]], produto[0], pid, produto[1], produto[1]))
        cur.execute("""INSERT INTO movimentos (tipo, origem, ref_id, descricao, valor,
                           forma_pagamento, data, status)
                       VALUES ('receita','comanda',%s,%s,%s,%s,%s,'pago')""",
                    (com, f"Comanda #{com}", total, forma, d))
        comandas_criadas += 1
        receita_lancada += total

# 2) Visitas dos assinantes (só nos dias do plano, com o benefício aplicado)
visitas_assinante = 0
for a in assinantes:
    if a['padrao'] == 'nunca':
        continue
    if a['padrao'] == 'dormente':
        # sumiu: última visita entre 40 e 70 dias atrás
        dias = [random.randint(40, 70) + 7 * k for k in range(3)]
    else:
        dias = list(range(3, DIAS_HISTORICO, a['cadencia']))
    servico = 'Cabelo' if a['plano_id'] == plano_cabelo else random.choice(['Cabelo', 'Barba'])
    for delta in dias:
        d = hoje - timedelta(days=delta)
        # O plano só cobre ter/qua/qui. Como a cadência anda de 7 em 7 (mesmo dia da semana
        # sempre), sem esse encaixe um assinante inteiro poderia não gerar visita NENHUMA.
        for k in range(4):
            if js_weekday(d + timedelta(days=k)) in DIAS_PLANO:
                d = d + timedelta(days=k)
                break
        else:
            continue
        if d > hoje or d < hoje - timedelta(days=DIAS_HISTORICO) or d not in LIVRES:
            continue
        pid = random.choice(PESO_PROF)
        slot = pega_slot(LIVRES[d], pid)
        if not slot:
            continue
        hora, minuto = slot
        if d == hoje and hora > agora_h:
            continue
        # Assinante marca hora — é o cliente mais fiel da casa.
        ag = novo_agendamento(d, pid, a['cliente_id'], hora, minuto, servico, 'atendido')
        agendados += 1
        com = nova_comanda(d, pid, a['cliente_id'], hora, minuto, None, 0, serv_dur[servico], ag)
        cur.execute("""INSERT INTO comanda_itens (comanda_id, tipo, ref_id, descricao, profissional_id,
                           preco_unit, qtd, subtotal, preco_tabela, coberto_plano, assinatura_id)
                       VALUES (%s,'servico',%s,%s,%s,0,1,0,%s,true,%s)""",
                    (com, serv_id[servico], servico, pid, serv_preco[servico], a['assinatura_id']))
        comandas_criadas += 1
        visitas_assinante += 1
print(f"[OK] {comandas_criadas} comandas em {DIAS_HISTORICO} dias ({visitas_assinante} de assinante)")

# ── Despesas ──────────────────────────────────────────────────────────
# Sem despesa o DRE só tem receita e não parece financeiro de verdade.
FIXAS = [('Aluguel do ponto', 'Aluguel', 2800, 5), ('Energia elétrica', 'Energia/Água', 640, 10),
         ('Internet', 'Outras', 149, 10), ('Simples Nacional', 'Impostos', 890, 20),
         ('Contador', 'Outras', 420, 10)]
for desc, cat, valor, dia in FIXAS:
    fid = um("""INSERT INTO despesas_fixas (descricao, categoria, valor, dia)
                VALUES (%s,%s,%s,%s) RETURNING id""", (desc, cat, valor, dia))
    for m in range(3, -1, -1):
        ref = hoje - timedelta(days=30 * m)
        d = date(ref.year, ref.month, min(dia, 28))
        if d <= hoje:
            cur.execute("""INSERT INTO movimentos (tipo, origem, descricao, categoria, valor, data,
                               status, despesa_fixa_id)
                           VALUES ('despesa','manual',%s,%s,%s,%s,'pago',%s)""", (desc, cat, valor, d, fid))

for delta, desc, cat, valor in [(72, 'Compra de produtos (distribuidora)', 'Insumos/Produtos', 980),
                                (44, 'Compra de produtos (distribuidora)', 'Insumos/Produtos', 760),
                                (18, 'Compra de produtos (distribuidora)', 'Insumos/Produtos', 1120),
                                (55, 'Manutenção das máquinas', 'Manutenção', 260),
                                (30, 'Impulsionamento Instagram', 'Marketing', 300)]:
    cur.execute("""INSERT INTO movimentos (tipo, origem, descricao, categoria, valor, data, status)
                   VALUES ('despesa','manual',%s,%s,%s,%s,'pago')""",
                (desc, cat, valor, hoje - timedelta(days=delta)))
print("[OK] despesas fixas e avulsas lançadas")

# ── Agenda ainda por atender: resto de hoje e os próximos dias ────────
# É a PRIMEIRA tela que o prospect abre. Vazia, ela mata a demo.
futuros = 0
for delta in range(0, 4):
    d = hoje + timedelta(days=delta)
    # Hoje reaproveita os slots que sobraram (o que já foi atendido de manhã não pode ser
    # remarcado por cima); nos dias seguintes a grade está inteira livre.
    livres = LIVRES.get(d) if d == hoje else slots_do_dia(d)
    if not livres:
        continue
    for _ in range(random.randint(7, 12)):
        pid = random.choice(PESO_PROF)
        slot = pega_slot(livres, pid)
        if not slot:
            continue
        hora, minuto = slot
        if d == hoje and hora < agora_h:      # hora que já passou não fica "agendado"
            continue
        servico = random.choice(PESO_SERVICO)
        novo_agendamento(d, pid, random.choice(cliente_ids), hora, minuto, servico, 'agendado')
        futuros += 1
print(f"[OK] {agendados} atendimentos com hora marcada no histórico · "
      f"{futuros} agendamentos ainda por atender")

# ── Quinzena anterior de comissão: fechada e paga ─────────────────────
# Mostra o ciclo inteiro (calcular → fechar → virar despesa), não só uma tela parada.
if hoje.day <= 15:
    fim_ant = hoje.replace(day=1) - timedelta(days=1)
    de, ate = fim_ant.replace(day=16), fim_ant
else:
    de, ate = hoje.replace(day=1), hoje.replace(day=15)

pool_revenue = um("""SELECT COALESCE(SUM(m.valor),0) FROM movimentos m
    JOIN assinaturas a ON a.id=m.ref_id JOIN planos p ON p.id=a.plano_id
    WHERE m.origem='assinatura' AND m.status='pago' AND m.data BETWEEN %s AND %s
      AND p.comissao_assinante_regra='bolo'""", (de, ate))
bolo = float(pool_revenue) * 0.45
total_visitas_bolo = um("""SELECT COUNT(DISTINCT c.id) FROM comandas c
    WHERE c.status='fechada' AND c.fechada_em::date BETWEEN %s AND %s
      AND EXISTS (SELECT 1 FROM comanda_itens ci JOIN assinaturas a ON a.id=ci.assinatura_id
                  JOIN planos p ON p.id=a.plano_id
                  WHERE ci.comanda_id=c.id AND ci.coberto_plano AND p.comissao_assinante_regra='bolo')""",
                        (de, ate))

fechadas = 0
for nome, pct, _, recebe in EQUIPE:
    if not recebe:
        continue
    pid = prof_id[nome]
    avulso = float(um("""SELECT COALESCE(SUM(ci.subtotal),0) FROM comanda_itens ci
        JOIN comandas c ON c.id=ci.comanda_id AND c.status='fechada'
        WHERE ci.tipo='servico' AND NOT ci.coberto_plano AND ci.profissional_id=%s
          AND c.fechada_em::date BETWEEN %s AND %s""", (pid, de, ate))) * pct / 100
    minhas_bolo = um("""SELECT COUNT(DISTINCT c.id) FROM comandas c
        WHERE c.status='fechada' AND c.profissional_id=%s AND c.fechada_em::date BETWEEN %s AND %s
          AND EXISTS (SELECT 1 FROM comanda_itens ci JOIN assinaturas a ON a.id=ci.assinatura_id
                      JOIN planos p ON p.id=a.plano_id
                      WHERE ci.comanda_id=c.id AND ci.coberto_plano AND p.comissao_assinante_regra='bolo')""",
                     (pid, de, ate))
    fixas = um("""SELECT COUNT(DISTINCT ci.comanda_id) FROM comanda_itens ci
        JOIN comandas c ON c.id=ci.comanda_id AND c.status='fechada'
        JOIN assinaturas a ON a.id=ci.assinatura_id JOIN planos p ON p.id=a.plano_id
        WHERE ci.coberto_plano AND ci.profissional_id=%s AND p.comissao_assinante_regra='fixo'
          AND c.fechada_em::date BETWEEN %s AND %s""", (pid, de, ate))
    pool_share = bolo * (minhas_bolo / total_visitas_bolo) if total_visitas_bolo else 0
    total = round(avulso + pool_share + fixas * 18, 2)
    if total <= 0:
        continue
    mid = um("""INSERT INTO movimentos (tipo, origem, descricao, categoria, valor, data, status)
                VALUES ('despesa','manual',%s,'Comissões',%s,%s,'pago') RETURNING id""",
             (f"Comissão {nome} ({de.strftime('%d/%m')}–{ate.strftime('%d/%m')})", total,
              ate + timedelta(days=1)))
    cur.execute("""INSERT INTO comissoes_pagas (profissional_id, periodo_de, periodo_ate, valor,
                       valor_calculado, movimento_id, data_pagamento)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (pid, de, ate, total, total, mid, ate + timedelta(days=1)))
    fechadas += 1
print(f"[OK] {fechadas} comissões fechadas na quinzena {de.strftime('%d/%m')}–{ate.strftime('%d/%m')}")

conn.commit()
cur.close()
conn.close()
print(f"\n[OK] Barbearia do Zé pronta — {comandas_criadas} atendimentos, "
      f"R$ {receita_lancada:,.2f} de serviço/produto em {DIAS_HISTORICO} dias.")
