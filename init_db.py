"""Inicializa o banco joga_barbearia. Rode 1x: python -X utf8 init_db.py
Idempotente. Os DADOS de exemplo ficam em seed_barbearia.py (rode depois)."""
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()

conn = psycopg2.connect(
    host=os.getenv('DB_HOST', 'localhost'),
    port=os.getenv('DB_PORT', '5432'),
    dbname=os.getenv('DB_NAME', 'joga_barbearia'),
    user=os.getenv('DB_USER', 'postgres'),
    password=os.getenv('DB_PASSWORD', ''),
)
cur = conn.cursor()

# ── Controle de migrações de DADO (one-shot) ──────────────────────────
# As migrações de SCHEMA são idempotentes por natureza (IF NOT EXISTS). As de DADO não:
# rodar duas vezes desfaria escolha do cliente. Este marcador garante "só uma vez".
cur.execute("""
    CREATE TABLE IF NOT EXISTS _migracoes (
        nome       VARCHAR(80) PRIMARY KEY,
        aplicada_em TIMESTAMP DEFAULT NOW()
    );
""")


def uma_vez(nome, sql, params=None):
    """Roda uma migração de dado só na primeira vez que o init_db passar por aqui."""
    cur.execute("SELECT 1 FROM _migracoes WHERE nome = %s", (nome,))
    if cur.fetchone():
        return False
    cur.execute(sql, params or ())
    cur.execute("INSERT INTO _migracoes (nome) VALUES (%s)", (nome,))
    print(f"[migração] {nome} aplicada.")
    return True


# ── Profissionais (barbeiros) ─────────────────────────────────────────
cur.execute("""
    CREATE TABLE IF NOT EXISTS profissionais (
        id           SERIAL PRIMARY KEY,
        nome         VARCHAR(120) NOT NULL,
        comissao_pct NUMERIC(5,2) DEFAULT 45,
        cor_agenda   VARCHAR(9) DEFAULT '#38bdf8',
        ativo        BOOLEAN DEFAULT true
    );
""")
# Dono-barbeiro não recebe comissão (o que atende é da casa)
cur.execute("ALTER TABLE profissionais ADD COLUMN IF NOT EXISTS recebe_comissao BOOLEAN DEFAULT true;")
# Nem todo barbeiro quer aparecer no agendamento online (a dona costuma preferir agenda controlada)
cur.execute("ALTER TABLE profissionais ADD COLUMN IF NOT EXISTS aceita_online BOOLEAN NOT NULL DEFAULT true;")

# ── Usuários (login) — dono / caixa / barbeiro ────────────────────────
cur.execute("""
    CREATE TABLE IF NOT EXISTS usuarios (
        id                   SERIAL PRIMARY KEY,
        nome                 VARCHAR(255) NOT NULL,
        email                VARCHAR(255) UNIQUE NOT NULL,
        password_hash        VARCHAR(255) NOT NULL,
        role                 VARCHAR(12) NOT NULL DEFAULT 'caixa'
                             CHECK (role IN ('dono', 'caixa', 'barbeiro')),
        profissional_id      INTEGER REFERENCES profissionais(id) ON DELETE SET NULL,
        ativo                BOOLEAN DEFAULT true,
        must_change_password BOOLEAN DEFAULT true,
        criado_em            TIMESTAMP DEFAULT NOW()
    );
""")
cur.execute("CREATE INDEX IF NOT EXISTS ix_usuarios_email ON usuarios(email);")

# ── Clientes ──────────────────────────────────────────────────────────
cur.execute("""
    CREATE TABLE IF NOT EXISTS clientes (
        id                  SERIAL PRIMARY KEY,
        nome                VARCHAR(255) NOT NULL,
        telefone            VARCHAR(30),
        tipo                VARCHAR(10) NOT NULL DEFAULT 'universal'
                            CHECK (tipo IN ('fixo', 'universal')),
        profissional_fixo_id INTEGER REFERENCES profissionais(id) ON DELETE SET NULL,
        observacoes         TEXT,
        ativo               BOOLEAN DEFAULT true,
        criado_em           TIMESTAMP DEFAULT NOW()
    );
""")
cur.execute("CREATE INDEX IF NOT EXISTS ix_clientes_nome ON clientes(nome);")
# Autocadastro via QR: clientes vindos do QR entram como 'pendente' até a dona aprovar.
# status='aprovado' (default) faz o backfill dos registros existentes — nada quebra.
cur.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS status VARCHAR(10) NOT NULL DEFAULT 'aprovado';")
cur.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS origem VARCHAR(12) NOT NULL DEFAULT 'manual';")

# ── Serviços ──────────────────────────────────────────────────────────
cur.execute("""
    CREATE TABLE IF NOT EXISTS servicos (
        id          SERIAL PRIMARY KEY,
        nome        VARCHAR(120) NOT NULL,
        preco       NUMERIC(10,2) NOT NULL,
        duracao_min INTEGER NOT NULL DEFAULT 30,
        ativo       BOOLEAN DEFAULT true
    );
""")

# ── Produtos (estoque pré-moldado, off no v1) ─────────────────────────
cur.execute("""
    CREATE TABLE IF NOT EXISTS produtos (
        id      SERIAL PRIMARY KEY,
        nome    VARCHAR(120) NOT NULL,
        preco   NUMERIC(10,2) NOT NULL,
        estoque INTEGER,                 -- NULL = sem controle (v1)
        ativo   BOOLEAN DEFAULT true
    );
""")

# ── Planos de assinatura (configuráveis) ──────────────────────────────
cur.execute("""
    CREATE TABLE IF NOT EXISTS planos (
        id                       SERIAL PRIMARY KEY,
        nome                     VARCHAR(120) NOT NULL,
        valor_mensal             NUMERIC(10,2) NOT NULL DEFAULT 0,
        limite_uso               INTEGER,         -- NULL = ilimitado
        dias_inclusos            JSONB DEFAULT '[1,2,3]'::jsonb,   -- 0=dom..6=sab
        comissao_assinante_regra VARCHAR(10) DEFAULT 'bolo'
                                 CHECK (comissao_assinante_regra IN ('bolo', 'tabela', 'fixo', 'zero')),
        comissao_assinante_valor NUMERIC(10,2),
        ativo                    BOOLEAN DEFAULT true
    );
""")
# Regra de comissão do assinante, por plano (antes era só o bolo, no código):
#   bolo   → % da arrecadação DESTE plano rateada entre os barbeiros pela produção (comportamento antigo)
#   tabela → % do barbeiro sobre o preço cheio do serviço coberto, direto pra quem executou
#   fixo   → R$ fixo (comissao_assinante_valor) por visita coberta, direto pra quem executou
#   zero   → assinante não gera comissão
cur.execute("ALTER TABLE planos ALTER COLUMN comissao_assinante_regra SET DEFAULT 'bolo';")
cur.execute("ALTER TABLE planos DROP CONSTRAINT IF EXISTS planos_comissao_assinante_regra_check;")
cur.execute("""ALTER TABLE planos ADD CONSTRAINT planos_comissao_assinante_regra_check
    CHECK (comissao_assinante_regra IN ('bolo', 'tabela', 'fixo', 'zero'));""")
# Planos que já existiam foram gravados como 'tabela' (default antigo) mas o cálculo SEMPRE os
# tratou como bolo — a coluna nunca era lida. Migra o rótulo pra verdade, sem mudar valor nenhum.
uma_vez('planos_regra_tabela_para_bolo',
        "UPDATE planos SET comissao_assinante_regra='bolo' WHERE comissao_assinante_regra='tabela'")

# Serviços inclusos no plano (m2m)
cur.execute("""
    CREATE TABLE IF NOT EXISTS plano_servicos (
        plano_id   INTEGER NOT NULL REFERENCES planos(id) ON DELETE CASCADE,
        servico_id INTEGER NOT NULL REFERENCES servicos(id) ON DELETE CASCADE,
        PRIMARY KEY (plano_id, servico_id)
    );
""")

# ── Assinaturas (cliente + plano) ─────────────────────────────────────
cur.execute("""
    CREATE TABLE IF NOT EXISTS assinaturas (
        id             SERIAL PRIMARY KEY,
        cliente_id     INTEGER NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
        plano_id       INTEGER NOT NULL REFERENCES planos(id) ON DELETE RESTRICT,
        dia_vencimento INTEGER NOT NULL DEFAULT 10 CHECK (dia_vencimento IN (10, 30)),
        status         VARCHAR(10) NOT NULL DEFAULT 'ativa'
                       CHECK (status IN ('ativa', 'pausada', 'cancelada')),
        data_inicio    DATE NOT NULL DEFAULT CURRENT_DATE,
        data_fim       DATE,
        criado_em      TIMESTAMP DEFAULT NOW()
    );
""")
cur.execute("CREATE INDEX IF NOT EXISTS ix_assin_cliente ON assinaturas(cliente_id);")
# Data da próxima cobrança (recorrência por dia 10/30 a partir do mês seguinte à assinatura)
cur.execute("ALTER TABLE assinaturas ADD COLUMN IF NOT EXISTS proxima_cobranca DATE;")

# ── Agendamentos ──────────────────────────────────────────────────────
cur.execute("""
    CREATE TABLE IF NOT EXISTS agendamentos (
        id              SERIAL PRIMARY KEY,
        profissional_id INTEGER NOT NULL REFERENCES profissionais(id) ON DELETE CASCADE,
        cliente_id      INTEGER REFERENCES clientes(id) ON DELETE SET NULL,
        servico_id      INTEGER REFERENCES servicos(id) ON DELETE SET NULL,
        data            DATE NOT NULL,
        hora_inicio     TIME NOT NULL,
        duracao_slots   INTEGER NOT NULL DEFAULT 1,
        status          VARCHAR(12) NOT NULL DEFAULT 'agendado'
                        CHECK (status IN ('agendado', 'atendido', 'cancelado', 'falta')),
        origem          VARCHAR(8) DEFAULT 'agenda' CHECK (origem IN ('agenda', 'walkin')),
        observacao      TEXT,
        criado_por      INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
        criado_em       TIMESTAMP DEFAULT NOW()
    );
""")
cur.execute("CREATE INDEX IF NOT EXISTS ix_agend_data ON agendamentos(data, profissional_id);")
# Múltiplos serviços por agendamento (lista de ids); servico_id segue como "principal" p/ display
cur.execute("ALTER TABLE agendamentos ADD COLUMN IF NOT EXISTS servicos_ids JSONB DEFAULT '[]'::jsonb;")
# Agendamento online: origem 'online' e o status 'pendente' (esperando a barbearia aceitar).
cur.execute("ALTER TABLE agendamentos DROP CONSTRAINT IF EXISTS agendamentos_origem_check;")
cur.execute("""ALTER TABLE agendamentos ADD CONSTRAINT agendamentos_origem_check
    CHECK (origem IN ('agenda', 'walkin', 'online'));""")
cur.execute("ALTER TABLE agendamentos DROP CONSTRAINT IF EXISTS agendamentos_status_check;")
cur.execute("""ALTER TABLE agendamentos ADD CONSTRAINT agendamentos_status_check
    CHECK (status IN ('pendente', 'agendado', 'atendido', 'cancelado', 'falta'));""")
# Fila de mensagens (WhatsApp manual): NULL = ainda não enviada. Sem isso não há fila — e a fila
# É a consulta, o que dispensa cron/agendador no servidor.
cur.execute("ALTER TABLE agendamentos ADD COLUMN IF NOT EXISTS confirmacao_enviada_em TIMESTAMP;")
cur.execute("ALTER TABLE agendamentos ADD COLUMN IF NOT EXISTS lembrete_enviado_em TIMESTAMP;")
cur.execute("CREATE INDEX IF NOT EXISTS ix_agend_pendente ON agendamentos(status, data) WHERE status='pendente';")

# ── Bloqueios de agenda (folga, almoço, férias) ───────────────────────
cur.execute("""
    CREATE TABLE IF NOT EXISTS bloqueios (
        id              SERIAL PRIMARY KEY,
        profissional_id INTEGER NOT NULL REFERENCES profissionais(id) ON DELETE CASCADE,
        data            DATE NOT NULL,
        hora_inicio     TIME NOT NULL,
        hora_fim        TIME NOT NULL,
        motivo          VARCHAR(120)
    );
""")
cur.execute("CREATE INDEX IF NOT EXISTS ix_bloq_data ON bloqueios(data, profissional_id);")

# ── Comandas (o pivô) ─────────────────────────────────────────────────
cur.execute("""
    CREATE TABLE IF NOT EXISTS comandas (
        id              SERIAL PRIMARY KEY,
        cliente_id      INTEGER REFERENCES clientes(id) ON DELETE SET NULL,
        profissional_id INTEGER REFERENCES profissionais(id) ON DELETE SET NULL,
        agendamento_id  INTEGER REFERENCES agendamentos(id) ON DELETE SET NULL,
        status          VARCHAR(10) NOT NULL DEFAULT 'aberta'
                        CHECK (status IN ('aberta', 'fechada', 'cancelada')),
        forma_pagamento VARCHAR(20),
        valor_total     NUMERIC(10,2) DEFAULT 0,
        aberta_em       TIMESTAMP DEFAULT NOW(),
        fechada_em      TIMESTAMP,
        criado_por      INTEGER REFERENCES usuarios(id) ON DELETE SET NULL
    );
""")
cur.execute("CREATE INDEX IF NOT EXISTS ix_comanda_status ON comandas(status);")

# ── Itens da comanda (serviço/produto) ────────────────────────────────
cur.execute("""
    CREATE TABLE IF NOT EXISTS comanda_itens (
        id              SERIAL PRIMARY KEY,
        comanda_id      INTEGER NOT NULL REFERENCES comandas(id) ON DELETE CASCADE,
        tipo            VARCHAR(8) NOT NULL CHECK (tipo IN ('servico', 'produto')),
        ref_id          INTEGER,                  -- servico_id ou produto_id
        descricao       VARCHAR(160) NOT NULL,
        profissional_id INTEGER REFERENCES profissionais(id) ON DELETE SET NULL,  -- executor (comissão)
        preco_unit      NUMERIC(10,2) NOT NULL DEFAULT 0,
        qtd             INTEGER NOT NULL DEFAULT 1,
        subtotal        NUMERIC(10,2) NOT NULL DEFAULT 0,
        preco_tabela    NUMERIC(10,2),            -- preço cheio do serviço (base de comissão se coberto)
        coberto_plano   BOOLEAN DEFAULT false,
        assinatura_id   INTEGER REFERENCES assinaturas(id) ON DELETE SET NULL
    );
""")
cur.execute("CREATE INDEX IF NOT EXISTS ix_item_comanda ON comanda_itens(comanda_id);")

# ── Movimentos financeiros (caixa) ────────────────────────────────────
cur.execute("""
    CREATE TABLE IF NOT EXISTS movimentos (
        id              SERIAL PRIMARY KEY,
        tipo            VARCHAR(8) NOT NULL CHECK (tipo IN ('receita', 'despesa')),
        origem          VARCHAR(12) NOT NULL DEFAULT 'manual'
                        CHECK (origem IN ('comanda', 'assinatura', 'manual')),
        ref_id          INTEGER,
        descricao       VARCHAR(200) NOT NULL,
        valor           NUMERIC(10,2) NOT NULL,
        forma_pagamento VARCHAR(20),
        data            DATE NOT NULL DEFAULT CURRENT_DATE,
        vencimento      DATE,
        status          VARCHAR(10) NOT NULL DEFAULT 'pago'
                        CHECK (status IN ('previsto', 'pago')),
        criado_por      INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
        criado_em       TIMESTAMP DEFAULT NOW()
    );
""")
cur.execute("CREATE INDEX IF NOT EXISTS ix_mov_data ON movimentos(data);")
cur.execute("CREATE INDEX IF NOT EXISTS ix_mov_status ON movimentos(status);")
# Despesas: categoria + vínculo com despesa fixa recorrente (aditivo)
cur.execute("ALTER TABLE movimentos ADD COLUMN IF NOT EXISTS categoria VARCHAR(40);")
cur.execute("ALTER TABLE movimentos ADD COLUMN IF NOT EXISTS despesa_fixa_id INTEGER;")
# origem='taxa': a taxa da maquininha, lançada como despesa e AMARRADA ao movimento de receita
# que a gerou (ref_id = id da receita). Sem esse vínculo não dá pra desfazer junto.
cur.execute("ALTER TABLE movimentos DROP CONSTRAINT IF EXISTS movimentos_origem_check;")
cur.execute("""ALTER TABLE movimentos ADD CONSTRAINT movimentos_origem_check
    CHECK (origem IN ('comanda', 'assinatura', 'manual', 'taxa'));""")
cur.execute("CREATE INDEX IF NOT EXISTS ix_mov_taxa ON movimentos(origem, ref_id) WHERE origem='taxa';")

# ── Despesas fixas (recorrentes: aluguel, impostos, internet...) ──────
cur.execute("""
    CREATE TABLE IF NOT EXISTS despesas_fixas (
        id        SERIAL PRIMARY KEY,
        descricao VARCHAR(160) NOT NULL,
        categoria VARCHAR(40),
        valor     NUMERIC(10,2) NOT NULL,
        dia       INTEGER DEFAULT 5 CHECK (dia BETWEEN 1 AND 28),
        ativo     BOOLEAN DEFAULT true,
        criado_em TIMESTAMP DEFAULT NOW()
    );
""")

# ── Configurações (linha única) + white-label ─────────────────────────
cur.execute("""
    CREATE TABLE IF NOT EXISTS configuracoes (
        id                INTEGER PRIMARY KEY DEFAULT 1,
        slot_min          INTEGER DEFAULT 30,
        comissao_padrao   NUMERIC(5,2) DEFAULT 45,
        formas_pagamento  JSONB DEFAULT '["Dinheiro","Pix","Cartão"]'::jsonb,
        horarios          JSONB DEFAULT '{}'::jsonb,
        taxa_cartao_pct   NUMERIC(5,2),   -- NÃO USADA: a taxa virou por forma (formas_pagamento)
        cancelamento_taxa NUMERIC(10,2),
        marca_nome        VARCHAR(120) DEFAULT 'JOGA Barbearia',
        marca_logo_url    VARCHAR(255),
        marca_cor         VARCHAR(9) DEFAULT '#38bdf8',   -- NÃO USADA: o tema do app é fixo (app.css)
        CONSTRAINT config_single_row CHECK (id = 1)
    );
""")
cur.execute("INSERT INTO configuracoes (id) VALUES (1) ON CONFLICT (id) DO NOTHING;")
# Categorias de despesa (configuráveis, incluindo Impostos)
cur.execute("""ALTER TABLE configuracoes ADD COLUMN IF NOT EXISTS categorias_despesa JSONB
    DEFAULT '["Impostos","Aluguel","Insumos/Produtos","Energia/Água","Salários","Comissões","Taxas de cartão","Marketing","Manutenção","Outras"]'::jsonb;""")
# O DEFAULT acima só vale pra instalação nova — quem já existe precisa ganhar a categoria.
uma_vez('categoria_taxas_cartao',
        """UPDATE configuracoes SET categorias_despesa = categorias_despesa || '["Taxas de cartão"]'::jsonb
           WHERE NOT (categorias_despesa @> '["Taxas de cartão"]'::jsonb)""")

# ── Agendamento online (público) ──────────────────────────────────────
# Nasce DESLIGADO: instância existente não ganha porta pública sem alguém decidir.
cur.execute("ALTER TABLE configuracoes ADD COLUMN IF NOT EXISTS agendamento_online BOOLEAN NOT NULL DEFAULT false;")
# true = o agendamento entra como 'pendente' e a barbearia aceita. É o padrão de entrega:
# a dona vê o movimento antes de confiar na máquina.
cur.execute("ALTER TABLE configuracoes ADD COLUMN IF NOT EXISTS agendamento_confirmar_manual BOOLEAN NOT NULL DEFAULT true;")
cur.execute("ALTER TABLE configuracoes ADD COLUMN IF NOT EXISTS agendamento_antecedencia_horas INTEGER NOT NULL DEFAULT 2;")
cur.execute("ALTER TABLE configuracoes ADD COLUMN IF NOT EXISTS agendamento_janela_dias INTEGER NOT NULL DEFAULT 30;")
# A ficha de coleta pergunta endereço e WhatsApp e o setup_aplicar jogava os dois fora —
# não havia onde guardar. As mensagens de confirmação querem o endereço.
cur.execute("ALTER TABLE configuracoes ADD COLUMN IF NOT EXISTS marca_endereco VARCHAR(255);")
cur.execute("ALTER TABLE configuracoes ADD COLUMN IF NOT EXISTS marca_whatsapp VARCHAR(30);")

# ── Formas de pagamento + taxa da maquininha ──────────────────────────
# Assume o lugar de configuracoes.formas_pagamento (que era só um array de nomes, sem taxa).
# O /api/config SEGUE devolvendo a lista de nomes derivada daqui — seis telas dependem daquela
# forma. Aqui é a fonte da verdade; lá é o contrato de compatibilidade.
cur.execute("""
    CREATE TABLE IF NOT EXISTS formas_pagamento (
        id       SERIAL PRIMARY KEY,
        nome     VARCHAR(40) UNIQUE NOT NULL,
        taxa_pct NUMERIC(5,2) NOT NULL DEFAULT 0,
        ordem    INTEGER NOT NULL DEFAULT 0,
        ativo    BOOLEAN NOT NULL DEFAULT true
    );
""")
# Semeia com o que a instância já usava (taxa 0 — ninguém perde nem ganha nada na migração).
# 'Cartão' continua existindo: o histórico em movimentos.forma_pagamento guarda essa string e ela
# precisa seguir significando algo. Quem quiser troca por Débito/Crédito e desativa no Config.
uma_vez('formas_pagamento_seed',
        """INSERT INTO formas_pagamento (nome, taxa_pct, ordem)
           SELECT nome, 0, (ord - 1)::int
             FROM configuracoes,
                  LATERAL jsonb_array_elements_text(formas_pagamento) WITH ORDINALITY AS t(nome, ord)
            WHERE configuracoes.id = 1
           ON CONFLICT (nome) DO NOTHING""")
# Débito e Crédito com taxa 0: a Regiane preenche o percentual dela no Config.
uma_vez('formas_pagamento_cartao',
        """INSERT INTO formas_pagamento (nome, taxa_pct, ordem) VALUES
             ('Débito', 0, 10), ('Crédito', 0, 11)
           ON CONFLICT (nome) DO NOTHING""")

# ── Comissões pagas (fechamento por barbeiro/período → despesa no caixa) ──
cur.execute("""
    CREATE TABLE IF NOT EXISTS comissoes_pagas (
        id              SERIAL PRIMARY KEY,
        profissional_id INTEGER NOT NULL REFERENCES profissionais(id) ON DELETE CASCADE,
        periodo_de      DATE NOT NULL,
        periodo_ate     DATE NOT NULL,
        valor           NUMERIC(10,2) NOT NULL,
        data_pagamento  DATE NOT NULL DEFAULT CURRENT_DATE,
        movimento_id    INTEGER REFERENCES movimentos(id) ON DELETE SET NULL,
        criado_por      INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
        criado_em       TIMESTAMP DEFAULT NOW()
    );
""")
cur.execute("CREATE INDEX IF NOT EXISTS ix_comissoes_prof ON comissoes_pagas(profissional_id, periodo_de, periodo_ate);")
# Override manual no fechamento: guarda o que o sistema calculou, o que foi pago e o porquê.
# Sem isso o ajuste some do histórico e ninguém consegue auditar a diferença depois.
cur.execute("ALTER TABLE comissoes_pagas ADD COLUMN IF NOT EXISTS valor_calculado NUMERIC(10,2);")
cur.execute("ALTER TABLE comissoes_pagas ADD COLUMN IF NOT EXISTS ajuste_motivo TEXT;")
# Fechamentos antigos não tinham ajuste: o pago É o calculado.
uma_vez('comissoes_valor_calculado_backfill',
        "UPDATE comissoes_pagas SET valor_calculado = valor WHERE valor_calculado IS NULL")

# ── Fichas de coleta / entrega da instância (onboarding assistido pela JOGA) ──
# A barbearia preenche o que só ela sabe (preços, barbeiros, horários) numa página pública;
# a JOGA revisa e APLICA, e a instância nasce populada.
#
# Numa instância de barbearia existe UMA ficha (a id=1, criada aqui). Numa instância rodando com
# MODO_COLETA=1 (o hub que coleta de vários prospects antes de existir servidor pra eles) existem
# VÁRIAS, uma por prospect — daí a tabela não ser mais de linha única.
cur.execute("""
    CREATE TABLE IF NOT EXISTS setup_coleta (
        id           INTEGER PRIMARY KEY DEFAULT 1,
        dados        JSONB NOT NULL DEFAULT '{}'::jsonb,
        status       VARCHAR(12) NOT NULL DEFAULT 'vazia'
                     CHECK (status IN ('vazia', 'rascunho', 'enviada', 'aplicada')),
        token        VARCHAR(40),
        enviada_em   TIMESTAMP,
        aplicada_em  TIMESTAMP,
        atualizada_em TIMESTAMP DEFAULT NOW()
    );
""")
# Multi-ficha (aditivo): tira a trava de linha única e dá sequence ao id.
cur.execute("ALTER TABLE setup_coleta DROP CONSTRAINT IF EXISTS setup_coleta_single_row;")
cur.execute("ALTER TABLE setup_coleta ADD COLUMN IF NOT EXISTS nome VARCHAR(120);")
cur.execute("ALTER TABLE setup_coleta ADD COLUMN IF NOT EXISTS criada_em TIMESTAMP DEFAULT NOW();")
cur.execute("CREATE SEQUENCE IF NOT EXISTS setup_coleta_id_seq OWNED BY setup_coleta.id;")
cur.execute("ALTER TABLE setup_coleta ALTER COLUMN id SET DEFAULT nextval('setup_coleta_id_seq');")
cur.execute("SELECT setval('setup_coleta_id_seq', GREATEST(1, COALESCE((SELECT MAX(id) FROM setup_coleta), 1)));")
# Token é a chave do link público: não pode repetir entre fichas.
cur.execute("""CREATE UNIQUE INDEX IF NOT EXISTS ix_setup_coleta_token
               ON setup_coleta(token) WHERE token IS NOT NULL;""")
# A ficha da própria instância (a que o /setup usa pra aplicar) é sempre a id=1.
cur.execute("INSERT INTO setup_coleta (id) VALUES (1) ON CONFLICT (id) DO NOTHING;")

conn.commit()
cur.close()
conn.close()
print("[OK] Schema pronto. Rode: python -X utf8 seed_barbearia.py")
